# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for search_memories()/link_project() (2026-08-01): the `memory search`/
`memory status`/`memory link` counterparts to store_memory()/recall_memories().

search_memories() is the fix for a real cross-tower visibility gap discovered while
building it: herdr-bridge's own store/recall API only ever writes/reads the
`hb-live-` self-protected namespace (see _state_paths.project_state_dir()'s
docstring), so a message an external tower writes via the standard `remagraph`
convention was invisible to herdr-bridge's own memory API. See
docs/decisions/hb-live-namespace-search-20260801.md for the full architecture
decision (PPLX-consulted); these tests cover the dual-namespace merge that
implements it.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from herdr_bridge.errors import HerdrMemoryError
from herdr_bridge.orchestration import memory as rg
from herdr_bridge.orchestration._state_paths import standard_project_state_dir


def _write_standard_namespace_record(project_id, *, task_id, summary, kind="task_handoff", agent_id="external-tower"):
    """Simulates an external tower writing via the standard `remagraph`
    convention (no `hb-live-` prefix knowledge), using RemaGraph's own
    connect()/process_store() so the resulting DB has a real, current schema
    (FTS5 tables included) -- not a hand-rolled approximation that could
    drift from the real thing. Sets REMAGRAPH_STATE_DIR to the standard path
    first (matching what a real external caller does before writing) --
    RemaGraph's own safety_validate_project() requires it.
    """
    import os

    from remagraph.db import connect
    from remagraph.models import StoreRequest
    from remagraph.store import process_store

    state_dir = standard_project_state_dir(project_id)
    prev = os.environ.get("REMAGRAPH_STATE_DIR")
    os.environ["REMAGRAPH_STATE_DIR"] = str(state_dir)
    try:
        conn = connect(state_dir=state_dir, project_id=project_id)
        try:
            resp = process_store(
                StoreRequest(
                    project_id=project_id, task_id=task_id, agent_id=agent_id,
                    kind=kind, summary=summary,
                    handoff_note=f"handoff note detail for: {summary}",
                    tags=[], learnings=[summary[:200]],
                ),
                conn,
            )
            assert resp.status == "stored", f"test fixture write was rejected: {resp}"
        finally:
            conn.close()
    finally:
        if prev is None:
            os.environ.pop("REMAGRAPH_STATE_DIR", None)
        else:
            os.environ["REMAGRAPH_STATE_DIR"] = prev


# ---------------------------------------------------------------------------
# search_memories: sees both namespaces
# ---------------------------------------------------------------------------


def test_search_memories_finds_record_written_via_standard_namespace_only():
    """The core regression this feature exists for: a message written by an
    external actor using the plain `remagraph` convention (no `hb-live-`
    prefix) must be findable via search_memories(), even though
    store_memory()/recall_memories() would never see it."""
    _write_standard_namespace_record(
        "search-cov-proj", task_id="external-task-handoff",
        summary="please review and apply the readme change requested",
    )

    results = rg.search_memories("readme change", project_id="search-cov-proj")

    assert len(results) == 1
    assert results[0]["task_id"] == "external-task-handoff"
    assert results[0]["_namespace"] == "standard"


def test_search_memories_finds_record_written_via_store_memory():
    """The other half: herdr-bridge's own store_memory() (isolated hb-live-
    namespace) must still be found by search_memories() -- this feature adds
    a second source, it must not regress the existing one."""
    rg.store_memory(
        "own-task", "own-agent", summary="internal status update about deployment",
        project_id="search-cov-proj2", learnings=["deployed successfully"],
    )

    results = rg.search_memories("deployment", project_id="search-cov-proj2")

    assert len(results) == 1
    assert results[0]["task_id"] == "own-task"
    assert results[0]["_namespace"] == "isolated"


def test_search_memories_merges_and_sorts_both_namespaces_by_recency():
    rg.store_memory("own-task", "own-agent", summary="isolated-side record about a merge test", project_id="search-cov-proj3", learnings=["x"])
    _write_standard_namespace_record(
        "search-cov-proj3", task_id="external-task", summary="standard-side record about a merge test"
    )

    results = rg.search_memories("record", project_id="search-cov-proj3", top_k=10)

    namespaces = {r["_namespace"] for r in results}
    assert namespaces == {"isolated", "standard"}


def test_search_memories_rejects_default_project_id():
    with pytest.raises(HerdrMemoryError):
        rg.search_memories("x", project_id="default")


def test_search_memories_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "off")
    assert rg.search_memories("x", project_id="search-cov-off") == []


def test_search_memories_standard_namespace_missing_db_degrades_to_empty():
    """No external tower has ever written for this project -- the standard
    path simply doesn't exist yet. Must not raise or affect the isolated
    side's results."""
    rg.store_memory("task-nodb", "agent-nodb", summary="only isolated data exists here for this test", project_id="search-cov-nodb", learnings=["x"])

    results = rg.search_memories("isolated data", project_id="search-cov-nodb")

    assert len(results) == 1
    assert results[0]["_namespace"] == "isolated"


def test_search_memories_all_projects_ignores_project_filter_within_each_namespace():
    _write_standard_namespace_record(
        "search-cov-shared", task_id="other-project-task", summary="own project's record for this shared-db test",
    )
    # A second row under a *different* project_id in the same standard DB file --
    # inserted directly via sqlite3 (bypassing RemaGraph's own write-path safety
    # valve, which by design refuses to let a caller write a mismatched
    # project_id into a given state_dir). This test only needs the row to exist
    # for read-side testing, not to exercise RemaGraph's write-path semantics.
    import sqlite3
    from datetime import UTC, datetime
    db_path = standard_project_state_dir("search-cov-shared") / "remagraph.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
    conn.execute(
        "INSERT INTO memories (id, project_id, task_id, agent_id, kind, summary, "
        "handoff_note, tags, learnings, status, timestamp, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("mem-other-project-row", "a-totally-different-project", "t2", "a2",
         "status_update", "other project's own record", "", "[]", "[]", "active", now, now, now),
    )
    conn.commit()
    conn.close()

    filtered = rg.search_memories("", project_id="search-cov-shared")
    unfiltered = rg.search_memories("", project_id="search-cov-shared", all_projects=True)

    assert len(filtered) == 1
    assert len(unfiltered) == 2


def test_search_memories_cli_fallback_merges_both_namespaces(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    _write_standard_namespace_record(
        "search-cov-clifallback", task_id="cli-fallback-external",
        summary="cli fallback standard side record for this test",
    )

    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run, \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"results": [{"id": "mem-iso-1", "task_id": "cli-fallback-isolated"}]}), ""
        )
        results = rg.search_memories("cli fallback", project_id="search-cov-clifallback")

    namespaces = {r["_namespace"] for r in results}
    assert namespaces == {"isolated", "standard"}


# ---------------------------------------------------------------------------
# link_project
# ---------------------------------------------------------------------------


def test_link_project_direct_import_calls_declare_project_edge(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", True)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    calls = []
    with patch("herdr_bridge.orchestration.memory._rg_declare_project_edge", side_effect=lambda *a: calls.append(a)):
        rg.link_project("herdr-bridge", "remagraph", "depends_on")
    assert calls == [("herdr-bridge", "remagraph", "depends_on")]


def test_link_project_cli_fallback_invokes_remagraph_link(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
        rg.link_project("herdr-bridge", "remagraph", "sibling")
    cmd = mock_run.call_args[0][0]
    assert cmd == ["remagraph", "link", "--from", "herdr-bridge", "--to", "remagraph", "--relation", "sibling"]


def test_link_project_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "off")
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run:
        rg.link_project("a", "b", "sibling")
    mock_run.assert_not_called()
