# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""The socket transport layer. Only responsible for "how to talk to herdr" — no
business logic here (spec §4.1).

Platform note (technical decision TD-001): CPython sets SIGPIPE to SIG_IGN at
startup, so a broken pipe always surfaces as a BrokenPipeError (an OSError
subclass), handled uniformly by the `except OSError` below — no need for
SO_NOSIGPIPE/MSG_NOSIGNAL. SO_PEERCRED is Linux-only (macOS uses LOCAL_PEERPID);
this layer does not authenticate the peer's identity.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any, cast

from herdr_bridge.errors import (
    AgentNotFoundError,
    HerdrApiError,
    HerdrConnectionError,
    HerdrTimeoutError,
)

logger = logging.getLogger("herdr_bridge.client")

_NOT_FOUND_CODES = frozenset({"pane_not_found", "agent_not_found", "terminal_not_found"})

# S-1: readline size cap — guards against a malicious/malfunctioning server
# sending an endless stream with no newline, which would OOM us. 16MB is far
# larger than any normal herdr message (the largest observed response is only
# tens of KB); hitting the cap is treated as a protocol violation.
_MAX_LINE_BYTES = 16 * 1024 * 1024


def _check_line_limit(line: str, *, context: str) -> None:
    """S-1: readline hit the cap without ending in a newline -> protocol violation
    (a truncated over-length line).

    readline(size) stops once it has read `size` bytes, without necessarily
    reaching a trailing newline; a normal line ends in \\n. Hitting the cap with
    no \\n means the server sent data beyond the line-size limit.
    """
    if len(line) >= _MAX_LINE_BYTES and not line.endswith("\n"):
        raise HerdrConnectionError(
            f"{context}: line too long (>{_MAX_LINE_BYTES} bytes, no newline)")

# 0.1.1 Fix A: default degraded thresholds for the subscription reader — once
# consecutive failures reach this count, or failures persist this many seconds,
# notify("degraded") fires exactly once (it's informational, not terminal;
# reconnect attempts keep going)
DEGRADED_AFTER_FAILURES = 10
DEGRADED_AFTER_SEC = 60.0


def detect_socket_path(herdr_bin: str = "herdr") -> str:
    """Socket path resolution (PPLX R-16 order): HERDR_SOCKET_PATH first, then the
    socket line from `herdr status`.

    Shared as a single implementation between the probe CLI and connect() (Task 12),
    to avoid behavioral divergence.
    """
    env_path = os.environ.get("HERDR_SOCKET_PATH")
    if env_path:
        return env_path
    out = subprocess.run([herdr_bin, "status"], capture_output=True,
                         text=True, timeout=10, check=False)
    for line in out.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("socket:"):
            return stripped.split(":", 1)[1].strip()
    raise HerdrConnectionError(
        "cannot detect herdr socket path (no HERDR_SOCKET_PATH, "
        "no `socket:` line in `herdr status`)")


class Subscription:
    def __init__(self) -> None:
        self._closed = threading.Event()
        self._connected = threading.Event()  # first ack received (wire-layer sync point)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def wait_connected(self, timeout: float) -> bool:
        """Block until the first subscribe ack arrives (or timeout).

        This is the wire-layer guarantee behind the rebuild rule: the caller must
        call wait_connected before taking a snapshot, otherwise "subscribe new
        first" is just fire-and-forget — the subscribe request could reach the
        server later than the snapshot does.
        """
        return self._connected.wait(timeout)

    def close(self) -> None:
        """Terminate the subscription (B2-5 exit contract).

        Sets the closed flag and shuts down + closes the socket: a reader blocked
        in readline gets EOF immediately, and a reader sleeping in backoff is woken
        immediately by the event — the thread exits within milliseconds without
        leaking; idempotent, safe to call repeatedly.
        """
        self._closed.set()
        with self._lock:
            if self._sock is not None:
                try:
                    # shutdown before close: wakes up a reader thread blocked in
                    # readline (makefile's _io_refs means close() alone isn't
                    # enough to deliver EOF)
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self._sock.close()
                except OSError:
                    pass

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


def _safe_close(f: Any, conn: socket.socket | None) -> None:
    """Uniform cleanup: close the makefile wrapper first (to release _io_refs), then the socket; never raises."""
    if f is not None:
        try:
            f.close()
        except OSError:
            pass
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass


class SocketClient:
    """Protocol as observed in practice (environment validation notes §3.2): one connection per request/response."""

    def __init__(self, socket_path: str, *, request_timeout_sec: float = 10.0) -> None:
        self._socket_path = socket_path
        self._request_timeout_sec = request_timeout_sec
        self._id_counter = itertools.count(1)
        self._id_lock = threading.Lock()

    @property
    def socket_path(self) -> str:
        """The target socket path for the connection (public read-only; B2-2 — upper layers shouldn't reach into private state)."""
        return self._socket_path

    def _next_id(self) -> str:
        with self._id_lock:
            return f"br-{next(self._id_counter)}"

    @staticmethod
    def _raise_for_error(envelope: Any) -> None:
        """Map an error envelope -> an exception (shared between request and subscribe ack).

        Valid JSON that isn't a dict (int/str/null...) is treated as a protocol
        violation -> HerdrConnectionError, so a TypeError doesn't escape and mask
        the real error (re-review MINOR-3).
        """
        if not isinstance(envelope, dict):
            raise HerdrConnectionError(
                f"protocol violation: expected JSON object envelope, got "
                f"{type(envelope).__name__}")
        if "error" not in envelope:
            return
        err = envelope["error"]
        if not isinstance(err, dict):
            raise HerdrConnectionError(
                f"protocol violation: error field is {type(err).__name__}")
        code = err.get("code", "unknown")
        message = err.get("message", "")
        if code in _NOT_FOUND_CODES:
            raise AgentNotFoundError(code=code, message=message)
        raise HerdrApiError(code=code, message=message)

    @staticmethod
    def unwrap(result: dict[str, Any]) -> dict[str, Any]:
        """Unwrap herdr's {type, <resource-key>: {...}} response envelope (capability-notes §3.1).

        Takes the value of the single non-"type" key; if the shape doesn't match
        (an empty result, or multiple keys), returns it as-is, tolerating future
        protocol changes rather than hard-assuming the envelope structure.
        """
        keys = [k for k in result if k != "type"]
        if len(keys) == 1 and isinstance(result[keys[0]], dict):
            return cast(dict[str, Any], result[keys[0]])
        return result

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """request + envelope unwrap. The default entry point for upper layers (cache/actions/probe)."""
        return self.unwrap(self.request(method, params))

    def _connect(self, timeout_sec: float | None = None) -> socket.socket:
        """Retry once on a connect-phase failure (safe to retry — no request has been sent yet)."""
        effective_timeout = timeout_sec if timeout_sec is not None else self._request_timeout_sec
        last_exc: OSError | None = None
        for attempt in range(2):
            conn: socket.socket | None = None
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.settimeout(effective_timeout)
                conn.connect(self._socket_path)
                return conn
            except OSError as exc:
                if conn is not None:
                    conn.close()  # don't rely on GC to reclaim the fd on the failure path
                last_exc = exc
                if attempt == 0:
                    time.sleep(0.1)
        raise HerdrConnectionError(
            f"cannot connect to herdr socket at {self._socket_path}: {last_exc}"
        ) from last_exc

    def request(self, method: str, params: dict[str, Any] | None = None,
                *, timeout_sec: float | None = None) -> dict[str, Any]:
        effective = timeout_sec if timeout_sec is not None else self._request_timeout_sec
        req_id = self._next_id()
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}})
        conn = self._connect(effective)
        f = None
        try:
            f = conn.makefile("rw", encoding="utf-8", newline="\n")
            f.write(payload + "\n")
            f.flush()
            line = f.readline(_MAX_LINE_BYTES)
        except TimeoutError as exc:
            raise HerdrTimeoutError(
                f"{method} timed out after {effective}s") from exc
        except OSError as exc:
            raise HerdrConnectionError(f"{method} transport failed: {exc}") from exc
        finally:
            _safe_close(f, conn)
        if not line:
            raise HerdrConnectionError(f"{method}: server closed connection without reply")
        _check_line_limit(line, context=method)
        resp = json.loads(line)
        self._raise_for_error(resp)
        return cast(dict[str, Any], resp.get("result", {}))

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def subscribe(
        self,
        subscriptions: list[dict[str, Any]],
        on_event: Callable[[str, dict[str, Any]], None],
        *,
        on_state: Callable[[str], None] | None = None,
        backoff_initial_sec: float = 0.5,
        backoff_max_sec: float = 30.0,
        degraded_after_failures: int = DEGRADED_AFTER_FAILURES,
        degraded_after_sec: float = DEGRADED_AFTER_SEC,
    ) -> Subscription:
        sub = Subscription()

        def notify(state: str) -> None:
            if on_state is not None:
                try:
                    on_state(state)
                except Exception:
                    logger.exception("on_state callback failed")

        def reader() -> None:
            backoff = backoff_initial_sec
            ever_connected = False
            # Fix A: consecutive-failure count + start timestamp; degraded fires
            # exactly once (a flag guards against duplicates). This lets a long-lived
            # consumer distinguish a "temporary restart" from "possibly gone for
            # good" — we keep reconnecting regardless (herdr may just be restarting);
            # degraded is informational, not a termination signal.
            fail_count = 0
            fail_since: float | None = None
            degraded_notified = False
            while not sub.closed:
                f = None
                conn: socket.socket | None = None
                try:
                    # _connect: shares the same connection path as request (timeout +
                    # single retry). Establishing the connection and reading the ack
                    # are guarded by request_timeout_sec (stalled-ack protection);
                    # once the ack succeeds, the timeout is lifted — an unbounded
                    # readline for events is legitimate here.
                    conn = self._connect()
                    with sub._lock:
                        sub._sock = conn
                    f = conn.makefile("rw", encoding="utf-8", newline="\n")
                    f.write(json.dumps({
                        "id": self._next_id(),
                        "method": "events.subscribe",
                        "params": {"subscriptions": subscriptions},
                    }) + "\n")
                    f.flush()
                    ack_line = f.readline(_MAX_LINE_BYTES)
                    if not ack_line:
                        raise HerdrConnectionError("subscribe: closed before ack")
                    _check_line_limit(ack_line, context="subscribe-ack")
                    ack = json.loads(ack_line)
                    self._raise_for_error(ack)
                    conn.settimeout(None)
                    sub._connected.set()
                    notify("reconnected" if ever_connected else "connected")
                    ever_connected = True
                    backoff = backoff_initial_sec
                    fail_count = 0
                    fail_since = None
                    degraded_notified = False
                    while not sub.closed:
                        line = f.readline(_MAX_LINE_BYTES)
                        if not line:
                            break  # server disconnected
                        _check_line_limit(line, context="subscribe-event")
                        msg = json.loads(line)
                        if not isinstance(msg, dict):
                            logger.warning("ignoring non-object event line: %r", line[:80])
                            continue
                        try:
                            on_event(msg.get("event", ""), msg.get("data", {}))
                        except Exception:
                            logger.exception("on_event callback failed")
                except HerdrApiError:
                    logger.exception("subscribe rejected by server; not retrying")
                    sub.close()
                    return
                except (OSError, HerdrConnectionError, ValueError):
                    # OSError/HerdrConnectionError: a connection-layer failure;
                    # ValueError (including JSONDecodeError): a malformed line, treated
                    # the same as a disconnect. All three always trigger a reconnect —
                    # the reader thread must never die silently.
                    logger.debug("subscribe connection cycle failed; will reconnect",
                                 exc_info=True)
                    fail_count += 1
                    now = time.monotonic()
                    if fail_since is None:
                        fail_since = now
                    if not degraded_notified and (
                            fail_count >= degraded_after_failures
                            or now - fail_since >= degraded_after_sec):
                        degraded_notified = True
                        logger.warning(
                            "subscribe degraded: %d consecutive failures over "
                            "%.1fs — herdr may be gone (still retrying)",
                            fail_count, now - fail_since)
                        notify("degraded")
                finally:
                    with sub._lock:
                        _safe_close(f, sub._sock)
                        sub._sock = None
                    conn = None
                if sub.closed:
                    return
                notify("disconnected")
                # B2-5 exit contract: the backoff wait uses the closed event, so
                # close() wakes it immediately — join must never have to wait out the
                # full backoff period (capped at backoff_max_sec).
                if sub._closed.wait(backoff):
                    return
                backoff = min(backoff * 2, backoff_max_sec)

        t = threading.Thread(target=reader, daemon=True,
                             name="herdr-bridge-subscribe")
        sub._thread = t
        t.start()
        return sub
