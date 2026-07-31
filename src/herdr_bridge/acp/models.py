# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Public ACP data models. All frozen dataclasses (design doc §4.2).

Two-tier vocabulary (D-3, a key decision, docs/acp-command-plane-design.md
§4.3):
- `PromptResult.reason` is the bridge's own frozen vocabulary (a 4-value
  Literal), which the governance layer branches on.
- `stop_reason: str | None` and `AcpEvent.type` are protocol vocabulary
  passed through as-is — new values may appear during alpha, so these are
  `str` plus documented known values plus unknown-value normalization,
  rather than being locked down with `Literal` (the R4 tolerant reader).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PromptReason = Literal["stop", "timeout", "error", "canceled"]


@dataclass(frozen=True)
class AcpAgentSpec:
    """One agent entry read from `acpx config show` (returned by `list_acp_agents()`, read-only)."""

    name: str
    command: str
    builtin: bool


@dataclass(frozen=True)
class AcpSessionInfo:
    """Session metadata returned by `ensure_session()`/`close_session()`.

    `session_name` is the bridge layer's display alias and the name acpx
    addresses by; the canonical join key is the herdr `pane_id` plus the
    dispatch ledger (design doc §4.4 v2 revision) — this field doesn't carry
    that responsibility.
    """

    session_name: str
    agent: str
    workdir: str
    acp_session_id: str | None
    closed: bool


@dataclass(frozen=True)
class AcpEvent:
    """A single NDJSON line, normalized into an event (a product of the tolerant reader, R4).

    `type` is taken directly from ACP protocol vocabulary (the `sessionUpdate`
    variant type of `session/update`, e.g. `agent_message_chunk`/`tool_call`/
    `usage_update`; or a top-level JSON-RPC `method` name, like
    `initialize`/`session/cancel`). Unknown types are always passed through
    without raising — M0 spike evidence showed that the envelope described
    in the acpx README (fields like `eventVersion`) doesn't actually exist in
    0.12.0, so the tolerant reader parses the standard ACP JSON-RPC fields
    directly and doesn't depend on any acpx-specific wrapper
    (m0-acp-spike-evidence.md §5.2).
    """

    type: str
    session_id: str | None
    text: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptResult:
    """Unified return type for `prompt()`/`exec_prompt()`/`wait_done()` (blocks until stopReason or timeout).

    `wait_done` convention: never raises — every outcome is expressed via
    `reason` (echoing the existing `WaitResult`/`wait_until` philosophy).
    """

    reason: PromptReason
    stop_reason: str | None
    text: str
    session_name: str
    usage: dict[str, Any] | None
    error: str | None = None


@dataclass(frozen=True)
class AcpPolicy:
    """The requested acpx permission policy value (the `policy=` argument to `prompt()`/`ensure_session()`).

    Warning, ADR 0003: the M0 spike's original conclusion — "the opencode ACP
    server never sends `session/request_permission`" — has since been
    **confirmed inaccurate** on deeper investigation. The negotiation
    mechanism itself is sound; the gaps are (a) acpx's
    `--approve-all`/`--approve-reads`/`--deny-all` flags are purely about
    "how acpx answers when it receives a request" — acpx never proactively
    writes opencode's local config for it; (b) setting only `permission.edit`
    protects just the native `write`/`edit`/`apply_patch` actions, while
    MCP-provided tools use the tool's own name as the permission key, so
    narrow-scope rules have no effect on them; (c) child/subagent sessions
    once had a real bug (G1) that permanently deadlocked their permission
    asks (now fixed — see `docs/acp-permission-wiring-design.md`).

    `mode` only takes real effect once opencode's local config has been
    proactively configured on a `"*"` basis via `herdr_bridge.acp.adapter`
    (`build_opencode_permission_config`/`write_session_config`/
    `build_acpx_argv_and_env`; see `docs/acpx-adapter-implementation-plan.md`).
    `policy_enforced` is filled in by the caller (`actions.py`) after it
    determines the agent type, for use in audit records — it's not a field
    this class derives on its own; `None` means the caller hasn't determined
    it yet. Policy negotiation for the claude-family tier has been confirmed
    to work.
    """

    mode: str = "approve-all"
    policy_enforced: bool | None = None
