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
        self.sent: list[bytes] = []

    def settimeout(self, timeout):
        pass

    def connect(self, path):
        self.connect_calls += 1

    def sendall(self, data):
        self.sendall_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise BrokenPipeError("simulated broken pipe")
        self.sent.append(data)

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


def test_send_moshi_envelope_recovers_from_broken_pipe_via_retry(tmp_path, monkeypatch):
    """Reproduce a broken pipe on the first attempt, handshake normally on
    the second. Verifies delivery still succeeds after retry, so the
    notification is no longer silently lost.

    Uses _FakeConn (pure object mocking, no real socket/background thread)
    rather than a real Unix socket with an accept-then-close daemon thread:
    the real-socket version raced the client's sendall() against the daemon
    thread's own scheduling to accept()+close() first, and _send_moshi_envelope
    only cares whether sendall() itself raises -- any error from the
    subsequent recv() (the ack read) is caught by its own inner
    `except OSError: pass` and does NOT affect the return value, so forcing
    an RST on close() doesn't help either. This was flaky in real CI (a
    thread-scheduling race that a fast/local machine rarely loses but a
    loaded CI runner does) even after that RST fix. _FakeConn removes the
    race entirely by controlling exactly which sendall() call fails.
    """
    sock_path = str(tmp_path / "moshi-hook.sock")
    (tmp_path / "moshi-hook.sock").touch()  # only needs to pass os.path.exists

    fake_conn = _FakeConn(fail_times=1)
    monkeypatch.setattr(router.socket, "socket", lambda *a, **kw: fake_conn)
    monkeypatch.setenv("MOSHI_SOCKET_PATH", sock_path)
    monkeypatch.setattr(router.time, "sleep", lambda s: None)

    envelope = {
        "type": "session.update",
        "eventName": "fleet.task_complete",
        "message": "hello",
    }
    ok = router._send_moshi_envelope(envelope, max_retries=3, base_delay=0.01)

    assert ok is True
    assert fake_conn.sendall_calls == 2
    assert len(fake_conn.sent) == 1
    assert json.loads(fake_conn.sent[0].decode("utf-8").strip())["eventName"] == "fleet.task_complete"


def test_send_moshi_envelope_gives_up_after_max_retries_and_logs_warning(
    tmp_path, monkeypatch, capsys
):
    """Every attempt fails: after retries are exhausted, return False and
    explicitly print a warning -- no longer silently swallowed like the old
    `except Exception: pass`, which let fleet notifications quietly vanish.
    See the broken-pipe-recovery test above for why this uses _FakeConn
    rather than a real socket + background-thread daemon."""
    sock_path = str(tmp_path / "moshi-hook.sock")
    (tmp_path / "moshi-hook.sock").touch()

    fake_conn = _FakeConn(fail_times=3)
    monkeypatch.setattr(router.socket, "socket", lambda *a, **kw: fake_conn)
    monkeypatch.setenv("MOSHI_SOCKET_PATH", sock_path)
    monkeypatch.setattr(router.time, "sleep", lambda s: None)

    envelope = {
        "type": "session.update",
        "eventName": "fleet.task_complete",
        "message": "hello",
    }
    ok = router._send_moshi_envelope(
        envelope, max_retries=3, base_delay=0.01, log_prefix="[test-moshi]"
    )

    assert ok is False
    assert fake_conn.sendall_calls == 3
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
