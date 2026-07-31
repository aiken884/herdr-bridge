# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""AcpActions -- the public facade for the ACP command plane
(docs/acp-command-plane-design.md §4.2).

`actor_id` is always the first parameter, and each method records exactly one
audit entry (reusing the existing `herdr_bridge.audit.AuditLogger`, the same
JSONL file, action names prefixed with `acp.` -- per §4.4's "two-plane
architecture": no new AuditLogger, no ACP-layer leasing since the acpx queue
already handles that, and no re-litigating ADR 0001), and returns a frozen
dataclass.

`AcpActions` itself holds the `session_name -> AcpSessionInfo` mapping
(`_sessions`), the only mutable internal state this facade has. Every
`AcpTransport` method requires session info to be passed in explicitly (see
the design notes in transport.py), so remembering "which session_name maps to
which AcpSessionInfo" is consolidated here in one place -- callers (the
governance layer) only ever need to pass a session_name string.
"""

from __future__ import annotations

import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from herdr_bridge.acp.adapter import resolve_patched_opencode_binary
from herdr_bridge.acp.errors import AcpAdapterError, AcpSessionError
from herdr_bridge.acp.models import AcpAgentSpec, AcpEvent, AcpPolicy, AcpSessionInfo, PromptResult
from herdr_bridge.acp.transport import AcpTransport, AcpxTransport, PromptHandle
from herdr_bridge.acp.workdir_guard import check_opencode_workdir_isolation
from herdr_bridge.audit import AuditLogger

_BUILTIN_AGENTS = ("opencode", "claude")


class AcpActions:
    def __init__(self, *, transport: AcpTransport, audit: AuditLogger, acpx_bin: str = "acpx") -> None:
        self._transport = transport
        self._audit = audit
        self._acpx_bin = acpx_bin
        self._sessions: dict[str, AcpSessionInfo] = {}
        # per-session_name lock, keyed lazily — see ensure_session() for why this
        # needs to exist at all (concurrent callers racing the same session_name).
        # Never removed on close_session(): the key set is bounded by the number
        # of distinct session_names a process ever sees, not by concurrency, so
        # unbounded growth here is not a real leak in practice.
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        # per-workdir lock (keyed by resolved absolute path), closes the
        # narrower race two different session_names can hit when they target
        # the same workdir concurrently — see ensure_session() docstring.
        # Unlike _session_locks, this one IS cleaned up in close_session():
        # ADR 0003 guarantees at most one active session per workdir at a
        # time, so once that session closes there is no reason to keep the
        # lock entry around, and the key set otherwise grows with every
        # distinct workdir ever used (not bounded like session_name reuse).
        self._workdir_locks: dict[str, threading.Lock] = {}
        self._workdir_registry_lock = threading.Lock()

    def _lock_for_session(self, session_name: str) -> threading.Lock:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_name)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_name] = lock
            return lock

    def _get_workdir_lock(self, resolved_workdir: str) -> threading.Lock:
        with self._workdir_registry_lock:
            lock = self._workdir_locks.get(resolved_workdir)
            if lock is None:
                lock = threading.Lock()
                self._workdir_locks[resolved_workdir] = lock
            return lock

    def _require_session(self, session_name: str) -> AcpSessionInfo:
        info = self._sessions.get(session_name)
        if info is None:
            raise AcpSessionError(f"unknown session_name (never ensure_session()'d, or already closed): {session_name!r}")
        return info

    def _enforce_workdir_isolation(
        self, actor_id: str, action: str, agent: str, workdir: Path, session_name: str
    ) -> None:
        """ADR 0003 Decision #2: mandatory workdir/worktree isolation for
        opencode-family tiers. Callers (`ensure_session()`/`exec_prompt()`)
        must run this before calling `self._transport.ensure_session()` --
        a rejection must never be discovered only after an acpx
        session/config file already exists (see the module notes in
        `workdir_guard.py`).

        `self._sessions` is the only view of active sessions this facade
        holds (see the module docstring); `AcpTransport` has no such view,
        so this check can only live at this layer.
        """
        active_workdirs = {
            name: Path(info.workdir) for name, info in list(self._sessions.items()) if not info.closed
        }
        try:
            check_opencode_workdir_isolation(agent=agent, workdir=workdir, active_workdirs=active_workdirs)
        except AcpSessionError as exc:
            self._audit.record(
                actor_id,
                action,
                session_name=session_name,
                agent=agent,
                idempotent_hit=False,
                rejected_reason=str(exc),
            )
            raise

    def list_acp_agents(self, actor_id: str) -> list[AcpAgentSpec]:
        """Reads the acpx config, read-only (§4.2). Reports the built-in
        tiers (`opencode`, `claude`) that this adapter already has resolvers
        wired up for.
        """
        _DESC: dict[str, str] = {
            "opencode": "<AcpxAdapter: resolve_patched_opencode_binary()>",
            "claude": "<AcpxAdapter: acpx claude (named subcommand, 0.12.0+)>",
        }
        specs = [
            AcpAgentSpec(name=name, command=_DESC.get(name, "<AcpxAdapter: builtin>"), builtin=True)
            for name in _BUILTIN_AGENTS
        ]
        self._audit.record(actor_id, "acp.list_acp_agents", count=len(specs))
        return specs

    def ensure_session(
        self,
        actor_id: str,
        agent: str,
        workdir: str,
        session_name: str,
        *,
        policy: AcpPolicy | None = None,
    ) -> AcpSessionInfo:
        """Idempotent -- if `session_name` already exists, returns the
        existing record directly without calling acpx again (avoids
        needlessly rewriting the permission config file / re-running
        sessions ensure).

        For concurrent calls on the same `session_name`, this check-then-act
        is serialized with one `threading.Lock` per session_name
        (`_lock_for_session()`) -- `self._sessions` is an unlocked dict, so
        without serialization two concurrent calls could both pass the
        "doesn't exist yet" check and each call
        `self._transport.ensure_session()`. Under `AcpxTransport` that would
        each call `adapter.write_session_config()`, whose orphan-file cleanup
        deletes "every file in the same directory matching the naming
        convention" without distinguishing whether a given file was just
        written by another concurrent call and hasn't been read by acpx yet
        -- if that file gets deleted, opencode failing to read the config is
        fail-open, silently falling back to `"*":"allow"` (see the
        `adapter.write_session_config` docstring) rather than erroring,
        which is why the lock here is the primary line of defense. The lock
        is first checked via a fast unlocked read (most calls are idempotent
        hits and never need to touch the lock); the lock is only acquired
        when actually creating a session, with a second check performed
        inside the lock (double-checked locking), guaranteeing that creation
        for a given session_name only ever actually runs once.

        `_enforce_workdir_isolation()` (ADR 0003 Decision #2) is deliberately
        placed **inside** the lock, before `self._transport.ensure_session()`
        -- the lock guarantees "the same session_name is only created once,"
        not "the same workdir is only claimed by one session_name"; the
        latter is handled by the workdir-specific lock described below.

        Fix from the PPLX high-rigor review consensus: `_session_locks` is
        keyed per session_name, so if two **different** session_names call
        concurrently and both target the same workdir, both could pass
        `_enforce_workdir_isolation()`'s "shared workdir" check before the
        other has written its workdir into `self._sessions` -- this window
        is much narrower than the same-session_name race (it only triggers
        if a caller deliberately points two different session_names at the
        same workdir), but it still exists in theory. The fix deliberately
        avoids a single global lock (which would make completely unrelated
        workdirs block each other -- a throughput bottleneck at fleet
        scale), and instead uses `_get_workdir_lock()`, keyed by `workdir`
        (the resolved absolute path string): like `_lock_for_session()`,
        it's double-checked lazy creation. The two locks resolve different
        races and are held nested -- the outer session_name lock guarantees
        the check-then-act for a given session_name only runs once; the
        inner workdir lock guarantees that, for a given workdir, the
        isolation check (`_enforce_workdir_isolation()`) through
        registration (`self._sessions[session_name] = info`, including the
        actual creation via `self._transport.ensure_session()` in between)
        is atomic with respect to other session_names -- there's no window
        where two different session_names both pass the check and both
        succeed in creating a session."""
        existing = self._sessions.get(session_name)
        if existing is not None:
            self._audit.record(actor_id, "acp.ensure_session", session_name=session_name, agent=agent, idempotent_hit=True)
            return existing

        with self._lock_for_session(session_name):
            existing = self._sessions.get(session_name)
            if existing is not None:
                self._audit.record(
                    actor_id, "acp.ensure_session", session_name=session_name, agent=agent, idempotent_hit=True
                )
                return existing

            workdir_path = Path(workdir)
            resolved_workdir = str(workdir_path.resolve())

            with self._get_workdir_lock(resolved_workdir):
                self._enforce_workdir_isolation(actor_id, "acp.ensure_session", agent, workdir_path, session_name)

                effective_policy = replace(policy or AcpPolicy(), policy_enforced=(agent != "opencode"))
                info = self._transport.ensure_session(
                    agent=agent, workdir=workdir_path, session_name=session_name, policy=effective_policy
                )
                self._sessions[session_name] = info

            self._audit.record(
                actor_id,
                "acp.ensure_session",
                session_name=session_name,
                agent=agent,
                policy_mode=effective_policy.mode,
                policy_enforced=effective_policy.policy_enforced,
                idempotent_hit=False,
            )
            return info

    def close_session(self, actor_id: str, session_name: str) -> None:
        info = self._sessions.pop(session_name, None)
        if info is None:
            raise AcpSessionError(f"cannot close unknown session_name: {session_name!r}")
        self._transport.close_session(info)
        # Safe to drop here: ADR 0003 guarantees at most one active session
        # per workdir, so once this session closes nothing still needs its
        # workdir lock entry — leaving it would grow _workdir_locks unbounded
        # with the number of distinct workdirs ever used.
        resolved_workdir = str(Path(info.workdir).resolve())
        with self._workdir_registry_lock:
            self._workdir_locks.pop(resolved_workdir, None)
        self._audit.record(actor_id, "acp.close_session", session_name=session_name)

    def get_history(self, actor_id: str, session_name: str) -> list[AcpEvent]:
        info = self._require_session(session_name)
        events = self._transport.get_history(info)
        self._audit.record(actor_id, "acp.get_history", session_name=session_name, count=len(events))
        return events

    def prompt(
        self,
        actor_id: str,
        session_name: str,
        text: str,
        *,
        priority: int = 0,
        policy: AcpPolicy | None = None,
        timeout_sec: float = 600,
        on_event: Callable[[AcpEvent], None] | None = None,
        before_prompt: Callable[[str], str] | None = None,
        after_prompt: Callable[[PromptResult], None] | None = None,
    ) -> PromptResult:
        """Blocks until stopReason. The `policy` parameter is currently
        unused -- policy can only be decided once, when `ensure_session()`
        creates the session; changing policy mid-session for a still-live
        session would require rewriting the config file (a future extension,
        out of scope for this stage). This parameter is accepted only to
        conform to the frozen §4.2 signature -- callers may pass it, but it
        has no effect; this is deliberate, not a fake-effect no-op.
        `priority` is written to the audit log as-is (a spec-reserved field;
        no scheduling decisions are made at this stage).

        before_prompt: if provided, called before run_prompt -- used e.g. for
        injecting memory.
        after_prompt: if provided, called after run_prompt and before audit
        -- used e.g. for persisting the result. Exceptions propagate through
        and are the caller's responsibility.
        """
        info = self._require_session(session_name)
        if before_prompt:
            text = before_prompt(text)
        result = self._transport.run_prompt(info, text, timeout_sec=timeout_sec, on_event=on_event)
        if after_prompt:
            after_prompt(result)
        self._audit.record(
            actor_id,
            "acp.prompt",
            session_name=session_name,
            priority=priority,
            chars=len(text),
            reason=result.reason,
            stop_reason=result.stop_reason,
        )
        return result

    def exec_prompt(
        self,
        actor_id: str,
        agent: str,
        text: str,
        *,
        workdir: str,
        policy: AcpPolicy | None = None,
        timeout_sec: float = 600,
        on_event: Callable[[AcpEvent], None] | None = None,
        before_prompt: Callable[[str], str] | None = None,
        after_prompt: Callable[[PromptResult], None] | None = None,
    ) -> PromptResult:
        """Stateless, one-shot: generates a one-off session_name internally
        and, regardless of success/failure/timeout, always calls
        `close_session` afterward -- no session is left behind for the
        caller to clean up.

        `exec_prompt()` calls `self._transport.ensure_session()` directly,
        just like `ensure_session()` does -- it's the same "create an
        opencode session" entry point, so the ADR 0003 Decision #2 workdir
        isolation check applies here too (otherwise this would be a bypass
        path around `ensure_session()`'s check entirely, rendering the whole
        M1 isolation line of defense moot).

        before_prompt / after_prompt / on_event behave the same as in
        prompt().
        """
        session_name = f"exec-{uuid.uuid4()}"
        workdir_path = Path(workdir)
        self._enforce_workdir_isolation(actor_id, "acp.exec_prompt", agent, workdir_path, session_name)

        effective_policy = replace(policy or AcpPolicy(), policy_enforced=(agent != "opencode"))
        info = self._transport.ensure_session(
            agent=agent, workdir=workdir_path, session_name=session_name, policy=effective_policy
        )
        try:
            if before_prompt:
                text = before_prompt(text)
            result = self._transport.run_prompt(info, text, timeout_sec=timeout_sec, on_event=on_event)
            if after_prompt:
                after_prompt(result)
        finally:
            self._transport.close_session(info)
        self._audit.record(
            actor_id,
            "acp.exec_prompt",
            agent=agent,
            chars=len(text),
            reason=result.reason,
            stop_reason=result.stop_reason,
            policy_mode=effective_policy.mode,
            policy_enforced=effective_policy.policy_enforced,
        )
        return result

    def start_prompt(self, actor_id: str, session_name: str, text: str) -> PromptHandle:
        info = self._require_session(session_name)
        handle = self._transport.start_prompt(info, text)
        self._audit.record(actor_id, "acp.start_prompt", session_name=session_name, chars=len(text))
        return handle

    def wait_done(self, actor_id: str, handle: PromptHandle, *, timeout_sec: float = 60) -> PromptResult:
        """Never raises -- everything is expressed via `PromptResult.reason`
        (mirroring the existing `wait_until`/`WaitResult` philosophy)."""
        result = self._transport.wait_done(handle, timeout_sec=timeout_sec)
        self._audit.record(
            actor_id,
            "acp.wait_done",
            session_name=handle.session_name,
            reason=result.reason,
            stop_reason=result.stop_reason,
        )
        return result

    def cancel(self, actor_id: str, handle: PromptHandle) -> None:
        self._transport.cancel(handle)
        self._audit.record(actor_id, "acp.cancel", session_name=handle.session_name)

    def close(self) -> None:
        """Closes every session still on record as active (no audit entry --
        there's no clear actor_id here; the caller typically calls this
        before process shutdown, not as a single user-triggered action)."""
        for session_name in list(self._sessions):
            info = self._sessions.pop(session_name)
            self._transport.close_session(info)


def connect(
    *,
    acpx_bin: str = "acpx",
    config_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    transcript_dir: str | Path | None = None,
    strict_version: bool = False,
) -> AcpActions:
    """`AcpActions` factory (§4.2).

    `transcript_dir` keeps the parameter shape but is currently a no-op --
    §4.4 explicitly documents that transcript mirroring is a companion piece
    for the O1/O2 optimization spike; the O3 baseline (this implementation)
    doesn't need it.

    When `strict_version=True`, uses `resolve_patched_opencode_binary()` to
    check whether `.vendor/opencode-patched/MANIFEST.json`'s
    `base_upstream_version` falls within the manually-verified
    `compatible_upstream_range`, raising `AcpAdapterError` directly (fail
    loud) rather than just logging a warning if it's out of range. **This is
    not** the acpx<->agent `protocolVersion` handshake verification planned
    for M0-V9 (the original purpose of `AcpVersionError`) -- that check isn't
    implemented at this stage and is a known piece of tech debt; see
    docs/acpx-adapter-implementation-plan.md.
    """
    del transcript_dir  # see the docstring above: the O3 baseline doesn't need it, so this parameter is deliberately accepted but unused

    vendor_dir = Path.cwd() / ".vendor" / "opencode-patched"
    if strict_version:
        _path, warnings = resolve_patched_opencode_binary(vendor_dir)
        if warnings:
            raise AcpAdapterError("; ".join(warnings))

    session_dir = Path(config_path) if config_path is not None else Path(tempfile.mkdtemp(prefix="herdr-bridge-acp-"))
    transport = AcpxTransport(session_dir=session_dir, vendor_dir=vendor_dir, acpx_bin=acpx_bin)
    audit = AuditLogger(audit_path)
    return AcpActions(transport=transport, audit=audit, acpx_bin=acpx_bin)
