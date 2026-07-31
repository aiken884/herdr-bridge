# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Fills existing coverage gaps in `src/herdr_bridge/orchestration/memory.py`.

Relationship to `tests/test_orchestration_remagraph_embedding.py`: that suite leans
end-to-end (actually exercising the embedded RemaGraph direct-import path); this one
specifically covers branches nobody was testing before -- mode switching (on/off), the
CLI fallback path (`_DIRECT_IMPORT=False`), schema-error retries, exception paths in
retention/metrics, and the migration logic in `migrate_herdr_bridge_memories`. Everything
is isolated with monkeypatch/mock and doesn't depend on any real LLM API; the
`_DIRECT_IMPORT` path still does real local filesystem operations via
`subprocess`/`sqlite3` (isolated under `tmp_path`), without connecting to any external
service.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from herdr_bridge.errors import HerdrMemoryError
from herdr_bridge.orchestration import memory as rg

# ---------------------------------------------------------------------------
# Mode control: _is_remagraph_available / is_remagraph_enabled / get_remagraph_mode
# ---------------------------------------------------------------------------


def test_mode_off_disables_everything(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "off")
    assert rg._is_remagraph_available() is False
    assert rg.is_remagraph_enabled() is False


def test_mode_on_forces_enabled(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    assert rg._is_remagraph_available() is True
    assert rg.is_remagraph_enabled() is True


def test_get_remagraph_mode_reflects_module_state(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "auto")
    assert rg.get_remagraph_mode() == "auto"


def test_is_remagraph_available_falls_back_to_which_when_no_direct_import(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "auto")
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg.shutil, "which", lambda name: None)
    assert rg._is_remagraph_available() is False
    monkeypatch.setattr(rg.shutil, "which", lambda name: "/usr/bin/remagraph")
    assert rg._is_remagraph_available() is True


# ---------------------------------------------------------------------------
# _ensure_remagraph_project
# ---------------------------------------------------------------------------


def test_ensure_remagraph_project_creates_state_dir_and_sets_env(monkeypatch, tmp_path):
    monkeypatch.delenv("REMAGRAPH_STATE_DIR", raising=False)
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
        rg._ensure_remagraph_project("cov-test-project")
    assert os.environ["REMAGRAPH_PROJECT"] == "cov-test-project"
    # state dir deliberately carries the "hb-live-" self-protective prefix (see the
    # docstring of _state_paths.project_state_dir, 2026-07-25 #66 evidence), not the
    # standard "remagraph-<project>" naming.
    assert "remagraph-hb-live-cov-test-project" in os.environ["REMAGRAPH_STATE_DIR"]
    mock_run.assert_called_once()


def test_ensure_remagraph_project_force_reinit_removes_existing_db(monkeypatch, tmp_path):
    monkeypatch.delenv("REMAGRAPH_STATE_DIR", raising=False)
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    state_dir = tmp_path / ".local" / "state" / "remagraph-hb-live-cov-test-2"
    state_dir.mkdir(parents=True)
    db_path = state_dir / "remagraph.db"
    db_path.write_text("stale")
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
        rg._ensure_remagraph_project("cov-test-2", force_reinit=True)
    assert not db_path.exists()  # unlinked, and subprocess is mocked so it won't be regenerated


def test_ensure_remagraph_project_skips_init_when_db_already_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("REMAGRAPH_STATE_DIR", raising=False)
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    state_dir = tmp_path / ".local" / "state" / "remagraph-hb-live-cov-test-3"
    state_dir.mkdir(parents=True)
    (state_dir / "remagraph.db").write_text("already-there")
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run:
        rg._ensure_remagraph_project("cov-test-3")
    mock_run.assert_not_called()


def test_ensure_remagraph_project_reads_env_sh_when_present(monkeypatch, tmp_path):
    monkeypatch.delenv("REMAGRAPH_STATE_DIR", raising=False)
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
        rg._ensure_remagraph_project("cov-test-4")
    state_dir = Path(os.environ["REMAGRAPH_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)  # subprocess is mocked, so real remagraph init never gets to create the dir
    (state_dir / "env.sh").write_text(
        'export REMAGRAPH_STATE_DIR="/some/other/path"\nexport REMAGRAPH_PROJECT="other-project"\n'
    )
    (state_dir / "remagraph.db").write_text("x")
    # Note: since REMAGRAPH_STATE_DIR/PROJECT are already force-written back to
    # os.environ at the start of the function (see source), the env.sh content never
    # actually overrides them -- that's existing behavior; this just exercises the
    # path (for coverage), not a claim that it "has any actual effect". Might be worth
    # revisiting later whether this is removable dead code.
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run2:
        rg._ensure_remagraph_project("cov-test-4")
        mock_run2.assert_not_called()
    assert os.environ["REMAGRAPH_PROJECT"] == "cov-test-4"


def test_ensure_remagraph_project_outer_exception_still_sets_basic_env(monkeypatch, tmp_path):
    monkeypatch.delenv("REMAGRAPH_STATE_DIR", raising=False)
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    with patch("herdr_bridge.orchestration.memory.subprocess.run", side_effect=OSError("boom")):
        rg._ensure_remagraph_project("cov-test-5")
    assert os.environ["REMAGRAPH_PROJECT"] == "cov-test-5"
    assert "remagraph-hb-live-cov-test-5" in os.environ["REMAGRAPH_STATE_DIR"]


# ---------------------------------------------------------------------------
# _enforce_remagraph_safety_valve
# ---------------------------------------------------------------------------


def test_enforce_safety_valve_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "off")
    rg._enforce_remagraph_safety_valve("cov-safety")  # should not raise


def test_enforce_safety_valve_raises_without_state_dir(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    monkeypatch.delenv("REMAGRAPH_STATE_DIR", raising=False)
    with (
        patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"),
        pytest.raises(HerdrMemoryError, match="not initialized for project"),
    ):
        rg._enforce_remagraph_safety_valve("cov-safety-2")


def test_enforce_safety_valve_raises_on_mismatched_state_dir(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    monkeypatch.setenv("REMAGRAPH_STATE_DIR", "/tmp/remagraph-totally-different-project")
    with (
        patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"),
        pytest.raises(HerdrMemoryError, match="safety check failed"),
    ):
        rg._enforce_remagraph_safety_valve("cov-safety-3")


def test_enforce_safety_valve_passes_when_state_dir_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("REMAGRAPH_STATE_DIR", str(tmp_path / ".local" / "state" / "remagraph-hb-live-cov-safety-4"))
    with patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"):
        rg._enforce_remagraph_safety_valve("cov-safety-4")  # should not raise


def test_enforce_safety_valve_herdr_default_project_id_warned_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("REMAGRAPH_STATE_DIR", str(tmp_path / ".local" / "state" / "remagraph-hb-live-herdr"))
    with patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"):
        # project_id="herdr" combined with the current cwd (this repo's path itself
        # contains "herdr-bridge") falls into the "allowed but warned" branch (the
        # current implementation just passes, doesn't raise).
        rg._enforce_remagraph_safety_valve("herdr")


# ---------------------------------------------------------------------------
# recall_memories: disabled / project_id validation / CLI fallback / direct-import exception
# ---------------------------------------------------------------------------


def test_recall_memories_disabled_returns_empty_list(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "off")
    assert rg.recall_memories("t", "a", project_id="cov") == []


def test_recall_memories_rejects_default_project_id(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with pytest.raises(HerdrMemoryError):
        rg.recall_memories("t", "a", project_id="herdr")


def test_recall_memories_direct_import_exception_returns_empty(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", True)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with patch("herdr_bridge.orchestration.memory._rg_connect", side_effect=RuntimeError("db locked")), \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"):
        result = rg.recall_memories("t1", "a1", project_id="cov-recall-exc")
    assert result == []


def test_recall_memories_cli_fallback_success(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run, \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"memories": [{"summary": "x"}]}), ""
        )
        result = rg.recall_memories("t1", "a1", project_id="cov-test-recall")
    assert result == [{"summary": "x"}]


def test_recall_memories_cli_fallback_nonzero_exit_returns_empty(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run, \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"):
        mock_run.return_value = subprocess.CompletedProcess([], 1, "", "boom")
        result = rg.recall_memories("t1", "a1", project_id="cov-test-recall2")
    assert result == []


def test_recall_memories_cli_fallback_exception_returns_empty(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with patch("herdr_bridge.orchestration.memory.subprocess.run", side_effect=OSError("boom")), \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"):
        result = rg.recall_memories("t1", "a1", project_id="cov-test-recall3")
    assert result == []


# ---------------------------------------------------------------------------
# format_memory_summary / augment_prompt_with_memory / extract_remagraph_notes
# ---------------------------------------------------------------------------


def test_format_memory_summary_empty_list_returns_empty_string():
    assert rg.format_memory_summary([]) == ""


def test_augment_prompt_with_memory_empty_memories_returns_original():
    assert rg.augment_prompt_with_memory("original text", []) == "original text"


def test_extract_remagraph_notes_empty_text_returns_empty_list():
    assert rg.extract_remagraph_notes("") == []


def test_extract_remagraph_notes_matches_new_memory_note_marker():
    text = "some agent output\n[[MEMORY_NOTE: finished the thumbnail function]]\nmore text"
    assert rg.extract_remagraph_notes(text) == ["finished the thumbnail function"]


def test_extract_remagraph_notes_still_matches_deprecated_remagraph_note_marker():
    """Backward-compat: agents dispatched under an older prompt version may
    still emit the pre-rename [[REMAGRAPH_NOTE: ...]] marker; it must keep
    working, not silently stop matching."""
    text = "[[REMAGRAPH_NOTE: old-style note]]"
    assert rg.extract_remagraph_notes(text) == ["old-style note"]


def test_extract_remagraph_notes_matches_multiple_and_ignores_case():
    text = "[[memory_note: first]] middle [[MEMORY_NOTE: second]]"
    assert rg.extract_remagraph_notes(text) == ["first", "second"]


# ---------------------------------------------------------------------------
# prepare_dispatch_text: disabled early-return + enabled branch
# ---------------------------------------------------------------------------


def test_prepare_dispatch_text_disabled_returns_base_unchanged(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "off")
    text, tid, aid = rg.prepare_dispatch_text(
        "original", base_task_id="bt", agent_id="ba", project="cov"
    )
    assert (text, tid, aid) == ("original", "bt", "ba")


def test_prepare_dispatch_text_enabled_branch_augments_text(monkeypatch):
    """`prepare_dispatch_text` used to have an internal bug: when calling
    `recall_memories(..., project_id=project_id)`, it referenced the undefined
    variable name `project_id` (the function signature's parameter is actually
    called `project`), which meant `is_remagraph_enabled()` being True always
    triggered a NameError; the outer `except Exception` swallowed it, so the whole
    function silently fell back to the original text forever -- memory augmentation
    never actually worked (now fixed, see the fix/prepare-dispatch-text-project-id
    branch merge record).

    This verifies the corrected post-fix behavior: when enabled, the text should
    actually be augmented (with a usage-instruction/ack directive appended), no
    longer the untouched original text; task_id gets normalized by
    `ensure_task_ids`/`generate_task_id` (including a date and hash, so it won't
    equal the original base_task_id passed in), while agent_id stays unchanged.
    """
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    text, tid, aid = rg.prepare_dispatch_text(
        "original text", base_task_id="bt2", agent_id="ba2", project="cov-prepare-bug"
    )
    assert text != "original text", "text is identical to the original, meaning memory augmentation silently failed again (a NameError-like exception was swallowed by the outer layer)"
    assert text.startswith("original text")
    assert tid.startswith("cov-prepare-bug-")
    assert "bt2" in tid
    assert aid == "ba2"


# ---------------------------------------------------------------------------
# store_memory: disabled / project_id validation / direct-import retry / CLI fallback
# ---------------------------------------------------------------------------


def test_store_memory_disabled_returns_status_disabled(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "off")
    assert rg.store_memory("t", "a", summary="x", project_id="cov") == {"status": "disabled"}


def test_store_memory_rejects_default_project_id(monkeypatch):
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with pytest.raises(HerdrMemoryError):
        rg.store_memory("t", "a", summary="x", project_id="default")


def test_store_memory_direct_import_retries_on_schema_error_then_falls_to_cli(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", True)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    calls = {"connect": 0}

    def fake_connect():
        calls["connect"] += 1
        raise RuntimeError("no such column: project_id")

    with patch("herdr_bridge.orchestration.memory._rg_connect", side_effect=fake_connect), \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"), \
         patch("herdr_bridge.orchestration.memory._ensure_remagraph_project") as mock_ensure, \
         patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"status": "stored"}), "")
        result = rg.store_memory("t1", "a1", summary="x", project_id="cov-retry")

    assert calls["connect"] == 2  # first attempt fails -> judged a schema issue -> retries once with force_reinit -> still fails -> falls through to CLI
    assert any(c.kwargs.get("force_reinit") is True for c in mock_ensure.call_args_list)
    assert result == {"status": "stored"}


def test_store_memory_direct_import_non_schema_error_breaks_immediately_to_cli(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", True)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    calls = {"connect": 0}

    def fake_connect():
        calls["connect"] += 1
        raise RuntimeError("totally unrelated failure")

    with patch("herdr_bridge.orchestration.memory._rg_connect", side_effect=fake_connect), \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"), \
         patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"), \
         patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"status": "stored"}), "")
        rg.store_memory("t1", "a1", summary="x", project_id="cov-noretry")

    assert calls["connect"] == 1  # non-schema error, no retry, goes straight to CLI fallback


def test_store_memory_cli_fallback_success(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run, \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"), \
         patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"status": "stored", "id": "m1"}), ""
        )
        result = rg.store_memory(
            "t1", "a1", summary="hello", project_id="cov-test-store", learnings=["l1"]
        )
    assert result == {"status": "stored", "id": "m1"}


def test_store_memory_cli_fallback_nonzero_returns_error(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with patch("herdr_bridge.orchestration.memory.subprocess.run") as mock_run, \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"), \
         patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"):
        mock_run.return_value = subprocess.CompletedProcess([], 2, "", "cli boom")
        result = rg.store_memory("t1", "a1", summary="hello", project_id="cov-test-store2")
    assert result == {"status": "error", "detail": "cli boom"}


def test_store_memory_cli_fallback_exception_returns_error(monkeypatch):
    monkeypatch.setattr(rg, "_DIRECT_IMPORT", False)
    monkeypatch.setattr(rg, "_REMAGRAPH_MODE", "on")
    with patch("herdr_bridge.orchestration.memory.subprocess.run", side_effect=OSError("boom")), \
         patch("herdr_bridge.orchestration.memory._enforce_remagraph_safety_valve"), \
         patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"):
        result = rg.store_memory("t1", "a1", summary="hello", project_id="cov-test-store3")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# record_fleet_member / record_fleet_recycle: project_id required field
# ---------------------------------------------------------------------------


def test_record_fleet_member_requires_project_id():
    with pytest.raises(HerdrMemoryError):
        rg.record_fleet_member("t", "a", pane_id="p1", name="n1", project_id=None)  # type: ignore[arg-type]


def test_record_fleet_member_requires_non_default_project_id():
    with pytest.raises(HerdrMemoryError):
        rg.record_fleet_member("t", "a", pane_id="p1", name="n1", project_id="default")


def test_record_fleet_recycle_requires_project_id():
    with pytest.raises(HerdrMemoryError):
        rg.record_fleet_recycle("t", "a", pane_id="p1", project_id=None)  # type: ignore[arg-type]


def test_recall_fleet_members_filters_by_tag_and_ids(monkeypatch):
    with patch("herdr_bridge.orchestration.memory.recall_memories") as mock_recall:
        mock_recall.return_value = [
            {"tags": ["fleet_member"], "task_id": "t1", "agent_id": "a1", "summary": "keep"},
            {"tags": ["fleet_member"], "task_id": "t2", "agent_id": "a2", "summary": "drop-task"},
            {"tags": ["other"], "task_id": "t1", "agent_id": "a1", "summary": "drop-tag"},
        ]
        result = rg.recall_fleet_members(task_id="t1", agent_id="a1", project_id="cov-fleet")
    assert len(result) == 1
    assert result[0]["summary"] == "keep"


# ---------------------------------------------------------------------------
# migrate_herdr_bridge_memories
# ---------------------------------------------------------------------------


def _make_legacy_db(db_path: Path, rows: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT, project_id TEXT, task_id TEXT, agent_id TEXT, kind TEXT,
            summary TEXT, handoff_note TEXT, tags TEXT, learnings TEXT,
            status TEXT, timestamp TEXT, created_at TEXT, updated_at TEXT
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO memories VALUES (:id,:project_id,:task_id,:agent_id,:kind,:summary,"
            ":handoff_note,:tags,:learnings,:status,:timestamp,:created_at,:updated_at)",
            r,
        )
    conn.commit()
    conn.close()


def test_migrate_no_old_db_returns_early(monkeypatch, tmp_path):
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    with patch("herdr_bridge.orchestration.memory._ensure_remagraph_project") as mock_ensure:
        result = rg.migrate_herdr_bridge_memories(old_project="cov-migrate-none")
    assert result["status"] == "ok"
    assert result["migrated"] == 0
    assert result["invalidated"] == 0
    mock_ensure.assert_called_once()


def test_migrate_matching_rows_are_migrated_and_marked_invalidated(monkeypatch, tmp_path):
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    old_db = tmp_path / ".local" / "state" / "remagraph-cov-migrate-src" / "remagraph.db"
    now = "2026-07-25T00:00:00Z"
    _make_legacy_db(
        old_db,
        [
            {
                "id": "m1", "project_id": "cov-migrate-src", "task_id": "t1", "agent_id": "a1",
                "kind": "status_update", "summary": "herdr-bridge dispatch done",
                "handoff_note": "note", "tags": "[]", "learnings": "[]",
                "status": "active", "timestamp": now, "created_at": now, "updated_at": now,
            },
            {
                "id": "m2", "project_id": "cov-migrate-src", "task_id": "t2", "agent_id": "a2",
                "kind": "status_update", "summary": "totally unrelated content",
                "handoff_note": "", "tags": "[]", "learnings": "[]",
                "status": "active", "timestamp": now, "created_at": now, "updated_at": now,
            },
        ],
    )

    with patch("herdr_bridge.orchestration.memory.store_memory") as mock_store, \
         patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"):
        mock_store.return_value = {"status": "stored"}
        result = rg.migrate_herdr_bridge_memories(
            old_project="cov-migrate-src", new_project="cov-migrate-dst"
        )

    assert result["status"] == "ok"
    assert result["migrated"] == 1  # only the row containing the "herdr-bridge" keyword
    assert result["invalidated"] == 1
    mock_store.assert_called_once()

    conn = sqlite3.connect(str(old_db))
    marker_rows = conn.execute("SELECT status FROM memories WHERE status = 'invalidated'").fetchall()
    conn.close()
    assert len(marker_rows) == 1


def test_migrate_store_failure_for_one_row_does_not_abort_others(monkeypatch, tmp_path):
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    old_db = tmp_path / ".local" / "state" / "remagraph-cov-migrate-partial" / "remagraph.db"
    now = "2026-07-25T00:00:00Z"
    _make_legacy_db(
        old_db,
        [
            {
                "id": "m1", "project_id": "p", "task_id": "t1", "agent_id": "a1",
                "kind": "status_update", "summary": "herdr-bridge one", "handoff_note": "",
                "tags": "[]", "learnings": "[]", "status": "active",
                "timestamp": now, "created_at": now, "updated_at": now,
            },
            {
                "id": "m2", "project_id": "p", "task_id": "t2", "agent_id": "a2",
                "kind": "status_update", "summary": "herdr-bridge two", "handoff_note": "",
                "tags": "[]", "learnings": "[]", "status": "active",
                "timestamp": now, "created_at": now, "updated_at": now,
            },
        ],
    )
    with patch("herdr_bridge.orchestration.memory.store_memory", side_effect=RuntimeError("store down")), \
         patch("herdr_bridge.orchestration.memory._ensure_remagraph_project"):
        result = rg.migrate_herdr_bridge_memories(
            old_project="cov-migrate-partial", new_project="cov-migrate-dst3"
        )
    assert result["migrated"] == 0  # store fails for all rows (swallowed)
    assert result["invalidated"] == 2  # the invalidate marker is an independent sqlite insert, not dependent on store succeeding


def test_migrate_outer_exception_returns_error_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(rg.Path, "home", lambda: tmp_path)
    old_db = tmp_path / ".local" / "state" / "remagraph-cov-migrate-bad" / "remagraph.db"
    old_db.parent.mkdir(parents=True, exist_ok=True)
    old_db.write_text("not a real sqlite database")
    result = rg.migrate_herdr_bridge_memories(old_project="cov-migrate-bad", new_project="cov-migrate-dst2")
    assert result["status"] == "error"
    assert "detail" in result


# ---------------------------------------------------------------------------
# get_delivery_metrics / apply_retention
# ---------------------------------------------------------------------------


def test_get_delivery_metrics_exception_returns_error_dict():
    with patch("herdr_bridge.orchestration.memory.recall_memories", side_effect=RuntimeError("boom")):
        result = rg.get_delivery_metrics("cov-metrics")
    assert "error" in result


def test_get_delivery_metrics_empty_memories_zero_success_rate():
    with patch("herdr_bridge.orchestration.memory.recall_memories", return_value=[]):
        result = rg.get_delivery_metrics("cov-metrics-empty")
    assert result["success_rate"] == 0
    assert result["total_memories_sampled"] == 0


def test_apply_retention_archives_old_completed_dry_run_false():
    old_ts = "2020-01-01T00:00:00Z"
    with patch("herdr_bridge.orchestration.memory.recall_memories") as mock_recall, \
         patch("herdr_bridge.orchestration.memory.store_memory") as mock_store:
        mock_recall.return_value = [
            {
                "tags": ["delivery-state"], "summary": "delivery_state=COMPLETED",
                "timestamp": old_ts, "task_id": "t1", "agent_id": "a1",
            },
        ]
        mock_store.return_value = {"status": "stored"}
        result = rg.apply_retention("cov-retention", dry_run=False)
    assert result["archived"] >= 1
    assert mock_store.called


def test_apply_retention_skips_memories_without_delivery_state_tag():
    with patch("herdr_bridge.orchestration.memory.recall_memories") as mock_recall:
        mock_recall.return_value = [{"tags": ["other"], "summary": "irrelevant"}]
        result = rg.apply_retention("cov-retention-skip", dry_run=True)
    assert result["archived"] == 0


def test_apply_retention_exception_returns_error_dict():
    with patch("herdr_bridge.orchestration.memory.recall_memories", side_effect=RuntimeError("boom")):
        result = rg.apply_retention("cov-retention-exc")
    assert "error" in result


# ---------------------------------------------------------------------------
# _validate_transition / update_delivery_state / get_delivery_state
# ---------------------------------------------------------------------------


def test_validate_transition_rejects_unknown_state():
    with pytest.raises(ValueError, match="Invalid state"):
        rg._validate_transition(None, "NOT_A_REAL_STATE")


def test_validate_transition_first_state_must_be_init():
    with pytest.raises(ValueError, match="First state must be INIT"):
        rg._validate_transition(None, "DISPATCH_PENDING")


def test_validate_transition_rejects_illegal_transition():
    with pytest.raises(ValueError, match="Invalid transition"):
        rg._validate_transition("INIT", "COMPLETED")


def test_update_delivery_state_second_guard_line_when_transition_check_bypassed(monkeypatch):
    """In `update_delivery_state`, the line
    `if new_state not in DELIVERY_STATES + [...]: raise` is actually dead code under
    the normal call path: the earlier call to `_validate_transition()` already checked
    the same condition, so under normal circumstances this line would never be the
    first place an invalid state gets caught. Here we deliberately monkeypatch
    `_validate_transition` into a no-op so this line actually gets executed -- purely
    for coverage, not a claim that this is a meaningful normal usage scenario. Might be
    worth removing this redundant safeguard later. **Nothing in src/ was touched.**
    """
    monkeypatch.setattr(rg, "_validate_transition", lambda *a, **k: None)
    with (
        patch("herdr_bridge.orchestration.memory.get_delivery_state", return_value=None),
        pytest.raises(ValueError, match="Invalid state"),
    ):
        rg.update_delivery_state("t1", "a1", "NOT_A_REAL_STATE", project_id="cov-fsm-dead")


def test_get_delivery_state_returns_latest_matching_and_none_when_absent():
    """After #72, `get_delivery_state` reads from the FSM's dedicated store
    (`_fsm_store.read_state`) instead of relying on `recall_memories`'s semantic
    search for multiple approximate matches and then picking the latest one --
    it does an exact lookup for a single row keyed on
    `(project_id, task_id, agent_id)`."""
    with patch("herdr_bridge.orchestration.memory._fsm_store.read_state") as mock_read_state:
        mock_read_state.return_value = {
            "state": "DISPATCH_PENDING",
            "context": {"foo": "bar"},
            "correlation": "corr-1",
        }
        result = rg.get_delivery_state("t1", "a1", project_id="cov-gds")
    assert result == {
        "state": "DISPATCH_PENDING",
        "context": {"foo": "bar"},
        "correlation": "corr-1",
    }

    with patch("herdr_bridge.orchestration.memory._fsm_store.read_state", return_value=None):
        assert rg.get_delivery_state("t1", "a1", project_id="cov-gds") is None
