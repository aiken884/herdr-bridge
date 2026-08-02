# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""herdr-bridge: semantic orchestration bridge over the Herdr socket API."""

import logging

from herdr_bridge.actions import BridgeActions, ControlHandle, connect
from herdr_bridge.audit import get_audit_log_path
from herdr_bridge.errors import (
    AgentNotFoundError,
    ControlLeaseError,
    HerdrApiError,
    HerdrBridgeError,
    HerdrConnectionError,
    HerdrMemoryError,
    HerdrTimeoutError,
    SchemaVersionError,
)
from herdr_bridge.models import (
    AgentInfo,
    AgentOutput,
    AgentStatus,
    SendResult,
    WaitResult,
    normalize_read_source,
)

# Herdr Bridge Memory (embedded memory backend): a required, always-installed
# dependency, so this import is not expected to fail in a normal install --
# but if it does (a broken/partial environment), it must not surface a raw,
# backend-internal traceback. orchestration/memory.py itself raises
# HerdrMemoryError with a clean message in that case (chained `from` the
# original ImportError for anyone catching it programmatically). But this
# import happens at PACKAGE-IMPORT time, before any CLI argument parsing --
# so there is no -v/--verbose to gate display by yet, and letting the chain
# propagate here would make Python's default uncaught-exception printer show
# the original ImportError's raw backend-internal text unconditionally. Catch
# and re-raise with `from None` to strip the chain, so only the clean
# top-level message is ever shown for this specific failure.
try:
    from herdr_bridge.orchestration import (
        memory as remagraph,  # governance-layer memory (embedded RemaGraph)
    )

    # Primary governance memory API (recommended for the command tower)
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
except HerdrMemoryError as _exc:
    raise HerdrMemoryError(str(_exc)) from None

logger = logging.getLogger("herdr_bridge")

__version__ = "0.8.0"

__all__ = [
    "AcpRouter",
    "AgentInfo",
    "AgentNotFoundError",
    "AgentOutput",
    "AgentStatus",
    "BridgeActions",
    "CentralTower",
    "ControlHandle",
    "ControlLeaseError",
    "HerdrApiError",
    "HerdrBridgeError",
    "HerdrConnectionError",
    "HerdrMemoryError",
    "HerdrTimeoutError",
    "SchemaVersionError",
    "SendResult",
    "WaitResult",
    "augment_prompt_with_memory",
    "connect",
    "create_central_tower",
    "create_herdr_router",
    "ensure_task_ids",
    "extract_remagraph_notes",
    "generate_task_id",
    "get_audit_log_path",
    "get_remagraph_mode",
    "get_usage_instruction",
    "is_remagraph_enabled",
    "normalize_read_source",
    "prepare_dispatch_text",
    "recall_fleet_members",
    "recall_memories",
    "record_fleet_member",
    "record_fleet_recycle",
    "remagraph",
    "store_memory",
    "uses_direct_import",
]

# High-level command-tower facade (AcpRouter + dispatch_with_memory_confirm is the central abstraction).
# External projects get a simple API: router = create_herdr_router(); router.dispatch_with_memory_confirm(...)
# or, for compatibility: tower = create_central_tower(); tower.dispatch(...)
from herdr_bridge.acp.router import (
    AcpRouter,
    CentralTower,
    create_central_tower,
    create_herdr_router,
)

# RemaGraph strict compliance: every entry point (including what __init__ exposes) must
# ensure the dedicated project first. In plain terms: whoever calls store/recall, we need
# to point them at the herdr-bridge-specific memory database first, so we don't accidentally
# hit the default DB.
if remagraph.is_remagraph_enabled():
    try:
        remagraph._ensure_remagraph_project("herdr-bridge")
        remagraph._enforce_remagraph_safety_valve("herdr-bridge")
    except Exception as exc:  # noqa: BLE001 — best-effort import-time guard; must never crash package import
        logger.debug("RemaGraph safety valve setup failed during import: %s", exc)
