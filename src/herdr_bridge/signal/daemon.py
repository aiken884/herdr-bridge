# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Signal daemon: a resident, per-tower background process that listens on a
Unix socket for wake envelopes and, once verified, self-injects a notification
into its own pane via the existing `herdr-commander notify-pane` (design doc
v6 — M0-B proved the "borrow Herdr's events.subscribe" transport-layer
simplification unreliable, so this is a self-built Unix socket server, per
§2.1's fallback and §3.7's module split).

Write ownership (§3.7, revised 2026-08-01): this module calls
`orchestration.mark_injected()` and, immediately after it succeeds,
`orchestration.mark_completed()`. The original design reserved `completed`
for the target agent's own conversation flow to call once it had actually
read and handled the wake -- but nothing in this codebase ever gave that
flow a way to call it (no CLI command, no hook), so every Signal sent was
permanently stuck at `injected`. That in turn made the dedup check below
treat every wake as forever in-flight, silently swallowing any subsequent
send to the same (to_project, inbox_ref) -- exactly the reminder/retry
scenario Signal exists for (2026-08-01 PPLX-reviewed fix). `completed` here
means "the daemon confirmed injection, the sender's responsibility is
done" -- a deliberately narrower claim than "the receiving agent actually
read and acted on it", which this module still cannot verify. `mark_seen`/
`mark_accepted_for_work` stay reserved for the target agent's own flow,
whenever one exists that can call them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from herdr_bridge.orchestration import find_active_signal_by_target, mark_completed, mark_injected
from herdr_bridge.orchestration._state_paths import signal_state_dir
from herdr_bridge.signal.envelope import Envelope, verify
from herdr_bridge.signal.lock import SingleInstanceLock

logger = logging.getLogger(__name__)

#: §3.6: wakes for the same pane arriving within this window collapse into one
#: notify-pane injection.
MERGE_WINDOW_SECONDS = 10.0

#: §3.3a: nonce replay cache only needs to cover the envelope TTL — anything
#: older is already rejected by the timestamp check in envelope.verify().
NONCE_CACHE_TTL_SECONDS = 60.0

#: §4 M0-A result: notify-pane self-injection measured ~1.0s worst case; this
#: is a generous ceiling for the subprocess call itself, not the §3.5 rule-3
#: escalation window (that lives in outbound.py, sender-side).
_NOTIFY_PANE_SUBPROCESS_TIMEOUT_SECONDS = 15.0

_MAX_LINE_BYTES = 64 * 1024


class PaneIdResolutionError(RuntimeError):
    """§3.4 tier 3 (dynamic cwd scan) found zero or multiple matching panes —
    the daemon refuses to start rather than guess (design doc BLOCK-4)."""


def resolve_own_pane_id(project_id: str, *, herdr_bin: str = "herdr") -> str:
    """§3.4's three-tier resolution, in order:

    1. `HERDR_PANE_ID` env var — Herdr injects this into every pane's shell
       (verified directly against a live pane, 2026-08-01, not assumed).
    2. Pin file (`<signal_state_dir>/pane_id.pin`) — survives a daemon restart
       that lost the env-var inheritance (e.g. a health-check-triggered
       restart not forked from the original pane's shell).
    3. Dynamic `herdr pane list` scan matching this process's `cwd` — refuses
       to start (raises PaneIdResolutionError) on zero or multiple matches
       rather than guess; the doc's own BLOCK-4 resolution.

    Tier 1 success writes the pin file so a later restart that loses env-var
    inheritance still has tier 2 available.
    """
    pin_path = signal_state_dir(project_id) / "pane_id.pin"

    env_pane_id = os.environ.get("HERDR_PANE_ID")
    if env_pane_id:
        pin_path.parent.mkdir(parents=True, exist_ok=True)
        pin_path.write_text(env_pane_id)
        return env_pane_id

    if pin_path.exists():
        pinned = pin_path.read_text().strip()
        if pinned:
            return pinned

    cwd = str(Path.cwd())
    out = subprocess.run(
        [herdr_bin, "pane", "list"], capture_output=True, text=True, timeout=10, check=False
    )
    try:
        data = json.loads(out.stdout)
        panes = data["result"].get("panes", data["result"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PaneIdResolutionError(f"could not parse `herdr pane list` output: {exc}") from exc

    matches: list[str] = [p["pane_id"] for p in panes if p.get("cwd") == cwd]
    if len(matches) != 1:
        raise PaneIdResolutionError(
            f"pane_id resolution failed for cwd={cwd}: found {len(matches)} candidate "
            f"pane(s) {matches} via dynamic scan (need exactly 1). Refusing to start — "
            f"set HERDR_PANE_ID explicitly instead of guessing (design doc §3.4/BLOCK-4)."
        )
    return matches[0]


@dataclass
class _PendingWake:
    envelope: Envelope


class SignalDaemon:
    def __init__(
        self,
        project_id: str,
        own_pane_id: str,
        shared_secret: str,
        *,
        herdr_commander_bin: str = "herdr-commander",
        merge_window_seconds: float = MERGE_WINDOW_SECONDS,
    ) -> None:
        self.project_id = project_id
        self.own_pane_id = own_pane_id
        self._shared_secret = shared_secret
        self._herdr_commander_bin = herdr_commander_bin
        self._merge_window_seconds = merge_window_seconds

        self._nonce_seen: dict[str, float] = {}
        self._pending: list[_PendingWake] = []
        self._pending_lock = asyncio.Lock()
        self._batch_task: asyncio.Task[None] | None = None
        # §2.4 defect 7 / round-6 review: guards against the SAME message_id being
        # processed twice concurrently; unrelated message_ids never contend.
        self._processing: set[str] = set()
        self._processing_lock = asyncio.Lock()
        self._server: asyncio.Server | None = None

    # -- nonce replay protection (§3.3a: in-memory, TTL-bounded, not persisted) --

    def _check_and_record_nonce(self, nonce: str) -> bool:
        """Returns True if this nonce is fresh (not a replay). Sweeps expired
        entries on every call — no separate background thread needed, the
        cache is small and short-lived (see envelope.py's rationale)."""
        now = time.time()
        expired = [n for n, exp in self._nonce_seen.items() if exp < now]
        for n in expired:
            del self._nonce_seen[n]
        if nonce in self._nonce_seen:
            return False
        self._nonce_seen[nonce] = now + NONCE_CACHE_TTL_SECONDS
        return True

    # -- connection handling --------------------------------------------------

    async def handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not raw:
                return
            if len(raw) > _MAX_LINE_BYTES:
                logger.warning("dropping oversized envelope (%d bytes)", len(raw))
                return
            try:
                envelope = Envelope.from_json(raw.decode().strip())
            except Exception:
                logger.debug("envelope parsing failed, dropping (no ACK)", exc_info=True)
                return
            # Checked BEFORE HMAC verification, deliberately (see envelope.py's
            # DEPLOYMENT CONSTRAINT docstring): the "shared" secret is only
            # shared because both sides happen to be the same OS user on the
            # same host, so a cross-host sender would otherwise fail HMAC
            # verification with a confusing "bad hmac" that looks like
            # tampering instead of a deployment mismatch.
            own_hostname = socket.gethostname()
            if envelope.sender_hostname != own_hostname:
                logger.warning(
                    "dropping envelope from host=%s (this daemon runs on host=%s) -- "
                    "Herdr Bridge Signal only supports same-host, same-user deployment, "
                    "see signal/envelope.py's DEPLOYMENT CONSTRAINT docstring",
                    envelope.sender_hostname, own_hostname,
                )
                return
            try:
                verify(envelope, self._shared_secret)
            except Exception:
                logger.debug("envelope verification failed, dropping (no ACK)", exc_info=True)
                return
            if not self._check_and_record_nonce(envelope.nonce):
                logger.warning("dropping replayed nonce for message_id=%s", envelope.message_id)
                return

            # §3.2 step 3: reply Accepted immediately, over this connection —
            # notify-pane self-injection happens later, out of band (merge window).
            writer.write((json.dumps({"status": "accepted", "message_id": envelope.message_id}) + "\n").encode())
            await writer.drain()

            # §3.8 acceptance test 5 / §3.3's idempotency_key: a resend for the
            # same (to_project, inbox_ref) while an earlier attempt is still in
            # flight (not yet completed) must not trigger a second independent
            # injection/escalation chain — reply Accepted (harmless, and keeps
            # the sender from escalating) but skip enqueuing a duplicate.
            active = find_active_signal_by_target(self.project_id, envelope.to_project, envelope.inbox_ref)
            if active is not None and active["message_id"] != envelope.message_id:
                logger.info(
                    "dedup: message_id=%s shares idempotency target with in-flight message_id=%s, not enqueuing a duplicate",
                    envelope.message_id, active["message_id"],
                )
                return

            async with self._pending_lock:
                self._pending.append(_PendingWake(envelope))
                if self._batch_task is None or self._batch_task.done():
                    self._batch_task = asyncio.ensure_future(self._run_batch_after_delay())
        finally:
            writer.close()

    async def _run_batch_after_delay(self) -> None:
        await asyncio.sleep(self._merge_window_seconds)
        async with self._pending_lock:
            batch, self._pending = self._pending, []
        if batch:
            await self._process_batch(batch)

    async def _process_batch(self, batch: list[_PendingWake]) -> None:
        # §2.4 defect 7: skip any message_id another concurrent batch is already
        # handling (shouldn't normally happen — one merge queue per daemon — but
        # guards against a future code path that processes batches concurrently).
        async with self._processing_lock:
            fresh = [w for w in batch if w.envelope.message_id not in self._processing]
            for w in fresh:
                self._processing.add(w.envelope.message_id)
        if not fresh:
            return
        try:
            message = self._build_merged_message(fresh)
            ok = await self._notify_pane_self_inject(message)
            if ok:
                for w in fresh:
                    mark_injected(self.project_id, w.envelope.message_id)
                    # See module docstring's 2026-08-01 write-ownership note:
                    # nothing else in this codebase ever advances the ACK
                    # chain past "injected", so the daemon does it itself
                    # right away rather than leaving every Signal stuck.
                    mark_completed(self.project_id, w.envelope.message_id)
        finally:
            async with self._processing_lock:
                for w in fresh:
                    self._processing.discard(w.envelope.message_id)

    @staticmethod
    def _build_merged_message(batch: list[_PendingWake]) -> str:
        """§3.6: the injected text is only a wake-up notice, never the content
        itself — content always lives in RemaGraph, this only says how many."""
        if len(batch) == 1:
            return f"[Herdr Bridge Signal] 有 1 筆新訊息待查(inbox_ref={batch[0].envelope.inbox_ref})"
        return f"[Herdr Bridge Signal] 有 {len(batch)} 筆新訊息待查,請查詢 RemaGraph inbox"

    async def _notify_pane_self_inject(self, message: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            self._herdr_commander_bin, "notify-pane", "--pane", self.own_pane_id,
            "--allow-busy", "--no-audit", message,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_NOTIFY_PANE_SUBPROCESS_TIMEOUT_SECONDS
            )
        except TimeoutError:
            proc.kill()
            logger.warning("notify-pane self-injection timed out for pane=%s", self.own_pane_id)
            return False
        if proc.returncode != 0:
            logger.warning(
                "notify-pane self-injection failed for pane=%s: %s",
                self.own_pane_id, stderr.decode(errors="replace"),
            )
            return False
        return True

    # -- lifecycle --------------------------------------------------------

    async def serve(self, socket_path: Path) -> asyncio.Server:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        self._server = await asyncio.start_unix_server(self.handle_connection, path=str(socket_path))
        os.chmod(socket_path, 0o600)
        return self._server

    async def serve_forever(self, socket_path: Path) -> None:
        server = await self.serve(socket_path)
        async with server:
            await server.serve_forever()


async def run(project_id: str, shared_secret: str) -> None:
    """Entry point for `herdr-commander signal start` (§3.7)."""
    state_dir = signal_state_dir(project_id)
    with SingleInstanceLock(state_dir / "daemon.lock"):
        own_pane_id = resolve_own_pane_id(project_id)
        daemon = SignalDaemon(project_id, own_pane_id, shared_secret)
        socket_path = state_dir / f"{project_id}.sock"
        await daemon.serve_forever(socket_path)
