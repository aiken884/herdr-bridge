# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""M0 capability-verification CLI. Runs against a real herdr server and produces
the data needed for the capability-verification notes."""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from typing import Any

from herdr_bridge.client import SocketClient, detect_socket_path
from herdr_bridge.schema import SchemaStore, check_server_compat, fetch_schema_via_cli


def cmd_info(client: SocketClient) -> None:
    info = check_server_compat(client)
    store = SchemaStore.load(fetch_schema_via_cli())
    print(json.dumps({
        "server": info,
        "client_schema_methods": len(store.methods),
    }, indent=2, ensure_ascii=False))


def cmd_snapshot(client: SocketClient) -> None:
    snap = client.call("session.snapshot")  # call() unwraps the {type, snapshot} envelope
    agents = snap.get("agents", [])
    print(json.dumps({
        "version": snap.get("version"),
        "protocol": snap.get("protocol"),
        "workspaces": len(snap.get("workspaces", [])),
        "tabs": len(snap.get("tabs", [])),
        "panes": len(snap.get("panes", [])),
        "agents": len(agents),
        "brands": dict(collections.Counter(a.get("agent") for a in agents)),
        "statuses": dict(collections.Counter(a.get("agent_status") for a in agents)),
    }, indent=2, ensure_ascii=False))


def cmd_watch(client: SocketClient, seconds: int) -> None:
    snap = client.call("session.snapshot")  # call() unwraps the {type, snapshot} envelope
    subs: list[dict[str, Any]] = [
        {"type": t} for t in (
            "pane.created", "pane.closed", "pane.focused",
            "pane.exited", "pane.agent_detected",
        )
    ]
    subs += [{"type": "pane.agent_status_changed", "pane_id": p["pane_id"]}
             for p in snap.get("panes", [])]
    print(f"watching {len(subs)} subscriptions for {seconds}s …", file=sys.stderr)
    sub = client.subscribe(subs, on_event=lambda e, d: print(
        json.dumps({"ts": time.time(), "event": e, "data": d}, ensure_ascii=False)))
    time.sleep(seconds)
    sub.close()


def cmd_control_probe(client: SocketClient, socket_path: str,
                      allow_default_session: bool) -> None:
    """Verify the actual behavior of the authority/takeover semantics family
    (the terminal session API doesn't exist as of 0.7.4).

    This performs WRITE operations, so Global Constraint 15's hard guard applies:
    the target must be a named session's socket (path contains /sessions/); the
    default session is always refused unless explicitly overridden with
    --allow-default-session (intended only for maintainers outside Aiken's
    environment).
    """
    if "/sessions/" not in socket_path and not allow_default_session:
        raise SystemExit(
            "control-probe performs WRITE operations and refuses to target the "
            f"default session socket ({socket_path}).\n"
            "Start a sandbox first:  herdr --session bridge-test server\n"
            "then:  HERDR_SOCKET_PATH=~/.config/herdr/sessions/bridge-test/herdr.sock "
            "python -m herdr_bridge.probe control-probe\n"
            "(override only with --allow-default-session)")
    snap = client.call("session.snapshot")  # call() unwraps the {type, snapshot} envelope
    panes = snap.get("panes", [])
    if not panes:
        raise SystemExit("no panes to probe")
    pane_id = panes[0]["pane_id"]
    for method, params in [
        ("pane.clear_agent_authority", {"pane_id": pane_id}),
        ("pane.release_agent", {"pane_id": pane_id, "source": "herdr-bridge-probe",
                                "agent": "probe"}),
    ]:
        try:
            result = client.call(method, params)
            print(f"{method}: OK {json.dumps(result, ensure_ascii=False)[:200]}")
        except Exception as exc:  # noqa: BLE001 — the probe needs to record every outcome
            print(f"{method}: {type(exc).__name__}: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="herdr_bridge.probe")
    ap.add_argument("command", choices=["info", "snapshot", "watch", "control-probe"])
    ap.add_argument("--socket", default=None)
    ap.add_argument("--watch-seconds", type=int, default=120)
    ap.add_argument("--allow-default-session", action="store_true",
                    help="override the sandbox guard for control-probe (writes!)")
    args = ap.parse_args()
    socket_path = args.socket or detect_socket_path()
    client = SocketClient(socket_path)
    if args.command == "info":
        cmd_info(client)
    elif args.command == "snapshot":
        cmd_snapshot(client)
    elif args.command == "watch":
        cmd_watch(client, args.watch_seconds)
    elif args.command == "control-probe":
        cmd_control_probe(client, socket_path, args.allow_default_session)


if __name__ == "__main__":
    main()
