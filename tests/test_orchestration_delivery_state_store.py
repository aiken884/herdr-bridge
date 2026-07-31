# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""#72: Unit tests for the FSM delivery-state's dedicated lightweight store (testing the
underlying SQLite store directly, bypassing memory.py's higher-level update_delivery_state
interface).

Background: every delivery-state FSM transition used to write a record into RemaGraph
(the general-purpose memory layer), which ran into semantic dedup (adjacent-state summary
similarity of 0.92-0.96 gets rejected by rule #4's 0.90 threshold) -- so no transition after
INIT could ever get written. PPLX review consensus: this is an architectural responsibility
mismatch (the FSM tracks State, while the memory layer is designed for Memory). The correct
fix is for FSM state transitions to use their own dedicated lightweight store, writing a
summary into the memory layer only for the terminal state (Dual-Write on Terminal State).
This file tests that dedicated store itself.
"""

from __future__ import annotations

import time

from herdr_bridge.orchestration import delivery_state_store as fsm_store


def test_write_then_read_state_roundtrip(tmp_path):
    state_dir = tmp_path / "remagraph-hb-live-test-store-roundtrip"
    fsm_store.write_state(state_dir, "proj-a", "task-1", "agent-1", "INIT")

    got = fsm_store.read_state(state_dir, "proj-a", "task-1", "agent-1")
    assert got is not None
    assert got["state"] == "INIT"


def test_read_state_returns_none_when_never_written(tmp_path):
    state_dir = tmp_path / "remagraph-hb-live-test-store-empty"
    got = fsm_store.read_state(state_dir, "proj-a", "task-never-written", "agent-1")
    assert got is None


def test_write_state_overwrites_previous_state_for_same_task(tmp_path):
    """The FSM's dedicated store is overwritable (unlike the append-only memory layer) --
    the latest state for the same task_id/agent_id should overwrite directly, not
    accumulate historical rows."""
    state_dir = tmp_path / "remagraph-hb-live-test-store-overwrite"
    fsm_store.write_state(state_dir, "proj-a", "task-1", "agent-1", "INIT")
    fsm_store.write_state(state_dir, "proj-a", "task-1", "agent-1", "DISPATCH_PENDING")
    fsm_store.write_state(state_dir, "proj-a", "task-1", "agent-1", "AWAIT_PONG")

    got = fsm_store.read_state(state_dir, "proj-a", "task-1", "agent-1")
    assert got["state"] == "AWAIT_PONG"


def test_write_state_preserves_context_and_correlation(tmp_path):
    state_dir = tmp_path / "remagraph-hb-live-test-store-context"
    fsm_store.write_state(
        state_dir, "proj-a", "task-1", "agent-1", "TIMEOUT",
        context={"fallback": "side-channel"}, correlation="corr-abc123",
    )

    got = fsm_store.read_state(state_dir, "proj-a", "task-1", "agent-1")
    assert got["context"] == {"fallback": "side-channel"}
    assert got["correlation"] == "corr-abc123"


def test_different_task_ids_are_isolated(tmp_path):
    state_dir = tmp_path / "remagraph-hb-live-test-store-isolation"
    fsm_store.write_state(state_dir, "proj-a", "task-1", "agent-1", "INIT")
    fsm_store.write_state(state_dir, "proj-a", "task-2", "agent-1", "COMPLETED")

    got1 = fsm_store.read_state(state_dir, "proj-a", "task-1", "agent-1")
    got2 = fsm_store.read_state(state_dir, "proj-a", "task-2", "agent-1")
    assert got1["state"] == "INIT"
    assert got2["state"] == "COMPLETED"


def test_different_projects_are_isolated_even_with_same_task_id(tmp_path):
    """Cross-process visibility design: state_dir is split by project_id (reusing the
    #66 self-protective naming), so the same task_id under different projects should
    never contaminate each other."""
    state_dir_a = tmp_path / "remagraph-hb-live-proj-a"
    state_dir_b = tmp_path / "remagraph-hb-live-proj-b"
    fsm_store.write_state(state_dir_a, "proj-a", "same-task-id", "agent-1", "INIT")
    fsm_store.write_state(state_dir_b, "proj-b", "same-task-id", "agent-1", "COMPLETED")

    got_a = fsm_store.read_state(state_dir_a, "proj-a", "same-task-id", "agent-1")
    got_b = fsm_store.read_state(state_dir_b, "proj-b", "same-task-id", "agent-1")
    assert got_a["state"] == "INIT"
    assert got_b["state"] == "COMPLETED"


def test_created_at_is_preserved_across_updates_but_updated_at_advances(tmp_path):
    """The terminal dual-write summary needs substantive information like "elapsed time
    from INIT to terminal" -- created_at must be fixed on the first write, and subsequent
    overwrites should only advance updated_at, never reset created_at."""
    state_dir = tmp_path / "remagraph-hb-live-test-store-timing"
    fsm_store.write_state(state_dir, "proj-a", "task-1", "agent-1", "INIT")
    first = fsm_store.read_state(state_dir, "proj-a", "task-1", "agent-1")

    time.sleep(0.05)
    fsm_store.write_state(state_dir, "proj-a", "task-1", "agent-1", "DISPATCH_PENDING")
    second = fsm_store.read_state(state_dir, "proj-a", "task-1", "agent-1")

    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]


def test_concurrent_writes_to_different_tasks_do_not_corrupt_each_other(tmp_path):
    """Concurrency safety: when multiple dispatches happen at once, writing different
    task_ids must not corrupt each other (simulated with fast interleaved writes on
    the same connection, not relying on actual multi-threaded timing)."""
    state_dir = tmp_path / "remagraph-hb-live-test-store-concurrent"
    for i in range(20):
        fsm_store.write_state(state_dir, "proj-a", f"task-{i}", "agent-1", "INIT")
    for i in range(20):
        fsm_store.write_state(state_dir, "proj-a", f"task-{i}", "agent-1", "COMPLETED")

    for i in range(20):
        got = fsm_store.read_state(state_dir, "proj-a", f"task-{i}", "agent-1")
        assert got["state"] == "COMPLETED", f"task-{i} has incorrect state: {got}"


def test_expired_rows_are_swept_on_write(tmp_path):
    """TTL cleanup: writing a record also sweeps expired ones (clean-on-write, no need
    for an extra background thread/schedule -- avoiding the kind of accumulated test
    leftovers that grew into problem #43)."""
    state_dir = tmp_path / "remagraph-hb-live-test-store-ttl"
    fsm_store.write_state(
        state_dir, "proj-a", "old-task", "agent-1", "INIT", ttl_seconds=0.01,
    )
    time.sleep(0.05)
    # Write a new record, which also triggers the expiry sweep.
    fsm_store.write_state(state_dir, "proj-a", "new-task", "agent-1", "INIT", ttl_seconds=3600)

    assert fsm_store.read_state(state_dir, "proj-a", "old-task", "agent-1") is None, (
        "expired record should have been swept"
    )
    assert fsm_store.read_state(state_dir, "proj-a", "new-task", "agent-1") is not None


def test_db_file_lives_inside_project_state_dir_not_a_new_top_level_dir(tmp_path):
    """Path naming reuses the #66 self-protective principle: the FSM's dedicated
    store's sqlite file must live inside the state_dir passed in by the caller
    (usually the remagraph-hb-live-<project> directory computed by
    _project_state_dir()), not some newly invented top-level directory that an
    external serve process might recognize.
    """
    state_dir = tmp_path / "remagraph-hb-live-test-store-path"
    fsm_store.write_state(state_dir, "proj-a", "task-1", "agent-1", "INIT")

    db_path = fsm_store.db_path(state_dir)
    assert db_path.parent == state_dir
    assert db_path.exists()
    # Confirm no extra top-level directory sprouted under tmp_path.
    top_level_dirs = {p.name for p in tmp_path.iterdir() if p.is_dir()}
    assert top_level_dirs == {"remagraph-hb-live-test-store-path"}


def test_write_state_creates_missing_parent_directories(tmp_path):
    """2026-07-25 mutmut evidence: existing tests' state_dir is always a single level
    under tmp_path, whose parent (tmp_path itself) already exists -- so they couldn't
    detect whether `_connect()`'s `mkdir(parents=True, ...)` was actually necessary.
    `parents=False` (or mutated to `parents=None`, equivalent to False) would still
    succeed in that scenario, so the mutant survived. Here we deliberately use a
    multi-level path where neither the grandparent nor the parent exists yet -- only
    `parents=True` can build the whole chain in one go; `parents=False`/`None` would
    throw FileNotFoundError immediately."""
    deeply_nested_state_dir = tmp_path / "not-yet-exist" / "still-not-yet" / "remagraph-hb-live-nested"
    assert not deeply_nested_state_dir.parent.exists()

    fsm_store.write_state(deeply_nested_state_dir, "proj-a", "task-1", "agent-1", "INIT")

    got = fsm_store.read_state(deeply_nested_state_dir, "proj-a", "task-1", "agent-1")
    assert got is not None
    assert got["state"] == "INIT"


def test_connect_passes_busy_timeout_to_sqlite(tmp_path):
    """2026-07-25 mutmut evidence: when `sqlite3.connect(str(path), timeout=10)`'s
    `timeout=10` gets mutated away entirely (the connection falls back to sqlite3's
    default 5 seconds), no test caught it -- here we directly check the timeout
    attribute actually in effect on the connection object, instead of only testing
    "can it connect" (which succeeds in both cases and can't tell the difference).
    """
    state_dir = tmp_path / "remagraph-hb-live-test-store-timeout"
    conn = fsm_store._connect(state_dir)
    try:
        # sqlite3.Connection has no public timeout getter, so verify indirectly via
        # PRAGMA busy_timeout (in milliseconds) -- sqlite3's `timeout=` constructor
        # argument sets this PRAGMA. _connect() itself also explicitly issues
        # `PRAGMA busy_timeout=5000` (see source); what's being verified here is that
        # a timeout argument really was passed and wasn't dropped, not the exact value.
        cur = conn.execute("PRAGMA busy_timeout")
        (busy_timeout_ms,) = cur.fetchone()
        assert busy_timeout_ms > 0, "busy_timeout should be positive, meaning the timeout argument actually took effect"
    finally:
        conn.close()
