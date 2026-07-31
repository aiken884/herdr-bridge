# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Supplementary tests for LightCommander -- focused on branches not covered by
the existing tests/test_light_commander.py: connect/send error handling, the
ACP pipeline's router/fallback/exception paths, fleet recycling safety
checks, and event-driven monitoring's start/stop status queries. Everything
is isolated with lightweight Dummy actions + monkeypatch, never touching the
real herdr/ACP daemon/git worktree.
"""

from __future__ import annotations

import subprocess

import pytest

import herdr_bridge.acp.router as router_module
import herdr_bridge.orchestration.memory as gov_memory
from herdr_bridge.light.commander import LightCommander, LightResult
from herdr_bridge.light.report import build_success_report


class _DummyActions:
    """A minimal actions double -- implements only the methods LightCommander
    actually calls."""

    def __init__(self, agents=None, *, read_error=None, send_error=None):
        self._agents = agents if agents is not None else []
        self._read_error = read_error
        self._send_error = send_error

    def list_agents(self, *_a, **_k):
        return self._agents

    def read_agent(self, *_a, **_k):
        if self._read_error:
            raise self._read_error
        raise AssertionError("read_agent should not have been called")

    def send_to_agent(self, *_a, **_k):
        if self._send_error:
            raise self._send_error
        raise AssertionError("send_to_agent should not have been called")


def _agent_stub(agent_id: str = "claude1"):
    from tests.test_light_commander import _agent

    return _agent(agent_id)


# ---------------------------------------------------------------------------
# run_task: connect failure / read failure (swallowed) + send failure
# ---------------------------------------------------------------------------


def test_run_task_connect_error_reports_failure():
    class _Boom:
        def list_agents(self, *_a, **_k):
            raise RuntimeError("connection refused")

    result = LightCommander(_Boom()).run_first_task()
    assert result.ok is False
    assert result.raw_reason == "connect_error"
    assert "Herdr" in result.user_text() or "environment" in result.user_text().lower()


def test_run_task_read_agent_failure_is_swallowed_but_send_failure_reported():
    actions = _DummyActions(
        agents=[_agent_stub()],
        read_error=RuntimeError("read boom"),
        send_error=RuntimeError("send boom"),
    )
    result = LightCommander(actions).run_first_task()
    assert result.ok is False
    assert result.raw_reason == "send_error"
    assert "assistant" in result.user_text().lower()


def test_run_task_safety_valve_failure_does_not_block_dispatch(monkeypatch):
    """Regression test: run_task()'s safety-valve check used to be unguarded
    (unlike the constructor's equivalent check), so a HerdrBridgeError raised by
    _enforce_remagraph_safety_valve would propagate raw instead of being caught
    and logged like every other safety-valve call site in this class."""
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: True)
    monkeypatch.setattr(gov_memory, "_ensure_remagraph_project", lambda *_a, **_k: None)

    def _boom(*_a, **_k):
        raise gov_memory.HerdrMemoryError("safety valve tripped")

    monkeypatch.setattr(gov_memory, "_enforce_remagraph_safety_valve", _boom)

    result = LightCommander(_DummyActions(agents=[_agent_stub()])).run_task(
        "thumbnail-py", dry_run=True
    )
    assert result.ok is True


# ---------------------------------------------------------------------------
# run_task_via_acp: unknown task / the two use_router outcomes / the legacy-path exception branch
# ---------------------------------------------------------------------------


def test_run_task_via_acp_unknown_task():
    result = LightCommander(_DummyActions()).run_task_via_acp("no-such-task")
    assert result.ok is False
    assert result.raw_reason == "unknown_task"


def test_run_task_via_acp_use_router_success(monkeypatch):
    cmd = LightCommander(_DummyActions())
    monkeypatch.setattr(
        cmd, "route_via_acp_router", lambda prompt, **kw: {"ok": True, "routed_to": "grok"}
    )
    result = cmd.run_task_via_acp(use_router=True, agent="grok")
    assert result.ok is True
    assert result.report.status == "success"


def test_run_task_via_acp_use_router_failure(monkeypatch):
    cmd = LightCommander(_DummyActions())
    monkeypatch.setattr(cmd, "route_via_acp_router", lambda prompt, **kw: {"error": "boom"})
    result = cmd.run_task_via_acp(use_router=True)
    assert result.ok is False
    assert result.report.status == "failed"


def test_run_task_via_acp_traditional_path_exception_is_caught(monkeypatch):
    import herdr_bridge.acp.isolated_workdir as iso

    def _boom(*_a, **_k):
        raise RuntimeError("no ACP daemon available in test env")

    monkeypatch.setattr(iso, "create_isolated_worktree_for_opencode", _boom)
    # disable RemaGraph: the store_memory call inside the exception handler uses a
    # fixed project_id="herdr-acp"; if the current environment's REMAGRAPH_STATE_DIR
    # is bound to a different project (e.g. herdr-bridge), it will hit the safety
    # valve and raise yet another exception inside the except block (see the
    # suspected-bug note in this task's report). Here we only want to test
    # run_task_via_acp's own except-branch logic, so we isolate this interaction first.
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: False)

    result = LightCommander(_DummyActions()).run_task_via_acp(use_router=False)
    assert result.ok is False
    assert result.raw_reason == "acp_error"


def test_run_task_via_acp_store_memory_failure_in_except_block_does_not_mask_original_error(monkeypatch):
    """Regression test for the bug flagged (but sidestepped, not verified) by the
    test above: when the ACP path itself fails AND the best-effort store_memory()
    call inside that except block *also* raises, the original ACP failure must
    still be what gets reported -- the secondary store_memory failure must not
    replace/mask it. This exercises the exact interaction the previous test
    disables RemaGraph to avoid.
    """
    import herdr_bridge.acp.isolated_workdir as iso

    def _boom(*_a, **_k):
        raise RuntimeError("no ACP daemon available in test env")

    monkeypatch.setattr(iso, "create_isolated_worktree_for_opencode", _boom)
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: True)

    def _store_memory_boom(*_a, **_k):
        raise RuntimeError("safety valve tripped while recording the ACP failure")

    monkeypatch.setattr(gov_memory, "store_memory", _store_memory_boom)

    result = LightCommander(_DummyActions()).run_task_via_acp(use_router=False)
    assert result.ok is False
    assert result.raw_reason == "acp_error"
    assert "ACP" in result.report.summary


# ---------------------------------------------------------------------------
# dispatch_with_memory_confirm: the non-router fallback path (with/without pane_id)
# ---------------------------------------------------------------------------


def test_dispatch_with_memory_confirm_fallback_records_with_pane(monkeypatch):
    cmd = LightCommander(_DummyActions())
    monkeypatch.setattr(
        cmd,
        "run_task_via_acp",
        lambda *a, **k: LightResult(
            ok=True, report=build_success_report("x", markers_found=(), hints=()), agent_id="acp-x"
        ),
    )
    recorded = []
    monkeypatch.setattr(
        cmd, "record_fleet_member", lambda *a, **k: recorded.append((a, k)) or {"status": "ok"}
    )
    cmd.dispatch_with_memory_confirm("do it", use_router=False, pane_id="pane1", name="worker1")
    assert recorded, "record_fleet_member should be called when pane_id/name are given"


def test_dispatch_with_memory_confirm_fallback_records_legacy_without_pane(monkeypatch):
    cmd = LightCommander(_DummyActions())
    monkeypatch.setattr(
        cmd,
        "run_task_via_acp",
        lambda *a, **k: LightResult(ok=True, report=build_success_report("x", markers_found=(), hints=())),
    )
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: True)
    recorded = []
    monkeypatch.setattr(
        cmd, "record_fleet_member", lambda *a, **k: recorded.append((a, k)) or {"status": "ok"}
    )
    cmd.dispatch_with_memory_confirm("do it", use_router=False)
    assert recorded, "when pane_id/name are absent but RemaGraph is enabled, it should go through legacy-fallback recording"
    _args, kwargs = recorded[0]
    assert kwargs["pane_id"].startswith("legacy-")
    assert kwargs["name"] == "legacy-fallback"


# ---------------------------------------------------------------------------
# batch_dispatch_with_memory: an exception in one item must not affect the others
# ---------------------------------------------------------------------------


def test_batch_dispatch_with_memory_records_per_item_error(monkeypatch):
    cmd = LightCommander(_DummyActions())

    def _raise(*_a, **_k):
        raise RuntimeError("dispatch boom")

    monkeypatch.setattr(cmd, "dispatch_with_memory_confirm", _raise)
    results = cmd.batch_dispatch_with_memory(["p1", "p2"])
    assert len(results) == 2
    assert all("error" in r for r in results)
    assert results[0]["idx"] == 0 and results[1]["idx"] == 1


# ---------------------------------------------------------------------------
# recycle_fleet_member: refused (not dispatched by me) / protected / success / close failed / exception
# ---------------------------------------------------------------------------


def test_recycle_fleet_member_refuses_when_not_recorded_as_mine(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: True)
    monkeypatch.setattr(
        gov_memory,
        "recall_fleet_members",
        lambda **kw: [{"learnings": ["pane_id=totally-different-pane"], "summary": ""}],
    )
    result = LightCommander(_DummyActions()).recycle_fleet_member("wX:target", "task1", "agent1")
    assert result["status"] == "refused"


def test_recycle_fleet_member_protected_last_pane(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: False)
    monkeypatch.setattr(router_module, "is_last_pane_in_first_tab", lambda pane_id: True)
    result = LightCommander(_DummyActions()).recycle_fleet_member("wX:p1", "task1", "agent1")
    assert result["status"] == "protected"


def test_recycle_fleet_member_success_records_recycle(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: True)
    # mock recall_fleet_members directly (without falling through to the real
    # recall_memories), so the "this really was dispatched by me" check comes out
    # True, avoiding interaction with the real RemaGraph project_id safety valve.
    monkeypatch.setattr(
        gov_memory, "recall_fleet_members",
        lambda **kw: [{"learnings": ["pane_id=wX:p1"], "summary": ""}],
    )
    monkeypatch.setattr(router_module, "is_last_pane_in_first_tab", lambda pane_id: False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr=""),
    )
    recorded = []
    monkeypatch.setattr(
        gov_memory, "record_fleet_recycle", lambda *a, **k: recorded.append((a, k))
    )
    result = LightCommander(_DummyActions()).recycle_fleet_member("wX:p1", "task1", "agent1")
    assert result["status"] == "recycled"
    assert result["pane_id"] == "wX:p1"
    assert recorded


def test_recycle_fleet_member_close_failed(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: False)
    monkeypatch.setattr(router_module, "is_last_pane_in_first_tab", lambda pane_id: False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="boom"),
    )
    result = LightCommander(_DummyActions()).recycle_fleet_member("wX:p1", "task1", "agent1")
    assert result["status"] == "close_failed"
    assert "boom" in result["error"]


def test_recycle_fleet_member_subprocess_exception(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: False)
    monkeypatch.setattr(router_module, "is_last_pane_in_first_tab", lambda pane_id: False)

    def _raise(*_a, **_k):
        raise OSError("herdr binary not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    result = LightCommander(_DummyActions()).recycle_fleet_member("wX:p1", "task1", "agent1")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# recycle_completed_fleet: no RemaGraph / skip entries with no pane_id / only recycle completed ones
# ---------------------------------------------------------------------------


def test_recycle_completed_fleet_no_remagraph(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: False)
    results = LightCommander(_DummyActions()).recycle_completed_fleet()
    assert results == [{"status": "memory_disabled"}]


def test_recycle_completed_fleet_default_project_does_not_crash(monkeypatch):
    """Regression test: recycle_completed_fleet()/recycle_fleet_member()/
    record_fleet_member() used to default project="herdr", which the
    memory-layer safety valve unconditionally rejects (project_id in
    ("herdr", "default")) with a raised HerdrMemoryError -- so calling any of
    these with no `project` argument (their own documented, ordinary usage
    pattern) crashed instead of degrading gracefully. Fixed by changing the
    default to "herdr-bridge" (this file's own project-id convention
    everywhere else) and adding a defensive try/except around the safety-valve
    call in each of the three methods. This test exercises the real
    recall_fleet_members()/safety-valve path (not mocked) with the default
    project argument.
    """
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: True)

    # Establish the premise: the old default "herdr" really is rejected by the
    # underlying safety valve.
    with pytest.raises(gov_memory.HerdrMemoryError):
        gov_memory.recall_memories("t", "a", project_id="herdr")

    cmd = LightCommander(_DummyActions())
    results = cmd.recycle_completed_fleet()
    assert isinstance(results, list)
    assert all(r.get("status") != "error" for r in results) or results == []

    record_result = cmd.record_fleet_member("t1", "a1", pane_id="wX:p1", name="n")
    assert record_result.get("status") != "error"

    recycle_result = cmd.recycle_fleet_member("wX:p1", "t1", "a1")
    assert recycle_result.get("status") != "error"

    # Even if a caller explicitly passes the forbidden "herdr" project_id, the
    # defensive try/except must degrade gracefully instead of raising.
    explicit_herdr_result = cmd.record_fleet_member(
        "t1", "a1", pane_id="wX:p1", name="n", project="herdr"
    )
    assert explicit_herdr_result["status"] == "error"


def test_recycle_completed_fleet_recycles_only_completed_members(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: True)
    monkeypatch.setattr(
        gov_memory,
        "recall_fleet_members",
        lambda **kw: [
            # no pane_id -> should be skipped
            {"learnings": ["name=nopane"], "task_id": "t0", "agent_id": "a0"},
            # has pane_id but not yet done -> don't recycle
            {"learnings": ["pane_id=wX:p2", "name=working"], "task_id": "t1", "agent_id": "a1"},
            # has pane_id and is done -> recycle
            {"learnings": ["pane_id=wX:p3", "name=done-one"], "task_id": "t2", "agent_id": "a2"},
        ],
    )

    def _recall_memories(task_id, agent_id, **kw):
        if task_id == "t2":
            return ["handoff: all done"]
        return ["still working"]

    monkeypatch.setattr(gov_memory, "recall_memories", _recall_memories)

    cmd = LightCommander(_DummyActions())
    recycle_calls = []
    monkeypatch.setattr(
        cmd,
        "recycle_fleet_member",
        lambda pane_id, task_id, agent_id, **kw: recycle_calls.append((pane_id, task_id, agent_id))
        or {"status": "recycled"},
    )

    results = cmd.recycle_completed_fleet()
    assert recycle_calls == [("wX:p3", "t2", "a2")]
    assert len(results) == 1
    assert results[0]["pane"] == "wX:p3"


# ---------------------------------------------------------------------------
# event-driven monitoring: start / status query (enabled/disabled) / stop
# ---------------------------------------------------------------------------


def test_start_fleet_permission_monitor_starts_listener(monkeypatch):
    calls = []

    class _FakeRouter:
        def _start_fleet_event_listener(self):
            calls.append(True)

    monkeypatch.setattr(router_module, "create_herdr_router", lambda **kw: _FakeRouter())
    result = LightCommander(_DummyActions()).start_fleet_permission_monitor(project="p1")
    assert result["ok"] is True
    assert calls == [True]


def test_start_fleet_permission_monitor_tolerates_router_without_listener(monkeypatch):
    class _BareRouter:
        pass

    monkeypatch.setattr(router_module, "create_herdr_router", lambda **kw: _BareRouter())
    result = LightCommander(_DummyActions()).start_fleet_permission_monitor(project="p1")
    assert result["ok"] is True


def test_get_fleet_permission_monitor_status_disabled(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: False)
    result = LightCommander(_DummyActions()).get_fleet_permission_monitor_status()
    assert result["ok"] is False


def test_get_fleet_permission_monitor_status_enabled_reports_count(monkeypatch):
    monkeypatch.setattr(gov_memory, "is_remagraph_enabled", lambda: True)
    monkeypatch.setattr(gov_memory, "recall_fleet_members", lambda **kw: [{"a": 1}, {"a": 2}])
    result = LightCommander(_DummyActions()).get_fleet_permission_monitor_status(project="p2")
    assert result["ok"] is True
    assert result["recorded_fleet"] == 2
    assert result["project"] == "p2"


def test_stop_fleet_permission_monitor_returns_ok():
    result = LightCommander(_DummyActions()).stop_fleet_permission_monitor(project="p3")
    assert result["ok"] is True
    assert result["project"] == "p3"
