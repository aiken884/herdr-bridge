# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import time

from herdr_bridge.client import SocketClient, Subscription
from tests.conftest import wait_until_true as _wait_until


def test_subscribe_receives_events(fake_herdr):
    got: list[tuple[str, dict]] = []
    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe(
        [{"type": "pane.created"}], on_event=lambda e, d: got.append((e, d)))
    assert _wait_until(lambda: len(fake_herdr.requests) >= 1)
    fake_herdr.push_event("pane_created", {"pane": {"pane_id": "w1:p2"}})
    assert _wait_until(lambda: len(got) == 1)
    assert got[0][0] == "pane_created"
    sub.close()


def test_subscribe_reconnects_and_notifies(fake_herdr):
    states: list[str] = []
    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe(
        [{"type": "pane.created"}],
        on_event=lambda e, d: None,
        on_state=states.append,
        backoff_initial_sec=0.05,
    )
    assert _wait_until(lambda: states == ["connected"])
    fake_herdr.drop_subscribers()  # simulate the server disconnecting
    assert _wait_until(lambda: "reconnected" in states)
    assert states[:2] == ["connected", "disconnected"]
    # After reconnecting, a subscribe request should be sent again
    assert sum(1 for r in fake_herdr.requests
               if r["method"] == "events.subscribe") >= 2
    sub.close()


def test_on_event_exception_does_not_kill_reader(fake_herdr):
    got: list[str] = []

    def bad_then_good(event, _data):
        got.append(event)
        if len(got) == 1:
            raise RuntimeError("handler bug")

    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe([{"type": "pane.created"}], on_event=bad_then_good)
    assert _wait_until(lambda: len(fake_herdr.requests) >= 1)
    fake_herdr.push_event("pane_created", {"n": 1})
    fake_herdr.push_event("pane_created", {"n": 2})
    assert _wait_until(lambda: len(got) == 2)
    sub.close()


def test_close_wakes_blocked_reader(fake_herdr):
    """M0 gate F-A1/B1 regression: close() must wake up the reader thread
    blocked in readline."""
    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe([{"type": "pane.created"}], on_event=lambda e, d: None)
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    assert sub._thread is not None and sub._thread.is_alive()
    sub.close()
    sub._thread.join(timeout=3.0)  # use its own thread reference, unaffected by same-named threads from concurrent tests
    assert not sub._thread.is_alive(), "reader thread did not exit after close()"


def test_malformed_push_line_triggers_reconnect(fake_herdr):
    """M0 gate F-C1 regression: a malformed JSON line must not kill the reader;
    it must go through reconnect instead."""
    got: list[str] = []
    states: list[str] = []
    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe([{"type": "pane.created"}],
                      on_event=lambda e, d: got.append(e),
                      on_state=states.append,
                      backoff_initial_sec=0.05)
    assert _wait_until(lambda: states == ["connected"])
    fake_herdr.push_raw("{not valid json")
    assert _wait_until(lambda: "reconnected" in states, timeout=3.0), \
        f"no reconnect after malformed line; states={states}"
    fake_herdr.push_event("pane_created", {"pane": {"pane_id": "w1:p9"}})
    assert _wait_until(lambda: len(got) >= 1, timeout=3.0)
    sub.close()


def test_subscribe_sends_per_pane_subscriptions_verbatim(fake_herdr):
    """Field test §3.3 regression: per-pane subscription objects (including
    pane_id) must reach the server verbatim."""
    c = SocketClient(fake_herdr.socket_path)
    subs = [{"type": "pane.created"},
            {"type": "pane.agent_status_changed", "pane_id": "w1:p7"}]
    sub = c.subscribe(subs, on_event=lambda e, d: None)
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    sent = next(r for r in fake_herdr.requests
                if r["method"] == "events.subscribe")["params"]["subscriptions"]
    assert sent == subs
    sub.close()


def test_degraded_notified_once_after_consecutive_failures(fake_herdr, monkeypatch):
    """0.1.1 Fix A: when herdr dies mid-run, the reader keeps reconnecting, but
    once consecutive failures hit the threshold it must notify("degraded")
    exactly once (informational, not terminal); a successful reconnect resets
    the counter so it can fire again."""
    states: list[str] = []
    c = SocketClient(fake_herdr.socket_path)
    orig_connect = c._connect
    fail_mode = {"on": True}

    def flaky_connect(timeout_sec=None):
        if fail_mode["on"]:
            raise OSError("connection refused (daemon dead)")
        return orig_connect(timeout_sec)

    monkeypatch.setattr(c, "_connect", flaky_connect)
    sub = c.subscribe([{"type": "pane.created"}],
                      on_event=lambda e, d: None,
                      on_state=states.append,
                      backoff_initial_sec=0.01, backoff_max_sec=0.02,
                      degraded_after_failures=3)
    try:
        assert _wait_until(lambda: states.count("degraded") == 1), \
            f"no degraded signal after threshold; states={states}"
        # Continued failures must not trigger repeat notifications (exactly once)
        n = states.count("disconnected")
        assert _wait_until(lambda: states.count("disconnected") >= n + 3)
        assert states.count("degraded") == 1
        # Successful reconnect -> resets
        fail_mode["on"] = False
        assert _wait_until(lambda: "connected" in states)
        # Consecutive failures again -> can fire again
        fail_mode["on"] = True
        fake_herdr.drop_subscribers()
        assert _wait_until(lambda: states.count("degraded") == 2, timeout=5.0), \
            f"degraded not re-armed after recovery; states={states}"
    finally:
        sub.close()


def test_backoff_caps_at_max(fake_herdr, monkeypatch):
    """Spec §4.1 regression: exponential backoff must not exceed the
    backoff_max_sec cap.

    After B2-5, the backoff wait goes through Event.wait (so close() can wake
    it), so the spy target is now threading.Event.wait (a timeout > 0.01s is
    the backoff value). threading.Event.wait is patched process-wide, so the
    spy filters to only this subscription's own _closed Event -- otherwise,
    in a full-suite run, an unrelated background thread from another
    still-running fixture can leak its own timeout value into `sleeps` and
    produce a spurious "backoff exceeded cap" failure (observed intermittently
    when run alongside the rest of the suite, never in isolation)."""
    import threading
    sleeps: list[float] = []
    target_event: list[threading.Event] = []
    orig_wait = threading.Event.wait

    def spy_wait(self, timeout=None):
        if target_event and self is target_event[0] and timeout is not None and timeout > 0.01:
            sleeps.append(timeout)
            timeout = min(timeout, 0.02)  # speed up the test: actually only wait 20ms
        return orig_wait(self, timeout)

    monkeypatch.setattr(threading.Event, "wait", spy_wait)
    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe([{"type": "pane.created"}],
                      on_event=lambda e, d: None,
                      backoff_initial_sec=0.5, backoff_max_sec=1.0)
    target_event.append(sub._closed)
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    for _ in range(8):
        fake_herdr.drop_subscribers()
        time.sleep(0.06)
    # Generous timeout: sleeps are already sped up to 20ms by the spy, so this
    # normally finishes in <1s; the 15s is just scheduling slack for slow CI
    # runners (macOS matrix) to avoid timing flakiness (the original 3s
    # occasionally went red on slow runners).
    assert _wait_until(lambda: len(sleeps) >= 3, timeout=15.0)
    assert max(sleeps) <= 1.0, f"backoff exceeded cap: {sleeps}"
    sub.close()


def test_subscribe_close_idempotent_handles_oserror():
    """Double close: the second close() should safely handle the OSError from
    shutdown/close."""
    import socket as sock_mod
    a, _b = sock_mod.socketpair()
    sub = Subscription()
    with sub._lock:
        sub._sock = a
    sub.close()
    a.close()
    sub.close()


def test_on_state_callback_exception_does_not_kill_reader(fake_herdr):
    got_events: list[str] = []

    def raising_on_state(state):
        raise RuntimeError(f"on_state bug: {state}")

    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe([{"type": "pane.created"}],
                      on_event=lambda e, d: got_events.append(e),
                      on_state=raising_on_state,
                      backoff_initial_sec=0.05)
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    fake_herdr.push_event("pane_created", {"pane": {"pane_id": "w1:x"}})
    assert _wait_until(lambda: len(got_events) >= 1)
    sub.close()


def test_subscribe_non_object_event_ignored(fake_herdr):
    got_events: list[str] = []
    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe([{"type": "pane.created"}],
                      on_event=lambda e, d: got_events.append(e))
    assert _wait_until(lambda: any(
        r["method"] == "events.subscribe" for r in fake_herdr.requests))
    fake_herdr.push_raw("[1, 2, 3]")
    fake_herdr.push_event("pane_created", {"pane": {"pane_id": "w1:y"}})
    assert _wait_until(lambda: len(got_events) >= 1)
    assert got_events == ["pane_created"]
    sub.close()


def test_subscribe_api_error_stops_reader(fake_herdr, monkeypatch):
    from herdr_bridge.errors import HerdrApiError

    states: list[str] = []
    fail_ack = {"once": True}

    def maybe_fail_raise(envelope):
        if fail_ack["once"] and isinstance(envelope, dict) and "error" not in envelope:
            fail_ack["once"] = False
            raise HerdrApiError(code="forbidden", message="access denied")

    c = SocketClient(fake_herdr.socket_path)
    original_raise = c._raise_for_error
    monkeypatch.setattr(c, "_raise_for_error", maybe_fail_raise)
    sub = c.subscribe([{"type": "pane.created"}],
                      on_event=lambda e, d: None,
                      on_state=states.append)
    assert _wait_until(lambda: sub.closed, timeout=3.0), \
        f"subscribe should stop after API error; states={states}"
    monkeypatch.setattr(c, "_raise_for_error", original_raise)


def test_subscribe_closed_before_ack_raises_and_reconnects(fake_herdr, monkeypatch):
    states: list[str] = []
    empty_first = {"once": True}

    orig_connect = SocketClient._connect

    def close_before_ack(self, timeout_sec=None):
        real_conn = orig_connect(self, timeout_sec)
        if not empty_first["once"]:
            return real_conn
        empty_first["once"] = False

        class ShortFile:
            def __init__(self):
                pass
            def write(self, data):
                pass
            def flush(self):
                pass
            def readline(self, _size=None):
                return ""
            def close(self):
                pass

        class ShortConn:
            def __init__(self, real):
                self._real = real
            def settimeout(self, t):
                pass
            def close(self):
                self._real.close()
            def makefile(self, *a, **kw):
                return ShortFile()

        return ShortConn(real_conn)

    monkeypatch.setattr(SocketClient, "_connect", close_before_ack)
    c = SocketClient(fake_herdr.socket_path)
    sub = c.subscribe([{"type": "pane.created"}],
                      on_event=lambda e, d: None,
                      on_state=states.append,
                      backoff_initial_sec=0.05)
    assert _wait_until(lambda: "connected" in states, timeout=5.0), \
        f"should reconnect after closed-before-ack; states={states}"
    sub.close()


def test_close_wakes_reader_sleeping_in_backoff():
    """B2-5 exit contract: close() must immediately wake up a reader sleeping
    in backoff, without waiting out the full backoff period (which can be as
    long as backoff_max_sec=30s)."""
    c = SocketClient("/nonexistent/no-such-herdr.sock")   # connection always fails
    sub = c.subscribe([{"type": "pane.created"}],
                      on_event=lambda e, d: None,
                      backoff_initial_sec=30.0)
    time.sleep(0.5)          # let the reader fail once and enter backoff sleep
    sub.close()
    assert sub._thread is not None
    sub._thread.join(timeout=2.0)
    assert not sub._thread.is_alive(), \
        "reader stuck in backoff sleep after close()"
