# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Test double: simulates the herdr socket protocol behavior observed in
real-world verification (environment verification notes §2/§3)."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class FakeApiError(Exception):
    """Handlers signal an error envelope by raising this (an explicit contract,
    instead of guessing at dict shapes)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _default_ping(_params: dict[str, Any]) -> dict[str, Any]:
    return {"type": "pong", "version": "0.7.3", "protocol": 16, "capabilities": {}}


class FakeHerdrServer:
    def __init__(self, handlers: dict[str, Handler] | None = None) -> None:
        self._handlers: dict[str, Handler] = {"ping": _default_ping}
        if handlers:
            self._handlers.update(handlers)
        # Technical decision TD-001 P0: macOS caps sun_path at 104 bytes, and both
        # pytest's tmp_path and macOS's default tempfile dir (/var/folders/…) can
        # exceed that — so pin a short /tmp prefix instead.
        self._tmp = tempfile.TemporaryDirectory(prefix="fh-", dir="/tmp")
        self.socket_path = str(Path(self._tmp.name) / "s")
        assert len(self.socket_path.encode()) < 104, (
            f"socket path too long for macOS sun_path: {self.socket_path}")
        self.requests: list[dict[str, Any]] = []
        # (conn, f): closing the makefile alone doesn't actually release the fd
        # (kept alive by _io_refs) — simulating a disconnect requires
        # shutdown+close on the underlying socket to deliver EOF to the peer.
        self._subscribers: list[tuple[socket.socket, Any]] = []
        self._lock = threading.Lock()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(16)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._server.close()
        self.drop_subscribers()
        self._tmp.cleanup()

    # -- test API ---------------------------------------------------------
    def set_handler(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    def push_event(self, event: str, data: dict[str, Any]) -> None:
        line = json.dumps({"data": data, "event": event}) + "\n"
        with self._lock:
            for pair in list(self._subscribers):
                _conn, f = pair
                try:
                    f.write(line)
                    f.flush()
                except OSError:
                    self._subscribers.remove(pair)

    def push_raw(self, line: str) -> None:
        """Push an arbitrary raw line (including malformed JSON), for protocol
        resilience tests."""
        with self._lock:
            for pair in list(self._subscribers):
                _conn, f = pair
                try:
                    f.write(line + "\n")
                    f.flush()
                except OSError:
                    self._subscribers.remove(pair)

    def drop_subscribers(self) -> None:
        """Simulate a server-side disconnect: shut down the underlying socket
        so the client gets EOF immediately."""
        with self._lock:
            for conn, f in self._subscribers:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    f.close()
                except OSError:
                    pass
                try:
                    conn.close()
                except OSError:
                    pass
            self._subscribers.clear()

    # -- internals --------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn: socket.socket) -> None:
        f = conn.makefile("rw", encoding="utf-8", newline="\n")
        line = f.readline()
        if not line:
            conn.close()
            return
        req = json.loads(line)
        self.requests.append(req)
        method = req.get("method", "")
        if method == "events.subscribe":
            # Register before sending the ack: the client may call push_event
            # the instant it gets the ack, so acking before registering would
            # drop the immediately-following push (~10% flaky in review testing).
            with self._lock:
                self._subscribers.append((conn, f))
            f.write(json.dumps(
                {"id": req["id"], "result": {"type": "subscription_started"}}) + "\n")
            f.flush()
            return  # long-lived connection: left open for push_event, don't close it
        handler = self._handlers.get(method)
        if handler is None:
            body = {"id": req.get("id", ""),
                    "error": {"code": "invalid_request", "message": "unknown method"}}
        else:
            try:
                result = handler(req.get("params", {}))
                body = {"id": req.get("id", ""), "result": result}
            except FakeApiError as exc:
                body = {"id": req.get("id", ""),
                        "error": {"code": exc.code, "message": exc.message}}
        try:
            f.write(json.dumps(body) + "\n")
            f.flush()
        except OSError:
            pass  # the client may have already closed on timeout (e.g. timeout tests) — don't blow up the daemon thread
        conn.close()  # one request per connection (verified in real-world testing §3.2)
