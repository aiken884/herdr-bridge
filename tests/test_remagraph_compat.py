# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the `herdr_bridge.remagraph` compatibility layer.

This module has no logic of its own -- it just re-exports the public API of
`herdr_bridge.orchestration.memory` for old import paths, and at import time,
depending on `is_remagraph_enabled()`, tries to call
`_ensure_remagraph_project`/`_enforce_remagraph_safety_valve` (failures must
be swallowed, since simply importing this compat layer must never crash the
caller).
"""

from __future__ import annotations

import importlib

import herdr_bridge.remagraph as compat
from herdr_bridge.orchestration import memory as rg


def test_reexports_match_orchestration_memory_api():
    for name in compat.__all__:
        assert hasattr(rg, name), f"orchestration.memory is missing {name}"
        assert getattr(compat, name) is getattr(rg, name)


def test_all_matches_documented_public_api():
    expected = {
        "augment_prompt_with_memory",
        "ensure_task_ids",
        "extract_remagraph_notes",
        "generate_task_id",
        "get_remagraph_mode",
        "get_usage_instruction",
        "is_remagraph_enabled",
        "prepare_dispatch_text",
        "recall_memories",
        "store_memory",
        "uses_direct_import",
        "record_fleet_member",
        "recall_fleet_members",
        "record_fleet_recycle",
    }
    assert set(compat.__all__) == expected


def test_import_time_safety_valve_call_is_swallowed_on_failure(monkeypatch):
    """When `is_remagraph_enabled()` is True, import tries to call
    `_ensure_remagraph_project`/`_enforce_remagraph_safety_valve`; even if
    these two calls raise, `import herdr_bridge.remagraph` itself must not
    fail (guarded by an outer `except Exception: pass`).
    """
    monkeypatch.setattr(rg, "is_remagraph_enabled", lambda: True)
    monkeypatch.setattr(
        rg,
        "_ensure_remagraph_project",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    # reload re-runs the module's top-level code (including the import-time if/try block)
    importlib.reload(compat)

    # Even if the underlying call raises, the module must still load fine and re-exports must still work
    assert compat.store_memory is rg.store_memory

    # Restore normal state so other tests' assumptions about this module aren't affected
    importlib.reload(compat)
