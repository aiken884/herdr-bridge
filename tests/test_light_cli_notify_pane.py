# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for herdr-commander notify-pane -- the fourth communication layer: a
keyboard-injection adapter for interactive TUI panes (Claude Code / OpenCode /
Grok, and other non-ACP headless agents) that verifies delivery via a screen
diff. Failing within the retry limit must error out clearly, never silently
report a false success.

Mocks out the `herdr` CLI (subprocess.run) so we don't need a real Herdr
socket connection.
"""

from __future__ import annotations

from herdr_bridge.light.cli import NotifyPaneDeliveryError, build_parser, main


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _read_result(text: str) -> _FakeResult:
    return _FakeResult(returncode=0, stdout=text)


def _ok_result() -> _FakeResult:
    return _FakeResult(returncode=0, stdout="")


def _install_fake_run(monkeypatch, handler, *, wait_output_ready: bool = True):
    """handler(argv: list[str]) -> _FakeResult, decides read/send-text/send-keys/get based on argv.

    `wait_output_ready`: #69's `herdr pane wait-output` (waits for the TUI to be
    ready before injection) is intercepted here by default and reports "ready"
    directly, so existing tests don't each need to update their handler to cover
    this new call. Tests that need to exercise the "not ready" case can pass
    `wait_output_ready=False`, or handle `["herdr", "pane", "wait-output"]`
    explicitly in their own handler (the handler takes priority).
    """

    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:3] == ["herdr", "pane", "wait-output"]:
            try:
                return handler(argv)
            except AssertionError:
                return _ok_result() if wait_output_ready else _FakeResult(returncode=1)
        return handler(argv)

    monkeypatch.setattr("herdr_bridge.light.cli.subprocess.run", _fake_run)
    monkeypatch.setattr("herdr_bridge.light.cli._rg", None)
    return calls


def test_parser_notify_pane_basic():
    p = build_parser()
    args = p.parse_args(["notify-pane", "--pane", "w1:p1", "hello world"])
    assert args.command == "notify-pane"
    assert args.pane_id == "w1:p1"
    assert args.message == "hello world"
    assert args.retries == 3
    assert args.no_audit is False


def test_notify_pane_atomic_injection_success(monkeypatch, capsys):
    """Scenario (a): atomic injection succeeds on the first try (e.g. OpenCode/Grok
    TUI), no need to follow up with Enter."""
    message = "hello from tower"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            # regardless of before/after, the input box (after ❯) is empty,
            # meaning the message is no longer in the input box
            return _read_result("history line\nsome earlier text\n❯ \n")
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            # use the neutral agent_status value idle ("working" now has real
            # meaning per #68: rejects injection by default)
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "w1:p1", "--no-audit", message])
    assert rc == 0

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    send_keys_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-keys"]]
    assert len(send_text_calls) == 1, "a single successful atomic injection should not retry"
    assert len(send_keys_calls) == 0, "once committed, no follow-up Enter should be sent"
    # atomic injection: the message and the newline character are sent together
    # (not split into a text step + a separate Enter step)
    assert send_text_calls[0][-1] == message + "\n"

    out = capsys.readouterr().out
    assert "✅" in out


def test_notify_pane_needs_enter_fallback(monkeypatch, capsys):
    """Scenario (b): atomic injection is not committed (typical Claude Code TUI
    behavior); delivery is only confirmed after a follow-up Enter."""
    message = "please check the deploy"
    read_call_count = {"n": 0}

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            read_call_count["n"] += 1
            n = read_call_count["n"]
            if n <= 2:
                # before / after: the message is still stuck in the input box, not committed yet
                return _read_result(f"history\n❯ {message}")
            # after the follow-up Enter: input box cleared, message moved into history
            return _read_result(f"history\n{message}\n❯ \n")
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            assert argv[3] == "w1:p1"
            assert argv[4] == "Enter"
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "w1:p1", "--no-audit", "--settle-delay", "0", message])
    assert rc == 0

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    send_keys_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-keys"]]
    assert len(send_text_calls) == 1, "a single atomic-injection attempt should trigger the fallback, no need to resend send-text"
    assert len(send_keys_calls) == 1, "when not committed, one follow-up Enter should be sent"

    out = capsys.readouterr().out
    assert "✅" in out
    assert "fallback_enter_used=True" in out


def test_notify_pane_exhausts_retries_raises_clear_error(monkeypatch, capsys):
    """Scenario (c): delivery is never confirmed within the retry limit; must
    error out clearly, never silently return success."""
    message = "stuck message"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            # the input box always keeps the message stuck, meaning it never actually gets committed
            return _read_result(f"history\n❯ {message}")
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(
        [
            "notify-pane", "--pane", "w1:p1", "--no-audit",
            "--retries", "2", "--settle-delay", "0", message,
        ]
    )
    assert rc != 0
    assert rc == 5

    err = capsys.readouterr().err
    assert "retries" in err.lower()
    assert "w1:p1" in err

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 2, "should actually retry up to the limit, not give up after one send"


def test_notify_pane_raises_typed_error_when_called_directly(monkeypatch):
    """cmd_notify_pane itself must raise a clearly-typed exception (main() just
    converts it into a CLI error message + a nonzero exit code)."""
    from herdr_bridge.light.cli import build_parser, cmd_notify_pane

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result("history\n❯ stuck")
        return _ok_result()

    _install_fake_run(monkeypatch, handler)

    p = build_parser()
    args = p.parse_args(
        ["notify-pane", "--pane", "w1:p1", "--no-audit", "--retries", "1", "--settle-delay", "0", "stuck"]
    )
    try:
        cmd_notify_pane(args)
        raise AssertionError("should have raised NotifyPaneDeliveryError")
    except NotifyPaneDeliveryError:
        pass


def test_notify_pane_special_characters_not_shell_interpreted(monkeypatch):
    """Scenario (d): the message contains shell special characters like
    backticks/brackets/$, and must be passed to subprocess verbatim as plain
    text (list form) -- it must not be assembled into a shell string and get
    parsed/substituted/glob-expanded.
    """
    dangerous_message = (
        "run `whoami` and echo $HOME then glob [a-z]*.txt "
        "and try $(rm -rf /tmp/should-not-run) plus \"quotes\" and 'single quotes'"
    )

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            # input box is cleared, treated as committed on the first try -- the
            # focus of this test is the argument-passing method, not the commit flow
            return _read_result("history\n❯ \n")
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "w1:p1", "--no-audit", dangerous_message])
    assert rc == 0

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 1
    argv = send_text_calls[0]
    # the dangerous characters must appear verbatim in a single argv element (i.e.
    # passed in list form, never shell-parsed)
    assert argv[-1] == dangerous_message + "\n"
    assert "`" in argv[-1]
    assert "$(rm -rf" in argv[-1]
    # no call is a shell command assembled from a string (e.g. via bash -c)
    for c in calls:
        assert c[0] == "herdr", f"must not be invoked through a shell: {c}"
        assert "bash" not in c and "sh" not in c[:1]


def test_notify_pane_long_message_rejected(monkeypatch, capsys):
    """Evidence (PPLX suggestion + tower hard-won lesson): when a message
    exceeds the recommended limit (4KB), the old behavior was to print a
    warning, send anyway, and report success -- but simulated keyboard
    injection is inherently unreliable for long messages in a narrow pane.
    This must now be a hard rejection: don't attempt injection at all, and the
    error message should suggest switching to file-based delivery.
    """
    long_message = "x" * 5000

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result("history\n❯ \n")
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "w1:p1", "--no-audit", long_message])
    assert rc == 5, "an oversized message should be rejected outright (typed error -> exit 5), not sent anyway after a warning"

    err = capsys.readouterr().err
    assert "4096" in err
    assert "temp file" in err or "file" in err

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 0, "when an oversized message is rejected, injection must not be attempted at all"


def test_notify_pane_rejects_injection_into_trust_confirmation_dialog(monkeypatch, capsys):
    """Evidence 1 (hard-won lesson): sending a message to a pane still stuck on
    the "Quick safety check: Is this a project you created or one you trust?"
    startup trust dialog used to result in atomic injection feeding the
    message into this interactive menu -- the agent hadn't even started yet,
    but it reported a delivery-confirmed success.

    New requirement: before injecting, detect whether the screen is in a
    known non-prompt state (startup trust confirmation/menu/y-n/password); if
    so, reject the injection outright with a clear error instead of sending
    first and checking the diff afterward.
    """
    message = "please start today's dispatch task"
    trust_dialog_snapshot = (
        " Quick safety check: Is this a project you created or one you trust?\n"
        " ❯ 1. Yes, I trust this folder\n"
        "   2. No, exit\n"
        " Enter to confirm · Esc to cancel\n"
    )

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(trust_dialog_snapshot)
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p18", "--no-audit", "--retries", "2", message])
    assert rc == 5, "detecting a trust confirmation dialog should reject outright, typed error -> exit 5"

    err = capsys.readouterr().err
    assert "wT:p18" in err

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 0, "when a non-prompt state is detected, injection must not be attempted at all -- never send first and check the diff after"


def test_notify_pane_detects_wrapped_message_still_stuck_in_input_box(monkeypatch, capsys):
    """Evidence 2 (hard-won lesson): when a message wraps across multiple lines
    filling the input box in a narrow pane, the prompt (❯) line itself can end
    up empty (the text got pushed down onto the following lines). The old
    detection logic only looked at "the ❯ line", saw an empty string, and
    misjudged it as "committed". The correct approach is to look at "the
    entire input box area" (between the Claude Code TUI's top and bottom ─
    border lines) -- as long as the message text is still on any line inside
    the box, it must be judged as not committed.
    """
    message = "When done, report back: what you did, the test results (paste the actual output), and anything still unresolved"
    # the ❯ line itself is empty; the message text got wrapped onto the following
    # lines, and those lines don't carry the ❯ marker
    stuck_wrapped_snapshot = (
        "──────────────────────────────────────\n"
        "❯\n"
        "  When done, report back: what you did, the test\n"
        "  results (paste the actual output), and anything still unresolved\n"
        "──────────────────────────────────────\n"
    )

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(stuck_wrapped_snapshot)
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(
        [
            "notify-pane", "--pane", "wT:p1E", "--no-audit",
            "--retries", "2", "--settle-delay", "0", message,
        ]
    )
    assert rc == 5, "the message is actually still stuck in the input box, must error out, not be misjudged as delivered"

    err = capsys.readouterr().err
    assert "wT:p1E" in err

    # should really retry up to the limit, since every attempt correctly judges it as not committed
    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 2


def test_notify_pane_detects_new_message_stuck_alongside_leftover_text(monkeypatch, capsys):
    """Evidence 3 (hard-won lesson): leftover text from a previous message
    (e.g. pressing Escape to clear the input box, but it didn't fully clear)
    is still sitting in the input box. Sending a second, new message this
    time also gets stuck in the input box without actually committing, but
    the old detection logic (looking only at "the ❯ line" or a fixed 3 lines
    at the bottom of the screen) could get thrown off by the leftover text and
    misjudge it as success. The correct approach is to check whether the new
    message's content is present anywhere in the whole input box area.
    """
    new_message = "Use the Read tool to read task-A.md and act on its content"
    # the ❯ line is the tail end of leftover text from a previous message; the
    # new message got wrapped onto the following lines without the ❯ marker
    mixed_leftover_snapshot = (
        "──────────────────────────────────────\n"
        "❯ tech debt, log every finding in the worklist.\n"
        "  Use the Read tool to read task-A.md\n"
        "  and act on its content\n"
        "──────────────────────────────────────\n"
    )

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(mixed_leftover_snapshot)
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(
        [
            "notify-pane", "--pane", "wT:p1E", "--no-audit",
            "--retries", "2", "--settle-delay", "0", new_message,
        ]
    )
    assert rc == 5, "the new message is actually still stuck in the input box alongside leftover text, must error out, not be misjudged as delivered"

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 2


def test_notify_pane_ready_check_ignores_keywords_in_scrollback_history(monkeypatch, capsys):
    """#55: the ready check (_detect_blocking_prompt) used to run a pattern
    search over the entire screen snapshot, not scoped to the input box/the
    last few lines of the screen. If the agent's conversation history (already
    scrolled above the visible area) happens to mention "password"/"(y/n)"/
    "enter to confirm"/"trust this folder" -- for example while discussing
    these keywords themselves (common during dogfooding: dispatch messages and
    replies frequently mention trust dialogs/y-n prompts/password input) --
    the ready check would misjudge the pane as being in a non-injectable
    state and reject an otherwise legitimate injection.

    The correct approach: only match these keywords in the "last few lines" of
    the screen (where the interactive UI is actually rendering right now);
    text in the scrolled-back history area should not affect the judgment.
    Here, the input box itself is in a normal, empty, injectable state.
    """
    message = "please continue with the next task"
    # a large block of "conversation history" text happens to mention these
    # keywords themselves (discussing the hard-won lessons from #44), but the
    # actual input box (the last few lines of the screen) shows a normal, empty
    # ❯ prompt -- it should not be blocked by keywords in the history area.
    history_with_trigger_words = "assistant: We're testing the notification mechanism itself, let's revisit the three hard-won lessons from #44:\n1. Sending a message to a pane still stuck on Quick safety check: Is this a project you created or one you trust?\n   (the keyword trust this folder is itself the content we're discussing, not an actual screen state)\n2. The misjudgment issue with the (y/n) confirmation prompt after a long message wraps in a narrow pane\n3. The detection logic for the password input prompt and the enter to confirm interactive menu prompt\nassistant: These patterns are already in the _BLOCKING_PROMPT_PATTERNS list.\nuser: Got it, what's next?"
    # after the trigger words there's more normal conversation (pushing the
    # trigger words out of the last-few-lines detection window), making sure
    # this really is "scrolled up into the history area" and not coincidentally
    # still within the detection range.
    neutral_filler = "\n".join(f"assistant: Line {i} of normal follow-up commentary, containing no trigger keywords." for i in range(10))
    normal_bottom_prompt = (
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
    )
    snapshot_with_history_noise = (
        history_with_trigger_words + "\n" + neutral_filler + "\n" + normal_bottom_prompt
    )

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(snapshot_with_history_noise)
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1E", "--no-audit", message])
    assert rc == 0, "the input box itself is in a normal state, should not be rejected as non-injectable due to keywords in conversation history"

    err = capsys.readouterr().err
    assert "non-injectable state" not in err

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 1, "injection should proceed normally, not be blocked by mistake in the ready check"


def test_notify_pane_ready_check_still_catches_real_dialog_near_bottom(monkeypatch, capsys):
    """Regression protection after the #55 fix: a real interactive prompt
    screen (in the last few lines of the screen, not the history area) must
    still be caught by the ready check -- fixing the false trigger must not
    shrink the detection range so much that it misses real problems (that
    would regress back to the false-success state before the #44 fix).
    """
    message = "please start the next task"
    # there's likewise some history text up front (with no trigger keywords);
    # the real trust confirmation dialog is in the last few lines of the screen
    snapshot_real_dialog_at_bottom = "assistant: Finished the previous task, starting the next Claude Code session.\nuser: OK, go ahead.\n Quick safety check: Is this a project you created or one you trust?\n ❯ 1. Yes, I trust this folder\n   2. No, exit\n Enter to confirm · Esc to cancel"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(snapshot_real_dialog_at_bottom)
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1E", "--no-audit", "--retries", "2", message])
    assert rc == 5, "a trust confirmation dialog genuinely stuck in the last few lines of the screen must still be blocked"

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 0, "when a non-prompt state is detected, injection must not be attempted at all"


def test_notify_pane_ready_check_ignores_password_keyword_in_bottom_status_panel(monkeypatch, capsys):
    """#55 hard-won lesson (2026-07-25, real case): `_BLOCKING_PROMPT_SCAN_LINES`
    only solved the case of "a trigger word already scrolled up into the
    history area", not the case of "a trigger word right at the bottom of the
    screen, but it's just ordinary text in a continuously-rendered todo/status
    panel, not a real interactive prompt".

    The tower's own screen keeps a todo panel visible at the bottom (built
    into the Claude Code TUI), and one of the task descriptions happened to be
    "evaluate false triggers from keywords like password in the ready check"
    -- this line of text itself fell within the `_BLOCKING_PROMPT_SCAN_LINES`
    scan range, got matched by the bare string comparison for "password", and
    caused a legitimate message from another agent to be rejected (what
    actually happened: a downstream project's agent tried to report a push
    decision, and was blocked by the tower's own notify-pane).

    Fix: the `password` pattern now requires an immediately following colon
    (`password\\s*:`), matching the actual password-input-field label format,
    so it no longer matches ordinary text like "this word is mentioned in a
    todo item description".
    """
    message = "report the push decision"
    snapshot_with_todo_panel_near_bottom = "⏺ Completed and merged the previous round of fixes\n  3 tasks (2 done, 1 in progress, 0 open)\n  ◻ Evaluate false triggers from keywords like password in the ready check that block legitimate injections\n  ✔ Fixed #74 multi-pane subscription reconnect race\n  ✔ Finished #43 second cleanup pass\n──────────────────────────────────────\n❯ \n──────────────────────────────────────"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(snapshot_with_todo_panel_near_bottom)
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:pX", "--no-audit", message])
    assert rc == 0, "mentioning the word password in the todo panel should not be misjudged as a real password input prompt"

    err = capsys.readouterr().err
    assert "non-injectable state" not in err

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 1, "injection should proceed normally, not be blocked by mistake due to a keyword in the todo panel"


def test_notify_pane_codex_marker_stuck_below_status_bar_triggers_enter_fallback(monkeypatch, capsys):
    """#65 hard-won lesson (decisive evidence, codex): codex uses "›" (U+203A),
    not claude's "❯", and there's a status line (model/usage) below the input
    line, so the input line doesn't always fall within the last few lines of
    the screen. Old detection logic: `_extract_input_box_text` finds no paired
    ─ border lines -> None; `_extract_input_line` only recognizes ❯, finds
    nothing -> None; falls back to the conservative "fixed 3 lines at the
    bottom of the screen" heuristic, which happens to miss the input line
    pushed out of the window by the status line, misjudging it as "committed"
    and skipping the follow-up Enter entirely, so the message stays stuck
    forever (confirmed today in real testing: both agy and codex hit this).

    After the fix: switched to per-TUI pattern matching
    (`tui_patterns.PROMPT_PATTERNS`), so codex's "›" marker correctly locates
    the input line regardless of how many status-line rows are below it,
    correctly judging it as not committed and triggering the follow-up Enter.
    """
    message = "be9460f9 please reply with only: be9460f9 OK"
    # below the input line there's a status line plus two lines of noise,
    # pushing the input line out of the "last 3 lines of the screen" window.
    # before: input box is empty (not yet injected); stuck: after injection the
    # message really did land in the input box, but hasn't committed yet -- the
    # two differ so we avoid hitting the "screen completely unchanged" shortcut,
    # to actually exercise the tail-diff fallback path this test is meant to verify.
    before_snapshot = (
        "history line 1\n"
        "history line 2\n"
        "› \n"
        "gpt-5.6-terra high · usage: 12%\n"
        "tips: ctrl+c to interrupt\n"
        "another footer line\n"
    )
    stuck_snapshot = (
        "history line 1\n"
        "history line 2\n"
        f"› {message}\n"
        "gpt-5.6-terra high · usage: 12%\n"
        "tips: ctrl+c to interrupt\n"
        "another footer line\n"
    )
    submitted_snapshot = (
        "history line 1\n"
        "history line 2\n"
        f"• {message}\n"
        "› \n"
        "gpt-5.6-terra high · usage: 12%\n"
        "tips: ctrl+c to interrupt\n"
        "another footer line\n"
    )
    read_call_count = {"n": 0}

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            read_call_count["n"] += 1
            if read_call_count["n"] == 1:
                return _read_result(before_snapshot)
            if read_call_count["n"] == 2:
                return _read_result(stuck_snapshot)
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1B", "--no-audit", "--settle-delay", "0", message])
    assert rc == 0, "the message really did commit after the follow-up Enter, should report success"

    send_keys_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-keys"]]
    assert len(send_keys_calls) == 1, (
        "when codex's \"›\" input line is pushed out of the last-3-lines-of-screen "
        "window by the status line, the old logic would misjudge it as committed and "
        "skip the follow-up Enter entirely; after the fix it should correctly judge it "
        "as not committed and attempt the follow-up Enter"
    )

    out = capsys.readouterr().out
    assert "fallback_enter_used=True" in out


def test_notify_pane_agy_marker_stuck_below_status_bar_triggers_enter_fallback(monkeypatch, capsys):
    """#65 hard-won lesson (decisive evidence, agy): agy uses plain ASCII ">",
    and likewise has a status line below the input line (time/directory/
    model/cost); the problem of the input line getting pushed out of the
    bottom-of-screen window has the same root cause as codex.
    """
    message = "0614dd82 please reply with only: 0614dd82 OK"
    before_snapshot = (
        "history line 1\n"
        "history line 2\n"
        "> \n"
        "────────────────────────────────────\n"
        "🕒 12:59:29 | 📁 tmp |🤖 Gemini 3.5 Flash (Medium) | 💰 $-0.00 sessi\n"
        "tips: ctrl+c to interrupt\n"
        "another footer line\n"
    )
    stuck_snapshot = (
        "history line 1\n"
        "history line 2\n"
        f"> {message}\n"
        "────────────────────────────────────\n"
        "🕒 12:59:29 | 📁 tmp |🤖 Gemini 3.5 Flash (Medium) | 💰 $-0.00 sessi\n"
        "tips: ctrl+c to interrupt\n"
        "another footer line\n"
    )
    submitted_snapshot = (
        "history line 1\n"
        f"{message}\n"
        "0614dd82 OK\n"
        "> \n"
        "────────────────────────────────────\n"
        "🕒 12:59:30 | 📁 tmp |🤖 Gemini 3.5 Flash (Medium) | 💰 $-0.00 sessi\n"
    )
    read_call_count = {"n": 0}

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            read_call_count["n"] += 1
            if read_call_count["n"] == 1:
                return _read_result(before_snapshot)
            if read_call_count["n"] == 2:
                return _read_result(stuck_snapshot)
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1C", "--no-audit", "--settle-delay", "0", message])
    assert rc == 0

    send_keys_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-keys"]]
    assert len(send_keys_calls) == 1, (
        "when agy's \">\" input line is pushed out of the bottom-of-screen window by "
        "the status line, the old logic would misjudge it as committed and skip the "
        "follow-up Enter entirely; after the fix it should correctly judge it as not "
        "committed and attempt the follow-up Enter"
    )


def test_notify_pane_ambiguous_fallback_never_confidently_confirms(monkeypatch, capsys):
    """#65 fix direction 2: when no known TUI pattern matches at all (an
    uncertain state), even if the conservative whole-screen diff judgment
    says "looks committed", it must not be treated as "definitely committed"
    and short-circuit early, skipping the follow-up Enter -- only a
    "definite" result from a matched known pattern can end the retry loop.
    For a completely unfamiliar screen structure, the established principle
    is to prefer misjudging as undelivered and erroring out (retry to the
    limit, then error out clearly) over misjudging as delivered.

    Each read returns a different screen (simulating a continuously changing
    screen), and none of it contains any known TUI marker (❯/›/>/┃╹) -- the
    old logic's conservative fallback would misjudge it as committed and skip
    the follow-up Enter entirely, based purely on "the screen changed + the
    message prefix isn't at the tail". After the fix: this kind of uncertain
    state is always treated as not committed, every attempt actually sends
    Enter, and it errors out clearly after exhausting retries instead of
    silently reporting a false success.
    """
    message = "some message"
    read_call_count = {"n": 0}

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            read_call_count["n"] += 1
            # contains no known TUI marker at all, and no trace of the message
            # itself -- differs every time, to avoid hitting the "after == before"
            # path and force the flow into the conservative fallback diagnostic path.
            return _read_result(f"totally unrecognized tui screen v{read_call_count['n']}\nline a\nline b\n")
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wZ:p1", "--no-audit", "--settle-delay", "0", message])
    assert rc == 5, "a completely unrecognizable screen (uncertain state) must not be treated as definitely delivered, should error out"

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    send_keys_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-keys"]]
    assert len(send_text_calls) == 3, "should really retry up to the default limit (3 times)"
    assert len(send_keys_calls) == 3, (
        "an uncertain state must not skip the follow-up Enter just because the "
        "conservative fallback guesses \"committed\" -- every attempt should really send Enter"
    )


def test_notify_pane_rejects_injection_into_zombie_pane(monkeypatch, capsys):
    """#64 hard-won lesson (F1-02, marked "most dangerous scenario" in the
    RUNBOOK): kill -9 on the agent's foreground process while keeping the
    pane -- agent_status goes from the known idle to unknown (herdr can't
    detect the agent). The screen still shows the agent's last normal prompt
    from before it died, looking fully injectable -- the old ready check
    (#44's _detect_blocking_prompt) only recognized these known non-prompt
    states: trust confirmation/menu/y-n/password. A zombie pane belongs to
    none of them, so after sending the message the pane returns to the shell,
    the screen changes, and the commit check misjudges it as committed -- but
    the agent is dead, so the message will never be processed.

    Fix: added zombie-pane detection to the ready check -- if a pane has ever
    detected an agent (the agent field is non-empty) but currently has
    agent_status == "unknown", treat it as suspicious and further verify with
    `herdr pane process-info` whether the foreground process still exists;
    only when the process is confirmed gone does it definitely reject
    injection (avoiding blocking a still-alive agent based solely on the
    weaker unknown signal).
    """
    message = "please start the next task"
    normal_looking_snapshot = "history\n❯ \n"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(normal_looking_snapshot)
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result(
                '{"result": {"pane": {"agent": "grok", "agent_status": "unknown"}}}'
            )
        if argv[:3] == ["herdr", "pane", "process-info"]:
            # empty foreground process list -> the process really is gone, the agent is dead
            return _read_result('{"result": {"process_info": {"foreground_processes": []}}}')
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1J", "--no-audit", "--retries", "2", message])
    assert rc == 5, "a zombie pane (agent is dead) must reject injection, must not send just because the screen looks normal"

    err = capsys.readouterr().err
    assert "wT:p1J" in err

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 0, "injection must not be attempted at all when a zombie pane is detected"


def test_notify_pane_unknown_status_with_live_process_is_not_treated_as_zombie(monkeypatch, capsys):
    """Regression protection: when agent_status=unknown but process-info shows
    the foreground process is genuinely still there, it must not be blocked
    based solely on the weaker unknown signal -- agent_status is known to have
    false negatives (documented in both #44/#64), process-info is the precise
    signal, and injection should proceed once process-info shows it's alive.
    """
    message = "please start the next task"
    submitted_snapshot = "history\n❯ \n"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result(
                '{"result": {"pane": {"agent": "grok", "agent_status": "unknown"}}}'
            )
        if argv[:3] == ["herdr", "pane", "process-info"]:
            return _read_result(
                '{"result": {"process_info": {"foreground_processes": [{"pid": 12345}]}}}'
            )
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1J", "--no-audit", message])
    assert rc == 0, "when process-info confirms the foreground process is still there, it should not be blocked by mistake due to the weaker unknown signal"

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 1


def test_notify_pane_normal_agent_status_skips_zombie_process_info_check(monkeypatch, capsys):
    """Cost control: when agent_status is a normal value (not unknown), the
    extra call to `herdr pane process-info` shouldn't happen at all (that's a
    more expensive precise query, only needed when the cheap agent_status=unknown
    signal has already triggered suspicion)."""
    message = "please start the next task"
    submitted_snapshot = "history\n❯ \n"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result(
                '{"result": {"pane": {"agent": "grok", "agent_status": "idle"}}}'
            )
        if argv[:3] == ["herdr", "pane", "process-info"]:
            raise AssertionError("process-info should not be called when agent_status is normal")
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1J", "--no-audit", message])
    assert rc == 0

    process_info_calls = [c for c in calls if c[:3] == ["herdr", "pane", "process-info"]]
    assert len(process_info_calls) == 0


def test_notify_pane_rejects_injection_into_busy_pane_by_default(monkeypatch, capsys):
    """#68 hard-won lesson: injecting into grok while busy (agent_status=working)
    interrupts the previous job, and the first message is lost for good (it's
    not queue-based -- the control group claude/agy are queue-based, both
    messages get processed). codex/opencode haven't been tested, so to be
    conservative we treat them all as possibly interrupt-based, and can't
    assume they'll queue.

    agent_status's reliability is asymmetric: working is a trustworthy
    "positive" signal (when it shows working, the agent really is busy),
    unlike idle's known false negatives. Using agent_status for a "reject"
    decision is safe -- reject injection into a busy pane by default, to
    avoid an interrupt-based TUI losing the previous job.
    """
    message = "second message"
    normal_looking_snapshot = "history\n❯ \n"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(normal_looking_snapshot)
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result(
                '{"result": {"pane": {"agent": "grok", "agent_status": "working"}}}'
            )
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1A", "--no-audit", "--retries", "2", message])
    assert rc == 5, "should reject injection by default when agent_status=working, to avoid an interrupt-based TUI losing the previous job"

    err = capsys.readouterr().err
    assert "wT:p1A" in err
    assert "--allow-busy" in err, "the error message should mention that --allow-busy can override this"

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 0, "injection must not be attempted at all by default when a busy pane is detected"


def test_notify_pane_allow_busy_flag_overrides_busy_rejection(monkeypatch, capsys):
    """Escape hatch: when the tower knows the target is a queue-based TUI
    (claude/agy) and wants to give additional instructions to an agent that's
    currently working, --allow-busy can explicitly override the default rejection."""
    message = "additional instructions"
    submitted_snapshot = "history\n❯ \n"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result(
                '{"result": {"pane": {"agent": "claude", "agent_status": "working"}}}'
            )
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1E", "--no-audit", "--allow-busy", message])
    assert rc == 0, "with --allow-busy set, injection into a busy pane should be allowed"

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 1


def test_notify_pane_idle_status_does_not_trigger_busy_or_zombie_rejection(monkeypatch, capsys):
    """Regression protection: agent_status=idle (normal, low reliability, but
    not working/unknown) should not trigger the busy or zombie check -- idle
    has no dedicated "confirm injectable" check because it's inherently
    untrustworthy, but it also should not be misjudged as working or unknown
    and rejected."""
    message = "normal dispatch"
    submitted_snapshot = "history\n❯ \n"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result(
                '{"result": {"pane": {"agent": "claude", "agent_status": "idle"}}}'
            )
        if argv[:3] == ["herdr", "pane", "process-info"]:
            raise AssertionError("idle status should not trigger a process-info query")
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1E", "--no-audit", message])
    assert rc == 0

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 1


def test_notify_pane_rejects_injection_before_tui_ready(monkeypatch, capsys):
    """#69 hard-won lesson (F1-04): injecting immediately after `herdr agent
    start`, agent_status already reports idle (untrustworthy -- it was never
    reliable for confirming injectability anyway), but the TUI actually hasn't
    finished initializing yet, so the message gets silently dropped during
    initialization. Fix: before injecting, use `herdr pane wait-output --regex`
    to wait for the screen to render any known TUI prompt; if none appears
    within the timeout, reject the injection outright without entering the
    retry loop.
    """
    message = "dispatch right after startup"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "wait-output"]:
            return _FakeResult(returncode=1)  # timed out, no known prompt appeared
        raise AssertionError(f"there should be no other calls while not ready: {argv}")

    calls = _install_fake_run(monkeypatch, handler, wait_output_ready=False)

    rc = main(["notify-pane", "--pane", "wT:p1A", "--no-audit", "--ready-timeout", "0.01", message])
    assert rc == 5, "must reject injection when the TUI hasn't rendered any known prompt yet"

    err = capsys.readouterr().err
    assert "wT:p1A" in err

    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 0, "must not enter the injection retry loop at all while not ready"


def test_notify_pane_proceeds_normally_once_tui_ready(monkeypatch, capsys):
    """Control group: wait-output confirms ready immediately (the TUI is
    already rendering a prompt), notify-pane should proceed through the whole
    injection flow as usual, unslowed and unblocked by this new readiness check."""
    message = "TUI is ready"
    submitted_snapshot = "history\n❯ \n"

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            return _read_result(submitted_snapshot)
        return _ok_result()

    calls = _install_fake_run(monkeypatch, handler)  # defaults to wait_output_ready=True

    rc = main(["notify-pane", "--pane", "wT:p1A", "--no-audit", message])
    assert rc == 0

    wait_output_calls = [c for c in calls if c[:3] == ["herdr", "pane", "wait-output"]]
    assert len(wait_output_calls) == 1
    send_text_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-text"]]
    assert len(send_text_calls) == 1


def test_notify_pane_opencode_box_marker_stuck_triggers_enter_fallback(monkeypatch, capsys):
    """#65 hard-won lesson, follow-up (2026-07-25 T1-03 supplementary test):
    opencode also FAILs -- of five providers, only grok (whose structure is
    close to Claude Code) passes; codex/agy/opencode all fail -- per-TUI
    detection isn't an optimization, it's a necessity, since the current
    (pre-fix) logic only works for two of them.

    opencode uses a boxed-border input box (wrapped line by line with ┃,
    terminated by ╹▀▀▀..., not a single-character marker, #53); its screen
    structure is completely different from codex/agy, and neither the old
    `_extract_input_box_text` (only recognizes paired ─ borders) nor
    `_extract_input_line` (only recognizes ❯) can pick it up, so it likewise
    falls back to the conservative fallback and misjudges.

    This uses a real-world screen structure (corresponding to the same real
    pane-read result already verified in the harness's `test_screen_oracle.py`
    via `test_opencode_marker_stuck_in_boxed_input`/
    `test_opencode_marker_submitted_moves_above_box`) to directly verify that
    the production path `cmd_notify_pane`/`_looks_submitted`, via the shared
    `tui_patterns.PROMPT_PATTERNS["opencode"]` (box_markers=("┃","╹")),
    correctly judges it as not committed and triggers the follow-up Enter --
    not just verified on the harness side.
    """
    message = "034271ce please reply with only 034271ce OK"
    before_snapshot = "┃\n┃\n┃\n╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀"
    stuck_snapshot = (
        f"┃\n┃  {message}\n┃\n╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀"
    )
    submitted_snapshot = (
        f"  {message}\n"
        "  ▣  Build · Grok Build 0.1 · 30.7s\n"
        "┃\n┃\n┃\n"
        "┃  Build auto · Grok Build 0.1 xAI\n"
        "╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀"
    )
    read_call_count = {"n": 0}

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            read_call_count["n"] += 1
            if read_call_count["n"] == 1:
                return _read_result(before_snapshot)
            if read_call_count["n"] == 2:
                return _read_result(stuck_snapshot)
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wY:p1", "--no-audit", "--settle-delay", "0", message])
    assert rc == 0, "the message really did commit after the follow-up Enter, should report success"

    send_keys_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-keys"]]
    assert len(send_keys_calls) == 1, (
        "the old logic can't pick up opencode's boxed-border input box (┃/╹) at all, and "
        "misjudges it as committed, skipping the follow-up Enter; after the fix it should "
        "correctly judge it as not committed and attempt the follow-up Enter"
    )

    out = capsys.readouterr().out
    assert "fallback_enter_used=True" in out


def test_notify_pane_long_scrolled_message_stuck_in_box_triggers_enter_fallback(monkeypatch, capsys):
    """2026-07-25 hard-won lesson (a real incident, not a hypothetical): sending
    a long, multi-paragraph decision notice to three other coordination
    towers, `herdr-commander notify-pane` reported "delivery
    confirmed, fallback_enter_used=False" for all three, but reading the
    screen with `--source visible` showed all three messages still entirely
    stuck in their input boxes, never actually sent -- Aiken caught this by
    staring directly at the screen, not because automated tests caught it first.

    Root cause: `_looks_submitted()` judges whether a message committed based
    on whether "the message's first line (`head`, first 40 chars) is still in
    the box". After injecting a long, multi-paragraph message, the Claude Code
    input box screen scrolls to the tail of the message, and the first line
    scrolls out of the visible range -- the box clearly still has the entire
    message stuffed in it (you just can't see the beginning), but the old
    logic, unable to "find head", directly judged it as "committed" and marked
    it `ambiguous=False` (meaning "definite"), completely skipping the
    follow-up Enter.

    This reproduces the actual incident with a real 5-paragraph message plus a
    box that only shows the last few paragraphs, verifying that after the fix:
    when the box isn't empty and head can't be found, it's judged as "not
    committed, uncertain" (ambiguous=True), triggering the follow-up Enter, so
    it no longer reports a false delivery success.
    """
    message = (
        "[herdr-bridge tower relaying Aiken's business decision]\n\n"
        "Aiken has decided: all four towers will adopt the new \"cross-tower "
        "shared infrastructure coordination protocol.\"\n\n"
        "1. Four fixed cross-project labels in RemaGraph.\n"
        "2. Three hard rules.\n"
        "3. Dual-track health checks.\n"
        "4. A minimal known-issues board.\n"
        "5. Ownership model: herdr-bridge is the template owner and first "
        "implementer of the coordination mechanism, with the other three "
        "towers rotating health-check duty.\n\n"
        "This is the direction Aiken has already decided to adopt, not a "
        "proposal still under consideration."
    )
    before_snapshot = "──────────────────────────────\n❯ \n──────────────────────────────"
    # simulates the screen scrolling to the tail after injecting a long message:
    # the box only shows the last few paragraphs, the first line (carrying the
    # head fingerprint) has already scrolled out of view, but the box itself
    # isn't empty -- this is exactly the real screen structure from the incident.
    stuck_snapshot = (
        "──────────────────────────────\n"
        "❯ 5. Ownership model: herdr-bridge is the template owner and first "
        "implementer of the coordination mechanism, with the other three\n"
        "towers rotating health-check duty.\n\n"
        "This is the direction Aiken has already decided to adopt, not a "
        "proposal still under consideration.\n"
        "──────────────────────────────"
    )
    submitted_snapshot = "──────────────────────────────\n❯ \n──────────────────────────────"
    read_call_count = {"n": 0}

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            read_call_count["n"] += 1
            if read_call_count["n"] == 1:
                return _read_result(before_snapshot)
            if read_call_count["n"] == 2:
                return _read_result(stuck_snapshot)
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "w17:p1", "--tui", "claude", "--no-audit", "--settle-delay", "0", message])
    assert rc == 0, "the message really did commit after the follow-up Enter, should report success"

    send_keys_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-keys"]]
    assert len(send_keys_calls) == 1, (
        "when a long message scrolling means the head fingerprint isn't in the visible "
        "box content, the pre-fix logic misjudges it as committed and skips the follow-up "
        "Enter; after the fix, a non-empty box should not be confidently treated as "
        "committed, and should trigger the follow-up Enter"
    )

    out = capsys.readouterr().out
    assert "fallback_enter_used=True" in out


def test_looks_submitted_returns_true_when_box_is_completely_empty():
    """The most reliable "committed" signal for `_looks_submitted` is that the
    input box is completely empty -- no matter how long the message is or
    whether it scrolled out of the visible range, an empty box is empty."""
    from herdr_bridge.light.cli import _looks_submitted

    before = "──────────────────────────────\n❯ \n──────────────────────────────"
    after = "──────────────────────────────\n❯ \n──────────────────────────────"
    result = _looks_submitted(before, after, "message content of any length", tui="claude")
    assert result.submitted is True
    assert result.ambiguous is False


def test_looks_submitted_returns_false_when_head_still_visible_in_box():
    """In the general case (short message, beginning visible in the box), the
    original behavior holds: finding head means it's definitely not committed."""
    from herdr_bridge.light.cli import _looks_submitted

    before = "──────────────────────────────\n❯ \n──────────────────────────────"
    after = "──────────────────────────────\n❯ short message still stuck here\n──────────────────────────────"
    result = _looks_submitted(before, after, "short message still stuck here", tui="claude")
    assert result.submitted is False
    assert result.ambiguous is False


def test_notify_pane_gemini_box_wrapped_message_stuck_triggers_enter_fallback(monkeypatch, capsys):
    """2026-07-25 hard-won lesson (a real incident, caught in the moment): the
    Gemini CLI's input box is a paired-border box like claude's (top border
    U+2584, bottom border U+2580, not U+2500), but its prompt is `>` not `❯`.
    The old `_extract_input_box_text` hardcoded recognition of only `❯` to
    validate box content; for Gemini's box it can't find `❯`, judges "this
    isn't an input box", and returns None, falling back to single-line marker
    scanning -- that path only looks at the last line containing `>`, and when
    the message wraps onto a second line without a `>` marker, it's missed
    entirely and misjudged as "committed".

    This reproduces the actual incident with a real two-line message (`>` only
    on the first line, the second line is plain wrapped content): the box
    content can't find `❯` but can find `>`, verifying that after the fix
    `_extract_input_box_text` uses the actual marker set for that TUI,
    correctly judging "box non-empty, complete message not found = not
    committed, uncertain", triggering the follow-up Enter.
    """
    top_border = "▄" * 45
    bottom_border = "▀" * 45
    message = "gemini-cli-pattern-probe-7c4d1 test message"
    before_snapshot = f"{top_border}\n >   Type your message or @path/to/file\n{bottom_border}"
    # the actual screen structure from the incident: the message wraps onto two
    # lines, `>` only on the first line, the second line "test message" is plain
    # content with no marker -- the box content can't find ❯ (the old logic's
    # hardcoded assumption) but can find > (the real prompt).
    stuck_snapshot = f"{top_border}\n > gemini-cli-pattern-probe-7c4d1\n   test message\n{bottom_border}"
    submitted_snapshot = before_snapshot
    read_call_count = {"n": 0}

    def handler(argv):
        if argv[:3] == ["herdr", "pane", "read"]:
            read_call_count["n"] += 1
            if read_call_count["n"] == 1:
                return _read_result(before_snapshot)
            if read_call_count["n"] == 2:
                return _read_result(stuck_snapshot)
            return _read_result(submitted_snapshot)
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            return _ok_result()
        if argv[:3] == ["herdr", "pane", "get"]:
            return _read_result('{"result": {"pane": {"agent_status": "idle"}}}')
        raise AssertionError(f"unexpected argv: {argv}")

    calls = _install_fake_run(monkeypatch, handler)

    rc = main(["notify-pane", "--pane", "wT:p1V", "--tui", "gemini", "--no-audit", "--settle-delay", "0", message])
    assert rc == 0, "the message really did commit after the follow-up Enter, should report success"

    send_keys_calls = [c for c in calls if c[:3] == ["herdr", "pane", "send-keys"]]
    assert len(send_keys_calls) == 1, (
        "Gemini uses > not ❯; the old logic's box content validation hardcoded ❯, causing "
        "it to misjudge as committed and skip the follow-up Enter; after the fix it should "
        "correctly judge it as not committed and attempt the follow-up Enter"
    )

    out = capsys.readouterr().out
    assert "fallback_enter_used=True" in out


def test_extract_input_box_text_uses_provided_markers_not_hardcoded_claude_marker():
    """`_extract_input_box_text`'s box content validation must use the markers
    passed in by the caller, not hardcode ❯ -- this is the minimal reproduction
    of the Gemini hard-won lesson."""
    from herdr_bridge.light.cli import _extract_input_box_text

    top_border = "▄" * 45
    bottom_border = "▀" * 45
    snapshot = f"{top_border}\n > content\n{bottom_border}"
    # the default (❯) can't be found, should return None
    assert _extract_input_box_text(snapshot) is None
    # only passing in the correct marker (>) finds it
    result = _extract_input_box_text(snapshot, markers=(">",))
    assert result is not None
    assert "content" in result
