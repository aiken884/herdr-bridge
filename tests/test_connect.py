# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Socket resolution safety for connect() (F-2, dogfooding P0).

Old behavior: when socket_path wasn't passed explicitly, it would silently
fall back to the env var HERDR_SOCKET_PATH. In automation contexts (where the
host shell carries another session's env), this would connect to the wrong
session with no trace. F-2 fix: an explicit path never touches env; falling
back to env now logs a WARNING (no longer silent); public read-only
resolved_socket_path/socket_source properties mean consumers don't need to
reach into _client.
"""

import logging
from types import SimpleNamespace

import pytest

from herdr_bridge.actions import BridgeActions, _resolve_socket
from herdr_bridge.audit import AuditLogger


def _stub_actions(socket_path="/run/herdr/x.sock"):
    return BridgeActions(SimpleNamespace(socket_path=socket_path),
                         SimpleNamespace(), AuditLogger(None))


# ---- _resolve_socket branching (F-2 core) --------------------------------


def test_explicit_socket_path_ignores_env_no_warning(monkeypatch, caplog):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/nonexistent/other-session.sock")
    with caplog.at_level(logging.WARNING, logger="herdr_bridge.actions"):
        path, source = _resolve_socket("/run/herdr/mine.sock", "herdr")
    assert (path, source) == ("/run/herdr/mine.sock", "explicit")
    assert "HERDR_SOCKET_PATH" not in caplog.text   # an explicit path must not touch env


def test_env_fallback_marks_source_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/run/herdr/from-env.sock")
    with caplog.at_level(logging.WARNING, logger="herdr_bridge.actions"):
        path, source = _resolve_socket(None, "herdr")
    assert (path, source) == ("/run/herdr/from-env.sock", "env")
    assert "HERDR_SOCKET_PATH" in caplog.text        # silent misconnect -> observable WARNING


def test_no_socket_no_env_falls_to_detect(monkeypatch):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setattr("herdr_bridge.actions.detect_socket_path",
                        lambda herdr_bin: "/detected/by-status.sock")
    assert _resolve_socket(None, "herdr") == ("/detected/by-status.sock",
                                              "detected")


# ---- BridgeActions public properties (F-2: no need to reach into _client) -


def test_resolved_socket_path_public_and_readonly():
    actions = _stub_actions("/run/herdr/mine.sock")
    assert actions.resolved_socket_path == "/run/herdr/mine.sock"
    # An instance not created via connect() doesn't know its path source --
    # it must honestly default to "unknown" (only the connect() path
    # overrides it to explicit/env/detected)
    assert actions.socket_source == "unknown"
    with pytest.raises(AttributeError):
        actions.resolved_socket_path = "/tmp/x"      # property has no setter
