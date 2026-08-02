# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `herdr-commander signal start/send/status` and the doctor
integration (design doc §3.4/§3.7)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_bridge.errors import HerdrBridgeError
from herdr_bridge.light import cli
from herdr_bridge.light.cli import build_parser
from herdr_bridge.signal import daemon as signal_daemon
from herdr_bridge.signal import outbound


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "herdr_bridge.orchestration._state_paths.signal_state_dir",
        lambda project_id: tmp_path / project_id,
    )
    monkeypatch.setattr(
        "herdr_bridge.orchestration.memory._signal_state_dir",
        lambda project_id: tmp_path / project_id,
    )


def _parse_and_run(argv):
    args = build_parser().parse_args(argv)
    return args.func(args)


# -- _resolve_signal_project ---------------------------------------------------

def test_resolve_signal_project_flag_wins_over_everything(monkeypatch, capsys):
    monkeypatch.setenv("CT_PROJECT", "other-tower")
    monkeypatch.setenv("HERDR_MEMORY_PROJECT", "downstream-tower")
    assert cli._resolve_signal_project("RemaGraph") == "RemaGraph"
    assert capsys.readouterr().err == ""  # explicit source given -- no warning


def test_resolve_signal_project_uses_ct_project_over_herdr_memory_project(monkeypatch, capsys):
    # This is the exact bug a real downstream deployment hit in the field: bare `signal start`
    # (no --project) must resolve to the tower's own CT_PROJECT, not fall
    # through to a more general-purpose env var and collide with
    # herdr-bridge's own daemon lock.
    monkeypatch.setenv("CT_PROJECT", "other-tower")
    monkeypatch.setenv("HERDR_MEMORY_PROJECT", "downstream-tower")
    assert cli._resolve_signal_project(None) == "other-tower"
    assert capsys.readouterr().err == ""  # CT_PROJECT found -- no warning


def test_resolve_signal_project_falls_back_to_herdr_memory_project(monkeypatch, capsys):
    monkeypatch.delenv("CT_PROJECT", raising=False)
    monkeypatch.setenv("HERDR_MEMORY_PROJECT", "downstream-tower")
    assert cli._resolve_signal_project(None) == "downstream-tower"
    assert capsys.readouterr().err == ""  # HERDR_MEMORY_PROJECT found -- no warning


def test_resolve_signal_project_ignores_remagraph_project_and_warns(monkeypatch, capsys):
    # REMAGRAPH_PROJECT is deliberately NOT in the signal resolution chain
    # (2026-08-01 field report -- a real bug in the first fix, not a
    # stylistic choice): `herdr_bridge/__init__.py` unconditionally
    # force-sets os.environ["REMAGRAPH_PROJECT"] = "herdr-bridge" as an
    # import-time side effect on every herdr-commander process, clobbering
    # any value the user exported themselves. Trusting it here would make
    # the warning below dead code again exactly like it was the first time.
    monkeypatch.delenv("CT_PROJECT", raising=False)
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.setenv("REMAGRAPH_PROJECT", "RemaGraph")
    assert cli._resolve_signal_project(None) == "herdr-bridge"
    assert "defaulting to project='herdr-bridge'" in capsys.readouterr().err


def test_resolve_signal_project_defaults_to_herdr_bridge(monkeypatch, capsys):
    monkeypatch.delenv("CT_PROJECT", raising=False)
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)
    assert cli._resolve_signal_project(None) == "herdr-bridge"


def test_resolve_signal_project_warns_on_stderr_when_falling_through_to_default(monkeypatch, capsys):
    # 2026-08-01 follow-up field report: a bare interactive shell
    # inside a non-herdr-bridge tower's own agent session doesn't inherit
    # CT_PROJECT at all (each Bash tool call is a fresh shell, not a child of
    # the bootstrap process) -- so it silently queries herdr-bridge's own
    # daemon and, if that daemon happens to be alive, reports a misleading
    # "✅ running" that looks like the caller's own tower. The fallback can't
    # be made correct in general, so it must at least stop being silent.
    monkeypatch.delenv("CT_PROJECT", raising=False)
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)
    cli._resolve_signal_project(None)
    err = capsys.readouterr().err
    assert "defaulting to project='herdr-bridge'" in err
    assert "--project" in err


def test_signal_status_warns_in_a_real_subprocess_despite_import_time_pollution(tmp_path):
    # Regression test for a real field report: a unit test that
    # calls _resolve_signal_project() directly never exercises importing the
    # herdr_bridge package itself, so it missed that `herdr_bridge/__init__.py`
    # unconditionally force-sets REMAGRAPH_PROJECT="herdr-bridge" as an
    # import-time side effect on every real `herdr-commander` invocation --
    # which made the first version of the warning dead code. Only a real
    # subprocess (which actually imports the package fresh, the way every
    # live `herdr-commander` call does) can catch this class of bug.
    # HOME is redirected to tmp_path so this doesn't touch the real
    # ~/.local/state/remagraph-hb-live-herdr-bridge directory.
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    # Deliberately set REMAGRAPH_PROJECT ourselves too, proving the
    # import-time assignment clobbers even an explicit user export.
    env["REMAGRAPH_PROJECT"] = "other-tower"

    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; from herdr_bridge.light.cli import main; sys.exit(main(['signal', 'status']))"],
        capture_output=True, text=True, timeout=30, env=env, check=False,
    )
    assert "defaulting to project='herdr-bridge'" in result.stderr


def test_signal_start_uses_ct_project_when_no_project_flag_given(monkeypatch):
    # Integration-level regression test for a real field report: a
    # bare `signal start` in a non-herdr-bridge tower's environment must
    # start that tower's own daemon, not herdr-bridge's.
    monkeypatch.setenv("CT_PROJECT", "other-tower")
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    seen_projects = []

    def _fake_resolve_own_pane_id(project):
        seen_projects.append(project)
        return "w18:p1"

    monkeypatch.setattr(signal_daemon, "resolve_own_pane_id", _fake_resolve_own_pane_id)
    monkeypatch.setattr("herdr_bridge.light.cli._load_or_create_shared_secret", lambda p: "s")

    async def _fake_run(project, secret):
        return None

    monkeypatch.setattr(signal_daemon, "run", _fake_run)

    code = _parse_and_run(["signal", "start"])
    assert code == 0
    assert seen_projects == ["other-tower"]


# -- signal send --------------------------------------------------------------

def test_signal_send_reports_success_on_injected(monkeypatch, capsys):
    async def _fake_send(*a, **k):
        return outbound.SendResult("msg-1", "injected")

    monkeypatch.setattr(outbound, "send", _fake_send)
    monkeypatch.setattr("herdr_bridge.light.cli._load_or_create_shared_secret", lambda p: "s")

    code = _parse_and_run([
        "signal", "send", "--to", "remagraph", "--inbox-ref", "task-1", "--project", "herdr-bridge",
    ])
    assert code == 0
    assert "delivered and injected" in capsys.readouterr().out


def test_signal_send_reports_failure_on_daemon_unreachable(monkeypatch, capsys):
    async def _fake_send(*a, **k):
        return outbound.SendResult("msg-2", "daemon_unreachable")

    monkeypatch.setattr(outbound, "send", _fake_send)
    monkeypatch.setattr("herdr_bridge.light.cli._load_or_create_shared_secret", lambda p: "s")

    code = _parse_and_run([
        "signal", "send", "--to", "remagraph", "--inbox-ref", "task-1", "--project", "herdr-bridge",
    ])
    assert code == 1
    assert "unreachable" in capsys.readouterr().err


def test_signal_send_reports_warning_on_injection_failed_transient(monkeypatch, capsys):
    async def _fake_send(*a, **k):
        return outbound.SendResult("msg-3", "injection_failed_transient")

    monkeypatch.setattr(outbound, "send", _fake_send)
    monkeypatch.setattr("herdr_bridge.light.cli._load_or_create_shared_secret", lambda p: "s")

    code = _parse_and_run([
        "signal", "send", "--to", "remagraph", "--inbox-ref", "task-1", "--project", "herdr-bridge",
    ])
    assert code == 1
    assert "injection unconfirmed" in capsys.readouterr().err


def test_signal_send_reports_deduplicated_inflight_distinctly(monkeypatch, capsys):
    """2026-08-01 status-split regression test: this must NOT show the same
    "injection unconfirmed" message as a plain transient miss -- a caller
    retrying immediately after seeing this needs a different signal (see
    SendResult.status's docstring for why)."""
    async def _fake_send(*a, **k):
        return outbound.SendResult("msg-4", "deduplicated_inflight")

    monkeypatch.setattr(outbound, "send", _fake_send)
    monkeypatch.setattr("herdr_bridge.light.cli._load_or_create_shared_secret", lambda p: "s")

    code = _parse_and_run([
        "signal", "send", "--to", "remagraph", "--inbox-ref", "task-1", "--project", "herdr-bridge",
    ])
    assert code == 1
    err = capsys.readouterr().err
    assert "already in flight" in err
    assert "injection unconfirmed" not in err


# -- signal status -------------------------------------------------------------

def test_signal_status_reports_no_daemon_started_yet(capsys):
    code = _parse_and_run(["signal", "status", "--project", "herdr-bridge"])
    assert code == 0
    out = capsys.readouterr().out
    assert "never been started" in out
    assert "No Signal records" in out


def test_signal_status_lists_recent_records(monkeypatch, capsys):
    from herdr_bridge.orchestration import memory as memory_mod

    memory_mod.mark_accepted(
        "herdr-bridge", "msg-4", from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-9"
    )
    code = _parse_and_run(["signal", "status", "--project", "herdr-bridge"])
    assert code == 0
    out = capsys.readouterr().out
    assert "msg-4" in out
    assert "task-9" in out


# -- signal status --notify-on-problem (task #84: daemon liveness is now
# scheduleable/observable, not just a manual `signal status` call) ---------

def test_signal_status_notifies_on_problem_when_flag_given(capsys, monkeypatch):
    from herdr_bridge.orchestration._state_paths import signal_state_dir

    state_dir = signal_state_dir("herdr-bridge")
    state_dir.mkdir(parents=True)
    (state_dir / "daemon.lock").touch()  # exists but nothing holds it -- a real problem

    notify_calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: notify_calls.append(args))

    code = _parse_and_run(["signal", "status", "--project", "herdr-bridge", "--notify-on-problem"])
    assert code == 1
    assert len(notify_calls) == 1
    assert notify_calls[0][:3] == ["herdr", "notification", "show"]


def test_signal_status_does_not_notify_without_the_flag(capsys, monkeypatch):
    """Default off (see the CLI help text): interactive use already shows
    the problem on stderr, a popup on top of that would just be noise."""
    from herdr_bridge.orchestration._state_paths import signal_state_dir

    state_dir = signal_state_dir("herdr-bridge")
    state_dir.mkdir(parents=True)
    (state_dir / "daemon.lock").touch()

    notify_calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: notify_calls.append(args))

    code = _parse_and_run(["signal", "status", "--project", "herdr-bridge"])
    assert code == 1
    assert notify_calls == []


def test_signal_status_does_not_notify_when_healthy_even_with_the_flag(capsys, monkeypatch):
    notify_calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: notify_calls.append(args))

    code = _parse_and_run(["signal", "status", "--project", "herdr-bridge", "--notify-on-problem"])
    assert code == 0
    assert notify_calls == []


def test_notify_signal_daemon_problem_tolerates_subprocess_failure(monkeypatch):
    def _raise(*a, **k):
        raise OSError("herdr command not found")

    monkeypatch.setattr(cli.subprocess, "run", _raise)
    cli._notify_signal_daemon_problem("herdr-bridge", ["daemon has stopped"])  # must not raise


# -- doctor integration ---------------------------------------------------------

def test_doctor_check_signal_daemon_reports_not_started(capsys):
    problems: list[str] = []
    cli._doctor_check_signal_daemon("herdr-bridge", problems)
    assert problems == []
    assert "never been started" in capsys.readouterr().out


def test_doctor_check_signal_daemon_situation_a_lock_not_held(tmp_path, capsys, monkeypatch):
    from herdr_bridge.orchestration._state_paths import signal_state_dir

    state_dir = signal_state_dir("herdr-bridge")
    state_dir.mkdir(parents=True)
    (state_dir / "daemon.lock").touch()  # exists but nothing holds it

    problems: list[str] = []
    cli._doctor_check_signal_daemon("herdr-bridge", problems)
    assert len(problems) == 1
    assert "daemon has stopped" in problems[0]
    assert "❌ Signal daemon is not running" in capsys.readouterr().err


def test_doctor_check_signal_daemon_situation_b_pane_rebuilt(tmp_path, capsys, monkeypatch):
    from herdr_bridge.orchestration._state_paths import signal_state_dir
    from herdr_bridge.signal.lock import SingleInstanceLock

    state_dir = signal_state_dir("herdr-bridge")
    state_dir.mkdir(parents=True)
    (state_dir / "pane_id.pin").write_text("w1:p1")

    lock = SingleInstanceLock(state_dir / "daemon.lock")
    lock.acquire()
    try:
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *a, **k: type("R", (), {"stdout": json.dumps({"result": {"panes": []}})})(),
        )
        problems: list[str] = []
        cli._doctor_check_signal_daemon("herdr-bridge", problems)
        assert len(problems) == 1
        assert "pane was rebuilt" in problems[0]
    finally:
        lock.release()


def test_doctor_check_signal_daemon_healthy(tmp_path, capsys, monkeypatch):
    from herdr_bridge.orchestration._state_paths import signal_state_dir
    from herdr_bridge.signal.lock import SingleInstanceLock

    state_dir = signal_state_dir("herdr-bridge")
    state_dir.mkdir(parents=True)
    (state_dir / "pane_id.pin").write_text("w1:p1")

    lock = SingleInstanceLock(state_dir / "daemon.lock")
    lock.acquire()
    try:
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *a, **k: type("R", (), {"stdout": json.dumps({"result": {"panes": [{"pane_id": "w1:p1"}]}})})(),
        )
        problems: list[str] = []
        cli._doctor_check_signal_daemon("herdr-bridge", problems)
        assert problems == []
        assert "✅ Signal daemon is running" in capsys.readouterr().out
    finally:
        lock.release()


# -- shared secret ---------------------------------------------------------

def test_load_or_create_shared_secret_persists_and_is_0600(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "herdr_bridge.orchestration._state_paths.signal_state_dir",
        lambda project_id: tmp_path / project_id,
    )
    secret1 = cli._load_or_create_shared_secret("herdr-bridge")
    secret2 = cli._load_or_create_shared_secret("herdr-bridge")
    assert secret1 == secret2
    path = cli._shared_secret_path("herdr-bridge")
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_shared_secret_file_is_never_briefly_world_readable(tmp_path, monkeypatch):
    """The fix this guards: the old write_text()-then-chmod() sequence left a
    real window where the secret sat at the process's default umask before
    being locked down. Creating at 0600 via O_CREAT|O_EXCL from the first
    byte closes that window -- assert the mode right after the underlying
    os.open() call, before any content is written, not just at the end."""
    monkeypatch.setattr(
        "herdr_bridge.orchestration._state_paths.signal_state_dir",
        lambda project_id: tmp_path / project_id,
    )
    real_open = os.open
    modes_seen_immediately_after_open = []

    def _spying_open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        modes_seen_immediately_after_open.append(os.fstat(fd).st_mode & 0o777)
        return fd

    monkeypatch.setattr(cli.os, "open", _spying_open)
    cli._load_or_create_shared_secret("herdr-bridge")
    assert modes_seen_immediately_after_open == [0o600]


def test_concurrent_first_creation_second_caller_reads_winners_secret(tmp_path, monkeypatch):
    """Two processes racing to create the secret on first use: O_EXCL makes
    the loser read what the winner wrote instead of clobbering it."""
    monkeypatch.setattr(
        "herdr_bridge.orchestration._state_paths.signal_state_dir",
        lambda project_id: tmp_path / project_id,
    )
    path = cli._shared_secret_path("herdr-bridge")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # simulate another process having already won the race
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("winner-secret")

    result = cli._load_or_create_shared_secret("herdr-bridge")
    assert result == "winner-secret"


def test_load_or_create_shared_secret_rejects_a_file_owned_by_another_user(tmp_path, monkeypatch):
    """2026-08-01 DEPLOYMENT CONSTRAINT fix: the whole "shared secret" scheme
    only works because sender and receiver are the same OS user reading the
    same file. If the file is somehow owned by someone else, silently using
    it would produce a confusing "bad hmac" downstream instead of this clear
    diagnostic."""
    monkeypatch.setattr(
        "herdr_bridge.orchestration._state_paths.signal_state_dir",
        lambda project_id: tmp_path / project_id,
    )
    path = cli._shared_secret_path("herdr-bridge")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("someone-elses-secret")

    real_uid = os.getuid()
    monkeypatch.setattr(cli.os, "getuid", lambda: real_uid + 1)  # pretend to be a different user
    with pytest.raises(HerdrBridgeError, match="not this process's uid"):
        cli._load_or_create_shared_secret("herdr-bridge")


# -- real two-process end-to-end: the "shared secret" actually IS shared ---

def test_two_independent_processes_sign_and_verify_with_the_same_disk_secret(tmp_path):
    """2026-08-01 adversarial-review finding (F3): every other test in this
    suite signs and verifies within the SAME process, sharing the secret via
    an in-memory Python variable -- which never actually exercises whether
    two independent processes reading `_load_or_create_shared_secret()` from
    disk get the same bytes. This is the one test that spawns two real,
    separate `python -S -c ...` processes (no shared memory, no shared
    module state) and proves the sign/verify round-trip actually works
    across them. HOME is redirected to tmp_path so this doesn't touch the
    real ~/.local/state/herdr-bridge/signal/ directory."""
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}

    sender_script = (
        "import json\n"
        "from herdr_bridge.light.cli import _load_or_create_shared_secret\n"
        "from herdr_bridge.signal.envelope import Envelope\n"
        "secret = _load_or_create_shared_secret('herdr-bridge')\n"
        "env = Envelope(from_project='sender-tower', to_project='herdr-bridge',\n"
        "               inbox_ref='task-cross-process', kind='task_handoff',\n"
        "               sender_id='sender-tower').signed(secret)\n"
        "print(env.to_json())\n"
    )
    sender_result = subprocess.run(
        [sys.executable, "-c", sender_script],
        capture_output=True, text=True, timeout=30, env=env, check=False,
    )
    assert sender_result.returncode == 0, sender_result.stderr
    envelope_json = sender_result.stdout.strip()

    receiver_script = (
        "import sys\n"
        "from herdr_bridge.light.cli import _load_or_create_shared_secret\n"
        "from herdr_bridge.signal.envelope import Envelope, verify\n"
        "secret = _load_or_create_shared_secret('herdr-bridge')\n"
        f"env = Envelope.from_json({envelope_json!r})\n"
        "verify(env, secret)\n"  # raises SignalEnvelopeError if the secrets don't match
        "print('OK')\n"
    )
    receiver_result = subprocess.run(
        [sys.executable, "-c", receiver_script],
        capture_output=True, text=True, timeout=30, env=env, check=False,
    )
    assert receiver_result.returncode == 0, receiver_result.stderr
    assert receiver_result.stdout.strip() == "OK"


# -- resolve_own_pane_id error path surfaces cleanly ------------------------

def test_signal_start_reports_pane_resolution_failure(monkeypatch, capsys):
    def _raise(*a, **k):
        raise signal_daemon.PaneIdResolutionError("no candidate panes found")

    monkeypatch.setattr(signal_daemon, "resolve_own_pane_id", _raise)
    code = _parse_and_run(["signal", "start", "--project", "herdr-bridge"])
    assert code == 1
    assert "no candidate panes" in capsys.readouterr().err

# -- signal install-watchdog / uninstall-watchdog ---------------------------

def test_signal_install_watchdog_reports_success(monkeypatch, capsys):
    from herdr_bridge.signal import watchdog_service

    seen = {}

    def _fake_install(project, **kwargs):
        seen["project"] = project
        seen["kwargs"] = kwargs
        return Path("/Users/x/Library/LaunchAgents/herdr.signal-healthcheck.herdr-bridge.plist")

    monkeypatch.setattr(watchdog_service, "install_watchdog", _fake_install)

    code = _parse_and_run(["signal", "install-watchdog", "--project", "herdr-bridge"])
    assert code == 0
    assert seen["project"] == "herdr-bridge"
    out = capsys.readouterr().out
    assert "installed" in out
    assert "herdr.signal-healthcheck.herdr-bridge.plist" in out


def test_signal_install_watchdog_honors_custom_interval(monkeypatch):
    from herdr_bridge.signal import watchdog_service

    seen = {}

    def _fake_install(project, **kwargs):
        seen["kwargs"] = kwargs
        return Path("/tmp/x.plist")

    monkeypatch.setattr(watchdog_service, "install_watchdog", _fake_install)

    code = _parse_and_run([
        "signal", "install-watchdog", "--project", "herdr-bridge", "--interval-sec", "60",
    ])
    assert code == 0
    assert seen["kwargs"]["interval_sec"] == 60


def test_signal_install_watchdog_passes_correct_launch_agents_and_log_dirs(monkeypatch):
    """2026-08-02 adversarial-review finding: these two kwargs were never
    asserted, so a wrong value (e.g. accidentally sharing state with another
    project) wouldn't have been caught by any test."""
    from herdr_bridge.orchestration._state_paths import signal_state_dir
    from herdr_bridge.signal import watchdog_service

    seen = {}

    def _fake_install(project, **kwargs):
        seen["kwargs"] = kwargs
        return Path("/tmp/x.plist")

    monkeypatch.setattr(watchdog_service, "install_watchdog", _fake_install)

    code = _parse_and_run(["signal", "install-watchdog", "--project", "herdr-bridge"])
    assert code == 0
    assert seen["kwargs"]["launch_agents_dir"] == Path.home() / "Library" / "LaunchAgents"
    assert seen["kwargs"]["log_dir"] == signal_state_dir("herdr-bridge")


def test_signal_install_watchdog_uses_stable_default_path_not_the_calling_shells_path(monkeypatch):
    """2026-08-02 real-deployment finding: install-watchdog used to capture
    os.environ["PATH"] verbatim -- fine from a normal shell, but baking in
    whatever ephemeral PATH the *installing* process happened to have (e.g. an
    agent session's PATH, full of temp plugin-cache bin dirs) into a
    long-lived scheduled service is the wrong default. It should use a
    stable, minimal baseline unless the caller opts out via --path-env."""
    from herdr_bridge.signal import watchdog_service

    monkeypatch.setenv("PATH", "/some/ephemeral/agent-session/only/bin:/usr/bin")
    seen = {}

    def _fake_install(project, **kwargs):
        seen["kwargs"] = kwargs
        return Path("/tmp/x.plist")

    monkeypatch.setattr(watchdog_service, "install_watchdog", _fake_install)

    code = _parse_and_run(["signal", "install-watchdog", "--project", "herdr-bridge"])
    assert code == 0
    assert "/some/ephemeral/agent-session/only/bin" not in seen["kwargs"]["path_env"]
    assert "/opt/homebrew/bin" in seen["kwargs"]["path_env"]
    assert "/usr/bin" in seen["kwargs"]["path_env"]


def test_signal_install_watchdog_honors_explicit_path_env_override(monkeypatch):
    from herdr_bridge.signal import watchdog_service

    seen = {}

    def _fake_install(project, **kwargs):
        seen["kwargs"] = kwargs
        return Path("/tmp/x.plist")

    monkeypatch.setattr(watchdog_service, "install_watchdog", _fake_install)

    code = _parse_and_run([
        "signal", "install-watchdog", "--project", "herdr-bridge",
        "--path-env", "/custom/bin:/usr/bin",
    ])
    assert code == 0
    assert seen["kwargs"]["path_env"] == "/custom/bin:/usr/bin"


def test_signal_install_watchdog_reports_error_cleanly(monkeypatch, capsys):
    from herdr_bridge.signal import watchdog_service

    def _fake_install(project, **kwargs):
        raise watchdog_service.WatchdogInstallError("herdr-commander not found on PATH")

    monkeypatch.setattr(watchdog_service, "install_watchdog", _fake_install)

    code = _parse_and_run(["signal", "install-watchdog", "--project", "herdr-bridge"])
    assert code == 1
    assert "herdr-commander not found" in capsys.readouterr().err


def test_signal_uninstall_watchdog_reports_removed(monkeypatch, capsys):
    from herdr_bridge.signal import watchdog_service

    monkeypatch.setattr(watchdog_service, "uninstall_watchdog", lambda project, **k: True)

    code = _parse_and_run(["signal", "uninstall-watchdog", "--project", "herdr-bridge"])
    assert code == 0
    assert "removed" in capsys.readouterr().out


def test_signal_uninstall_watchdog_reports_nothing_installed(monkeypatch, capsys):
    from herdr_bridge.signal import watchdog_service

    monkeypatch.setattr(watchdog_service, "uninstall_watchdog", lambda project, **k: False)

    code = _parse_and_run(["signal", "uninstall-watchdog", "--project", "herdr-bridge"])
    assert code == 0
    assert "No Signal healthcheck watchdog was installed" in capsys.readouterr().out


def test_signal_uninstall_watchdog_reports_error_cleanly(monkeypatch, capsys):
    from herdr_bridge.signal import watchdog_service

    def _fake_uninstall(project, **kwargs):
        raise watchdog_service.WatchdogInstallError("signal watchdog install/uninstall only supports macOS")

    monkeypatch.setattr(watchdog_service, "uninstall_watchdog", _fake_uninstall)

    code = _parse_and_run(["signal", "uninstall-watchdog", "--project", "herdr-bridge"])
    assert code == 1
    assert "only supports macOS" in capsys.readouterr().err
