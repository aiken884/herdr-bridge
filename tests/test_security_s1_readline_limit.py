# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""S-1: socket readline cap protection -- must not OOM when a malicious server sends
an unbounded data stream with no newline."""

import socket
import tempfile
import threading
from pathlib import Path
from typing import Self

import pytest

from herdr_bridge.client import _MAX_LINE_BYTES, SocketClient
from herdr_bridge.errors import HerdrConnectionError


class _OversizedLineServer:
    """Sends data exceeding _MAX_LINE_BYTES with no newline, simulating a malicious/misbehaving server."""

    def __init__(self, payload_size: int) -> None:
        self._payload_size = payload_size
        self._tmp = tempfile.TemporaryDirectory(prefix="s1-", dir="/tmp")
        self.socket_path = str(Path(self._tmp.name) / "s")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._server.close()
        self._tmp.cleanup()

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        # Read the client's request (doesn't matter), then send an oversized response with no newline
        try:
            conn.recv(4096)
            conn.sendall(b"x" * self._payload_size)  # no \n
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def test_max_line_bytes_constant_exists():
    """Module constant exists and is a reasonable cap (16MB)."""
    assert _MAX_LINE_BYTES == 16 * 1024 * 1024


def test_request_oversized_line_raises_connection_error():
    """Server sends data over the cap with no newline -> request raises HerdrConnectionError."""
    with _OversizedLineServer(payload_size=_MAX_LINE_BYTES + 1024) as srv:
        c = SocketClient(srv.socket_path, request_timeout_sec=5.0)
        with pytest.raises(HerdrConnectionError, match="line too long"):
            c.request("ping")


def test_request_normal_size_still_works(fake_herdr):
    """A normal-sized response is unaffected."""
    c = SocketClient(fake_herdr.socket_path)
    result = c.ping()
    assert result["type"] == "pong"


# ---------------------------------------------------------- discriminative-power hardening
# Gate finding: the tests above only verify the after-the-fact guard of "raise if too
# long" -- mutating readline(_MAX_LINE_BYTES) back to an unbounded readline() would
# still pass. The key to preventing OOM is the readline cap itself (an unbounded read
# would slurp the whole massive payload into memory before the guard ever gets a
# turn), so the following directly asserts on the size argument readline receives.


class _SpyFile:
    """file-like stand-in: records the size argument received by each readline call."""

    def __init__(self, lines):
        self.readline_sizes: list[int] = []
        self._lines = list(lines)

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def readline(self, size=-1):
        self.readline_sizes.append(size)
        return self._lines.pop(0) if self._lines else ""

    def close(self):
        pass


class _SpySock:
    def __init__(self, spy_file):
        self._f = spy_file

    def makefile(self, *a, **k):
        return self._f

    def settimeout(self, t):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


def test_request_readline_is_called_with_cap(monkeypatch):
    spy = _SpyFile(['{"id":"br-1","result":{"type":"pong"}}\n'])
    c = SocketClient("/unused.sock")
    monkeypatch.setattr(c, "_connect", lambda timeout_sec=None: _SpySock(spy))
    c.request("ping")
    assert spy.readline_sizes == [_MAX_LINE_BYTES], (
        f"request readline not capped: {spy.readline_sizes}")


def test_subscribe_ack_and_event_readline_are_capped(monkeypatch):
    from tests.conftest import wait_until_true
    spy = _SpyFile(['{"result":{}}\n', '{"event":"e","data":{}}\n'])
    c = SocketClient("/unused.sock")
    monkeypatch.setattr(c, "_connect", lambda timeout_sec=None: _SpySock(spy))
    sub = c.subscribe([{"type": "pane.created"}], on_event=lambda e, d: None)
    try:
        # ack + event + EOF: at least 3 readline calls, all carrying the cap
        assert wait_until_true(lambda: len(spy.readline_sizes) >= 3)
    finally:
        sub.close()
    assert set(spy.readline_sizes) == {_MAX_LINE_BYTES}, (
        f"subscribe readline not capped: {set(spy.readline_sizes)}")
