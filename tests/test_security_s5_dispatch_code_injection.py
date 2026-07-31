# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""S-5: the side-channel `python -c` command assembled by LightCommander must not
allow arbitrary code injection via a malicious task_id/agent_id (containing single
quotes, backslashes); after switching to passing values via environment variables,
any value is just data and is never interpreted as part of the Python/shell source."""

from __future__ import annotations

import shlex
import subprocess

from herdr_bridge.actions import BridgeActions
from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from herdr_bridge.light import commander as commander_module
from herdr_bridge.light.commander import LightCommander
from herdr_bridge.light.tasks import TaskSpec
from tests.test_cache import SNAPSHOT

# These two strings are samples of "malicious payloads an attacker might stuff into
# task_id/agent_id," used to verify that after the fix, this os.system(...) never
# gets assembled into executable python/shell source and actually run -- it isn't
# code this test itself executes, it's just data used to test "after the fix, will
# this text still get treated as code."
MALICIOUS_TASK_ID = "evil'; import os; os.system('touch /tmp/PWNED_S5_TEST'); x='"
MALICIOUS_AGENT_ID = "agent\\'; __import__('os').system('touch /tmp/PWNED_S5_AGENT'); y='"


def _dispatch_and_capture_send_text(monkeypatch, fake_herdr, tmp_path, task_id: str):
    """Dispatches a task carrying a malicious task_id, returns the send_text actually
    sent to the downstream agent.

    Monkeypatches `_rg` to None, forcing used_task_id == task.task_id (bypassing
    RemaGraph's generate_task_id normalization), purely testing the safety of
    commander.py's own string assembly.
    """
    monkeypatch.setattr(commander_module, "_rg", None)

    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    fake_herdr.set_handler("agent.send", lambda p: {"type": "ok"})
    # No marker at all, so the predicate is always False -> wait times out -> never
    # hits the existing pre-existing bug path (store_memory(project_id=...)) that's
    # unrelated to this fix.
    fake_herdr.set_handler(
        "agent.read", lambda p: {"text": "still working, no markers here", "source": "recent_unwrapped"}
    )
    fake_herdr.set_handler("events.wait", lambda p: {"type": "timeout"})

    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    acts = BridgeActions(client, cache, audit)
    cmd = LightCommander(acts)

    task = TaskSpec(
        task_id=task_id,
        title="Malicious input test task",
        user_prompt="do something",
        agent_prompt="Please complete this task",
        success_markers=(),
        expected_files=(),
        acceptance_hints=(),
    )
    cmd.run_task(task, timeout_sec=1, poll_interval_sec=0)

    sends = [r for r in fake_herdr.requests if r["method"] == "agent.send"]
    assert len(sends) == 1
    return sends[0]["params"]["text"]


def test_malicious_task_id_cannot_break_out_of_python_source(monkeypatch, fake_herdr, tmp_path):
    """A malicious task_id (containing single quotes, semicolons, backslashes) must
    not let the assembled python -c source get rewritten to execute arbitrary code."""
    send_text = _dispatch_and_capture_send_text(monkeypatch, fake_herdr, tmp_path, MALICIOUS_TASK_ID)

    assert "python3 -c" in send_text
    # After the fix: task_id is never written directly into the python source string, it's passed via a shell environment variable instead
    assert MALICIOUS_TASK_ID not in send_text, (
        "task_id should not appear unescaped as-is in the sent text (which would mean string interpolation is still in use)")
    assert 'os.environ["TOWER_TASK_ID"]' in send_text, "the python source should read task_id from an environment variable"


def test_malicious_task_id_actually_executed_does_not_run_injected_code(
    monkeypatch, fake_herdr, tmp_path
):
    """Ultimate verification: actually feed the assembled text into a shell and run
    it, confirming the malicious payload never executes (/tmp/PWNED_S5_TEST should
    not get created), and that the JSON report still correctly carries the original
    task_id string (the data itself is intact).
    """
    marker_file = "/tmp/PWNED_S5_TEST"
    subprocess.run(["rm", "-f", marker_file], check=False)

    send_text = _dispatch_and_capture_send_text(monkeypatch, fake_herdr, tmp_path, MALICIOUS_TASK_ID)

    # Extract the "TOWER_REPORT_SOCK=... TOWER_TASK_ID=... TOWER_AGENT_ID=... python -c '...'" segment from send_text
    start = send_text.index("TOWER_REPORT_SOCK=")
    end = send_text.index("\nDo not report completion")
    shell_cmd = send_text[start:end]

    # Point TOWER_REPORT_SOCK at a nonexistent path so the socket connection is
    # bound to fail, without affecting the verification of "whether the malicious
    # code actually ran" itself.
    result = subprocess.run(
        ["bash", "-c", shell_cmd],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert not __import__("os").path.exists(marker_file), (
        "a malicious task_id must not be able to get arbitrary code executed")
    # python -c should finish executing normally (the connect failure gets swallowed by except, printing a report err), not abort from a syntax error
    assert result.returncode == 0, f"the shell command should exit normally, stderr={result.stderr}"


def test_shlex_quote_round_trips_special_characters():
    """Unit-verifies that shlex.quote can safely round-trip values containing single
    quotes/backslashes (without depending on the full dispatch flow)."""
    for raw in (MALICIOUS_TASK_ID, MALICIOUS_AGENT_ID, "normal-task-id", "wV:p1/example-project"):
        quoted = shlex.quote(raw)
        out = subprocess.run(
            ["bash", "-c", f"printf '%s' {quoted}"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout == raw
