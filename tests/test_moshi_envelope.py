# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Broken-pipe retry/handshake regression tests for `_send_moshi_envelope()`
(issue #38).

Background: in the Moshi local socket protocol (see moshi-hook API §1), even
a fire-and-forget `session.update` gets an `ack` frame back from the daemon
once received. The old implementation sent without reading, closed the
connection, and silently swallowed every error with `except Exception: pass`,
which meant:
1. The daemon side routinely hit a broken pipe while writing the ack (one of
   the causes behind the flood of WARN entries in Moshi's hook.log).
2. On the herdr-bridge side, when the connection failed, the fleet
   task-complete notification would just vanish -- no retry, no log -- and
   the user would have no idea the push never arrived.

The tests below cover: reproducing a broken pipe on a real Unix socket and
verifying successful delivery after retry, precisely verifying the
exponential-backoff timing sequence with a fake socket, and edge cases where
the path doesn't exist / retries are exhausted.
"""

import json
import os
import socket
import struct
import threading
import uuid

import pytest

from herdr_bridge.acp import router


@pytest.fixture()
def short_sock_path():
    """AF_UNIX's sun_path is capped at ~104 bytes on macOS, and pytest's
    tmp_path is nested too deep and would exceed that, so use a short path
    under /tmp instead (cleaned up after the test)."""
    path = f"/tmp/moshi-test-{uuid.uuid4().hex[:12]}.sock"
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _start_fake_moshi_daemon(sock_path, handler, *, accepts=1):
    """Start a fake Moshi daemon on a background thread, calling
    handler(conn) on each accept."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(accepts)

    def run():
        for _ in range(accepts):
            conn, _ = srv.accept()
            try:
                handler(conn)
            finally:
                conn.close()
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


class _FakeConn:
    """A fake socket connection object used to precisely control which call
    fails, so we can verify the retry/backoff logic itself."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.connect_calls = 0
        self.sendall_calls = 0

    def settimeout(self, timeout):
        pass

    def connect(self, path):
        self.connect_calls += 1

    def sendall(self, data):
        self.sendall_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise BrokenPipeError("simulated broken pipe")

    def recv(self, bufsize):
        return b'{"type":"ack"}\n'

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_send_moshi_envelope_success_reads_ack(short_sock_path, monkeypatch):
    """Happy path: after sending the envelope, read the daemon's ack, complete
    the full handshake, and return True."""
    sock_path = short_sock_path
    received = []

    def handler(conn):
        conn.settimeout(2)
        data = conn.recv(65536)
        received.append(json.loads(data.decode("utf-8").strip()))
        conn.sendall(b'{"type":"ack"}\n')

    _start_fake_moshi_daemon(sock_path, handler, accepts=1)
    monkeypatch.setenv("MOSHI_SOCKET_PATH", sock_path)

    ok = router._send_moshi_envelope(
        {"type": "session.update", "eventName": "fleet.task_complete"}
    )

    assert ok is True
    assert received[0]["eventName"] == "fleet.task_complete"


def test_send_moshi_envelope_recovers_from_broken_pipe_via_retry(short_sock_path, monkeypatch):
    """Reproduce a broken pipe: the daemon disconnects immediately after its
    first accept (simulating a daemon restart/connection drop), and only
    handshakes normally on the second try. Verifies delivery still succeeds
    after retry, so the notification is no longer silently lost."""
    sock_path = short_sock_path
    received = []
    attempts = {"n": 0}

    def handler(conn):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Force an RST (hard reset) instead of a graceful FIN: once the
            # connection actually closes, a zero SO_LINGER timeout guarantees
            # the client's next sendall/recv sees an immediate error rather
            # than a silent EOF that could be mistaken for success (a plain
            # close() only *sometimes* surfaces as BrokenPipeError, depending
            # on platform socket semantics).
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            conn.close()
            return
        conn.settimeout(2)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        received.append(json.loads(buf.decode("utf-8").strip()))
        conn.sendall(b'{"type":"ack"}\n')

    _start_fake_moshi_daemon(sock_path, handler, accepts=2)
    monkeypatch.setenv("MOSHI_SOCKET_PATH", sock_path)

    # The payload is deliberately large enough that the client's sendall()
    # (multiple syscalls to write ~200KB) takes long enough for the fake
    # daemon's background thread to actually get scheduled, accept(), and
    # close() first -- with a tiny payload, the client's single-syscall write
    # can complete before the daemon thread is even scheduled by the GIL,
    # making the close() arrive too late to be observed at all (this is a
    # thread-scheduling race, not a kernel-buffer-size one -- confirmed by
    # reproducing the failure locally with a small payload even with the
    # SO_LINGER/RST fix above).
    big_envelope = {
        "type": "session.update",
        "eventName": "fleet.task_complete",
        "message": "x" * 200_000,
    }
    ok = router._send_moshi_envelope(big_envelope, max_retries=3, base_delay=0.01)

    assert ok is True
    assert attempts["n"] == 2
    assert received[0]["eventName"] == "fleet.task_complete"


def test_send_moshi_envelope_gives_up_after_max_retries_and_logs_warning(
    short_sock_path, monkeypatch, capsys
):
    """The daemon keeps disconnecting: after retries are exhausted, return
    False and explicitly print a warning -- no longer silently swallowed like
    the old `except Exception: pass`, which let fleet notifications quietly
    vanish."""
    sock_path = short_sock_path
    attempts = {"n": 0}

    def handler(conn):
        attempts["n"] += 1
        # Force an RST + use a large payload -- see the comments in the
        # broken-pipe-recovery test above for why both are needed together.
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    _start_fake_moshi_daemon(sock_path, handler, accepts=3)
    monkeypatch.setenv("MOSHI_SOCKET_PATH", sock_path)

    big_envelope = {
        "type": "session.update",
        "eventName": "fleet.task_complete",
        "message": "x" * 200_000,
    }
    ok = router._send_moshi_envelope(
        big_envelope, max_retries=3, base_delay=0.01, log_prefix="[test-moshi]"
    )

    assert ok is False
    assert attempts["n"] == 3
    captured = capsys.readouterr()
    assert "[test-moshi]" in captured.out
    assert "fleet.task_complete" in captured.out


def test_send_moshi_envelope_missing_socket_returns_false_without_retry(tmp_path, monkeypatch):
    """The socket file doesn't exist (Moshi isn't installed/running): return
    False immediately, without a pointless retry wait."""
    sock_path = str(tmp_path / "does-not-exist.sock")
    monkeypatch.setenv("MOSHI_SOCKET_PATH", sock_path)

    sleep_calls = []
    monkeypatch.setattr(router.time, "sleep", lambda s: sleep_calls.append(s))

    ok = router._send_moshi_envelope({"type": "session.update"}, max_retries=3, base_delay=0.01)

    assert ok is False
    assert sleep_calls == []


def test_send_moshi_envelope_exponential_backoff_sequence(tmp_path, monkeypatch):
    """Precisely verify the backoff timing sequence is base_delay * 2**attempt
    (doesn't depend on real socket timing, to avoid test flakiness)."""
    sock_path = str(tmp_path / "moshi-hook.sock")
    sock_path_file = tmp_path / "moshi-hook.sock"
    sock_path_file.touch()  # only needs to pass the os.path.exists check, doesn't need to be a real socket

    fake_conn = _FakeConn(fail_times=2)
    monkeypatch.setattr(router.socket, "socket", lambda *a, **kw: fake_conn)
    monkeypatch.setenv("MOSHI_SOCKET_PATH", sock_path)

    sleeps = []
    monkeypatch.setattr(router.time, "sleep", lambda s: sleeps.append(s))

    ok = router._send_moshi_envelope({"type": "session.update"}, max_retries=3, base_delay=0.1)

    assert ok is True
    assert sleeps == pytest.approx([0.1, 0.2])
    assert fake_conn.connect_calls == 3
    assert fake_conn.sendall_calls == 3
