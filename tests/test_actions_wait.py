# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import threading
import time

import pytest

from herdr_bridge.actions import BridgeActions
from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from tests.test_cache import SNAPSHOT


@pytest.fixture()
def wired(fake_herdr, tmp_path):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    return BridgeActions(client, cache, AuditLogger(tmp_path / "a.jsonl")), fake_herdr


def test_wait_succeeds_when_predicate_matches(wired):
    """The current wait_until implementation is event-driven via
    subscribe(pane.agent_status_changed) and no longer calls events.wait (the
    old mechanism removed after the PPLX priority-1 refactor). We use
    fake_herdr.push_event to simulate the event push, which triggers the
    read_agent + predicate check inside _wait_on_event, rather than setting an
    events.wait handler (the current code never calls it, so setting one
    would just be dead code)."""
    acts, srv = wired
    outputs = iter(["building…", "building…", "DONE: all green"])
    srv.set_handler("agent.read",
                    lambda p: {"text": next(outputs, "DONE: all green")})

    def push_events():
        for _ in range(3):
            time.sleep(0.05)
            srv.push_event("pane.agent_status_changed",
                           {"pane_id": "w1:p1", "agent_status": "working"})

    t = threading.Thread(target=push_events, daemon=True)
    t.start()
    r = acts.wait_until("ruler-1", "term_a", lambda o: "DONE" in o.text,
                        timeout_sec=10, poll_interval_sec=0)
    t.join()
    assert r.success is True
    assert r.reason == "predicate"
    assert "DONE" in r.last_output.text


def test_wait_times_out_returns_result_not_exception(wired):
    acts, srv = wired
    srv.set_handler("agent.read", lambda p: {"text": "still working"})
    srv.set_handler("events.wait", lambda p: {"type": "event", "matched": False})
    t0 = time.monotonic()
    r = acts.wait_until("ruler-1", "term_a", lambda o: "NEVER" in o.text,
                        timeout_sec=1, poll_interval_sec=0)
    assert r.success is False
    assert r.reason == "timeout"
    assert r.elapsed_sec >= 1.0
    assert time.monotonic() - t0 < 5.0


def test_wait_agent_gone(wired):
    acts, _ = wired
    r = acts.wait_until("ruler-1", "ghost", lambda o: True, timeout_sec=1,
                        poll_interval_sec=0)
    assert r.success is False
    assert r.reason == "agent_gone"


def test_wait_swallows_predicate_exception(wired):
    acts, srv = wired
    srv.set_handler("agent.read", lambda p: {"text": "x"})
    srv.set_handler("events.wait", lambda p: {"type": "event", "matched": False})

    def bad_predicate(_o):
        raise RuntimeError("governance rule bug")

    r = acts.wait_until("ruler-1", "term_a", bad_predicate, timeout_sec=1,
                        poll_interval_sec=0)
    assert r.success is False
    assert r.reason == "error"
    assert "governance rule bug" in r.error


def test_wait_reconfirms_after_event(wired):
    """After an event arrives, the code must actively re-read to confirm
    (triple confirmation), not just trust a single event.

    The current _wait_on_event always calls read_agent to re-read and
    re-evaluate the predicate on a pane.agent_status_changed event (it never
    just trusts the event's own payload). This test verifies that on the
    first event the predicate hasn't matched yet (reads[0] returns
    "working"), and only the re-read triggered by the second event matches
    (reads[1] returns "DONE") -- pinning down the "must re-read to confirm"
    behavior so a future refactor can't quietly drop it."""
    acts, srv = wired
    reads: list[float] = []

    def read_handler(_p):
        reads.append(time.monotonic())
        return {"text": "DONE" if len(reads) >= 2 else "working"}

    srv.set_handler("agent.read", read_handler)

    def push_events():
        time.sleep(0.05)
        srv.push_event("pane.agent_status_changed",
                       {"pane_id": "w1:p1", "agent_status": "working"})
        time.sleep(0.1)
        srv.push_event("pane.agent_status_changed",
                       {"pane_id": "w1:p1", "agent_status": "working"})

    t = threading.Thread(target=push_events, daemon=True)
    t.start()
    r = acts.wait_until("ruler-1", "term_a", lambda o: "DONE" in o.text,
                        timeout_sec=10, poll_interval_sec=1)
    t.join()
    assert r.success is True
    assert len(reads) >= 2  # at least 1 before the event + 1 re-confirm after


def test_wait_exits_early_when_agent_blocked(fake_herdr, tmp_path):
    """T-1: when an agent enters blocked (waiting for approval/input) ->
    exit early with reason="blocked" instead of dumbly waiting for the
    timeout. A predicate match still takes priority (see the next test)."""
    import copy
    snap = copy.deepcopy(SNAPSHOT)
    for a in snap["snapshot"]["agents"]:
        a["agent_status"] = "blocked"
    for p in snap["snapshot"]["panes"]:
        if p["agent_status"] is not None:
            p["agent_status"] = "blocked"
    fake_herdr.set_handler("session.snapshot", lambda p: snap)
    fake_herdr.set_handler("agent.read", lambda p: {"text": "waiting for approval…"})
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    acts = BridgeActions(client, cache, AuditLogger(tmp_path / "a.jsonl"))

    def push_soon():
        time.sleep(0.1)
        fake_herdr.push_event("pane.agent_status_changed",
                              {"pane_id": "w1:p1", "agent_status": "blocked"})

    t = threading.Thread(target=push_soon, daemon=True)
    t.start()
    t0 = time.monotonic()
    r = acts.wait_until("ruler-1", "term_a", lambda o: "NEVER" in o.text,
                        timeout_sec=30, poll_interval_sec=0)
    t.join()
    assert r.success is False
    assert r.reason == "blocked"
    assert time.monotonic() - t0 < 5.0, "did not exit early on blocked"


def test_wait_predicate_wins_over_blocked(fake_herdr, tmp_path):
    """T-1 priority order: still returns "predicate" when the predicate
    matches, even if the agent is simultaneously blocked."""
    import copy
    snap = copy.deepcopy(SNAPSHOT)
    for a in snap["snapshot"]["agents"]:
        a["agent_status"] = "blocked"
    fake_herdr.set_handler("session.snapshot", lambda p: snap)
    fake_herdr.set_handler("agent.read", lambda p: {"text": "DONE: green"})
    fake_herdr.set_handler("events.wait",
                           lambda p: {"type": "event", "matched": True})
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    acts = BridgeActions(client, cache, AuditLogger(tmp_path / "a.jsonl"))
    r = acts.wait_until("ruler-1", "term_a", lambda o: "DONE" in o.text,
                        timeout_sec=10, poll_interval_sec=0)
    assert r.success is True
    assert r.reason == "predicate"
