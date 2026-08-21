# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import json

import pytest

from herdr_bridge.actions import BridgeActions
from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from herdr_bridge.errors import AgentNotFoundError, AgentPromptRefusedError, HerdrApiError
from herdr_bridge.testing import FakeApiError
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


@pytest.mark.parametrize("protocol", [19, 20])
def test_send_uses_agent_prompt_and_pane_id_on_protocol_ge_19(actions, protocol):
    """0.8.2 kept agent.send absent and terminal_id targeting broken; the
    workaround applies to protocol >= 19."""
    acts, srv, _ = actions
    acts._protocol = protocol
    srv.set_handler("agent.prompt", lambda p: {"type": "ok"})
    result = acts.send_to_agent("ruler-1", "term_a", "run tests")
    assert result.ok is True
    assert result.agent_id == "term_a"
    assert not any(r["method"] == "agent.send" for r in srv.requests)
    req = next(r for r in srv.requests if r["method"] == "agent.prompt")
    assert req["params"] == {"target": "w1:p1", "text": "run tests"}


def test_send_uses_agent_send_and_terminal_id_on_protocol_16(actions):
    """Protocol 16 keeps agent.send + terminal_id."""
    acts, srv, _ = actions
    acts._protocol = 16
    acts.send_to_agent("ruler-1", "term_a", "run tests")
    req = next(r for r in srv.requests if r["method"] == "agent.send")
    assert req["params"] == {"target": "term_a", "text": "run tests"}
    assert not any(r["method"] == "agent.prompt" for r in srv.requests)


@pytest.mark.parametrize("code", ["agent_blocked", "agent_prompt_stalled"])
def test_send_to_agent_refuses_blocked_or_stalled_prompt(actions, code):
    """Herdr 0.8.2 agent.prompt rejects a blocked/stalled agent without
    sending text (#2788). send_to_agent must not return ok=True."""
    acts, srv, _ = actions
    acts._protocol = 20
    srv.set_handler("agent.prompt", lambda p: (_ for _ in ()).throw(
        FakeApiError(code, f"agent refused: {code}")))
    with pytest.raises(AgentPromptRefusedError) as exc:
        acts.send_to_agent("ruler-1", "term_a", "hello")
    assert exc.value.code == code
    assert isinstance(exc.value, HerdrApiError)
    req = next(r for r in srv.requests if r["method"] == "agent.prompt")
    assert req["params"]["target"] == "w1:p1"
    assert req["params"]["text"] == "hello"
    assert not any(r["method"] == "agent.send" for r in srv.requests)


def test_send_to_agent_other_api_errors_are_not_wrapped_as_refused(actions):
    """Only blocked/stalled become AgentPromptRefusedError; other wire errors
    stay generic HerdrApiError so callers can still tell them apart."""
    acts, srv, _ = actions
    acts._protocol = 20
    srv.set_handler("agent.prompt", lambda p: (_ for _ in ()).throw(
        FakeApiError("invalid_request", "unknown variant")))
    with pytest.raises(HerdrApiError) as exc:
        acts.send_to_agent("ruler-1", "term_a", "hello")
    assert type(exc.value) is HerdrApiError
    assert not isinstance(exc.value, AgentPromptRefusedError)
    assert exc.value.code == "invalid_request"
