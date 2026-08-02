# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Session Cache: a local mirror of state (spec §4.2).

Bootstraps via session.snapshot; see start() for incremental updates and the
consistency check (Task 10/11).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from herdr_bridge.client import SocketClient, Subscription
from herdr_bridge.models import AgentInfo, AgentStatus

logger = logging.getLogger("herdr_bridge.cache")

_VALID_STATUSES: frozenset[str] = frozenset(
    {"idle", "working", "blocked", "done", "unknown", "agent_prompt_stalled"})


def _status(raw: Any) -> AgentStatus:
    return raw if raw in _VALID_STATUSES else "unknown"


def _to_agent_info(rec: dict[str, Any]) -> AgentInfo:
    return AgentInfo(
        agent_id=rec["terminal_id"],
        brand=rec.get("agent") or "unknown",
        status=_status(rec.get("agent_status")),
        pane_id=rec["pane_id"],
        workspace_id=rec.get("workspace_id", ""),
        tab_id=rec.get("tab_id", ""),
        cwd=rec.get("cwd"),
        session_ref=rec.get("agent_session"),
        focused=bool(rec.get("focused")),
    )


class SessionCache:
    def __init__(self, client: SocketClient, *,
                 consistency_interval_sec: float = 300.0) -> None:
        self._client = client
        self._lock = threading.RLock()
        self._panes: dict[str, dict[str, Any]] = {}
        self._agents: dict[str, AgentInfo] = {}      # terminal_id -> AgentInfo
        self._pane_to_terminal: dict[str, str] = {}  # pane_id -> terminal_id
        self._focus: dict[str, str | None] = {}
        self._sub: Subscription | None = None
        self._rebuild_timer: threading.Timer | None = None
        self._rebuild_lock = threading.Lock()  # serializes _rebuild / stop (review CC2/CC3)
        self._consistency_timer: threading.Timer | None = None
        self._stopped = threading.Event()
        self._consistency_interval_sec = consistency_interval_sec
        self.drift_count = 0
        self._consistency_tick_count = 0
        # PPLX priority 2: pane_state map, used for per-pane event seq / last_status idempotency checks (safety net)
        self._pane_state: dict[str, dict[str, Any]] = {}  # pane_id -> {"last_seq": , "last_status": }

    # -- bootstrap --------------------------------------------------------
    def refresh_snapshot(self) -> None:
        snap = self._client.call("session.snapshot")
        with self._lock:
            self._panes = {p["pane_id"]: p for p in snap.get("panes", [])}
            self._agents = {}
            self._pane_to_terminal = {}
            for rec in snap.get("agents", []):
                info = _to_agent_info(rec)
                self._agents[info.agent_id] = info
                self._pane_to_terminal[info.pane_id] = info.agent_id
            self._focus = {
                "workspace_id": snap.get("focused_workspace_id"),
                "tab_id": snap.get("focused_tab_id"),
                "pane_id": snap.get("focused_pane_id"),
            }

    # -- read API ---------------------------------------------------------
    def list_agents(self) -> list[AgentInfo]:
        with self._lock:
            return sorted(self._agents.values(), key=lambda a: a.agent_id)

    def resolve(self, target: str) -> AgentInfo | None:
        with self._lock:
            if target in self._agents:
                return self._agents[target]
            terminal = self._pane_to_terminal.get(target)
            return self._agents.get(terminal) if terminal else None

    def pane_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._panes)

    def focused_pane_id(self) -> str | None:
        with self._lock:
            return self._focus.get("pane_id")

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.refresh_snapshot()  # get the initial pane set to build the subscription list
        self._rebuild()          # from here on, everything goes through the rebuild-rule flow
        self._schedule_consistency()

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            if self._rebuild_timer is not None:
                self._rebuild_timer.cancel()
            if self._consistency_timer is not None:
                self._consistency_timer.cancel()
        with self._rebuild_lock:  # wait for any in-flight rebuild to finish, so no subscription is orphaned (CC3)
            if self._sub is not None:
                self._sub.close()
                self._sub = None

    # -- consistency check -----------------------------------------------
    def _schedule_consistency(self) -> None:
        if self._stopped.is_set():
            return
        t = threading.Timer(self._consistency_interval_sec, self._consistency_tick)
        t.daemon = True
        t.start()
        self._consistency_timer = t

    def _consistency_tick(self) -> None:
        # PPLX priority 2: the consistency timer is only a safety net, not the primary
        # update source. Primary state updates rely entirely on subscription + _apply_event.
        # Each tick does only the "minimum necessary check":
        # - first check whether sub is alive (lightweight, no snapshot)
        # - do a full snapshot comparison only every N ticks (avoid heavy operations)
        # - only trigger a rebuild if a problem is found
        # Rebuild and resubscribe are handled by _on_conn_state / _request_rebuild.
        self._consistency_tick_count += 1
        try:
            # Lightweight health check: if sub is dead, rebuild immediately, no snapshot needed
            if self._sub is None or getattr(self._sub, "closed", False):
                logger.warning("live subscription dead (safety net); rebuilding")
                self._request_rebuild(immediate=True)
                return

            # Only do a full snapshot every 3rd tick (5min * 3 = a full check every 15min)
            if self._consistency_tick_count % 3 == 0:
                before = {a.agent_id: a.status for a in self.list_agents()}
                before_panes = set(self.pane_ids())
                self.refresh_snapshot()
                after = {a.agent_id: a.status for a in self.list_agents()}
                after_panes = set(self.pane_ids())
                if before != after or before_panes != after_panes:
                    self.drift_count += 1
                    logger.warning(
                        "cache drift healed (safety net): agents %s->%s panes %s->%s",
                        before, after, sorted(before_panes), sorted(after_panes))
                    if before_panes != after_panes:
                        self._request_rebuild(immediate=True)
            # sync pane_state as well (for idempotency)
            with self._lock:
                for pid in self._panes:
                    self._pane_state.setdefault(pid, {})
        except Exception:
            logger.exception("consistency check (safety) failed")
        finally:
            self._schedule_consistency()

    # -- subscription management -----------------------------------------
    def _subscription_set(self) -> list[dict[str, Any]]:
        subs: list[dict[str, Any]] = [
            {"type": t} for t in (
                "pane.created", "pane.closed", "pane.focused",
                "pane.exited", "pane.agent_detected",
            )
        ]
        with self._lock:
            subs.extend({"type": "pane.agent_status_changed", "pane_id": pid}
                        for pid in self._panes)
        return subs

    def _request_rebuild(self, *, immediate: bool) -> None:
        """Schedule a rebuild. New pane -> immediate (Timer(0), doesn't block the reader
        thread; multiple triggers naturally collapse into the last one); pane closed ->
        0.5s debounce [D7/PPLX R-06]."""
        with self._lock:
            if self._stopped.is_set():
                return
            if self._rebuild_timer is not None:
                self._rebuild_timer.cancel()
            delay = 0.0 if immediate else 0.5
            self._rebuild_timer = threading.Timer(delay, self._rebuild)
            self._rebuild_timer.daemon = True
            self._rebuild_timer.start()

    def _rebuild(self) -> None:
        """Rebuild rule (D7/PPLX R-07): subscribe new -> overwrite with a snapshot ->
        close old, in that order.

        The snapshot is taken after the new subscription is established: any old
        events the new subscription missed are already covered by the snapshot, and
        events after the snapshot are caught by the new subscription — there's no
        uncovered gap. Serialized via _rebuild_lock: the debounce timer and the
        immediate timer can fire concurrently (Timer.cancel can't stop a callback
        that has already started, review CC2).
        """
        with self._rebuild_lock:
            if self._stopped.is_set():
                return
            old = self._sub
            try:
                new_sub = self._client.subscribe(    # (1) open the new subscription first
                    self._subscription_set(),
                    on_event=self._apply_event,
                    on_state=self._on_conn_state,
                )
                # Wire-layer sync point: wait for the first ack to confirm the
                # subscription actually took effect (review CC1 + the root cause of a
                # flake — under fire-and-forget, subscribe could land later than the snapshot)
                if not new_sub.wait_connected(5.0):
                    new_sub.close()
                    logger.warning("rebuild: new subscription not acked in 5s; "
                                   "keeping old subscription")
                    return
                self.refresh_snapshot()              # (2) overwrite with the authoritative snapshot
            except Exception:
                logger.exception("subscription rebuild failed; keeping old subscription")
                return
            if self._stopped.is_set():               # stop() already ran: don't leave an orphan (CC3)
                new_sub.close()
                return
            self._sub = new_sub
            if old is not None:
                old.close()                          # (3) close the old one last

    def _on_conn_state(self, state: str) -> None:
        if state == "reconnected":
            # PPLX priority 2/3: stateful reconnect + resub + reconcile using pane_state.
            # The core rebuild goes through subscription; consistency is just a safety net.
            with self._lock:
                for pid, st in list(self._pane_state.items()):
                    # reconcile last known status
                    if pid in self._panes and "last_status" in st:
                        # update the matching local agent state, if any
                        for tid, a in list(self._agents.items()):
                            if a.pane_id == pid:
                                self._agents[tid] = AgentInfo(
                                    agent_id=a.agent_id, brand=a.brand, status=st["last_status"],
                                    pane_id=a.pane_id, workspace_id=a.workspace_id,
                                    tab_id=a.tab_id, cwd=a.cwd, session_ref=a.session_ref, focused=a.focused
                                )
                                break
            self._request_rebuild(immediate=True)  # make sure everything watched gets resubscribed + reconciled via snapshot

    # -- event application ------------------------------------------------
    def _apply_event(self, event: str, data: dict[str, Any]) -> None:
        if event == "pane_created":
            pane = data.get("pane", {})
            pane_id = pane.get("pane_id")
            if pane_id:
                with self._lock:
                    self._panes[pane_id] = pane
                    if pane.get("terminal_id") and pane.get("agent"):
                        info = _to_agent_info(pane)
                        self._agents[info.agent_id] = info
                        self._pane_to_terminal[pane_id] = info.agent_id
                self._request_rebuild(immediate=True)   # subscribe to a new pane right away
        elif event == "pane_closed":
            pane_id = data.get("pane_id")
            if pane_id:
                with self._lock:
                    self._panes.pop(pane_id, None)
                    terminal = self._pane_to_terminal.pop(pane_id, None)
                    if terminal:
                        self._agents.pop(terminal, None)
                self._request_rebuild(immediate=False)      # unsubscribing isn't urgent, debounce it
        elif event == "pane_exited":
            pane_id = data.get("pane_id")
            with self._lock:
                terminal = self._pane_to_terminal.get(pane_id or "")
                if terminal and terminal in self._agents:
                    old_info = self._agents[terminal]
                    self._agents[terminal] = AgentInfo(
                        agent_id=old_info.agent_id, brand=old_info.brand,
                        status="unknown", pane_id=old_info.pane_id,
                        workspace_id=old_info.workspace_id, tab_id=old_info.tab_id,
                        cwd=old_info.cwd, session_ref=old_info.session_ref,
                        focused=old_info.focused,
                    )
        elif event == "pane_focused":
            with self._lock:
                self._focus["pane_id"] = data.get("pane_id")
                self._focus["workspace_id"] = data.get("workspace_id")
        elif event in ("pane_agent_detected", "pane_agent_status_changed"):
            pane_id = data.get("pane_id") or data.get("pane", {}).get("pane_id")
            new_status = data.get("agent_status") or data.get(
                "pane", {}).get("agent_status")
            with self._lock:
                # PPLX priority 2/3: update the pane_state map for reconcile / idempotency
                if pane_id:
                    self._pane_state[pane_id] = {
                        "last_status": _status(new_status),
                        "last_event_ts": time.time(),
                        "last_event": event,
                    }
                terminal = self._pane_to_terminal.get(pane_id or "")
                if terminal and terminal in self._agents:
                    old = self._agents[terminal]
                    self._agents[terminal] = AgentInfo(
                        agent_id=old.agent_id, brand=old.brand,
                        status=_status(new_status), pane_id=old.pane_id,
                        workspace_id=old.workspace_id, tab_id=old.tab_id,
                        cwd=old.cwd, session_ref=old.session_ref,
                        focused=old.focused,
                    )
                elif event == "pane_agent_detected":
                    pane = self._panes.get(pane_id or "")
                    if pane is not None:
                        merged = {**pane, **data.get("pane", {}),
                                  "agent_status": new_status}
                        if merged.get("terminal_id") and merged.get("agent"):
                            info = _to_agent_info(merged)
                            self._agents[info.agent_id] = info
                            self._pane_to_terminal[info.pane_id] = info.agent_id
