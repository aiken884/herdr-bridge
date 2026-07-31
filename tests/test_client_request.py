# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import threading

import pytest

from herdr_bridge.client import SocketClient
from herdr_bridge.errors import (
    AgentNotFoundError,
    HerdrApiError,
    HerdrConnectionError,
    HerdrTimeoutError,
)
from herdr_bridge.testing import FakeApiError


def test_request_returns_result(fake_herdr):
    c = SocketClient(fake_herdr.socket_path)
    assert c.ping()["type"] == "pong"


def test_request_ids_unique_and_echoed(fake_herdr):
    c = SocketClient(fake_herdr.socket_path)
    c.ping()
    c.ping()
    ids = [r["id"] for r in fake_herdr.requests]
    assert len(set(ids)) == 2


def test_api_error_maps_to_exception(fake_herdr):
    def nope(_p):
        raise FakeApiError("pane_not_found", "nope")

    fake_herdr.set_handler("pane.read", nope)
    c = SocketClient(fake_herdr.socket_path)
    with pytest.raises(AgentNotFoundError):
        c.request("pane.read", {"pane_id": "x", "source": "recent_unwrapped"})


def test_generic_api_error(fake_herdr):
    def boom(_p):
        raise FakeApiError("internal", "boom")

    fake_herdr.set_handler("workspace.list", boom)
    c = SocketClient(fake_herdr.socket_path)
    with pytest.raises(HerdrApiError) as ei:
        c.request("workspace.list")
    assert ei.value.code == "internal"


def test_connect_failure_raises_connection_error(tmp_path):
    c = SocketClient(str(tmp_path / "no.sock"))
    with pytest.raises(HerdrConnectionError):
        c.ping()


def test_timeout_raises(fake_herdr):
    hold = threading.Event()

    def slow(_p):
        hold.wait(2.0)
        return {"ok": True}

    fake_herdr.set_handler("slow.op", slow)
    c = SocketClient(fake_herdr.socket_path, request_timeout_sec=0.2)
    with pytest.raises(HerdrTimeoutError):
        c.request("slow.op")
    hold.set()


def test_detect_socket_path_from_herdr_status(monkeypatch):
    import subprocess

    from herdr_bridge.client import detect_socket_path
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)

    class FakeCompleted:
        stdout = "herdr 0.7.4\nsocket: /run/herdr/test.sock\npid: 1234\n"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: FakeCompleted())
    assert detect_socket_path() == "/run/herdr/test.sock"


def test_detect_socket_path_raises_when_no_status_output(monkeypatch):
    import subprocess

    from herdr_bridge.client import detect_socket_path
    from herdr_bridge.errors import HerdrConnectionError
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    import pytest

    class FakeCompletedNoSocket:
        stdout = "herdr 0.7.4\npid: 1234\n"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: FakeCompletedNoSocket())
    with pytest.raises(HerdrConnectionError, match="cannot detect"):
        detect_socket_path()


def test_request_empty_response_raises_connection_error(fake_herdr, monkeypatch):
    c = SocketClient(fake_herdr.socket_path)

    class EmptyFile:
        def readline(self, _size=None):
            return ""
        def write(self, _data):
            pass
        def flush(self):
            pass
        def close(self):
            pass

    class EmptyConn:
        def __init__(self):
            pass
        def settimeout(self, _t):
            pass
        def close(self):
            pass
        def makefile(self, *a, **kw):
            return EmptyFile()

    monkeypatch.setattr(c, "_connect", lambda timeout_sec=None: EmptyConn())
    with pytest.raises(HerdrConnectionError, match="closed connection"):
        c.ping()


def test_request_transport_oserror_raises_connection_error(fake_herdr, monkeypatch):
    c = SocketClient(fake_herdr.socket_path)

    class OSErrorFile:
        def write(self, _data):
            pass
        def flush(self):
            pass
        def readline(self, _size=None):
            raise OSError("broken pipe")
        def close(self):
            pass

    class OSErrorConn:
        def __init__(self):
            pass
        def settimeout(self, _t):
            pass
        def close(self):
            pass
        def makefile(self, *a, **kw):
            return OSErrorFile()

    monkeypatch.setattr(c, "_connect", lambda timeout_sec=None: OSErrorConn())
    with pytest.raises(HerdrConnectionError, match="transport failed"):
        c.ping()


def test_raise_for_error_non_dict_envelope():
    from herdr_bridge.client import SocketClient
    with pytest.raises(HerdrConnectionError, match="protocol violation"):
        SocketClient._raise_for_error("not a dict")


def test_raise_for_error_non_dict_error_field():
    from herdr_bridge.client import SocketClient
    with pytest.raises(HerdrConnectionError, match="protocol violation"):
        SocketClient._raise_for_error({"error": "just a string"})


def test_safe_close_handles_oserror_on_file():
    from herdr_bridge.client import _safe_close

    class BadFile:
        def close(self):
            raise OSError("close failed")
    # Must not raise an exception
    _safe_close(BadFile(), None)


def test_safe_close_handles_oserror_on_socket():
    from herdr_bridge.client import _safe_close

    class BadSocket:
        def close(self):
            raise OSError("close failed")
    # Must not raise an exception
    _safe_close(None, BadSocket())


def test_socket_path_public_readonly_property():
    # B2-2: the actions layer must not reach into client._socket_path -- expose a public read-only property instead
    from herdr_bridge.client import SocketClient
    c = SocketClient("/run/herdr/mine.sock")
    assert c.socket_path == "/run/herdr/mine.sock"
    import pytest
    with pytest.raises(AttributeError):
        c.socket_path = "/tmp/x"
