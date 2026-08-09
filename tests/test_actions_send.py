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
    fake_herdr.set_handler("agent.send", lambda p: {"type": "ok"})
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    return BridgeActions(client, cache, audit), fake_herdr, audit


def test_send_delivers_text(actions):
    acts, srv, _ = actions
    result = acts.send_to_agent("ruler-1", "term_a", "run tests", priority=3)
    assert result.ok is True
    assert result.priority == 3
    req = next(r for r in srv.requests if r["method"] == "agent.send")
    assert req["params"] == {"target": "term_a", "text": "run tests"}


def test_send_audits_actor_and_priority(actions):
    acts, _, audit = actions
    acts.send_to_agent("ruler-9", "term_a", "hello", priority=7)
    entry = json.loads(audit.path.read_text().strip().splitlines()[-1])
    assert entry["actor_id"] == "ruler-9"
    assert entry["action"] == "send_to_agent"
    assert entry["priority"] == 7
    assert entry["agent_id"] == "term_a"


def test_send_unknown_agent_raises(actions):
    acts, _, _ = actions
    with pytest.raises(AgentNotFoundError):
        acts.send_to_agent("ruler-1", "ghost", "x")


def test_send_uses_agent_prompt_and_pane_id_on_protocol_19(actions):
    """2026-08-04 Herdr 0.8.0 finding: protocol 19 removed the agent.send method
    entirely (invalid_request: unknown variant agent.send); agent.prompt is the
    replacement, and it has the same terminal_id-target regression as
    agent.read. See docs/decisions/pplx-agent-read-terminal-id-0.8.0-20260804.md."""
    acts, srv, _ = actions
    acts._protocol = 19
    srv.set_handler("agent.prompt", lambda p: {"type": "ok"})
    result = acts.send_to_agent("ruler-1", "term_a", "run tests")
    assert result.ok is True
    assert not any(r["method"] == "agent.send" for r in srv.requests)
    req = next(r for r in srv.requests if r["method"] == "agent.prompt")
    assert req["params"] == {"target": "w1:p1", "text": "run tests"}


def test_send_uses_agent_send_and_terminal_id_off_protocol_19(actions):
    """Scoped to exactly protocol 19, not >=19."""
    acts, srv, _ = actions
    acts._protocol = 20
    acts.send_to_agent("ruler-1", "term_a", "run tests")
    req = next(r for r in srv.requests if r["method"] == "agent.send")
    assert req["params"] == {"target": "term_a", "text": "run tests"}
