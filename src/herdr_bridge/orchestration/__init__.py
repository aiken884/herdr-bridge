# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Command tower orchestration layer.

Provides the memory/state infrastructure that the ACP command plane
(`herdr_bridge.acp`) needs to operate. It is not part of some separate,
external "governance layer" product — that's a different downstream
consumer that uses this package through `herdr_bridge.acp`'s public API,
and it neither is nor should be embedded here.

Currently includes:
- memory: RemaGraph memory integration (embedded specifically for the
  herdr Bridge command tower)
"""

from herdr_bridge.orchestration.memory import (
    augment_prompt_with_memory,
    ensure_task_ids,
    extract_remagraph_notes,
    format_memory_summary,
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
    "format_memory_summary",
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