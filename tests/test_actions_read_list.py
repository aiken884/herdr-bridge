# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import json

import pytest

from herdr_bridge.actions import BridgeActions
from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from herdr_bridge.errors import AgentNotFoundError
from tests.test_cache import SNAPSHOT


@pytest.fixture()
def actions(fake_herdr, tmp_path):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    fake_herdr.set_handler("agent.read", lambda p: {
        "type": "pane_read", "read": {
            "text": "$ pytest\nFAILED tests/test_x.py::test_y\n", "truncated": False}})
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    return BridgeActions(client, cache, audit), fake_herdr, audit


def test_list_agents_records_actor(actions):
    acts, _srv, audit = actions
    agents = acts.list_agents(actor_id="ruler-1")
    assert [a.agent_id for a in agents] == ["term_a"]
    entry = json.loads(audit.path.read_text().strip().splitlines()[-1])
    assert entry["actor_id"] == "ruler-1"
    assert entry["action"] == "list_agents"


def test_read_agent_default_mode(actions):
    acts, srv, _ = actions
    out = acts.read_agent("ruler-1", "term_a")
    assert "FAILED" in out.text
    assert out.source == "recent_unwrapped"
    assert out.status_at_read == "idle"
    read_req = next(r for r in srv.requests if r["method"] == "agent.read")
    assert read_req["params"] == {"target": "term_a", "source": "recent_unwrapped",
                                 "format": "text", "strip_ansi": True}


def test_read_agent_unknown_mode_raises_value_error(actions):
    acts, _srv, _ = actions
    with pytest.raises(ValueError, match="unknown mode"):
        acts.read_agent("ruler-1", "term_a", mode="not-a-real-mode")


def test_read_agent_raw_ansi_mode(actions):
    acts, srv, _ = actions
    acts.read_agent("ruler-1", "term_a", mode="raw-ansi")
    read_req = [r for r in srv.requests if r["method"] == "agent.read"][-1]
    assert read_req["params"]["format"] == "ansi"
    assert read_req["params"]["strip_ansi"] is False


def test_read_agent_accepts_pane_id(actions):
    acts, _, _ = actions
    out = acts.read_agent("ruler-1", "w1:p1")
    assert out.agent_id == "term_a"


def test_read_unknown_agent_raises(actions):
    acts, _, _ = actions
    with pytest.raises(AgentNotFoundError):
        acts.read_agent("ruler-1", "no-such")


def test_resolve_refreshes_snapshot_on_cache_miss(fake_herdr, tmp_path):
    # B2-4 discriminator: a cache miss must trigger one snapshot refresh before
    # declaring the agent dead -- if it only appears after the snapshot, raising
    # without refreshing first is a regression
    import copy
    empty = copy.deepcopy(SNAPSHOT)
    empty["snapshot"]["agents"] = []
    empty["snapshot"]["panes"] = []
    snap = {"cur": empty}
    fake_herdr.set_handler("session.snapshot", lambda p: snap["cur"])
    fake_herdr.set_handler("agent.read", lambda p: {
        "type": "pane_read", "read": {"text": "hi", "truncated": False}})
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()              # load the empty snapshot first (term_a absent)
    acts = BridgeActions(client, cache, AuditLogger(tmp_path / "a.jsonl"))
    snap["cur"] = SNAPSHOT                # agent shows up -- only visible after a refresh
    out = acts.read_agent("ruler-1", "term_a")
    assert out.agent_id == "term_a"


def test_get_agent_status_reports_current_status(actions):
    # T-2 (0.1.2 additive sixth function): faithfully returns Herdr's current
    # status without interpreting its meaning
    acts, _srv, audit = actions
    assert acts.get_agent_status("ruler-1", "term_a") == "idle"
    entry = json.loads(audit.path.read_text().strip().splitlines()[-1])
    assert entry["action"] == "get_agent_status"
    assert entry["agent_id"] == "term_a"


def test_get_agent_status_unknown_agent_raises(actions):
    acts, _srv, _ = actions
    with pytest.raises(AgentNotFoundError):
        acts.get_agent_status("ruler-1", "ghost")


def test_get_agent_status_is_exported_and_five_frozen_signatures_unchanged():
    # Evidence for the five-function freeze: compare signatures one by one;
    # get_agent_status is an additive addition
    import inspect

    from herdr_bridge import BridgeActions
    sigs = {
        "list_agents": "(self, actor_id: 'str') -> 'list[AgentInfo]'",
        "send_to_agent": "(self, actor_id: 'str', agent_id: 'str', "
                         "text: 'str', priority: 'int' = 0) -> 'SendResult'",
    }
    for name, expected in sigs.items():
        assert str(inspect.signature(getattr(BridgeActions, name))) == expected
    assert str(inspect.signature(BridgeActions.get_agent_status)) == \
        "(self, actor_id: 'str', agent_id: 'str') -> 'AgentStatus'"
