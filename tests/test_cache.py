# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from tests.conftest import wait_until_true as _wait_until

SNAPSHOT = {
    "type": "session_snapshot",
    "snapshot": {
        "version": "0.7.3", "protocol": 16,
        "focused_workspace_id": "w1", "focused_tab_id": "w1:t1",
        "focused_pane_id": "w1:p1",
        "workspaces": [{"workspace_id": "w1", "label": "main", "focused": True,
                        "active_tab_id": "w1:t1", "number": 1,
                        "tab_count": 1, "pane_count": 2, "agent_status": "idle"}],
        "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "t", "focused": True,
                  "number": 1, "pane_count": 2, "agent_status": "idle"}],
        "panes": [
            {"pane_id": "w1:p1", "terminal_id": "term_a", "workspace_id": "w1",
             "tab_id": "w1:t1", "focused": True, "cwd": "/tmp/a",
             "foreground_cwd": "/tmp/a", "agent": "claude", "agent_status": "idle",
             "agent_session": {"kind": "id", "value": "u-a"}, "revision": 0,
             "scroll": {"offset_from_bottom": 0, "max_offset_from_bottom": 0,
                        "viewport_rows": 50}},
            {"pane_id": "w1:p2", "terminal_id": "term_b", "workspace_id": "w1",
             "tab_id": "w1:t1", "focused": False, "cwd": "/tmp/b",
             "foreground_cwd": "/tmp/b", "agent": None, "agent_status": None,
             "agent_session": None, "revision": 0,
             "scroll": {"offset_from_bottom": 0, "max_offset_from_bottom": 0,
                        "viewport_rows": 50}},
        ],
        "agents": [
            {"terminal_id": "term_a", "agent": "claude", "agent_status": "idle",
             "agent_session": {"kind": "id", "value": "u-a"}, "workspace_id": "w1",
             "tab_id": "w1:t1", "pane_id": "w1:p1", "focused": True,
             "cwd": "/tmp/a", "foreground_cwd": "/tmp/a", "revision": 0},
        ],
        "layouts": [],
    },
}


def _cache(fake_herdr) -> SessionCache:
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.refresh_snapshot()
    return cache


def test_bootstrap_lists_agents(fake_herdr):
    cache = _cache(fake_herdr)
    agents = cache.list_agents()
    assert len(agents) == 1
    a = agents[0]
    assert a.agent_id == "term_a"
    assert a.brand == "claude"
    assert a.status == "idle"
    assert a.pane_id == "w1:p1"


def test_resolve_by_terminal_or_pane(fake_herdr):
    cache = _cache(fake_herdr)
    assert cache.resolve("term_a").pane_id == "w1:p1"
    assert cache.resolve("w1:p1").agent_id == "term_a"
    assert cache.resolve("nope") is None


def test_pane_without_agent_not_listed_but_pane_known(fake_herdr):
    cache = _cache(fake_herdr)
    assert set(cache.pane_ids()) == {"w1:p1", "w1:p2"}
    assert all(a.agent_id != "term_b" for a in cache.list_agents())


def test_unknown_status_maps_to_unknown(fake_herdr):
    snap = {"type": "session_snapshot", "snapshot": dict(SNAPSHOT["snapshot"])}
    snap["snapshot"]["agents"] = [dict(SNAPSHOT["snapshot"]["agents"][0],
                                       agent_status="weird_new_state")]
    fake_herdr.set_handler("session.snapshot", lambda p: snap)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.refresh_snapshot()
    assert cache.list_agents()[0].status == "unknown"


def test_start_subscribes_global_and_per_pane(fake_herdr):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    sub_req = next(r for r in fake_herdr.requests
                   if r["method"] == "events.subscribe")
    types = [s["type"] for s in sub_req["params"]["subscriptions"]]
    assert "pane.created" in types
    per_pane = [s for s in sub_req["params"]["subscriptions"]
                if s["type"] == "pane.agent_status_changed"]
    assert {s["pane_id"] for s in per_pane} == {"w1:p1", "w1:p2"}
    cache.stop()


def test_status_change_event_updates_agent(fake_herdr):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    fake_herdr.push_event("pane_agent_status_changed",
                          {"pane_id": "w1:p1", "agent_status": "working"})
    assert _wait_until(
        lambda: cache.resolve("term_a") and cache.resolve("term_a").status == "working")
    cache.stop()


def test_pane_created_triggers_resubscribe(fake_herdr):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    new_pane = {"pane_id": "w1:p3", "terminal_id": "term_c", "workspace_id": "w1",
                "tab_id": "w1:t1", "focused": False, "cwd": "/tmp/c",
                "foreground_cwd": "/tmp/c", "agent": "claude",
                "agent_status": "working",
                "agent_session": None, "revision": 0,
                "scroll": {"offset_from_bottom": 0, "max_offset_from_bottom": 0,
                           "viewport_rows": 50}}
    fake_herdr.push_event("pane_created", {"pane": new_pane})
    # A new pane triggers immediate resubscribe (no debounce wait): the last
    # subscribe request should include w1:p3.
    def resubscribed() -> bool:
        subs = [r for r in fake_herdr.requests if r["method"] == "events.subscribe"]
        if len(subs) < 2:
            return False
        latest = subs[-1]["params"]["subscriptions"]
        return any(s.get("pane_id") == "w1:p3" for s in latest)
    assert _wait_until(resubscribed, timeout=2.0)
    cache.stop()


def test_rebuild_order_subscribe_before_snapshot(fake_herdr):
    """Rebuild ordering rule: the new subscribe request must reach the server
    before the (rebuild-triggered) snapshot request."""
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    base = len(fake_herdr.requests)
    new_pane = {"pane_id": "w1:p4", "terminal_id": "term_d", "workspace_id": "w1",
                "tab_id": "w1:t1", "focused": False, "cwd": "/tmp/d",
                "foreground_cwd": "/tmp/d", "agent": "claude",
                "agent_status": "idle", "agent_session": None, "revision": 0,
                "scroll": {"offset_from_bottom": 0, "max_offset_from_bottom": 0,
                           "viewport_rows": 50}}
    fake_herdr.push_event("pane_created", {"pane": new_pane})
    assert _wait_until(lambda: any(
        r["method"] == "session.snapshot" for r in fake_herdr.requests[base:]))
    tail = [r["method"] for r in fake_herdr.requests[base:]]
    assert tail.index("events.subscribe") < tail.index("session.snapshot")
    cache.stop()


def test_pane_closed_removes_pane_and_agent(fake_herdr):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: cache.resolve("term_a") is not None)
    assert "w1:p1" in cache.pane_ids()
    fake_herdr.push_event("pane_closed", {"pane_id": "w1:p1"})
    assert _wait_until(lambda: "w1:p1" not in cache.pane_ids())
    assert cache.resolve("term_a") is None
    # w1:p2 (a pane with no agent) should still exist
    assert "w1:p2" in cache.pane_ids()
    cache.stop()


def test_pane_exited_sets_agent_unknown(fake_herdr):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: cache.resolve("term_a") is not None)
    assert cache.resolve("term_a").status == "idle"
    fake_herdr.push_event("pane_exited", {"pane_id": "w1:p1"})
    assert _wait_until(lambda: cache.resolve("term_a").status == "unknown")
    # Other fields should stay unchanged
    a = cache.resolve("term_a")
    assert a.agent_id == "term_a"
    assert a.brand == "claude"
    assert a.pane_id == "w1:p1"
    cache.stop()


def test_pane_focused_updates_focus_info(fake_herdr):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: cache.resolve("term_a") is not None)
    # Initial focus comes from the snapshot: w1:p1
    assert cache.focused_pane_id() == "w1:p1"
    fake_herdr.push_event("pane_focused",
                          {"pane_id": "w1:p2", "workspace_id": "w1"})
    # Focus should have switched to the target pane
    assert _wait_until(lambda: cache.focused_pane_id() == "w1:p2")
    cache.stop()


def test_pane_agent_detected_creates_agent_from_pane(fake_herdr):
    """An unresolved pane receiving agent_detected should create an agent from
    the pane data."""
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: cache.resolve("term_a") is not None)
    # w1:p2 is in _panes but not in _pane_to_terminal (no agent)
    assert cache.resolve("w1:p2") is None
    fake_herdr.push_event("pane_agent_detected", {
        "pane_id": "w1:p2",
        "pane": {"terminal_id": "term_b", "agent": "codex"},
        "agent_status": "working",
    })
    assert _wait_until(lambda: cache.resolve("term_b") is not None)
    b = cache.resolve("term_b")
    assert b.agent_id == "term_b"
    assert b.brand == "codex"
    assert b.status == "working"
    assert b.pane_id == "w1:p2"
    # Should also be resolvable by pane_id
    assert cache.resolve("w1:p2").agent_id == "term_b"
    cache.stop()


def test_subscription_dead_triggers_rebuild_in_consistency(fake_herdr):
    """The consistency check finding a dead subscription (_sub is None or
    closed) should trigger a rebuild."""
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path),
                         consistency_interval_sec=0.2)
    cache.start()
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    # Record the subscribe request count before the rebuild
    base_sub_count = sum(1 for r in fake_herdr.requests
                         if r["method"] == "events.subscribe")
    # Manually close the subscription to simulate it dying
    with cache._rebuild_lock:
        if cache._sub is not None:
            cache._sub.close()
            cache._sub = None
    # The consistency check should notice _sub is None and trigger an immediate rebuild
    assert _wait_until(
        lambda: sum(1 for r in fake_herdr.requests
                    if r["method"] == "events.subscribe") > base_sub_count,
        timeout=3.0)
    cache.stop()


def test_consistency_pane_count_change_triggers_rebuild(fake_herdr):
    """A pane count change triggers an immediate rebuild during the
    consistency check."""
    snap_with_extra = {"type": "session_snapshot",
                       "snapshot": dict(SNAPSHOT["snapshot"])}
    extra_pane = {"pane_id": "w1:p3", "terminal_id": "term_c",
                  "workspace_id": "w1", "tab_id": "w1:t1", "focused": False,
                  "cwd": "/tmp/c", "foreground_cwd": "/tmp/c",
                  "agent": "claude", "agent_status": "idle",
                  "agent_session": None, "revision": 0,
                  "scroll": {"offset_from_bottom": 0,
                             "max_offset_from_bottom": 0,
                             "viewport_rows": 50}}
    snap_with_extra["snapshot"]["panes"] = \
        list(SNAPSHOT["snapshot"]["panes"]) + [extra_pane]
    snap_with_extra["snapshot"]["agents"] = \
        list(SNAPSHOT["snapshot"]["agents"]) + [{
            "terminal_id": "term_c", "agent": "claude",
            "agent_status": "idle", "agent_session": None,
            "workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p3",
            "focused": False, "cwd": "/tmp/c", "foreground_cwd": "/tmp/c",
            "revision": 0}]

    use_extra = {"value": False}
    def handler(_p):
        return snap_with_extra if use_extra["value"] else SNAPSHOT
    fake_herdr.set_handler("session.snapshot", handler)
    cache = SessionCache(SocketClient(fake_herdr.socket_path),
                         consistency_interval_sec=0.2)
    cache.start()
    assert _wait_until(lambda: cache.resolve("term_a") is not None)
    base_sub_count = sum(1 for r in fake_herdr.requests
                         if r["method"] == "events.subscribe")
    use_extra["value"] = True
    assert _wait_until(
        lambda: sum(1 for r in fake_herdr.requests
                    if r["method"] == "events.subscribe") > base_sub_count,
        timeout=3.0)
    assert cache.drift_count >= 1
    cache.stop()


def test_rebuild_wait_connected_timeout_keeps_old_sub(fake_herdr, monkeypatch):
    """wait_connected timeout: close the new subscription, keep the old one,
    and log a warning."""
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    cache = SessionCache(SocketClient(fake_herdr.socket_path))
    cache.start()
    assert _wait_until(lambda: cache._sub is not None)
    old_sub = cache._sub
    # monkeypatch: make the new subscription's wait_connected always return False

    class NeverConnects:
        def wait_connected(self, timeout):
            return False
        def close(self):
            pass

    def fake_subscribe(*args, **kwargs):
        return NeverConnects()

    monkeypatch.setattr(cache._client, "subscribe", fake_subscribe)
    # Manually trigger a rebuild (via _request_rebuild(immediate=True))
    cache._request_rebuild(immediate=True)
    import time
    time.sleep(0.3)  # wait for the Timer to run
    # The old subscription should be kept, not replaced
    assert cache._sub is old_sub
    cache.stop()


def test_consistency_check_detects_and_heals_drift(fake_herdr):
    state = {"snap": SNAPSHOT}
    fake_herdr.set_handler("session.snapshot", lambda p: state["snap"])
    cache = SessionCache(SocketClient(fake_herdr.socket_path),
                         consistency_interval_sec=0.2)
    cache.start()
    assert _wait_until(lambda: cache.resolve("term_a") is not None)
    # The server-side state quietly changes (simulating a missed event that causes cache drift)
    drifted = {"type": "session_snapshot", "snapshot": dict(SNAPSHOT["snapshot"])}
    drifted["snapshot"]["agents"] = [dict(SNAPSHOT["snapshot"]["agents"][0],
                                          agent_status="blocked")]
    state["snap"] = drifted
    assert _wait_until(
        lambda: cache.resolve("term_a").status == "blocked", timeout=3.0)
    assert cache.drift_count >= 1
    cache.stop()
