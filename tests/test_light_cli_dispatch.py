# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for herdr-commander dispatch -- a thin CLI wrapper that calls
AcpRouter.dispatch_with_memory_confirm() under the hood, so the official
dispatch path is as easy to use as a raw command.
"""

from __future__ import annotations

import time

from herdr_bridge.light.cli import build_parser, main


def test_parser_dispatch_basic():
    p = build_parser()
    args = p.parse_args(["dispatch", "--target", "pane1", "hello world"])
    assert args.command == "dispatch"
    assert args.target == "pane1"
    assert args.prompt == "hello world"
    assert args.pane_id is None


def test_parser_dispatch_with_pane_id_and_project():
    p = build_parser()
    args = p.parse_args(
        ["dispatch", "--pane-id", "w1:p1", "--project", "example-downstream-project", "do X"]
    )
    assert args.pane_id == "w1:p1"
    assert args.project == "example-downstream-project"


def test_dispatch_requires_target_or_pane_id(capsys):
    # Echoes dispatch discipline: auto-routing is forbidden -- without an
    # explicit target/pane_id it must error out, not fail silently
    rc = main(["dispatch", "some prompt without a target"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "target" in err or "pane" in err


def test_dispatch_reports_pong_confirmed(monkeypatch, capsys):
    calls = {}

    class _FakeRouter:
        def dispatch_with_memory_confirm(self, prompt, *, target=None, pane_id=None, name=None):
            calls["prompt"] = prompt
            calls["target"] = target
            calls["pane_id"] = pane_id
            calls["name"] = name
            return {
                "ok": True,
                "confirmed_via": "pong",
                "pong_confirmed": True,
                "side_confirmed": False,
                "routed_to": "pane1",
                "task_id": "herdr-bridge-tower-confirm-1",
            }

    def _fake_factory(*, project="herdr-router", additional_paths=None):
        calls["project"] = project
        return _FakeRouter()

    monkeypatch.setattr(
        "herdr_bridge.acp.router.create_herdr_router", _fake_factory
    )

    rc = main(["dispatch", "--target", "pane1", "--project", "herdr-bridge", "do X"])
    assert rc == 0
    assert calls["prompt"] == "do X"
    assert calls["target"] == "pane1"
    assert calls["project"] == "herdr-bridge"

    out = capsys.readouterr().out
    assert "confirmed_via=pong" in out
    assert "pong_confirmed=True" in out


def test_dispatch_none_confirmed_is_nonzero(monkeypatch, capsys):
    class _FakeRouter:
        def dispatch_with_memory_confirm(self, prompt, *, target=None, pane_id=None, name=None):
            return {
                "ok": True,
                "confirmed_via": "none",
                "pong_confirmed": False,
                "side_confirmed": False,
                "routed_to": "pane1",
                "task_id": "t1",
            }

    monkeypatch.setattr(
        "herdr_bridge.acp.router.create_herdr_router",
        lambda **kw: _FakeRouter(),
    )

    rc = main(["dispatch", "--target", "pane1", "do X"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "confirm" in err.lower()


def test_dispatch_echo_fallback_confirmed_via_is_also_nonzero(monkeypatch, capsys):
    """#60: after AcpRouter.dispatch_with_memory_confirm() was fixed, the
    confirmed_via value for the echo fallback case changed from the generic
    "none" to the more specific "echo-fallback". cmd_dispatch() originally
    only errored out when confirmed_via == "none" -- if that check weren't
    updated too, this more specific new value would instead get misread by
    the CLI as "all fine", regressing from "report an error" to "silently
    report success", which is exactly the silent-success problem this fix is
    meant to address.

    After PPLX consensus review (see
    docs/decisions/acp-layer-status-20260725.md): the echo fallback case now
    has `ok` as `False` too (no longer a "flagged success"), so the fake data
    here has been updated to reflect what router.py actually returns now
    (ok=False, degraded=True, delivery_status=not_attempted).
    """
    class _FakeRouter:
        def dispatch_with_memory_confirm(self, prompt, *, target=None, pane_id=None, name=None):
            return {
                "ok": False,
                "confirmed_via": "echo-fallback",
                "echo_fallback": True,
                "acp_unavailable": True,
                "degraded": True,
                "delivery_status": "not_attempted",
                "pong_confirmed": False,
                "side_confirmed": False,
                "routed_to": "pane1",
                "task_id": "t1",
            }

    monkeypatch.setattr(
        "herdr_bridge.acp.router.create_herdr_router",
        lambda **kw: _FakeRouter(),
    )

    rc = main(["dispatch", "--target", "pane1", "do X"])
    assert rc == 4, "echo fallback must not be treated by the CLI as a confirmed-delivery success case -- it must go through the dedicated degraded error path"

    err = capsys.readouterr().err
    assert "not_attempted" in err
    assert "never attempted" in err


def test_dispatch_ok_false_is_nonzero(monkeypatch):
    class _FakeRouter:
        def dispatch_with_memory_confirm(self, prompt, *, target=None, pane_id=None, name=None):
            return {"ok": False, "error": "boom", "confirmed_via": "none"}

    monkeypatch.setattr(
        "herdr_bridge.acp.router.create_herdr_router",
        lambda **kw: _FakeRouter(),
    )

    rc = main(["dispatch", "--target", "pane1", "do X"])
    assert rc != 0


def test_dispatch_cli_timeout(monkeypatch, capsys):
    class _FakeRouter:
        def dispatch_with_memory_confirm(self, prompt, *, target=None, pane_id=None, name=None):
            time.sleep(0.3)
            return {"ok": True, "confirmed_via": "pong", "pong_confirmed": True}

    monkeypatch.setattr(
        "herdr_bridge.acp.router.create_herdr_router",
        lambda **kw: _FakeRouter(),
    )

    rc = main(
        ["dispatch", "--target", "pane1", "--timeout", "0.05", "do X"]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "timeout" in err.lower()
