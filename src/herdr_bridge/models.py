# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Public data models. All frozen dataclasses — immutable as they pass between layers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

AgentStatus = Literal["idle", "working", "blocked", "done", "unknown",
                      "agent_prompt_stalled"]

# 0.1.2 T-1: additive addition of "blocked" — exits early when an agent is stuck
# waiting for approval/input; the existing four-value semantics are unchanged
# Herdr 0.7.5: additive addition of "stalled" — mirrors "blocked" for the new
# agent_prompt_stalled status (a prompt wait stuck >5s with no observed state
# change after an ineffective submission)
WaitReason = Literal["predicate", "timeout", "agent_gone", "error", "blocked",
                     "stalled"]

_SOURCE_ALIASES = {
    "visible": "visible",
    "recent": "recent",
    "recent-unwrapped": "recent_unwrapped",
    "recent_unwrapped": "recent_unwrapped",
    "detection": "detection",
}


def normalize_read_source(mode: str) -> str:
    """Normalize aliases for herdr's four native read sources (hyphenated form ->
    schema's underscore form).

    Note the boundary of responsibility (review X5): this function only recognizes
    herdr's native sources (visible/recent/recent-unwrapped/detection). `read_agent`'s
    `mode` parameter is a superset — the extra "raw-ansi" value is mapped by
    actions._MODE_MAP to (source="recent", format="ansi") and is not a source alias,
    so this function raising ValueError on it is expected behavior."""
    try:
        return _SOURCE_ALIASES[mode]
    except KeyError:
        raise ValueError(
            f"unknown read source {mode!r}; expected one of {sorted(set(_SOURCE_ALIASES))}"
        ) from None


def subscription_type_to_event_name(sub_type: str) -> str:
    """Convert between herdr's two naming conventions (environment validation notes §3.4).

    herdr subscription types use dots ("pane.agent_status_changed"), while pushed
    events and events.wait use underscores ("pane_agent_status_changed").
    Rule: replace every dot with an underscore (applies to all 26 known
    subscription types).
    """
    return sub_type.replace(".", "_")


@dataclass(frozen=True)
class AgentInfo:
    agent_id: str  # herdr terminal_id (stable primary key, usable directly as the target for agent.*)
    brand: str  # herdr's `agent` field ("claude", "codex", ...)
    status: AgentStatus
    pane_id: str
    workspace_id: str
    tab_id: str
    cwd: str | None
    session_ref: dict[str, Any] | None  # herdr's agent_session, kept as-is
    focused: bool


def _normalize_terminal_wrap(text: str) -> str:
    """Merge isolated newlines left by hard PTY line-wrapping (blank-line paragraph
    breaks are preserved).

    A narrow pane hard-wraps a long line (like a marker) across two lines; a lone
    newline in terminal output is almost always a wrap artifact, so it's removed.
    Consecutive newlines (a blank line) are a genuine paragraph break, and are kept.
    """
    return re.sub(r"(?<!\n)\n(?!\n)", "", text)


@dataclass(frozen=True)
class AgentOutput:
    agent_id: str
    text: str
    source: str  # the socket read source actually used (underscore format)
    status_at_read: AgentStatus
    revision: int | None = None  # WP4 (0.2.2): the monotonic revision counter from herdr's response

    @property
    def normalized_text(self) -> str:
        """0.1.1 Fix B: for marker/long-string matching — hard PTY line-wraps are already merged.

        A plain property, recomputed every time (a frozen dataclass can't have a
        cached attribute); the semantics of `text` remain frozen and unchanged."""
        return _normalize_terminal_wrap(self.text)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    agent_id: str
    actor_id: str
    priority: int
    sent_at: datetime


@dataclass(frozen=True)
class WaitResult:
    success: bool
    agent_id: str
    reason: WaitReason
    elapsed_sec: float
    last_output: AgentOutput | None
    error: str | None
