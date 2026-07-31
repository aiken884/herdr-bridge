# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Dedicated lightweight store for FSM delivery state (#72, PPLX review consensus).

## Why this module exists

Previously, every state transition in the delivery-state FSM
(`update_delivery_state`/`get_delivery_state` in `orchestration/memory.py`)
wrote a `status_update` record into RemaGraph (the general-purpose memory
layer). But RemaGraph's semantic dedup (rule #4: reject when adjacent-record
similarity is >=0.90) almost always blocked the summary for consecutive
transitions of the same task_id (measured similarity landed at 0.92-0.96),
so nothing after INIT could ever get written — the FSM chain was effectively
broken.

The PPLX review consensus was explicit that this is an **architectural
layer mismatch**, not an implementation detail you can work around by
fudging summary wording or hunting for a memory-layer exception parameter:

- Multi-agent coordination terminology splits data into three layers —
  Context (what's visible at inference time) / Memory (long-term knowledge,
  weeks to months, searched by semantic similarity) / **State (real-time
  coordination state shared across agents, lives for minutes, needs exact
  lookup)**. The FSM tracks State, not Memory.
- The memory layer is built for long-term knowledge, not for something
  that lives and dies within minutes.

The correct fix: FSM state transitions now go through this dedicated
lightweight store (this module). Only terminal states
(COMPLETED/DEGRADED/DISPATCH_FAILED) get an extra summary written into the
memory layer by `memory.py` (Dual-Write on Terminal State — see
`update_delivery_state` in `memory.py`).

## Design tradeoffs

- **Cross-process visibility**: the command tower and fleet members may be
  different processes, so this uses a single SQLite table (not an
  in-process dict) — any process on the same machine that knows the
  project_id/task_id/agent_id can look up the latest state.
- **Overwritable, not append-only**: only the latest state per
  `(project_id, task_id, agent_id)` is kept (`INSERT ... ON CONFLICT DO
  UPDATE`), unlike the memory layer which accumulates history — the FSM
  only cares about "what's the current state"; keeping the full transition
  history isn't this store's job.
- **Concurrency-safe**: WAL mode + busy_timeout so concurrent dispatches
  don't block each other's writes for long; SQLite's single-file,
  single-writer model is more than enough for this data volume (a handful
  of state records per task).
- **TTL and cleanup**: expired records get swept "in passing" on every
  write (`ttl_seconds`, default 24 hours — the FSM's lifecycle is measured
  in minutes, so 24 hours is already a generous debugging-friendly
  retention window). No extra background thread or scheduled job — lesson
  from the 2026-07-25 #43 postmortem: tests left 127 leftover directories
  behind in `~/.local/state`, teaching us that any persistent/scheduled
  mechanism will run out of control in test environments; passive
  clean-as-you-go is safer.
- **Path naming follows the #66 self-protection principle**: the sqlite
  file lives inside the `state_dir` the caller passes in (usually the
  `remagraph-hb-live-<project>` directory computed by
  `orchestration._state_paths.project_state_dir()`), not a new top-level
  directory of its own — this avoids an unrelated external `remagraph
  serve` process recognizing it via the standard naming rule and wiping it
  out.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_DB_FILENAME = "fsm-delivery-state.sqlite3"

# The FSM state transition lifecycle is measured in minutes; 24 hours is a generous
# debugging-friendly retention window, not a design statement about how long the FSM
# "should" live. The full summary for terminal states is separately dual-written into
# the memory layer for long-term retention.
DEFAULT_TTL_SECONDS = 24 * 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_state (
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    state TEXT NOT NULL,
    context TEXT,
    correlation TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (project_id, task_id, agent_id)
)
"""


def db_path(state_dir: Path) -> Path:
    """Path to the FSM's dedicated sqlite file — lives inside `state_dir`, not a separate new directory."""
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
    project_id: str,
    task_id: str,
    agent_id: str,
    state: str,
    *,
    context: dict[str, Any] | None = None,
    correlation: str | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> None:
    """Write (or overwrite) the latest FSM state for `(project_id, task_id, agent_id)`.

    `expires_at` is recorded at write time (`now + ttl_seconds`) — it's this
    record's own expiry point and isn't affected by later write calls
    (which might use a different `ttl_seconds`). While we're at it, sweep
    every expired (`expires_at < now`) record out of this sqlite file —
    clean-as-you-go, no extra background thread or scheduled job.
    """
    now = time.time()
    conn = _connect(state_dir)
    try:
        conn.execute(
            """
            INSERT INTO delivery_state
                (project_id, task_id, agent_id, state, context, correlation,
                 created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, task_id, agent_id) DO UPDATE SET
                state=excluded.state,
                context=excluded.context,
                correlation=excluded.correlation,
                updated_at=excluded.updated_at,
                expires_at=excluded.expires_at
            """,
            (
                project_id,
                task_id,
                agent_id,
                state,
                json.dumps(context, ensure_ascii=False) if context else None,
                correlation,
                now,
                now,
                now + ttl_seconds,
            ),
        )
        conn.execute("DELETE FROM delivery_state WHERE expires_at < ?", (now,))
        conn.commit()
    finally:
        conn.close()


def read_state(
    state_dir: Path, project_id: str, task_id: str, agent_id: str
) -> dict[str, Any] | None:
    """Read the current FSM state for `(project_id, task_id, agent_id)`; returns None if not found."""
    path = db_path(state_dir)
    if not path.exists():
        return None
    conn = _connect(state_dir)
    try:
        row = conn.execute(
            """
            SELECT state, context, correlation, created_at, updated_at
            FROM delivery_state
            WHERE project_id = ? AND task_id = ? AND agent_id = ?
            """,
            (project_id, task_id, agent_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    state, context_json, correlation, created_at, updated_at = row
    return {
        "state": state,
        "context": json.loads(context_json) if context_json else None,
        "correlation": correlation,
        "created_at": created_at,
        "updated_at": updated_at,
    }
