# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import herdr_bridge.light.commander as commander_module
from herdr_bridge.actions import BridgeActions
from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from herdr_bridge.light.commander import LightCommander, _pick_coder
from herdr_bridge.models import AgentInfo
from tests.test_cache import SNAPSHOT


def _agent(agent_id: str, brand: str = "claude") -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        brand=brand,
        status="idle",
        pane_id=f"pane_{agent_id}",
        workspace_id="w1",
        tab_id="t1",
        cwd="/tmp",
        session_ref=None,
        focused=False,
    )


def test_pick_coder_prefers_claude_over_bash():
    agents = [
        _agent("term_bash", brand="bash"),
        _agent("term_claude", brand="claude"),
    ]
    # brand bash isn't real but name scoring uses agent_id+brand
    picked = _pick_coder(agents)
    assert picked is not None
    assert "claude" in picked.agent_id or picked.brand == "claude"


def test_pick_coder_empty():
    assert _pick_coder([]) is None


@pytest.fixture()
def light_actions(fake_herdr, tmp_path):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    fake_herdr.set_handler("agent.send", lambda p: {"type": "ok"})
    fake_herdr.set_handler(
        "agent.read",
        lambda p: {
            "text": "working...\ncreated thumbnail.py and test_thumbnail.py\nwrote def make_thumbnail and import\n[[[THUMBNAIL_COMPLETE_20260722]]]\n",
            "source": "recent_unwrapped",
        },
    )
    fake_herdr.set_handler(
        "events.wait",
        lambda p: {"type": "timeout"},
    )
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    return BridgeActions(client, cache, audit), fake_herdr


def test_run_first_task_success(light_actions):
    acts, srv = light_actions
    cmd = LightCommander(acts)
    result = cmd.run_first_task(timeout_sec=5, poll_interval_sec=1)
    assert result.ok is True
    assert result.agent_id == "term_a"
    assert "Completed" in result.user_text()
    assert result.report.status == "success"
    # confirm the dispatch actually happened
    sends = [r for r in srv.requests if r["method"] == "agent.send"]
    assert len(sends) >= 1
    assert "thumbnail" in sends[0]["params"]["text"].lower() or "THUMBNAIL_COMPLETE" in sends[0]["params"]["text"]


def test_run_dry_run_no_send(light_actions):
    acts, srv = light_actions
    before = len([r for r in srv.requests if r["method"] == "agent.send"])
    cmd = LightCommander(acts)
    result = cmd.run_first_task(dry_run=True)
    assert result.ok is True
    assert result.raw_reason == "dry_run"
    assert "Preview" in result.user_text()
    after = len([r for r in srv.requests if r["method"] == "agent.send"])
    assert after == before


def test_run_no_agent(fake_herdr, tmp_path):
    empty = {
        "type": "session_snapshot",
        "snapshot": {
            **SNAPSHOT["snapshot"],
            "agents": [],
            "panes": [],
        },
    }
    fake_herdr.set_handler("session.snapshot", lambda p: empty)
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    acts = BridgeActions(client, cache, audit)
    result = LightCommander(acts).run_first_task()
    assert result.ok is False
    assert result.raw_reason == "no_agent"
    assert "assistant" in result.user_text().lower()


def test_run_timeout(fake_herdr, tmp_path):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    fake_herdr.set_handler("agent.send", lambda p: {"type": "ok"})
    fake_herdr.set_handler(
        "agent.read",
        lambda p: {"text": "still working...", "source": "recent_unwrapped"},
    )
    fake_herdr.set_handler("events.wait", lambda p: {"type": "timeout"})
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    acts = BridgeActions(client, cache, audit)
    result = LightCommander(acts).run_first_task(timeout_sec=1, poll_interval_sec=1)
    assert result.ok is False
    assert result.raw_reason == "timeout"
    assert "Timed out" in result.user_text() or "time limit" in result.user_text()


def test_unknown_task():
    # no real actions needed -- run_task fails while resolving the task
    class _Dummy:
        def list_agents(self, *_a, **_k):
            return []

    result = LightCommander(_Dummy()).run_task("nope")  # type: ignore[arg-type]
    assert result.ok is False
    assert result.raw_reason == "unknown_task"


# ---------------------------------------------------------------------------
# #58 supplementary test: commander.py previously had a test file yet almost
# 0% coverage (in practice it was ~49% when the test ran standalone; the
# discrepancy came from the full test-suite execution environment, see the
# same-round commit notes for details). Regardless, the point here isn't to
# chase a coverage number -- it's to make sure a bug like #51 (the
# side-channel command hardcoded `python`, breaking the third communication
# layer entirely, with no test ever catching it) can be caught next time.
# ---------------------------------------------------------------------------


def test_run_task_side_channel_instructions_use_python3_not_python(light_actions):
    """#51 regression test: the side-channel report command must be
    `python3 -c`, not `python -c` -- this machine only has python3, so if the
    agent runs `python -c` it gets a command-not-found and the third
    communication layer is entirely broken. Nobody knew how long this bug had
    existed, because this path was never exercised by a single test line
    (even though commander.py overall had a test file). This asserts directly
    on the actual generated command string content, not on mock call counts
    or exit codes.
    """
    acts, srv = light_actions
    cmd = LightCommander(acts)
    cmd.run_first_task(timeout_sec=5, poll_interval_sec=1)

    sends = [r for r in srv.requests if r["method"] == "agent.send"]
    assert sends, "there should have been a dispatch"
    send_text = sends[0]["params"]["text"]

    assert "python3 -c" in send_text, "the side-channel report command must be python3 -c"
    assert "python -c" not in send_text, "the bare python -c from before the #51 fix must not reappear"

    # external values (task_id/agent_id) go through environment variables, not
    # string interpolation (the security design fixed in #51/#56; see
    # tests/test_security_s5_dispatch_code_injection.py for full injection-safety verification).
    assert 'os.environ["TOWER_TASK_ID"]' in send_text
    assert 'os.environ["TOWER_AGENT_ID"]' in send_text


def test_run_task_connect_error_when_list_agents_raises():
    """run_task's connect_error error path -- when list_agents raises, it must
    explicitly report a connection failure, not let the exception propagate
    (the caller is a user-facing interface, it shouldn't see a traceback)."""
    class _FailingActions:
        def list_agents(self, *_a, **_k):
            raise RuntimeError("boom-connect")

    result = LightCommander(_FailingActions()).run_task("thumbnail-py")  # type: ignore[arg-type]

    assert result.ok is False
    assert result.raw_reason == "connect_error"
    assert "Herdr" in result.user_text()


def test_run_task_send_error_when_send_to_agent_raises(light_actions):
    """run_task's send_error error path -- when send_to_agent raises, it must
    explicitly report a dispatch failure, not let the exception propagate or
    silently treat it as success."""
    acts, _srv = light_actions

    class _BrokenSend:
        def __getattr__(self, name):
            return getattr(acts, name)

        def send_to_agent(self, *_a, **_k):
            raise RuntimeError("boom-send")

    cmd = LightCommander(_BrokenSend())  # type: ignore[arg-type]
    result = cmd.run_first_task(timeout_sec=5, poll_interval_sec=1)

    assert result.ok is False
    assert result.raw_reason == "send_error"


def test_record_fleet_member_delegates_to_rg_when_enabled(monkeypatch, light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)
    calls = {}
    fake_rg = SimpleNamespace(
        is_remagraph_enabled=lambda: True,
        record_fleet_member=lambda task_id, agent_id, **kw: (
            calls.update(task_id=task_id, agent_id=agent_id, **kw) or {"status": "ok"}
        ),
    )
    monkeypatch.setattr(commander_module, "_rg", fake_rg)

    result = cmd.record_fleet_member("tid", "aid", pane_id="wT:p1", name="grok", project="herdr-test")

    assert result == {"status": "ok"}
    assert calls["pane_id"] == "wT:p1"
    assert calls["project_id"] == "herdr-test"


def test_record_fleet_member_returns_no_remagraph_status_when_disabled(light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)
    # using the real _rg but simulating a "not enabled" equivalent state would be
    # fragile; take the most direct route instead: the branch for
    # is_remagraph_enabled() returning False is already covered by the
    # `not (_rg and ...)` check, so here we instead test the case where _rg itself
    # is None (e.g. the fallback when the governance module fails to load).
    import herdr_bridge.light.commander as _cm

    original = _cm._rg
    _cm._rg = None
    try:
        result = cmd.record_fleet_member("tid", "aid", pane_id="wT:p1", name="grok")
    finally:
        _cm._rg = original

    assert result == {"status": "memory_disabled"}


def test_recycle_fleet_member_refuses_when_not_recorded_as_mine(monkeypatch, light_actions):
    """Safety boundary: only recycle panes that have a fleet_member_dispatched
    record in our own RemaGraph, never touch a pane we didn't dispatch --
    this verifies that "no record found" results in an explicit refusal, not
    a silent skip or an accidental deletion.
    """
    acts, _srv = light_actions
    cmd = LightCommander(acts)
    fake_rg = SimpleNamespace(
        is_remagraph_enabled=lambda: True,
        recall_fleet_members=lambda **kw: [{"learnings": ["something else"], "summary": "unrelated"}],
    )
    monkeypatch.setattr(commander_module, "_rg", fake_rg)

    result = cmd.recycle_fleet_member("wT:p99", "tid", "aid")

    assert result["status"] == "refused"
    assert "not my dispatched fleet member" in result["reason"]


def test_recycle_fleet_member_protects_last_pane_in_first_tab(monkeypatch, light_actions):
    """The same #45-style protection: even a pane we dispatched ourselves must
    not be the only remaining pane in the workspace's first tab -- closing it
    would make the entire project disappear from the Space. This verifies the
    recycle_fleet_member path (it shares the same is_last_pane_in_first_tab()
    check with fault_inject.py's cleanup_*, but this is a completely
    independent call site -- missing either one would repeat the hard-won lesson).
    """
    acts, _srv = light_actions
    cmd = LightCommander(acts)
    fake_rg = SimpleNamespace(
        is_remagraph_enabled=lambda: True,
        recall_fleet_members=lambda **kw: [{"learnings": ["pane_id=wT:p1"], "summary": ""}],
    )
    monkeypatch.setattr(commander_module, "_rg", fake_rg)
    monkeypatch.setattr("herdr_bridge.acp.router.is_last_pane_in_first_tab", lambda pane_id: True)

    result = cmd.recycle_fleet_member("wT:p1", "tid", "aid")

    assert result["status"] == "protected"
    assert "last pane" in result["reason"]


def test_recycle_fleet_member_closes_pane_when_safe(monkeypatch, light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)
    recycle_calls = []
    fake_rg = SimpleNamespace(
        is_remagraph_enabled=lambda: True,
        recall_fleet_members=lambda **kw: [{"learnings": ["pane_id=wT:p1"], "summary": ""}],
        record_fleet_recycle=lambda *a, **k: recycle_calls.append((a, k)),
    )
    monkeypatch.setattr(commander_module, "_rg", fake_rg)
    monkeypatch.setattr("herdr_bridge.acp.router.is_last_pane_in_first_tab", lambda pane_id: False)

    close_calls = []

    def fake_run(argv, **kw):
        close_calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = cmd.recycle_fleet_member("wT:p1", "tid", "aid", reason="done")

    assert result == {"status": "recycled", "pane_id": "wT:p1", "reason": "done"}
    assert close_calls == [["herdr", "pane", "close", "wT:p1"]]
    assert recycle_calls, "should record the recycle after a successful close"


def test_dispatch_with_memory_confirm_uses_router_by_default(monkeypatch, light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)
    captured = {}

    class _FakeRouter:
        def dispatch_with_memory_confirm(self, prompt, *, target=None, pane_id=None, name=None):
            captured.update(prompt=prompt, target=target, pane_id=pane_id, name=name)
            return {"ok": True, "confirmed_via": "pong"}

    monkeypatch.setattr("herdr_bridge.acp.router.create_herdr_router", lambda project: _FakeRouter())

    result = cmd.dispatch_with_memory_confirm("hello", target_agent="grok", pane_id="wT:p1", name="grok")

    assert result == {"ok": True, "confirmed_via": "pong"}
    assert captured == {"prompt": "hello", "target": "grok", "pane_id": "wT:p1", "name": "grok"}


def test_dispatch_with_memory_confirm_fallback_records_fleet_member_when_pane_given(
    monkeypatch, light_actions
):
    """The legacy fallback path for use_router=False: when both pane_id/name
    are given, record_fleet_member must be called explicitly -- this is the
    precondition for "the tower being responsible for its own recycling"; if
    this step is missed, recycle_fleet_member will forever refuse to recycle
    because it finds no record."""
    acts, _srv = light_actions
    cmd = LightCommander(acts)

    class _FakeAcpResult:
        task_id = "tid-123"
        agent_id = "aid-456"

    monkeypatch.setattr(cmd, "run_task_via_acp", lambda prompt, use_router=False: _FakeAcpResult())

    recorded = {}
    monkeypatch.setattr(
        cmd,
        "record_fleet_member",
        lambda tid, aid, *, pane_id, name, project: recorded.update(
            tid=tid, aid=aid, pane_id=pane_id, name=name, project=project
        ),
    )

    cmd.dispatch_with_memory_confirm("hi", use_router=False, pane_id="wT:p2", name="codex")

    assert recorded == {
        "tid": "tid-123", "aid": "aid-456", "pane_id": "wT:p2", "name": "codex", "project": "herdr-router",
    }


def test_route_via_acp_router_registers_dynamic_fallback_agent(monkeypatch, light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)

    class _FakeRouter:
        def __init__(self):
            self.registered_agents: dict = {}
            self.register_calls = []

        def register_agent(self, name, command, args, **meta):
            self.register_calls.append((name, command, args, meta))
            self.registered_agents[name] = {}

        def dispatch_with_memory_confirm(self, prompt, *, target=None):
            return {"ok": True, "routed_to": target}

    fake_router = _FakeRouter()
    monkeypatch.setattr("herdr_bridge.acp.router.create_herdr_router", lambda project: fake_router)

    result = cmd.route_via_acp_router("do stuff", target_agent="unknown-agent")

    assert result == {"ok": True, "routed_to": "unknown-agent"}
    assert fake_router.register_calls, "should dynamically register a fallback when target isn't in registered_agents"
    assert fake_router.register_calls[0][0] == "unknown-agent"


def test_start_fleet_permission_monitor_calls_router_listener(monkeypatch, light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)

    class _FakeRouter:
        def __init__(self):
            self.started = False

        def _start_fleet_event_listener(self):
            self.started = True

    fake_router = _FakeRouter()
    monkeypatch.setattr("herdr_bridge.acp.router.create_herdr_router", lambda project: fake_router)

    result = cmd.start_fleet_permission_monitor(project="herdr-test")

    assert result["ok"] is True
    assert fake_router.started is True


def test_batch_dispatch_with_memory_collects_results_and_errors(monkeypatch, light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)

    def fake_dispatch(prompt, *, project, target_agent, use_router):
        if "boom" in prompt:
            raise RuntimeError("dispatch failed")
        return {"ok": True, "prompt": prompt}

    monkeypatch.setattr(cmd, "dispatch_with_memory_confirm", fake_dispatch)

    results = cmd.batch_dispatch_with_memory(["task one", "boom task"])

    assert results[0]["idx"] == 0
    assert results[0]["result"] == {"ok": True, "prompt": "task one"}
    assert results[1]["idx"] == 1
    assert "error" in results[1]


def test_stop_fleet_permission_monitor_returns_ok_dict(light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)

    result = cmd.stop_fleet_permission_monitor(project="herdr-test")

    assert result["ok"] is True
    assert result["project"] == "herdr-test"


def test_get_fleet_permission_monitor_status_when_remagraph_disabled(light_actions):
    acts, _srv = light_actions
    cmd = LightCommander(acts)

    original = commander_module._rg
    commander_module._rg = None
    try:
        result = cmd.get_fleet_permission_monitor_status()
    finally:
        commander_module._rg = original

    assert result == {"ok": False, "msg": "Herdr Bridge Memory not enabled"}
