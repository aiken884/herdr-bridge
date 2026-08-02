# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Dedicated lightweight store for the Signal ACK state machine (design doc
§3.1/§3.7), keyed by `message_id`.

Mirrors `delivery_state_store.py`'s architecture and rationale exactly: the
Signal ACK chain (Accepted -> Injected -> Seen -> Accepted-for-work ->
Completed, plus the escalation markers `daemon_unreachable` /
`injection_unconfirmed` / `needs_attention`) is multi-agent-coordination
**State** — lifecycle measured in seconds to minutes, needs exact lookup by
message_id — not **Memory** (RemaGraph's semantic-search, weeks-to-months
layer). Writing every ACK transition into RemaGraph would hit the same
architectural mismatch `delivery_state_store.py`'s docstring documents for the
delivery-state FSM (PPLX review consensus, #72): a dedicated store is the
correct fix, not a workaround.

Path: lives under `orchestration._state_paths.signal_state_dir()` (Signal's
own state directory), not inside a `remagraph-*` directory — Signal state
isn't RemaGraph data.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

_DB_FILENAME = "signal-ack-state.sqlite3"

#: The ACK chain lives for seconds to low minutes; 24h is a generous
#: debugging-friendly retention window, not a design statement about how long
#: a wake signal "should" take to resolve.
DEFAULT_TTL_SECONDS = 24 * 3600.0

#: Defense-in-depth for find_active_by_target() (2026-08-01 PPLX-reviewed
#: fix): daemon.py now advances "injected" straight to "completed" itself
#: (see orchestration/memory.py's SIGNAL_STATE_TRANSITIONS docstring), so in
#: the normal case a record stops looking "in flight" within milliseconds.
#: But if the daemon crashes between those two writes, the record would
#: otherwise stay "in flight" forever and permanently block any later send
#: to the same (to_project, inbox_ref) -- exactly the bug this whole fix is
#: for. A record older than this is no longer treated as active, regardless
#: of its state; nothing is deleted or mutated, so the full history stays
#: readable via read_state()/list_recent().
DEFAULT_INFLIGHT_TIMEOUT_SECONDS = 30 * 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_state (
    message_id TEXT PRIMARY KEY,
    from_project TEXT NOT NULL,
    to_project TEXT NOT NULL,
    inbox_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    accepted_at REAL,
    injected_at REAL,
    seen_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL
)
"""


def db_path(state_dir: Path) -> Path:
    return Path(state_dir) / _DB_FILENAME


def _connect(state_dir: Path) -> sqlite3.Connection:
    path = db_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(_SCHEMA)
    return conn


def write_state(
    state_dir: Path,
    message_id: str,
    *,
    from_project: str,
    to_project: str,
    inbox_ref: str,
    state: str,
    attempt_count: int | None = None,
    accepted_at: float | None = None,
    injected_at: float | None = None,
    seen_at: float | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> None:
    """Insert or overwrite the latest ACK state for `message_id`.

    `accepted_at`/`injected_at`/`seen_at` are sticky once set: passing `None`
    for one of them does NOT clear a previously recorded timestamp — a caller
    moving the state from `injected` to `seen` only needs to pass `seen_at`,
    the earlier `accepted_at`/`injected_at` stay intact (`COALESCE` against
    the existing row). `attempt_count` follows the same sticky rule when not
    explicitly overridden.
    """
    now = time.time()
    conn = _connect(state_dir)
    try:
        conn.execute(
            """
            INSERT INTO signal_state
                (message_id, from_project, to_project, inbox_ref, state,
                 attempt_count, accepted_at, injected_at, seen_at,
                 created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                state=excluded.state,
                attempt_count=COALESCE(excluded.attempt_count, signal_state.attempt_count),
                accepted_at=COALESCE(excluded.accepted_at, signal_state.accepted_at),
                injected_at=COALESCE(excluded.injected_at, signal_state.injected_at),
                seen_at=COALESCE(excluded.seen_at, signal_state.seen_at),
                updated_at=excluded.updated_at,
                expires_at=excluded.expires_at
            """,
            (
                message_id, from_project, to_project, inbox_ref, state,
                attempt_count if attempt_count is not None else 0,
                accepted_at, injected_at, seen_at,
                now, now, now + ttl_seconds,
            ),
        )
        conn.execute("DELETE FROM signal_state WHERE expires_at < ?", (now,))
        conn.commit()
    finally:
        conn.close()


def read_state(state_dir: Path, message_id: str) -> dict[str, Any] | None:
    path = db_path(state_dir)
    if not path.exists():
        return None
    conn = _connect(state_dir)
    try:
        row = conn.execute(
            """
            SELECT from_project, to_project, inbox_ref, state, attempt_count,
                   accepted_at, injected_at, seen_at, created_at, updated_at
            FROM signal_state WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    (from_project, to_project, inbox_ref, state, attempt_count,
     accepted_at, injected_at, seen_at, created_at, updated_at) = row
    return {
        "message_id": message_id, "from_project": from_project, "to_project": to_project,
        "inbox_ref": inbox_ref, "state": state, "attempt_count": attempt_count,
        "accepted_at": accepted_at, "injected_at": injected_at, "seen_at": seen_at,
        "created_at": created_at, "updated_at": updated_at,
    }


def find_active_by_target(
    state_dir: Path, to_project: str, inbox_ref: str,
    *, exclude_message_id: str | None = None,
    inflight_timeout_seconds: float = DEFAULT_INFLIGHT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Most recent non-completed record for (to_project, inbox_ref) — the
    idempotency_key's underlying identity (§3.3: idempotency_key is a hash of
    exactly these two fields, so querying by them directly is equivalent and
    avoids a redundant stored column). Backs daemon.py's dedup check (§3.8
    acceptance test 5): a resend for the same target while one is already in
    flight must not trigger a second independent injection.

    `exclude_message_id` (2026-08-01 fix): pass the caller's own message_id
    to exclude it from the search. daemon.py never needs this (the incoming
    envelope's message_id has no row yet when it queries). outbound.py does:
    by the time it checks for dedup, it has already called mark_accepted()
    for its OWN message_id, so without this exclusion "ORDER BY updated_at
    DESC" would just find its own just-written row (the most recent update)
    instead of the real earlier in-flight one it's trying to detect.

    `inflight_timeout_seconds` (see its module constant's docstring): a
    non-completed record older than this is no longer considered active,
    so a stuck record can't block resends forever.
    """
    path = db_path(state_dir)
    if not path.exists():
        return None
    cutoff = time.time() - inflight_timeout_seconds
    conn = _connect(state_dir)
    try:
        row = conn.execute(
            """
            SELECT message_id, from_project, to_project, inbox_ref, state, attempt_count,
                   accepted_at, injected_at, seen_at, created_at, updated_at
            FROM signal_state
            WHERE to_project = ? AND inbox_ref = ? AND state != 'completed' AND updated_at >= ?
                  AND message_id != ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (to_project, inbox_ref, cutoff, exclude_message_id or ""),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "message_id": row[0], "from_project": row[1], "to_project": row[2], "inbox_ref": row[3],
        "state": row[4], "attempt_count": row[5], "accepted_at": row[6], "injected_at": row[7],
        "seen_at": row[8], "created_at": row[9], "updated_at": row[10],
    }


def list_recent(state_dir: Path, limit: int = 10) -> list[dict[str, Any]]:
    """Most-recently-updated records first — backs `herdr-commander signal status`."""
    path = db_path(state_dir)
    if not path.exists():
        return []
    conn = _connect(state_dir)
    try:
        rows = conn.execute(
            """
            SELECT message_id, from_project, to_project, inbox_ref, state, attempt_count,
                   accepted_at, injected_at, seen_at, created_at, updated_at
            FROM signal_state ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "message_id": r[0], "from_project": r[1], "to_project": r[2], "inbox_ref": r[3],
            "state": r[4], "attempt_count": r[5], "accepted_at": r[6], "injected_at": r[7],
            "seen_at": r[8], "created_at": r[9], "updated_at": r[10],
        }
        for r in rows
    ]
