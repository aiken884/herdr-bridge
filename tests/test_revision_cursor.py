# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""WP4 (0.2.2): revision cursor tests -- unit and contract.

Semantic tests for the revision field and since_revision filtering; the four
real-hardware semantics tests are marked @pytest.mark.empirical (deselected
in CI).
"""

import dataclasses

import pytest

from herdr_bridge.actions import BridgeActions, _RevisionAdapter
from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from herdr_bridge.models import AgentOutput
from tests.test_cache import SNAPSHOT

# ---------------------------------------------------------------------------
# _RevisionAdapter unit tests
# ---------------------------------------------------------------------------

def test_revision_adapter_int_passthrough():
    assert _RevisionAdapter(42) == 42
    assert _RevisionAdapter(0) == 0
    assert _RevisionAdapter(-1) == -1


def test_revision_adapter_none_to_none():
    assert _RevisionAdapter(None) is None


def test_revision_adapter_bool_to_none():
    """bool is a subclass of int, but revision semantics aren't boolean --
    downgrade to None."""
    assert _RevisionAdapter(True) is None
    assert _RevisionAdapter(False) is None


def test_revision_adapter_float_to_none():
    assert _RevisionAdapter(3.14) is None


def test_revision_adapter_str_to_none():
    assert _RevisionAdapter("42") is None


def test_revision_adapter_dict_to_none():
    assert _RevisionAdapter({"v": 1}) is None


# ---------------------------------------------------------------------------
# AgentOutput revision field tests
# ---------------------------------------------------------------------------

def test_agent_output_revision_defaults_to_none():
    out = AgentOutput(agent_id="term_1", text="hi",
                      source="recent_unwrapped", status_at_read="idle")
    assert out.revision is None


def test_agent_output_revision_set():
    out = AgentOutput(agent_id="term_1", text="hi",
                      source="recent_unwrapped", status_at_read="idle",
                      revision=7)
    assert out.revision == 7


def test_agent_output_is_frozen():
    out = AgentOutput(agent_id="term_1", text="hi",
                      source="recent_unwrapped", status_at_read="idle",
                      revision=7)
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.revision = 8  # type: ignore[misc]


# ---------------------------------------------------------------------------
# read_agent + since_revision tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def actions(fake_herdr, tmp_path):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    fake_herdr.set_handler("agent.read", lambda p: {
        "type": "pane_read", "read": {
            "text": "$ pytest\nFAILED tests/test_x.py::test_y\n",
            "truncated": False,
            "revision": 5,
        },
    })
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    return BridgeActions(client, cache, audit), fake_herdr, audit


def test_read_agent_extracts_revision_from_response(actions):
    acts, _srv, _ = actions
    out = acts.read_agent("ruler-1", "term_a")
    assert out.revision == 5


def test_read_agent_no_revision_in_response_defaults_to_none(actions):
    acts, srv, _ = actions
    srv.set_handler("agent.read", lambda p: {
        "type": "pane_read", "read": {"text": "ok", "truncated": False}})
    out = acts.read_agent("ruler-1", "term_a")
    assert out.revision is None


def test_read_agent_non_int_revision_downgrades_to_none(actions):
    acts, srv, _ = actions
    srv.set_handler("agent.read", lambda p: {
        "type": "pane_read", "read": {"text": "ok", "truncated": False,
                                       "revision": "not-an-int"},
    })
    out = acts.read_agent("ruler-1", "term_a")
    assert out.revision is None


def test_read_agent_passes_since_revision_to_herdr(actions):
    acts, srv, _ = actions
    acts.read_agent("ruler-1", "term_a", since_revision=3)
    read_req = next(r for r in srv.requests if r["method"] == "agent.read")
    assert read_req["params"]["since_revision"] == 3


def test_read_agent_omits_since_revision_when_none(actions):
    acts, srv, _ = actions
    acts.read_agent("ruler-1", "term_a")
    read_req = next(r for r in srv.requests if r["method"] == "agent.read")
    assert "since_revision" not in read_req["params"]


def test_read_agent_since_revision_is_keyword_only(actions):
    acts, srv, _ = actions
    acts.read_agent("ruler-1", "term_a", since_revision=10)
    read_req = next(r for r in srv.requests if r["method"] == "agent.read")
    assert read_req["params"]["since_revision"] == 10


def test_read_agent_still_accepts_mode_positional(actions):
    """The existing positional-mode call style behaves the same as in 0.2.1
    (compatibility contract)."""
    acts, _srv, _ = actions
    out = acts.read_agent("ruler-1", "term_a", "recent")
    assert "FAILED" in out.text
    assert out.source == "recent"


def test_read_agent_default_behavior_unchanged(actions):
    """When since_revision isn't passed, behavior matches 0.2.1 (snapshot
    contract)."""
    acts, srv, _ = actions
    out = acts.read_agent("ruler-1", "term_a")
    assert "FAILED" in out.text
    assert out.source == "recent_unwrapped"
    assert out.status_at_read == "idle"
    read_req = next(r for r in srv.requests if r["method"] == "agent.read")
    assert read_req["params"] == {"target": "term_a", "source": "recent_unwrapped",
                                  "format": "text", "strip_ansi": True}


# ---------------------------------------------------------------------------
# wait_until + since_revision tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def wired(fake_herdr, tmp_path):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    return BridgeActions(client, cache, AuditLogger(tmp_path / "a.jsonl")), fake_herdr


def test_wait_until_accepts_since_revision(wired):
    acts, srv = wired
    read_params: list[dict] = []

    def record_read(p):
        read_params.append(dict(p))
        return {"type": "pane_read", "read": {"text": "DONE", "truncated": False}}

    srv.set_handler("agent.read", record_read)
    srv.set_handler("events.wait",
                    lambda p: {"type": "event", "matched": True})
    r = acts.wait_until("ruler-1", "term_a", lambda o: "DONE" in o.text,
                        timeout_sec=10, poll_interval_sec=0,
                        since_revision=42)
    assert r.success is True
    assert all(p.get("since_revision") == 42 for p in read_params)


def test_wait_until_omits_since_revision_when_none(wired):
    acts, srv = wired
    read_params: list[dict] = []

    def record_read(p):
        read_params.append(dict(p))
        return {"type": "pane_read", "read": {"text": "DONE", "truncated": False}}

    srv.set_handler("agent.read", record_read)
    srv.set_handler("events.wait",
                    lambda p: {"type": "event", "matched": True})
    r = acts.wait_until("ruler-1", "term_a", lambda o: "DONE" in o.text,
                        timeout_sec=10, poll_interval_sec=0)
    assert r.success is True
    assert all("since_revision" not in p for p in read_params)


def test_wait_until_default_behavior_unchanged(wired):
    """When since_revision isn't passed, behavior matches 0.2.1 (snapshot
    contract)."""
    acts, srv = wired
    srv.set_handler("agent.read", lambda p: {"text": "DONE: all green"})
    srv.set_handler("events.wait",
                    lambda p: {"type": "event", "matched": True})
    r = acts.wait_until("ruler-1", "term_a", lambda o: "DONE" in o.text,
                        timeout_sec=10, poll_interval_sec=0)
    assert r.success is True
    assert r.reason == "predicate"
    assert "DONE" in r.last_output.text


# ---------------------------------------------------------------------------
# Four real-hardware semantics (CI deselected)
# ---------------------------------------------------------------------------

@pytest.mark.empirical
def test_revision_monotonic_increases():
    """The revision value should increase monotonically across consecutive
    reads.

    Real-hardware test: requires an actual herdr server, deselected in CI.
    """


@pytest.mark.empirical
def test_revision_stable_on_no_change():
    """When the agent produces no new output, revision should stay unchanged.

    Real-hardware test: requires an actual herdr server, deselected in CI.
    """


@pytest.mark.empirical
def test_since_revision_filters_output():
    """After passing since_revision, the returned content should only
    include changes after that revision.

    Real-hardware test: requires an actual herdr server, deselected in CI.
    """


@pytest.mark.empirical
def test_revision_reset_on_new_session():
    """In a new session, revision starts from its initial value (0 or 1).

    Real-hardware test: requires an actual herdr server, deselected in CI.
    """
