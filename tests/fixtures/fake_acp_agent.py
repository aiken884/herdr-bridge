#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""stdlib-only fake ACP agent (N3: for CI contract tests, no real opencode binary or model needed).

Implements only the minimal ACP subset needed for acpx to drive one
`sessions ensure` plus one prompt (`initialize`/`session/new`/`session/load`/
`session/resume`/`session/prompt`). Framing is newline-delimited JSON-RPC
2.0 — confirmed directly against `@agentclientprotocol/sdk`'s `stream.js`
(`ndJsonStream`), not LSP-style Content-Length framing.

There's exactly one thing this verifies: when this script is spawned by acpx
as its `--agent` target, does the `OPENCODE_CONFIG` environment variable it
reads (and that file's contents) match what the caller side
(`build_acpx_argv_and_env`/`write_session_config`) expects? That proves the
argv/env assembly path can be tested in CI without a real opencode binary or
model. argv itself is ignored (acpx's `--agent` escape hatch always appends a
positional `acp` argument; this script only ever talks over stdin/stdout and
doesn't care about it).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _read_opencode_config() -> str:
    config_path = os.environ.get("OPENCODE_CONFIG", "")
    if not config_path:
        return "OPENCODE_CONFIG=<unset>"
    try:
        with open(config_path) as f:
            content = f.read()
    except OSError as exc:
        return f"OPENCODE_CONFIG={config_path} <read error: {exc}>"
    return f"OPENCODE_CONFIG={config_path} CONTENT={content}"


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {"loadSession": True},
                        "authMethods": [],
                        "agentInfo": {"name": "fake-acp-agent", "version": "0.0.0"},
                    },
                }
            )
        elif method == "session/new":
            # Each fresh session gets its own id — a fixed default here would
            # make acpx's session store (keyed by sessionId, not cwd) collide
            # across unrelated test/caller invocations that all resolve to the
            # same hardcoded id (found the hard way: two contract tests sharing
            # one hardcoded "fake-session-1" caused acpx to silently resume the
            # first test's session/config for the second).
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": f"fake-{uuid.uuid4()}"}})
        elif method in ("session/load", "session/resume"):
            params = request.get("params") or {}
            session_id = params.get("sessionId", f"fake-{uuid.uuid4()}")
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": session_id}})
        elif method == "session/prompt":
            params = request.get("params") or {}
            session_id = params.get("sessionId", "fake-session-1")

            # Test trigger detection: check the raw request JSON for trigger strings.
            raw_request = json.dumps(request)
            if "$HANG$" in raw_request:
                # Simulate pre-generation hang: never respond to the prompt,
                # so no result with stopReason is ever sent.
                time.sleep(60)
                return
            stop_reason = "cancelled" if "$CANCEL$" in raw_request else "end_turn"

            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": _read_opencode_config()},
                        },
                    },
                }
            )
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": stop_reason}})
        elif method == "session/cancel":
            continue
        elif request_id is not None:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unhandled method: {method}"},
                }
            )


if __name__ == "__main__":
    main()
