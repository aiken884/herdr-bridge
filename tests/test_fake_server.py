# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import json
import socket

from herdr_bridge.testing import FakeHerdrServer


def _call(path: str, payload: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(path)
    f = s.makefile("rw", encoding="utf-8", newline="\n")
    f.write(json.dumps(payload) + "\n")
    f.flush()
    resp = json.loads(f.readline())
    extra = f.readline()  # the server should have already closed the connection -> EOF
    s.close()
    assert extra == ""
    return resp


def test_request_response_then_close():
    with FakeHerdrServer() as srv:
        resp = _call(srv.socket_path, {"id": "1", "method": "ping", "params": {}})
        assert resp["id"] == "1"
        assert resp["result"]["type"] == "pong"
        assert srv.requests[0]["method"] == "ping"


def test_unknown_method_error_envelope():
    with FakeHerdrServer() as srv:
        resp = _call(srv.socket_path, {"id": "2", "method": "no.such", "params": {}})
        assert resp["error"]["code"] == "invalid_request"


def test_subscribe_stays_open_and_receives_push():
    with FakeHerdrServer() as srv:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(srv.socket_path)
        f = s.makefile("rw", encoding="utf-8", newline="\n")
        f.write(json.dumps({
            "id": "s1", "method": "events.subscribe",
            "params": {"subscriptions": [{"type": "pane.created"}]},
        }) + "\n")
        f.flush()
        ack = json.loads(f.readline())
        assert ack["result"]["type"] == "subscription_started"
        srv.push_event("pane_created", {"pane": {"pane_id": "w1:p9"}})
        ev = json.loads(f.readline())
        assert ev["event"] == "pane_created"
        assert "id" not in ev
        s.close()
