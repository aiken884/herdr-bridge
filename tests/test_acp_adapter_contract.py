# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.adapter contract tests (N3): verifies that the acpx -> config -> env
assembly line is testable in CI, without needing a real opencode binary or
model credentials — only the acpx CLI itself (a lightweight npm package) plus
a stdlib-only fake ACP agent (`tests/fixtures/fake_acp_agent.py`).

Relationship to `tests/test_acp_adapter_integration.py`
(`@pytest.mark.integration`): that suite verifies "opencode's own real
behavior" (permissions actually being blocked/allowed), which requires a real
binary + a real model, so it's necessarily skipped in CI. This suite verifies
that "the argv/env we assemble ourselves actually reaches the subprocess",
which has nothing to do with opencode's internal logic — a fake agent lets
this run in CI every time, closing the gap where CI previously couldn't catch
regressions in the argv/env assembly itself.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from herdr_bridge.acp.adapter import build_acpx_argv_and_env, write_session_config
from herdr_bridge.acp.events import parse_stream
from herdr_bridge.acp.models import AcpPolicy

_FAKE_AGENT = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"

requires_acpx = pytest.mark.skipif(
    shutil.which("acpx") is None,
    reason="requires the acpx CLI on PATH (npm i -g acpx@0.12.0)",
)


def _echoed_config_text(stdout: str) -> str:
    """Extract the OPENCODE_CONFIG content reported back by the fake agent
    from the NDJSON stream (already JSON-unescaped, so it isn't tripped up by
    escaped quotes the way a raw string match against stdout would be)."""
    for event in parse_stream(stdout.splitlines()):
        if event.text:
            return event.text
    return ""


@requires_acpx
def test_opencode_config_env_reaches_the_spawned_agent(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    policy = AcpPolicy(mode="deny-all")
    config_path = write_session_config(policy, session_dir=session_dir)
    argv, env = build_acpx_argv_and_env(agent="opencode", agent_binary=_FAKE_AGENT, config_path=config_path, cwd=workdir)
    argv = [*argv, "--ttl", "5"]

    ensure = subprocess.run(
        [*argv, "sessions", "ensure"], cwd=workdir, env=env, capture_output=True, text=True, timeout=15, check=False
    )
    assert ensure.returncode == 0, f"sessions ensure failed: {ensure.stdout}\n{ensure.stderr}"

    result = subprocess.run(
        [*argv, "--format", "json", "--timeout", "10", "hello"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    echoed = _echoed_config_text(result.stdout)
    assert str(config_path) in echoed, (
        f"expected the spawned agent to see OPENCODE_CONFIG={config_path}; got: {echoed!r}"
    )
    assert '"deny"' in echoed and '"task": "ask"' in echoed, (
        f"expected the echoed config content to reflect the deny-all policy; got: {echoed!r}"
    )


@requires_acpx
def test_extra_env_override_attempt_does_not_reach_the_spawned_agent(tmp_path: Path):
    """Regression lock: even if the caller tries to smuggle in a malicious
    OPENCODE_CONFIG_CONTENT via extra_env, the environment the subprocess
    actually receives should still be the sanitized version (end-to-end proof
    of the adversarial-review CRITICAL fix, not just a unit-test-level dict
    assertion)."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    policy = AcpPolicy(mode="approve-all")
    config_path = write_session_config(policy, session_dir=session_dir)
    argv, env = build_acpx_argv_and_env(
        agent="opencode",
        agent_binary=_FAKE_AGENT,
        config_path=config_path,
        cwd=workdir,
        extra_env={"OPENCODE_CONFIG_CONTENT": '{"permission":{"*":"allow"}}'},
    )
    argv = [*argv, "--ttl", "5"]

    assert "OPENCODE_CONFIG_CONTENT" not in env

    ensure = subprocess.run(
        [*argv, "sessions", "ensure"], cwd=workdir, env=env, capture_output=True, text=True, timeout=15, check=False
    )
    assert ensure.returncode == 0, f"sessions ensure failed: {ensure.stdout}\n{ensure.stderr}"

    result = subprocess.run(
        [*argv, "--format", "json", "--timeout", "10", "hello"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    echoed = _echoed_config_text(result.stdout)
    assert str(config_path) in echoed, (
        f"expected the spawned agent to see OPENCODE_CONFIG={config_path}; got: {echoed!r}"
    )
