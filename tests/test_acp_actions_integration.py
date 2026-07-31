# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.actions: end-to-end tests of `AcpActions`/`connect()` against the real acpx CLI
+ a fake ACP agent (no real opencode binary or model credentials needed, same
rationale as test_acp_transport.py).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from herdr_bridge.acp.actions import AcpActions, connect
from herdr_bridge.acp.errors import AcpAdapterError
from herdr_bridge.acp.models import AcpPolicy
from herdr_bridge.acp.transport import AcpxTransport
from herdr_bridge.audit import AuditLogger

_FAKE_AGENT = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"

requires_acpx = pytest.mark.skipif(
    shutil.which("acpx") is None,
    reason="requires the acpx CLI on PATH (npm i -g acpx@0.12.0)",
)


def _acp(tmp_path: Path) -> AcpActions:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    transport = AcpxTransport(session_dir=session_dir, agent_resolver=lambda agent: _FAKE_AGENT, ttl_sec=5)
    audit = AuditLogger(tmp_path / "audit.jsonl")
    return AcpActions(transport=transport, audit=audit)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_isolated_workdir(tmp_path: Path) -> Path:
    """A real scratch git repo plus one valid, non-primary linked worktree.
    `AcpActions` now enforces the ADR 0003 workdir isolation check for
    `agent="opencode"` (see `workdir_guard.py`), so a plain empty folder from
    `mkdir()` no longer counts as a valid workdir."""
    repo = tmp_path / "primary-repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.invalid", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("test\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    workdir = tmp_path / "workdir"
    _git("worktree", "add", "-q", "-b", "wt-a", str(workdir), cwd=repo)
    return workdir


@requires_acpx
def test_ensure_prompt_close_round_trip(tmp_path: Path):
    workdir = _make_isolated_workdir(tmp_path)
    acp = _acp(tmp_path)

    acp.ensure_session("gov:main", "opencode", str(workdir), "s1", policy=AcpPolicy(mode="approve-all"))
    result = acp.prompt("gov:main", "s1", "hello", timeout_sec=10)
    acp.close_session("gov:main", "s1")

    assert result.reason == "stop"
    assert result.stop_reason == "end_turn"


@requires_acpx
def test_exec_prompt_is_fully_self_contained(tmp_path: Path):
    workdir = _make_isolated_workdir(tmp_path)
    acp = _acp(tmp_path)

    result = acp.exec_prompt("gov:main", "opencode", "hello", workdir=str(workdir), timeout_sec=10)

    assert result.reason == "stop"
    assert acp._sessions == {}


@requires_acpx
def test_start_prompt_wait_done_round_trip(tmp_path: Path):
    workdir = _make_isolated_workdir(tmp_path)
    acp = _acp(tmp_path)
    acp.ensure_session("gov:main", "opencode", str(workdir), "s1", policy=AcpPolicy(mode="approve-all"))

    handle = acp.start_prompt("gov:main", "s1", "hello")
    result = acp.wait_done("gov:main", handle, timeout_sec=15)
    acp.close_session("gov:main", "s1")

    assert result.reason == "stop"


class TestConnectStrictVersion:
    def test_strict_version_raises_when_manifest_outside_range(self, tmp_path: Path, monkeypatch):
        vendor_dir = tmp_path / "vendor"
        target_dir = vendor_dir / "darwin-arm64"
        target_dir.mkdir(parents=True)
        (target_dir / "opencode").write_text("#!/bin/sh\n")
        (vendor_dir / "MANIFEST.json").write_text(
            '{"target_triple":"darwin-arm64","base_upstream_version":"9.9.9",'
            '"compatible_upstream_range":{"min_inclusive":"1.18.0","max_inclusive":"1.18.99"}}'
        )
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(AcpAdapterError):
            connect(strict_version=True, audit_path=tmp_path / "audit.jsonl")

    def test_non_strict_does_not_raise_for_missing_vendor_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no .vendor/ here at all
        acp = connect(strict_version=False, audit_path=tmp_path / "audit.jsonl", config_path=tmp_path / "sessions")
        assert isinstance(acp, AcpActions)
