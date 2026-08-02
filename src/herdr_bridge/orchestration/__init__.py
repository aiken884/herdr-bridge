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
    find_active_signal_by_target,
    format_memory_summary,
    generate_task_id,
    get_remagraph_mode,
    get_signal_state,
    get_usage_instruction,
    is_remagraph_enabled,
    list_recent_signal_states,
    mark_accepted,
    mark_accepted_for_work,
    mark_completed,
    mark_escalated,
    mark_injected,
    mark_seen,
    prepare_dispatch_text,
    recall_fleet_members,
    recall_memories,
    record_fleet_member,
    record_fleet_recycle,
    search_memories,
    store_memory,
    uses_direct_import,
)

__all__ = [
    "augment_prompt_with_memory",
    "ensure_task_ids",
    "extract_remagraph_notes",
    "find_active_signal_by_target",
    "format_memory_summary",
    "generate_task_id",
    "get_remagraph_mode",
    "get_signal_state",
    "get_usage_instruction",
    "is_remagraph_enabled",
    "list_recent_signal_states",
    "mark_accepted",
    "mark_accepted_for_work",
    "mark_completed",
    "mark_escalated",
    "mark_injected",
    "mark_seen",
    "prepare_dispatch_text",
    "recall_fleet_members",
    "recall_memories",
    "record_fleet_member",
    "record_fleet_recycle",
    "search_memories",
    "store_memory",
    "uses_direct_import",
]