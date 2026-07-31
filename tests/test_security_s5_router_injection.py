# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""S-5, same category of risk (router.py version): the side-channel `python -c`
completion-report command template assembled by AcpRouter.prompt() (see
`_build_report_instruction` in router.py) has three interpolation points --
sock / task_id / agent_id -- that can all come from user-controllable input (the
`--project` CLI flag flows into task_id without sanitization; the
`TOWER_REPORT_SOCK` environment variable affects sock).

The fix mirrors commander.py's S-5 fix (#51): all external values are switched to
being passed via shell environment variables (escaped with shlex.quote), and the
`python3 -c` source itself remains a pure static string. Also fixes the hardcoded
`python` (this machine usually has no python shim, only python3, which would cause
the report to never get sent).
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from herdr_bridge.acp import router as router_module
from herdr_bridge.acp.router import AcpRouter, _build_report_instruction

# Malicious payload samples -- these three strings are just "data an attacker might
# stuff into task_id/agent_id/sock," not code this test itself executes. The
# os.system(...) inside the strings is purely test material, used to verify that
# after the fix, this text never gets assembled into executable Python/shell source
# and actually interpreted/run (mirroring the same style used in the existing
# tests/test_security_s5_dispatch_code_injection.py).
MALICIOUS_TASK_ID = "evil'; import os; os.system('touch /tmp/PWNED_S5_ROUTER_TASK'); x='"
MALICIOUS_AGENT_ID = "agent\\'; __import__('os').system('touch /tmp/PWNED_S5_ROUTER_AGENT'); y='"
MALICIOUS_SOCK = "/tmp/fake.sock'; import os; os.system('touch /tmp/PWNED_S5_ROUTER_SOCK'); z='"


@pytest.fixture(autouse=True)
def _isolate_remagraph_env(monkeypatch):
    """Avoids touching real RemaGraph state when constructing AcpRouter(project=...)."""
    monkeypatch.setattr(router_module, "_rg", None)


def test_report_instruction_uses_python3_not_bare_python():
    """The generated command should use python3, not the python that's usually absent on this machine (same category of issue, see #51)."""
    inst = _build_report_instruction("/tmp/tower-reports.sock", "task-1", "agent-1")
    assert "python3 -c" in inst
    # There must be no "standalone" python -c (i.e. not as part of python3 -c)
    assert "\npython -c" not in inst
    assert " python -c" not in inst.replace("python3 -c", "")


def test_report_instruction_does_not_string_interpolate_external_values():
    """A malicious task_id/agent_id/sock should not appear unescaped as-is in the
    command text (which would mean string interpolation is still in use) -- instead
    it should go through environment variables, with the python source reading from
    os.environ."""
    inst = _build_report_instruction(MALICIOUS_SOCK, MALICIOUS_TASK_ID, MALICIOUS_AGENT_ID)

    assert MALICIOUS_TASK_ID not in inst, "task_id should not appear as-is in the command text"
    assert MALICIOUS_AGENT_ID not in inst, "agent_id should not appear as-is in the command text"
    assert MALICIOUS_SOCK not in inst, "the sock path should not appear as-is in the command text"

    assert 'os.environ["TOWER_TASK_ID"]' in inst
    assert 'os.environ["TOWER_AGENT_ID"]' in inst
    assert 'os.environ["TOWER_REPORT_SOCK"]' in inst


def test_report_instruction_malicious_values_do_not_execute_when_actually_run():
    """Ultimate verification: actually feed the assembled command into a shell and
    run it, confirming the malicious payload (all three interpolation points --
    task_id/agent_id/sock) never executes."""
    markers = [
        "/tmp/PWNED_S5_ROUTER_TASK",
        "/tmp/PWNED_S5_ROUTER_AGENT",
        "/tmp/PWNED_S5_ROUTER_SOCK",
    ]
    for m in markers:
        subprocess.run(["rm", "-f", m], check=False)

    inst = _build_report_instruction(MALICIOUS_SOCK, MALICIOUS_TASK_ID, MALICIOUS_AGENT_ID)

    # Extract the "TOWER_REPORT_SOCK=... python3 -c '...'" segment (there's Chinese
    # explanation text before and after the command text, so we can't feed the
    # whole thing to bash as-is)
    start = inst.index("TOWER_REPORT_SOCK=")
    shell_cmd = inst[start:].strip()

    result = subprocess.run(
        ["bash", "-c", shell_cmd],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    for m in markers:
        assert not __import__("os").path.exists(m), f"a malicious payload must not be able to get arbitrary code executed: {m}"
    # should finish executing normally (the connect failure gets swallowed by except, printing a report err), not abort from a syntax error
    assert result.returncode == 0, f"the shell command should exit normally, stderr={result.stderr}"


def test_report_instruction_round_trips_normal_values():
    """Control group: normal values (no special characters) should still round-trip
    correctly through environment variables -- not only malicious values are
    tested."""
    inst = _build_report_instruction("/tmp/tower-reports.sock", "herdr-bridge-task-123", "pane:wT:p18")
    start = inst.index("TOWER_REPORT_SOCK=")
    prefix = inst[start:].split(" python3 -c")[0]

    out = subprocess.run(
        ["bash", "-c", f"{prefix} bash -c 'printf \"%s|%s|%s\" \"$TOWER_REPORT_SOCK\" \"$TOWER_TASK_ID\" \"$TOWER_AGENT_ID\"'"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == "/tmp/tower-reports.sock|herdr-bridge-task-123|pane:wT:p18"


def test_prompt_wires_unsanitized_project_name_into_report_instruction_builder(monkeypatch):
    """Verifies the vulnerability is really reachable, not just theoretical:
    `--project` (AcpRouter(project=...)) flows unsanitized into the task_id
    generated by `_make_valid_task_id()` (see router.py:847-858, which only
    sanitizes the `base` parameter, never `self.project`), and that task_id then
    gets fed into `_build_report_instruction()`. Don't just take the mitigating
    assumption "task_id is always sanitized" at face value -- this directly proves
    that an unsanitized project name really does flow into the command-building
    function.
    """
    malicious_project = MALICIOUS_TASK_ID  # borrows the same malicious sample as the project name
    r = AcpRouter(project=malicious_project)
    r._report_sock_path = "/tmp/fake-router-report-wiring-test.sock"

    captured: dict[str, str] = {}
    original = router_module._build_report_instruction

    def _spy(sock: str, task_id: str, agent_id: str) -> str:
        captured["sock"] = sock
        captured["task_id"] = task_id
        captured["agent_id"] = agent_id
        return original(sock, task_id, agent_id)

    monkeypatch.setattr(router_module, "_build_report_instruction", _spy)

    async def _p():
        return await r.prompt(session_id="s1", prompt=["hello"], target="not-a-registered-agent")

    asyncio.run(_p())

    assert captured, "_build_report_instruction should get called by prompt() (confirms it's actually wired in, not dead code)"
    # _make_valid_task_id() truncates the whole tid to 63 characters, so the full
    # malicious string won't survive intact (that's existing, deliberate behavior,
    # not the vulnerability being fixed here) -- but as long as "unescaped
    # quotes/semicolons" flow into the first 63 characters of task_id, that's enough
    # to prove the project name wasn't sanitized.
    assert "os.system('touch /tmp/PWNED_S5_ROUTER_TASK'" in captured["task_id"], (
        "verifies the mitigating assumption doesn't hold: the project name really "
        "does flow into task_id unsanitized, so the environment-variable-based fix "
        "inside _build_report_instruction is genuinely necessary as the real line of defense"
    )
