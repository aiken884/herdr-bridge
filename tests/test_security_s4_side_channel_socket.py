# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""S-4: side-channel Unix socket security -- private directory / permissions /
symlink protection / peer uid verification."""

from __future__ import annotations

import os
import shutil
import socket
import stat
import tempfile
import threading
import time

import pytest

from herdr_bridge.acp.router import (
    _get_peer_uid,
    _peer_uid_allowed,
    _safe_unlink_socket,
)


def test_safe_unlink_socket_refuses_symlink_and_does_not_delete_target(tmp_path):
    """Symlink attack scenario: the target path is a symlink pointing at a file
    elsewhere -- _safe_unlink_socket must refuse to delete it, and the real file the
    symlink points to must not be mistakenly deleted."""
    victim = tmp_path / "victim.txt"
    victim.write_text("important data")
    symlink_path = tmp_path / "fake.sock"
    os.symlink(victim, symlink_path)

    with pytest.raises(RuntimeError, match="symlink"):
        _safe_unlink_socket(str(symlink_path))

    assert victim.exists(), "the real file pointed to by the symlink should not be deleted"
    assert victim.read_text() == "important data"
    assert os.path.islink(symlink_path), "the symlink itself should also not be removed"


def test_safe_unlink_socket_refuses_regular_file(tmp_path):
    """Deletion should likewise be refused when the target path is a regular file (not a socket)."""
    regular = tmp_path / "not-a-socket"
    regular.write_text("data")

    with pytest.raises(RuntimeError, match="symlink"):
        _safe_unlink_socket(str(regular))

    assert regular.exists()


def test_safe_unlink_socket_removes_real_socket():
    """When the target path is genuinely a socket file, delete it normally (so a
    subsequent bind() can reuse that path).

    Uses tempfile.mkdtemp() instead of pytest's tmp_path: AF_UNIX path length is
    capped around 104 bytes (macOS), and pytest's nested tmp_path
    (pytest-of-<user>/pytest-N/<test-name>/...) usually already exceeds that, causing
    bind() to throw "AF_UNIX path too long".
    """
    d = tempfile.mkdtemp(prefix="s4-test-")
    try:
        sock_path = os.path.join(d, "real.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(sock_path)
        s.close()

        assert stat.S_ISSOCK(os.lstat(sock_path).st_mode)
        _safe_unlink_socket(sock_path)
        assert not os.path.exists(sock_path)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_safe_unlink_socket_missing_path_is_noop(tmp_path):
    """When the path doesn't exist, just skip it (no raise)."""
    _safe_unlink_socket(str(tmp_path / "does-not-exist.sock"))


def test_peer_uid_allowed_matches_current_user():
    assert _peer_uid_allowed(os.getuid()) is True


def test_peer_uid_allowed_rejects_other_uid():
    other_uid = os.getuid() + 12345
    assert _peer_uid_allowed(other_uid) is False


def test_peer_uid_allowed_fails_closed_when_unknown():
    """When uid can't be verified (None), fail-closed: always reject, never default-allow."""
    assert _peer_uid_allowed(None) is False


def test_get_peer_uid_returns_own_uid_over_real_unix_socket():
    """Verifies the SO_PEERCRED / LOCAL_PEERCRED parsing logic itself is correct using
    a real AF_UNIX socket pair: when a process connects to a server it started
    itself, the retrieved peer uid must equal its own uid."""
    d = tempfile.mkdtemp(prefix="s4-test-")
    try:
        sock_path = os.path.join(d, "peer.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        def _client():
            time.sleep(0.1)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(sock_path)
            time.sleep(0.3)
            c.close()

        t = threading.Thread(target=_client)
        t.start()
        conn, _ = server.accept()

        peer_uid = _get_peer_uid(conn)

        conn.close()
        t.join()
        server.close()

        assert peer_uid == os.getuid()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_start_report_side_channel_uses_private_dir_and_secure_perms(monkeypatch):
    """Run through _start_report_side_channel end to end: the directory must be a
    private temp directory (0o700), the socket file must be 0o600, and it must not
    be the hardcoded /tmp/tower-reports.sock."""
    from herdr_bridge.acp.router import AcpRouter

    router = AcpRouter.__new__(AcpRouter)
    router.project = "herdr-bridge"

    router._start_report_side_channel()
    try:
        sock_path = router._report_sock_path
        assert sock_path != "/tmp/tower-reports.sock"
        assert os.environ["TOWER_REPORT_SOCK"] == sock_path

        sock_dir = os.path.dirname(sock_path)
        dir_mode = stat.S_IMODE(os.stat(sock_dir).st_mode)
        assert dir_mode == 0o700, f"directory permission should be 0o700, actual is {oct(dir_mode)}"

        file_mode = stat.S_IMODE(os.stat(sock_path).st_mode)
        assert file_mode == 0o600, f"socket file permission should be 0o600, actual is {oct(file_mode)}"
        assert stat.S_ISSOCK(os.stat(sock_path).st_mode)
    finally:
        try:
            os.unlink(router._report_sock_path)
        except OSError:
            pass
        try:
            os.rmdir(os.path.dirname(router._report_sock_path))
        except OSError:
            pass
        os.environ.pop("TOWER_REPORT_SOCK", None)


def test_report_server_rejects_connection_from_wrong_uid(monkeypatch):
    """Simulates the "connection from a different uid" scenario: mocks _get_peer_uid
    to return a uid that isn't the current user's, confirming the accept loop rejects
    it and never calls _handle_structured_report."""
    from herdr_bridge.acp import router as router_mod
    from herdr_bridge.acp.router import AcpRouter

    router = AcpRouter.__new__(AcpRouter)
    router.project = "herdr-bridge"

    handled: list[dict] = []
    router._handle_structured_report = lambda report: handled.append(report)  # type: ignore[method-assign]

    monkeypatch.setattr(router_mod, "_get_peer_uid", lambda conn: os.getuid() + 999)

    router._start_report_side_channel()
    sock_path = router._report_sock_path
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)
        client.sendall(
            b'{"type": "task_report", "task_id": "t1", "agent_id": "a1"}\n'
        )
        client.close()
        time.sleep(0.3)
        assert handled == [], "when peer uid doesn't match, the connection's content should not be processed"
    finally:
        try:
            os.unlink(router._report_sock_path)
        except OSError:
            pass
        try:
            os.rmdir(os.path.dirname(router._report_sock_path))
        except OSError:
            pass
        os.environ.pop("TOWER_REPORT_SOCK", None)


def test_report_server_accepts_connection_from_same_uid(monkeypatch):
    """Normal scenario (same uid): the connection should be accepted and processed."""
    from herdr_bridge.acp.router import AcpRouter

    router = AcpRouter.__new__(AcpRouter)
    router.project = "herdr-bridge"

    handled: list[dict] = []
    router._handle_structured_report = lambda report: handled.append(report)  # type: ignore[method-assign]

    router._start_report_side_channel()
    sock_path = router._report_sock_path
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)
        client.sendall(
            b'{"type": "task_report", "task_id": "t1", "agent_id": "a1"}\n'
        )
        client.close()
        time.sleep(0.3)
        assert len(handled) == 1
        assert handled[0]["task_id"] == "t1"
    finally:
        try:
            os.unlink(router._report_sock_path)
        except OSError:
            pass
        try:
            os.rmdir(os.path.dirname(router._report_sock_path))
        except OSError:
            pass
        os.environ.pop("TOWER_REPORT_SOCK", None)
