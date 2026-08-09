# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""screen_oracle unit tests — all use pre-built screen snapshot strings as
fixtures, never touch a real pane, never call the herdr CLI (`capture()`'s
tests isolate subprocess via monkeypatch).
"""

from __future__ import annotations

import subprocess

import pytest

import tests.support.screen_oracle as screen_oracle_module
from tests.support.screen_oracle import (
    PROMPT_PATTERNS,
    capture,
    read_until,
    verify_submitted,
)

MARKER = "HERDR_TEST_7f3a9b"


# ---------------------------------------------------------------------------
# verify_submitted — claude (❯, a single-character marker, not box-drawn)
# ---------------------------------------------------------------------------


def test_claude_submitted_marker_moves_to_history():
    before = f"  some earlier conversation history\n\n❯ {MARKER}"
    after = f"  some earlier conversation history\n\n  {MARKER}\n\n❯ "
    result = verify_submitted(before, after, MARKER, tui="claude")

    assert result.submitted is True
    assert result.still_in_input_box is False
    assert result.in_history is True
    assert result.ambiguous is False
    assert result.tui == "claude"


def test_claude_not_submitted_marker_stuck_in_input_box():
    before = "  some earlier conversation history\n\n❯ "
    after = f"  some earlier conversation history\n\n❯ {MARKER}"
    result = verify_submitted(before, after, MARKER, tui="claude")

    assert result.submitted is False
    assert result.still_in_input_box is True
    assert result.in_history is False


# ---------------------------------------------------------------------------
# verify_submitted — codex (›, careful not to mistake the idle placeholder
# for actual user input)
# ---------------------------------------------------------------------------


def test_codex_submitted_with_placeholder_prompt_line():
    before = f"› {MARKER}"
    # After submission codex's input box returns to its idle placeholder;
    # the marker appears in the history region above.
    after = f"  {MARKER}\n\n› Implement {{feature}}"
    result = verify_submitted(before, after, MARKER, tui="codex")

    assert result.submitted is True
    assert result.still_in_input_box is False
    assert result.in_history is True


def test_codex_placeholder_alone_is_not_mistaken_for_history_match():
    # The marker never actually appears in the history region — it's just
    # the input box itself still sitting on its original placeholder text —
    # this must not be mistaken for submitted.
    before = "› Implement {feature}"
    after = f"› {MARKER}"
    result = verify_submitted(before, after, MARKER, tui="codex")

    assert result.submitted is False
    assert result.still_in_input_box is True
    assert result.in_history is False


def test_codex_truncated_history_display_uses_reply_marker_fallback():
    """#59 regression test: a real T1-01 screen captured against wT:p1B on
    2026-07-25. When codex displays a sent message in the history region it
    **truncates** it (not the #48 wrap-based line break, an actual
    truncation) — no substring of the full marker matches anything, so a
    direct substring comparison misjudges in_history=False -> submitted=False
    (a false FAIL). This instead detects codex's reply-block `•` prefix as
    an alternative signal: if `after` has a new line starting with `•` that
    wasn't in `before`, that means the downstream really did process it, and
    it's treated as in_history=True.
    """
    real_marker = "Reply with only this line, do nothing else: HERDR_T_co53306 ACK"
    before = f"› {real_marker}"
    after = "› 254 ACK\n• ACK\n› Summarize recent commits"
    result = verify_submitted(before, after, real_marker, tui="codex")

    assert result.submitted is True
    assert result.still_in_input_box is False
    assert result.in_history is True
    assert result.ambiguous is False
    assert "reply_markers" in result.detail or "reply block" in result.detail


def test_codex_reply_marker_fallback_does_not_fire_on_preexisting_reply_line():
    """The reply_markers fallback signal must only recognize a reply line
    that's "new in `after`, absent from `before`" — it must not be
    triggered by a stale reply line already on screen from something
    unrelated to this injection."""
    before = "• an earlier, unrelated reply\n› please confirm"
    after = f"• an earlier, unrelated reply\n› {MARKER}"  # marker stuck in the input box, no new reply appeared
    result = verify_submitted(before, after, MARKER, tui="codex")

    assert result.submitted is False
    assert result.still_in_input_box is True
    assert result.in_history is False


# ---------------------------------------------------------------------------
# verify_submitted — opencode (box-drawn input: wrapped line-by-line in ┃,
# terminated by ╹▀▀▀…, #53)
# ---------------------------------------------------------------------------


def test_opencode_marker_stuck_in_boxed_input():
    # Corresponds to a real screen captured against wY:p1 on 2026-07-25
    # (the box contains text the user just typed, not yet submitted).
    before = "┃\n┃\n┃\n╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀"
    after = f"┃\n┃  {MARKER}\n┃\n╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀"
    result = verify_submitted(before, after, MARKER, tui="opencode")

    assert result.ambiguous is False
    assert result.submitted is False
    assert result.still_in_input_box is True
    assert result.in_history is False


def test_opencode_marker_submitted_moves_above_box():
    # Corresponds to a real screen captured against wY:p1 on 2026-07-25:
    # after submission the marker appears in the history region outside the
    # box (the "▣ Build · ..." section above), and the box collapses back
    # down to just its status line (still starting with ┃).
    before = f"┃\n┃  {MARKER}\n┃\n╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀"
    after = (
        f"  {MARKER}\n"
        "  ▣  Build · Grok Build 0.1 · 30.7s\n"
        "┃\n┃\n┃\n"
        "┃  Build auto · Grok Build 0.1 xAI\n"
        "╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀"
    )
    result = verify_submitted(before, after, MARKER, tui="opencode")

    assert result.ambiguous is False
    assert result.submitted is True
    assert result.still_in_input_box is False
    assert result.in_history is True


def test_opencode_no_boxed_input_found_is_ambiguous():
    # No ╹ terminator line found (e.g. the screen hasn't finished
    # rendering): fall back to a conservative verdict, don't misjudge it as submitted.
    before = "an ordinary line of text"
    after = f"an ordinary line of text\n{MARKER}"
    result = verify_submitted(before, after, MARKER, tui="opencode")

    assert result.ambiguous is True
    assert result.submitted is False


# ---------------------------------------------------------------------------
# verify_submitted — no known marker pattern found (simulating a completely
# unregistered TUI format) → falls back to a conservative overall diff verdict
# ---------------------------------------------------------------------------


def test_unknown_tui_pattern_falls_back_to_conservative_diff():
    # Uses a made-up box-drawing style that was never registered (not
    # opencode's ┃/╹, which now has its own dedicated detection, see #53),
    # to make sure this actually tests the "no known pattern found at all" case.
    before = "[[INPUT]]\nprevious content\n[[/INPUT]]"
    after = f"[[INPUT]]\n{MARKER}\n[[/INPUT]]"
    result = verify_submitted(before, after, MARKER, tui=None)

    assert result.ambiguous is True
    # The marker still appears in the last few lines of the screen (near the
    # input box), so the conservative verdict is "not submitted".
    assert result.submitted is False
    assert result.still_in_input_box is True


def test_unknown_tui_pattern_no_change_is_not_submitted():
    snapshot = "[[INPUT]]\nprevious content\n[[/INPUT]]"
    result = verify_submitted(snapshot, snapshot, MARKER, tui=None)

    assert result.ambiguous is True
    assert result.submitted is False
    assert result.in_history is False


def test_tui_none_auto_detects_known_pattern():
    # No tui specified, but the screen content matches the ❯ pattern shared
    # by claude/grok, so it should still be able to auto-locate the input box.
    before = f"❯ {MARKER}"
    after = f"  {MARKER}\n\n❯ "
    result = verify_submitted(before, after, MARKER, tui=None)

    assert result.ambiguous is False
    assert result.submitted is True
    assert result.tui in ("claude", "grok")  # both share the same marker, whichever matches first wins


# ---------------------------------------------------------------------------
# edge cases
# ---------------------------------------------------------------------------


def test_intentional_empty_message_is_submitted_when_expect_empty_declared():
    # Only treated as necessarily submitted when the caller explicitly
    # declares "this is deliberately testing an empty message" (Runbook
    # Section 3 axis 3).
    result = verify_submitted("❯ ", "❯ ", "\n", tui="claude", expect_empty=True)
    assert result.submitted is True
    assert result.still_in_input_box is False
    assert result.ambiguous is False


def test_marker_becoming_empty_without_declared_intent_is_ambiguous_not_submitted():
    """#47 regression test: `_marker_head()` takes the first 40 characters
    after stripping — if the caller never declared it was testing an empty
    message (e.g. T1-04's special-character test has a marker that
    unexpectedly ends up being entirely whitespace/control characters, or
    starts with a newline), head ends up empty just the same. This
    "unexpectedly empty" case must not be unconditionally judged
    submitted=True, or it produces a false success; it must return
    ambiguous=True instead, honoring the module docstring's principle:
    better to misjudge "not submitted" than to misjudge "submitted".
    """
    result = verify_submitted("❯ ", "❯ ", "   \n\t  ", tui="claude")
    assert result.ambiguous is True
    assert result.submitted is False


def test_expect_empty_defaults_to_false():
    # Without an explicit declaration, the default assumes "unexpectedly
    # empty", not "deliberately testing an empty message".
    result = verify_submitted("❯ ", "❯ ", "\n", tui="claude")
    assert result.ambiguous is True
    assert result.submitted is False


def test_unknown_tui_name_behaves_like_no_pattern_found():
    before = "a screen with no known prompt"
    after = f"a screen with no known prompt\n{MARKER}"
    result = verify_submitted(before, after, MARKER, tui="totally-unknown-agent")
    assert result.ambiguous is True


def test_head_len_is_configurable_for_narrow_panes():
    """#48 defense-in-depth: even if the caller didn't use
    recent-unwrapped, and the screen itself is text that got wrapped by a
    narrow pane, shortening the fingerprint length below the pane's width
    still avoids the line-break problem. The default stays at 40
    (compatible with existing behavior), but can be overridden.
    """
    long_marker = "HERDR_T_deadbeef_this_is_a_long_marker_that_would_wrap_on_a_narrow_pane"
    # Simulate a 41-column narrow pane: the long marker gets broken across
    # two displayed lines after submission.
    after = "  HERDR_T_deadbeef_this_is_a_long_ma\n  rker_that_would_wrap_on_a_narrow_pane\n\n❯ "
    before = f"❯ {long_marker}"

    # The default 40-character fingerprint spans the line-break point, so it
    # can't be matched -> conservative verdict of "not submitted"
    default_result = verify_submitted(before, after, long_marker, tui="claude")
    assert default_result.submitted is False

    # Shortening the fingerprint to within the line-break point (e.g. the
    # short-token convention Runbook recommends, < 20 characters) lets it
    # locate correctly
    short_result = verify_submitted(before, after, long_marker, tui="claude", head_len=16)
    assert short_result.submitted is True


def test_marker_only_in_scrollback_history_not_current_input_line():
    # The marker string appeared once before, further back in the history
    # (e.g. a duplicate-message test), and the input box is currently empty
    # — the input box's line (the last line) must be excluded from the
    # "history region" comparison, only the remaining lines should count.
    before = f"  {MARKER} (an earlier history entry)\n\n❯ "
    after = f"  {MARKER} (an earlier history entry)\n\n  {MARKER}\n\n❯ "
    result = verify_submitted(before, after, MARKER, tui="claude")
    assert result.submitted is True
    assert result.still_in_input_box is False


# ---------------------------------------------------------------------------
# capture() — isolates subprocess, verifies wrapper behavior and failure tolerance
# ---------------------------------------------------------------------------


def test_capture_returns_stdout_on_success(monkeypatch):
    captured_argv = {}

    def fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout="screen content", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = capture("wT:p1A", source="recent", lines=30)

    assert result == "screen content"
    assert captured_argv["argv"] == [
        "herdr", "pane", "read", "wT:p1A",
        "--source", "recent",
        "--lines", "30",
        "--format", "text",
    ]


def test_capture_defaults_to_recent_unwrapped_source(monkeypatch):
    """#48 regression test: on a narrow pane (e.g. a 41-42 column split
    view), `recent` (which wraps lines based on screen width) easily breaks
    a 40-character fingerprint across two lines, so no single-line `in`
    comparison finds it, producing a false "not submitted" verdict.
    `recent-unwrapped` returns the unwrapped version, sidestepping the
    problem entirely, so it should be the default rather than requiring
    every caller to remember to pass `source="recent-unwrapped"`.
    """
    captured_argv = {}

    def fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    capture("wT:p1A")

    assert "--source" in captured_argv["argv"]
    idx = captured_argv["argv"].index("--source")
    assert captured_argv["argv"][idx + 1] == "recent-unwrapped"


def test_capture_returns_empty_string_on_nonzero_exit(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="pane not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert capture("does:not:exist") == ""


def test_capture_returns_empty_string_on_exception(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert capture("wT:p1A") == ""


# ---------------------------------------------------------------------------
# read_until() — polling wait, #67: addresses "reading the screen too early"
# and "a one-shot read can't tell still-waiting from genuinely-never-happened",
# which together were the root cause of at least two of the tower's four
# false failures today.
# All isolated via monkeypatch on capture()/time.time()/time.sleep(); no real
# waiting needed.
# ---------------------------------------------------------------------------


def test_read_until_returns_true_immediately_when_predicate_already_true(monkeypatch):
    monkeypatch.setattr(screen_oracle_module, "capture", lambda pane_id, **kw: "ready")
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    ok, snapshot = read_until("wT:p1A", lambda s: "ready" in s, timeout=10, interval=2)

    assert ok is True
    assert snapshot == "ready"
    assert sleep_calls == [], "predicate was already true on the first try, so it should never actually sleep for a polling interval"


def test_read_until_polls_until_predicate_becomes_true(monkeypatch):
    snapshots = iter(["not yet", "still not yet", "HERDR_OK now visible"])
    monkeypatch.setattr(screen_oracle_module, "capture", lambda pane_id, **kw: next(snapshots))
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("time.time", lambda: 0.0)  # fixed clock, loop only ends via the predicate

    ok, snapshot = read_until("wT:p1A", lambda s: "HERDR_OK" in s, timeout=10, interval=1)

    assert ok is True
    assert snapshot == "HERDR_OK now visible"
    assert len(sleep_calls) == 2, "it should sleep once between reads 1-2 and 2-3, and return immediately once read 3 succeeds"


def test_read_until_returns_false_after_timeout_with_last_snapshot(monkeypatch):
    call_count = {"n": 0}

    def fake_capture(pane_id, **kw):
        call_count["n"] += 1
        return f"snapshot-{call_count['n']}"

    monkeypatch.setattr(screen_oracle_module, "capture", fake_capture)

    fake_clock = {"t": 0.0}
    monkeypatch.setattr("time.time", lambda: fake_clock["t"])

    def fake_sleep(seconds):
        fake_clock["t"] += seconds

    monkeypatch.setattr("time.sleep", fake_sleep)

    ok, snapshot = read_until("wT:p1A", lambda s: "NEVER_APPEARS" in s, timeout=5, interval=2)

    assert ok is False
    assert snapshot.startswith("snapshot-")
    assert call_count["n"] >= 1, "it must have read the screen at least once before timing out — it can't declare failure without reading anything at all"


def test_read_until_defaults_to_visible_source():
    import inspect

    sig = inspect.signature(read_until)
    assert sig.parameters["source"].default == "visible", (
        "the default should be visible (the live screen most commonly used "
        "for manual review), different from capture()'s own default of "
        "recent-unwrapped — the two serve different purposes, don't conflate them"
    )


def test_read_until_passes_source_and_lines_through_to_capture(monkeypatch):
    captured_kwargs = {}

    def fake_capture(pane_id, **kw):
        captured_kwargs.update(kw)
        return "x"

    monkeypatch.setattr(screen_oracle_module, "capture", fake_capture)

    read_until("wY:p1", lambda s: True, source="recent", lines=80)

    assert captured_kwargs == {"source": "recent", "lines": 80}


# ---------------------------------------------------------------------------
# PROMPT_PATTERNS table structure — pins down this task's empirical findings
# so they don't get changed by accident later
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tui", ["claude", "opencode", "grok", "codex", "agy"])
def test_prompt_patterns_covers_all_five_tuis(tui):
    assert tui in PROMPT_PATTERNS
    pattern = PROMPT_PATTERNS[tui]
    # opencode has a box-drawn input (#53) with no single-character marker,
    # relying on box_markers to locate it instead.
    assert pattern.markers or pattern.box_markers, f"{tui} must have at least one of markers or box_markers"


def test_confirmed_flags_match_this_session_live_findings():
    # Read-only empirical checks on 2026-07-25 (herdr pane read): all five
    # confirmed (opencode's dedicated box-drawn-input detection logic was
    # completed in #53, see the test_opencode_* series).
    assert PROMPT_PATTERNS["claude"].confirmed is True
    assert PROMPT_PATTERNS["grok"].confirmed is True
    assert PROMPT_PATTERNS["codex"].confirmed is True
    assert PROMPT_PATTERNS["agy"].confirmed is True
    assert PROMPT_PATTERNS["opencode"].confirmed is True


def test_codex_marker_differs_from_claude_marker():
    # The key empirical finding of this task: codex uses › (U+203A), not claude's ❯ (U+276F)
    assert PROMPT_PATTERNS["codex"].markers != PROMPT_PATTERNS["claude"].markers
    assert "›" in PROMPT_PATTERNS["codex"].markers
    assert "❯" in PROMPT_PATTERNS["claude"].markers


def test_agy_marker_is_plain_ascii_gt_distinct_from_others():
    # Empirical finding of this task: agy uses a plain ASCII ">", different from both claude/grok's ❯ and codex's ›
    assert PROMPT_PATTERNS["agy"].markers == (">",)
    assert ">" not in PROMPT_PATTERNS["claude"].markers
    assert ">" not in PROMPT_PATTERNS["codex"].markers


def test_agy_submitted_marker_moves_to_history():
    before = "──────\n>\n──────\n🕒 08:27:31 status"
    # after the user has typed, before submitting
    typing = f"──────\n> {MARKER}\n──────\n🕒 08:27:32 status"
    result_typing = verify_submitted(before, typing, MARKER, tui="agy")
    assert result_typing.submitted is False
    assert result_typing.still_in_input_box is True

    after = f"  {MARKER}\n──────\n>\n──────\n🕒 08:27:33 status"
    result_submitted = verify_submitted(typing, after, MARKER, tui="agy")
    assert result_submitted.submitted is True
    assert result_submitted.still_in_input_box is False
    assert result_submitted.in_history is True
