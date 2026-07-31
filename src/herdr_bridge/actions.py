# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Bridge Actions: the tool layer's public interface (spec §4.3, frozen signatures).

actor_id / priority / mode are fields reserved for the governance layer: the tool
layer does no permission or priority judgment of its own, it just records these
verbatim to the audit log (see docs/api.md for the full semantics).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient, detect_socket_path
from herdr_bridge.errors import AgentNotFoundError, ControlLeaseError
from herdr_bridge.models import (
    AgentInfo,
    AgentOutput,
    AgentStatus,
    SendResult,
    WaitResult,
)
from herdr_bridge.schema import check_server_compat

logger = logging.getLogger("herdr_bridge.actions")

# mode -> (source, format, strip_ansi)
_MODE_MAP: dict[str, tuple[str, str, bool]] = {
    "recent-unwrapped": ("recent_unwrapped", "text", True),
    "recent_unwrapped": ("recent_unwrapped", "text", True),
    "raw-ansi": ("recent", "ansi", False),
    "visible": ("visible", "text", True),
    "recent": ("recent", "text", True),
    "detection": ("detection", "text", True),
}


def _RevisionAdapter(value: Any) -> int | None:
    """Experimental (WP4): normalize the revision value from a herdr response to int | None.

    Any non-int value (None, float, str, bool, etc.) is downgraded to None.
    bool is a subclass of int in Python, but revision isn't boolean by meaning,
    so a bool value is treated as a type error and downgraded to None.

    This is an experimental feature (0.2.2); the interface may still shift as
    the herdr protocol evolves. Consumers shouldn't depend on it directly.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


class ControlHandle:
    """A bridge-layer pane control lease (not a herdr-server-side lock, see docs/api.md)."""

    def __init__(self, registry: _ControlRegistry, pane_id: str,
                 actor_id: str, mode: str) -> None:
        self._registry = registry
        self.pane_id = pane_id
        self.actor_id = actor_id
        self.mode = mode
        self.released = False

    def release(self) -> None:
        self._registry.release(self)  # idempotency check happens inside the registry lock (review CC4)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class _ControlRegistry:
    def __init__(self, audit: AuditLogger) -> None:
        self._audit = audit
        self._lock = threading.Lock()
        self._control_leases: dict[str, ControlHandle] = {}  # pane_id -> handle

    def acquire(self, pane_id: str, actor_id: str, mode: str) -> ControlHandle:
        with self._lock:
            if mode == "control":
                existing = self._control_leases.get(pane_id)
                if existing is not None and not existing.released:
                    # A denied takeover attempt is exactly the kind of audit event the
                    # governance layer needs most (review X3)
                    self._audit.record(actor_id, "acquire_control_denied",
                                       pane_id=pane_id, mode=mode,
                                       held_by=existing.actor_id)
                    raise ControlLeaseError(
                        f"pane {pane_id} already controlled by "
                        f"actor {existing.actor_id!r}")
            handle = ControlHandle(self, pane_id, actor_id, mode)
            if mode == "control":
                self._control_leases[pane_id] = handle
            self._audit.record(actor_id, "acquire_control",
                               pane_id=pane_id, mode=mode)
            return handle

    def release(self, handle: ControlHandle) -> None:
        with self._lock:
            if handle.released:  # idempotency check inside the lock: a concurrent double-release still logs only one audit entry (CC4)
                return
            handle.released = True
            if self._control_leases.get(handle.pane_id) is handle:
                del self._control_leases[handle.pane_id]
        self._audit.record(handle.actor_id, "release_control",
                           pane_id=handle.pane_id, mode=handle.mode)


class BridgeActions:
    def __init__(self, client: SocketClient, cache: SessionCache,
                 audit: AuditLogger) -> None:
        self._client = client
        self._cache = cache
        self._audit = audit
        self._controls = _ControlRegistry(audit)
        # F-2: the socket we actually connected to and its source ('explicit'|'env'|'detected').
        # Defaults to the client's path with source "unknown" — only the connect() factory
        # knows how the path was resolved and overwrites the source tag; an instance built
        # directly must not falsely claim "explicit".
        self._resolved_socket_path = client.socket_path
        self._socket_source = "unknown"

    @property
    def resolved_socket_path(self) -> str:
        """The herdr socket path we actually connected to (F-2: callers can assert on this to guard against connecting to the wrong session)."""
        return self._resolved_socket_path

    @property
    def socket_source(self) -> str:
        """Where the socket path came from: 'explicit' (passed in directly) | 'env'
        (HERDR_SOCKET_PATH) | 'detected' (via `herdr status`) | 'unknown' (didn't go
        through the connect() factory, so the source can't be known). Automation
        should always see 'explicit'."""
        return self._socket_source

    # -- helpers ----------------------------------------------------------
    def _resolve_with_refresh(self, target: str,
                              exists: Callable[[], bool] | None = None,
                              ) -> AgentInfo | None:
        """Shared miss -> refresh -> recheck skeleton (B2-4).

        On a resolve miss (and exists() doesn't confirm existence otherwise), take one
        extra snapshot refresh and resolve again; the existence check and the resulting
        error semantics are left to the caller."""
        info = self._cache.resolve(target)
        if info is None and (exists is None or not exists()):
            self._cache.refresh_snapshot()      # on a cache miss, take one extra snapshot before declaring it dead
            info = self._cache.resolve(target)
        return info

    def _resolve_or_raise(self, agent_id: str) -> AgentInfo:
        info = self._resolve_with_refresh(agent_id)
        if info is None:
            raise AgentNotFoundError(code="agent_not_found",
                                     message=f"agent {agent_id!r} not found")
        return info

    # -- public API (spec §4.3) -------------------------------------------
    def list_agents(self, actor_id: str) -> list[AgentInfo]:
        agents = self._cache.list_agents()
        self._audit.record(actor_id, "list_agents", count=len(agents))
        return agents

    def read_agent(self, actor_id: str, agent_id: str,
                   mode: str = "recent-unwrapped", *,
                   since_revision: int | None = None) -> AgentOutput:
        try:
            source, fmt, strip = _MODE_MAP[mode]
        except KeyError:
            raise ValueError(
                f"unknown mode {mode!r}; expected one of {sorted(_MODE_MAP)}") from None
        info = self._resolve_or_raise(agent_id)
        params: dict[str, Any] = {
            "target": info.agent_id, "source": source,
            "format": fmt, "strip_ansi": strip,
        }
        if since_revision is not None:
            params["since_revision"] = since_revision
        data = self._client.call("agent.read", params)
        out = AgentOutput(agent_id=info.agent_id,
                          text=data.get("text", ""),
                          source=source,
                          status_at_read=info.status,
                          revision=_RevisionAdapter(data.get("revision")))
        self._audit.record(actor_id, "read_agent", agent_id=info.agent_id,
                           mode=mode, chars=len(out.text))
        return out

    def send_to_agent(self, actor_id: str, agent_id: str, text: str,
                      priority: int = 0) -> SendResult:
        info = self._resolve_or_raise(agent_id)
        self._client.request("agent.send", {"target": info.agent_id, "text": text})
        result = SendResult(ok=True, agent_id=info.agent_id, actor_id=actor_id,
                            priority=priority,
                            sent_at=datetime.now(UTC))
        self._audit.record(actor_id, "send_to_agent", agent_id=info.agent_id,
                           priority=priority, chars=len(text))
        return result

    def get_agent_status(self, actor_id: str, agent_id: str) -> AgentStatus:
        """The agent's current Herdr status (idle/working/blocked/done/unknown).

        T-2 (0.1.2 additive sixth function; the five frozen-signature functions are
        untouched): this faithfully reports Herdr's own detected value without
        interpreting it — whether "blocked" needs intervention is a governance-layer
        policy call. Raises AgentNotFoundError if the agent doesn't exist (including
        after a snapshot refresh).
        """
        info = self._resolve_or_raise(agent_id)
        self._audit.record(actor_id, "get_agent_status",
                           agent_id=info.agent_id, status=info.status)
        return info.status

    def wait_until(self, actor_id: str, agent_id: str,
                   predicate: Callable[[AgentOutput], bool],
                   timeout_sec: int = 60,
                   poll_interval_sec: int = 2, *,
                   since_revision: int | None = None) -> WaitResult:
        """Wait for predicate to hold on the agent's output (triple-confirmed via
        event subscription + active polling + timeout).

        Correct usage: base the predicate on the "output text", not on the status
        value — Claude Code also reports idle while "waiting for input"
        (capability-notes §3.3)::

            result = actions.wait_until(
                "rule:my-rule", agent_id,
                predicate=lambda out: "PASSED" in out.text or "FAILED" in out.text,
                timeout_sec=300,
            )
            if not result.success and result.reason == "timeout":
                pass  # don't auto-resend the command (governance memo rule 3: terminal commands aren't idempotent)

        timeout_sec is the total time budget starting from the call (not an idle
        timeout); this function never raises — every outcome is expressed via
        WaitResult.reason (predicate/timeout/agent_gone/error/blocked — it exits
        early if the agent goes blocked and the predicate hasn't matched yet;
        0.1.2 additive).

        For marker matching, prefer `out.normalized_text` — a narrow pane's hard PTY
        line-wrapping can split a marker across two lines, so `out.text` won't match.
        PPLX priority 1: switched to subscribe (pane.agent_status_changed +
        output_matched) as the primary path, removing most of the time.sleep loop +
        events.wait poll fallback and keeping only a minimal safety net.
        """
        t0 = time.monotonic()
        last_output: AgentOutput | None = None

        def done(success: bool, reason: str, error: str | None = None) -> WaitResult:
            result = WaitResult(success=success, agent_id=agent_id, reason=reason,  # type: ignore[arg-type]
                                elapsed_sec=time.monotonic() - t0,
                                last_output=last_output, error=error)
            self._audit.record(actor_id, "wait_until", agent_id=agent_id,
                               success=success, reason=reason,
                               elapsed_sec=round(result.elapsed_sec, 3))
            return result

        try:
            info = self._resolve_or_raise(agent_id)
        except AgentNotFoundError:
            return done(False, "agent_gone")
        except Exception as exc:  # noqa: BLE001 — spec: must not let an exception escape uncaught
            return done(False, "error", f"{type(exc).__name__}: {exc}")

        # PPLX priority 1: fully event-driven now — subscribe replaces events.wait + poll fallback + sleep loop.
        # Subscribes to pane.agent_status_changed + pane.output_matched.
        # The callback checks predicate / blocked and sets the Event.
        # Dedupe added (last_matched + revision).
        # Almost no time.sleep left (only the timeout wait).
        last_matched_line = ""
        match_ev = threading.Event()
        res: dict[str, Any] = {}

        def _wait_on_event(ev_name: str, data: dict[str, Any]) -> None:
            nonlocal last_matched_line
            try:
                p = data.get("pane_id") or data.get("pane", {}).get("pane_id")
                if p != info.pane_id:
                    return
                if ev_name == "pane.output_matched":
                    ml = data.get("matched_line") or data.get("data", {}).get("matched_line", "")
                    norm = (ml or "").strip().lower()
                    if norm and norm != last_matched_line.lower() if last_matched_line else norm:
                        last_matched_line = ml
                        # match using normalized_text-style comparison
                        fake_out = type("O", (), {"text": ml, "normalized_text": norm, "status_at_read": data.get("agent_status", "unknown")})()
                        if predicate(fake_out):
                            res.update(success=True, reason="predicate", last=fake_out)
                            match_ev.set()
                            return
                # read the latest output for a final predicate / status check
                out = self.read_agent(actor_id, info.agent_id, since_revision=since_revision)
                if predicate(out):
                    res.update(success=True, reason="predicate", last=out)
                    match_ev.set()
                    return
                if getattr(out, "status_at_read", None) == "blocked":
                    res.update(success=False, reason="blocked", last=out)
                    match_ev.set()
                    return
            except Exception:  # event callback must never crash the subscription reader thread
                logger.debug("wait_until event callback failed", exc_info=True)

        sub = None
        try:
            subs = [
                {"type": "pane.agent_status_changed", "pane_id": info.pane_id},
                # broad output_matched subscription; the callback filters via normalized + predicate (supports any marker)
                {"type": "pane.output_matched", "pane_id": info.pane_id, "source": "recent"},
            ]
            sub = self._client.subscribe(subs, on_event=_wait_on_event)
            if not sub.wait_connected(3.0):
                sub.close()
                sub = None
        except Exception:  # best-effort subscribe setup; falls back to the poll/timeout path below
            logger.debug("wait_until subscribe setup failed; falling back to timeout path", exc_info=True)
            if sub:
                sub.close()
            sub = None

        rem = timeout_sec - (time.monotonic() - t0)
        if rem > 0:
            match_ev.wait(rem)

        if sub:
            try:
                sub.close()
            except Exception:  # best-effort cleanup; subscription is being discarded regardless
                logger.debug("wait_until subscription close failed", exc_info=True)

        if "success" in res:
            lo = res.get("last")
            last_output = lo if lo else last_output
            return done(res["success"], res["reason"])

        # Final timeout fallback (no event matched): the docstring promises a "triple
        # confirmation via event + active polling + timeout" — this is the third one.
        # We still do this confirming read even if the budget is exhausted, because it's
        # a single quick call, not another wait. This branch used to return timeout early
        # whenever rem<=0, but match_ev.wait(rem) almost always burns through the whole
        # budget, which made that early-return branch permanently dead code — causing
        # cases like a predicate exception (see test_wait_swallows_predicate_exception)
        # to get misreported as timeout instead of their real reason (error/predicate/blocked).
        try:
            last_output = self.read_agent(actor_id, info.agent_id, since_revision=since_revision)
            if predicate(last_output):
                return done(True, "predicate")
            if getattr(last_output, "status_at_read", None) == "blocked":
                return done(False, "blocked")
        except AgentNotFoundError:
            return done(False, "agent_gone")
        except Exception as exc:  # noqa: BLE001 — spec: must not let an exception escape uncaught
            return done(False, "error", f"{type(exc).__name__}: {exc}")
        return done(False, "timeout")

    def acquire_control(self, actor_id: str, pane_id: str,
                        mode: str = "control") -> ControlHandle:
        if mode not in ("observe", "control"):
            raise ValueError(f"mode must be 'observe' or 'control', got {mode!r}")
        # The lease key is always normalized to the canonical pane_id: if the caller
        # mistakenly passes an agent_id (terminal_id), it must not create a second
        # lease that bypasses mutual exclusion (review X2).
        def in_panes() -> bool:
            return pane_id in self._cache.pane_ids()

        info = self._resolve_with_refresh(pane_id, exists=in_panes)
        if info is None and not in_panes():
            raise AgentNotFoundError(code="pane_not_found",
                                     message=f"pane {pane_id!r} not found")
        canonical = info.pane_id if info is not None else pane_id
        return self._controls.acquire(canonical, actor_id, mode)


def _resolve_socket(socket_path: str | None, herdr_bin: str) -> tuple[str, str]:
    """Resolve the socket path and return (path, source) (F-2, P0 safety).

    When socket_path is passed explicitly, env is never touched — in automation,
    the host shell may be carrying another session's HERDR_SOCKET_PATH, and the old
    `socket_path or detect_socket_path()` would silently fall back to env, connect
    to the wrong session, and never raise. This is now an explicit branch instead;
    when auto-detection falls back to env, it emits a WARNING (no longer silent).
    source is one of explicit|env|detected.
    """
    if socket_path is not None:
        return socket_path, "explicit"
    env_path = os.environ.get("HERDR_SOCKET_PATH")
    if env_path:
        logger.warning(
            "connect() resolved socket via HERDR_SOCKET_PATH=%s; pass "
            "socket_path explicitly in automation to avoid silently "
            "connecting to the wrong herdr session.", env_path)
        return env_path, "env"
    return detect_socket_path(herdr_bin), "detected"


def connect(socket_path: str | None = None, *,
            audit_path: str | Path | None = None,
            herdr_bin: str = "herdr") -> BridgeActions:
    path, source = _resolve_socket(socket_path, herdr_bin)
    client = SocketClient(path)
    check_server_compat(client)
    cache = SessionCache(client)
    cache.start()
    actions = BridgeActions(client, cache, AuditLogger(audit_path))
    actions._resolved_socket_path = path
    actions._socket_source = source
    return actions
