# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""One NDJSON line -> normalized `AcpEvent` (the R4 tolerant reader).

M0 spike evidence (m0-acp-spike-evidence.md §5.2): the NDJSON envelope
described in the acpx README (fields like `eventVersion`/`seq`/`stream`)
**does not exist at all** in the actually-tested 0.12.0 — `--format json`
output is the raw ACP JSON-RPC messages passed through line by line, mixed
in with acpx's own CLI status lines (not JSON, e.g.
`[acpx] session ... agent needs reconnect`).

This module therefore:
1. Parses the standard JSON-RPC 2.0 fields directly (`method`/`id`/`result`/
   `error`), without assuming any acpx-specific wrapper exists.
2. For `session/update` notifications, takes `params.update.sessionUpdate`
   as the `type` (ACP protocol vocabulary, not acpx vocabulary — design doc
   D-10).
3. For non-JSON lines (CLI chrome) and malformed JSON, always normalizes to
   `type="cli_status"`, `text=<the raw line>` — **never raises** — this is
   the core promise of the tolerant reader (R4).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from herdr_bridge.acp.models import AcpEvent


def _chrome_event(line: str) -> AcpEvent:
    return AcpEvent(type="cli_status", session_id=None, text=line, raw={"line": line})


def parse_line(line: str) -> AcpEvent | None:
    """One NDJSON line -> `AcpEvent`. Blank lines return `None`; everything else always returns an event (never raises)."""
    stripped = line.strip()
    if not stripped:
        return None

    try:
        payload: dict[str, Any] = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return _chrome_event(stripped)

    if not isinstance(payload, dict):
        return _chrome_event(stripped)

    method = payload.get("method")

    if method == "session/update":
        params = payload.get("params", {}) or {}
        update = params.get("update", {}) or {}
        variant = update.get("sessionUpdate")
        text = None
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            text = content.get("text")
        return AcpEvent(
            type=variant if isinstance(variant, str) else "session_update",
            session_id=params.get("sessionId"),
            text=text,
            raw=payload,
        )

    if isinstance(method, str):
        params = payload.get("params", {}) or {}
        return AcpEvent(
            type=method,
            session_id=params.get("sessionId") if isinstance(params, dict) else None,
            text=None,
            raw=payload,
        )

    if "error" in payload:
        return AcpEvent(type="error", session_id=None, text=None, raw=payload)

    if "result" in payload:
        return AcpEvent(type="result", session_id=None, text=None, raw=payload)

    # Valid JSON with no method/result/error (unknown shape) — pass through, don't guess at meaning
    return AcpEvent(type="unknown", session_id=None, text=None, raw=payload)


def parse_stream(lines: Iterable[str]) -> Iterator[AcpEvent]:
    """Convert each line to an event; blank lines are skipped automatically (`None` filtered out)."""
    for line in lines:
        event = parse_line(line)
        if event is not None:
            yield event


def extract_final_result(events: Iterable[AcpEvent]) -> dict[str, Any] | None:
    """Take the last `result` carrying a `stopReason` from the event sequence (used by M1 `prompt()` to converge).

    `None` means the sequence has no terminal result (it may still be in
    progress, or the output was malformed and got absorbed by the tolerant
    reader — callers should handle this via their own timeout/connection-layer
    error path rather than guessing).
    """
    final: dict[str, Any] | None = None
    for ev in events:
        if ev.type == "result":
            result = ev.raw.get("result")
            if isinstance(result, dict) and "stopReason" in result:
                final = result
    return final
