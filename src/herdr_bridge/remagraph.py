# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""
RemaGraph memory integration (compatibility shim).

This has moved to `herdr_bridge.orchestration.memory`.
This file is kept only for backward compatibility; import directly from
orchestration instead.

For all new code, please use:
    from herdr_bridge.orchestration import memory
    or
    from herdr_bridge.orchestration.memory import prepare_dispatch_text
"""

import logging

from herdr_bridge.errors import HerdrMemoryError

# Herdr Bridge Memory (embedded memory backend): a required, always-installed
# dependency. If this import fails (a broken/partial environment),
# orchestration/memory.py itself raises HerdrMemoryError with a clean message
# -- but that happens at module-import time, before any CLI argument parsing
# (so there is no -v/--verbose to gate display by yet). Catch and re-raise
# with `from None` to strip the chain, so only the clean top-level message is
# shown, matching herdr_bridge/__init__.py's identical handling.
try:
    from herdr_bridge.orchestration.memory import *

    # RemaGraph strict compliance: enforced in the compatibility shim too
    from herdr_bridge.orchestration.memory import (
        _enforce_remagraph_safety_valve,
        _ensure_remagraph_project,
        is_remagraph_enabled,
    )
except HerdrMemoryError as _exc:
    raise HerdrMemoryError(str(_exc)) from None

logger = logging.getLogger("herdr_bridge.remagraph")

if is_remagraph_enabled():
    try:
        _ensure_remagraph_project("herdr-bridge")
        _enforce_remagraph_safety_valve("herdr-bridge")
    except Exception as exc:  # noqa: BLE001 — best-effort import-time guard; must never crash package import
        logger.debug("RemaGraph safety valve setup failed during import: %s", exc)

# Explicitly re-export the main items, to keep old imports working
from herdr_bridge.orchestration.memory import (
    augment_prompt_with_memory,
    ensure_task_ids,
    extract_remagraph_notes,
    generate_task_id,
    get_remagraph_mode,
    get_usage_instruction,
    is_remagraph_enabled,
    prepare_dispatch_text,
    recall_fleet_members,
    recall_memories,
    record_fleet_member,
    record_fleet_recycle,
    store_memory,
    uses_direct_import,
)

__all__ = [
    "augment_prompt_with_memory",
    "ensure_task_ids",
    "extract_remagraph_notes",
    "generate_task_id",
    "get_remagraph_mode",
    "get_usage_instruction",
    "is_remagraph_enabled",
    "prepare_dispatch_text",
    "recall_fleet_members",
    "recall_memories",
    "record_fleet_member",
    "record_fleet_recycle",
    "store_memory",
    "uses_direct_import",
]