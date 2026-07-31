# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""AcpTransport Protocol — the seam for SDK evolution (docs/acp-command-plane-design.md §4.7, D-10).

The only implementation right now is `AcpxTransport` (wraps the acpx CLI
subprocess). If we ever switch to connecting directly to opencode via the
official ACP Python SDK, we just need to add another class implementing this
Protocol; the calling code in `AcpActions` (actions.py) doesn't need to
change.

`PromptHandle` is not a frozen dataclass — it holds a live
`subprocess.Popen`, which is inherently a mutable-state handle, so it doesn't
fit `models.py`'s "everything frozen" data-model convention. That's why it
lives here instead of in models.py.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from herdr_bridge.acp.adapter import (
    build_acpx_argv_and_env,
    build_acpx_policy_flags,
    resolve_claude_binary,
    resolve_patched_opencode_binary,
    write_session_config,
)
from herdr_bridge.acp.errors import AcpSessionError
from herdr_bridge.acp.events import extract_final_result, parse_stream
from herdr_bridge.acp.models import AcpEvent, AcpPolicy, AcpSessionInfo, PromptReason, PromptResult

logger = logging.getLogger(__name__)


class PromptHandle(Protocol):
    """The non-blocking call handle returned by `start_prompt()` and consumed by `wait_done()`/`cancel()`."""

    @property
    def session_name(self) -> str: ...


class AcpTransport(Protocol):
    """Abstraction over the ACP transport layer. Every method wraps a single,
    stateless acpx call — session state lives in acpx's own
    `~/.acpx/sessions/` storage, not in this object; the mapping from
    `session_name -> (agent, workdir)` is tracked by the caller
    (`AcpActions`) itself, and every method here requires the caller to pass
    both of those back in as-is.
    """

    def ensure_session(
        self, *, agent: str, workdir: Path, session_name: str, policy: AcpPolicy
    ) -> AcpSessionInfo: ...

    def close_session(self, session: AcpSessionInfo) -> None: ...

    def get_history(self, session: AcpSessionInfo) -> list[AcpEvent]: ...

    def run_prompt(
        self,
        session: AcpSessionInfo,
        text: str,
        *,
        timeout_sec: float,
        on_event: Callable[[AcpEvent], None] | None = None,
    ) -> PromptResult: ...

    def start_prompt(self, session: AcpSessionInfo, text: str) -> PromptHandle: ...

    def wait_done(self, handle: PromptHandle, *, timeout_sec: float) -> PromptResult: ...

    def cancel(self, handle: PromptHandle) -> None: ...


class AcpxPromptHandle:
    """The concrete implementation of `AcpxTransport.start_prompt()` — wraps a live `Popen`.

    `_events` is read/parsed all at once when `wait_done()` is called
    (`Popen.communicate()`), rather than being accumulated line-by-line on a
    background thread — the price of this simplified concurrency model is
    that `get_history()`/mid-flight event inspection sees nothing until
    `wait_done()` completes; you can only wait for the whole call to finish.
    A known limitation — see the M1 known tech debt in
    `docs/acpx-adapter-implementation-plan.md`.
    """

    def __init__(self, *, session_name: str, text: str, process: subprocess.Popen[str]) -> None:
        self._session_name = session_name
        self.text = text
        self.process = process
        self.started_at = time.monotonic()
        # Dual-determination fallback (docs/m0-acp-spike-evidence.md §11.2, M1
        # design-conclusion update): an in-generation cancel reliably reports
        # stopReason=="cancelled", but in the pre-generation edge case
        # opencode may return end_turn instead — this flag ensures that any
        # handle cancel() has been called on will still end up marked
        # reason="canceled" in wait_done().
        self.cancel_requested: bool = False

    @property
    def session_name(self) -> str:
        return self._session_name


def _default_agent_resolver(vendor_dir: Path) -> Callable[[str], Path]:
    def resolve(agent: str) -> Path:
        if agent == "opencode":
            path, warnings = resolve_patched_opencode_binary(vendor_dir)
            for warning in warnings:
                logger.warning(warning)
            return path
        if agent == "claude":
            return resolve_claude_binary()
        raise AcpSessionError(
            f"unsupported agent {agent!r} — only 'opencode' and 'claude' are wired up"
        )

    return resolve


def _collect_text(events: list[AcpEvent]) -> str:
    return "".join(e.text for e in events if e.text)


def _is_needs_reconnect(result: PromptResult) -> bool:
    """Detect a broken IPC socket connection between acpx's background queue-owner and the agent subprocess.

    acpx's `probeQueueOwnerHealth()` "needs reconnect" state means the socket
    probe failed; this function triggers on an error-message string match —
    not scoped to any particular policy or situation (the root cause isn't
    100% confirmed).
    """
    return result.reason == "error" and "needs reconnect" in (result.error or "")


class AcpxTransport:
    """The concrete implementation of `AcpTransport` — wraps the acpx CLI subprocess.

    The mapping from `session_name -> (config_path, policy)` is internal
    state this class tracks itself (`_configs`/`_policies`) — it is not
    stateless. `write_session_config()` uses `tempfile.mkstemp()` to generate
    a random filename, so later calls can't re-derive the same path from
    session_name alone; it has to be remembered. `ensure_session()` uses a
    **dedicated `session_dir` subdirectory** per session_name
    (`{session_dir}/{session_name}/`) — this is the correct fix, at this
    layer, for N5 (`write_session_config`'s orphan cleanup not distinguishing
    whether files are in use by another session): each session's orphan
    cleanup scope is inherently limited to its own directory, so it can never
    delete another session's config file.
    """

    def __init__(
        self,
        *,
        session_dir: Path,
        vendor_dir: Path | None = None,
        agent_resolver: Callable[[str], Path] | None = None,
        acpx_bin: str = "acpx",
        ttl_sec: int = 300,
    ) -> None:
        self._session_dir = session_dir
        self._resolve_agent = agent_resolver or _default_agent_resolver(
            vendor_dir or Path.cwd() / ".vendor" / "opencode-patched"
        )
        self._acpx_bin = acpx_bin
        # acpx keeps a detached background "queue-owner" + agent subprocess
        # alive per session for `--ttl` seconds after each CLI call returns
        # (default 300s) — found the hard way debugging this module's own
        # tests, where a short-lived test run left a pile of these running.
        # Real production use of long-lived multi-turn sessions wants the
        # 300s default (session reuse); tests should pass a small value.
        self._ttl_sec = ttl_sec
        self._configs: dict[str, Path | None] = {}
        self._policies: dict[str, AcpPolicy] = {}
        self._agents: dict[str, str] = {}
        self._workdirs: dict[str, Path] = {}

    def _base_argv(self, session_name: str, workdir: Path) -> tuple[list[str], dict[str, str]]:
        agent = self._agents[session_name]
        agent_binary = self._resolve_agent(agent)
        config_path = self._configs[session_name]
        policy = self._policies[session_name]
        argv, env = build_acpx_argv_and_env(
            agent=agent, agent_binary=agent_binary, config_path=config_path, cwd=workdir
        )
        argv[0] = self._acpx_bin
        argv = [*argv, "--ttl", str(self._ttl_sec), *build_acpx_policy_flags(policy)]
        if agent == "claude":
            argv.append("claude")
        return argv, env

    def ensure_session(
        self, *, agent: str, workdir: Path, session_name: str, policy: AcpPolicy
    ) -> AcpSessionInfo:
        agent_binary = self._resolve_agent(agent)
        this_session_dir = self._session_dir / session_name
        this_session_dir.mkdir(parents=True, exist_ok=True)

        if agent == "opencode":
            config_path: Path | None = write_session_config(policy, session_dir=this_session_dir)
        else:
            config_path = None

        argv, env = build_acpx_argv_and_env(
            agent=agent, agent_binary=agent_binary, config_path=config_path, cwd=workdir
        )
        argv[0] = self._acpx_bin
        if agent == "claude":
            argv = [*argv, "--ttl", str(self._ttl_sec), *build_acpx_policy_flags(policy), "claude", "sessions", "ensure"]
        else:
            argv = [*argv, "--ttl", str(self._ttl_sec), *build_acpx_policy_flags(policy), "sessions", "ensure"]
        result = subprocess.run(
            argv, cwd=workdir, env=env, capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode != 0:
            raise AcpSessionError(
                f"ensure_session({session_name!r}) failed: {result.stdout}\n{result.stderr}"
            )

        self._configs[session_name] = config_path
        self._policies[session_name] = policy
        self._agents[session_name] = agent
        self._workdirs[session_name] = workdir

        return AcpSessionInfo(
            session_name=session_name,
            agent=agent,
            workdir=str(workdir),
            acp_session_id=None,
            closed=False,
        )

    def close_session(self, session: AcpSessionInfo) -> None:
        argv, env = self._base_argv(session.session_name, Path(session.workdir))
        argv = [*argv, "sessions", "close"]
        subprocess.run(
            argv, cwd=session.workdir, env=env, capture_output=True, text=True, timeout=15, check=False
        )

        config_path = self._configs.pop(session.session_name, None)
        self._policies.pop(session.session_name, None)
        self._agents.pop(session.session_name, None)
        self._workdirs.pop(session.session_name, None)
        if config_path is not None:
            config_path.unlink(missing_ok=True)

    def get_history(self, session: AcpSessionInfo) -> list[AcpEvent]:
        argv, env = self._base_argv(session.session_name, Path(session.workdir))
        argv = [*argv, "--format", "json", "sessions", "read"]
        result = subprocess.run(
            argv, cwd=session.workdir, env=env, capture_output=True, text=True, timeout=30, check=False
        )
        return list(parse_stream(result.stdout.splitlines()))

    def _run_prompt_once(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        *,
        timeout_sec: float,
        session_name: str,
        on_event: Callable[[AcpEvent], None] | None,
    ) -> PromptResult:
        """A single acpx prompt subprocess call — parses NDJSON and extracts the final result."""
        try:
            result = subprocess.run(
                argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_sec + 30, check=False
            )
        except subprocess.TimeoutExpired:
            return PromptResult(
                reason="timeout",
                stop_reason=None,
                text="",
                session_name=session_name,
                usage=None,
                error="acpx did not return within timeout_sec + grace period",
            )

        events = list(parse_stream(result.stdout.splitlines()))
        if on_event:
            for event in events:
                on_event(event)

        final = extract_final_result(events)
        if final is None:
            return PromptResult(
                reason="error",
                stop_reason=None,
                text=_collect_text(events),
                session_name=session_name,
                usage=None,
                error=f"no final result observed; acpx exit_code={result.returncode}; stderr={result.stderr[-500:]}",
            )

        stop_reason = final.get("stopReason")
        return PromptResult(
            reason="canceled" if stop_reason == "cancelled" else "stop",
            stop_reason=stop_reason,
            text=_collect_text(events),
            session_name=session_name,
            usage=final.get("usage"),
            error=None,
        )

    def run_prompt(
        self,
        session: AcpSessionInfo,
        text: str,
        *,
        timeout_sec: float = 600,
        on_event: Callable[[AcpEvent], None] | None = None,
    ) -> PromptResult:
        argv, env = self._base_argv(session.session_name, Path(session.workdir))
        argv = [*argv, "--format", "json", "--timeout", str(int(timeout_sec)), text]

        result = self._run_prompt_once(
            argv, session.workdir, env,
            timeout_sec=timeout_sec,
            session_name=session.session_name,
            on_event=on_event,
        )

        if _is_needs_reconnect(result):
            logger.warning(
                "Detected 'needs reconnect' for session %r — retrying once after re-establishing session",
                session.session_name,
            )
            self.ensure_session(
                agent=self._agents[session.session_name],
                workdir=Path(session.workdir),
                session_name=session.session_name,
                policy=self._policies[session.session_name],
            )
            argv, env = self._base_argv(session.session_name, Path(session.workdir))
            argv = [*argv, "--format", "json", "--timeout", str(int(timeout_sec)), text]
            result = self._run_prompt_once(
                argv, session.workdir, env,
                timeout_sec=timeout_sec,
                session_name=session.session_name,
                on_event=on_event,
            )

        return result

    def start_prompt(self, session: AcpSessionInfo, text: str) -> AcpxPromptHandle:
        argv, env = self._base_argv(session.session_name, Path(session.workdir))
        argv = [*argv, "--format", "json", text]
        process = subprocess.Popen(
            argv, cwd=session.workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return AcpxPromptHandle(session_name=session.session_name, text=text, process=process)

    def _wait_done_once(
        self, handle: AcpxPromptHandle, *, timeout_sec: float
    ) -> PromptResult:
        """The single-shot wait_done logic (communicate + parse) — pulled out
        so a retry can call just this, without recursively triggering a
        second retry."""
        try:
            stdout, stderr = handle.process.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            return PromptResult(
                reason="timeout",
                stop_reason=None,
                text="",
                session_name=handle.session_name,
                usage=None,
                error="did not complete within timeout_sec",
            )

        events = list(parse_stream(stdout.splitlines()))
        final = extract_final_result(events)
        if final is None:
            if handle.cancel_requested:
                return PromptResult(
                    reason="canceled",
                    stop_reason=None,
                    text=_collect_text(events),
                    session_name=handle.session_name,
                    usage=None,
                    error=None,
                )
            return PromptResult(
                reason="error",
                stop_reason=None,
                text=_collect_text(events),
                session_name=handle.session_name,
                usage=None,
                error=f"no final result observed; acpx exit_code={handle.process.returncode}; stderr={stderr[-500:]}",
            )

        stop_reason = final.get("stopReason")
        reason: PromptReason = (
            "canceled"
            if stop_reason == "cancelled" or handle.cancel_requested
            else "stop"
        )
        return PromptResult(
            reason=reason,
            stop_reason=stop_reason,
            text=_collect_text(events),
            session_name=handle.session_name,
            usage=final.get("usage"),
            error=None,
        )

    def wait_done(self, handle: PromptHandle, *, timeout_sec: float = 60) -> PromptResult:
        assert isinstance(handle, AcpxPromptHandle), f"AcpxTransport.wait_done() got a foreign handle: {handle!r}"

        result = self._wait_done_once(handle, timeout_sec=timeout_sec)

        if _is_needs_reconnect(result):
            logger.warning(
                "Detected 'needs reconnect' for session %r in wait_done() — retrying once after re-establishing session",
                handle.session_name,
            )
            # Reap the old subprocess — communicate() has already finished
            # reading its output, but the OS may not have fully reaped the
            # subprocess yet; if it's still alive, wait for it to exit to
            # avoid accumulating zombie processes. Modeled on how cancel()
            # handles the process lifecycle (terminate -> wait -> kill ->
            # wait), but terminate isn't needed here: communicate() has
            # already closed stdin/stdout/stderr, so the subprocess should
            # have exited naturally — we just need to make sure it gets
            # reaped.
            if handle.process.poll() is None:
                handle.process.wait(timeout=5)
            self.ensure_session(
                agent=self._agents[handle.session_name],
                workdir=self._workdirs[handle.session_name],
                session_name=handle.session_name,
                policy=self._policies[handle.session_name],
            )
            retry_session = AcpSessionInfo(
                session_name=handle.session_name,
                agent=self._agents[handle.session_name],
                workdir=str(self._workdirs[handle.session_name]),
                acp_session_id=None,
                closed=False,
            )
            new_handle = self.start_prompt(retry_session, handle.text)
            result = self._wait_done_once(new_handle, timeout_sec=timeout_sec)

        return result

    def cancel(self, handle: PromptHandle) -> None:
        """Terminate the acpx subprocess underlying this handle (SIGTERM, then SIGKILL on timeout).

        Known simplification (M1 tech debt): this just kills the subprocess
        directly rather than sending acpx's actual `session/cancel` protocol
        message (which would need a separate, named `acpx <agent> cancel`
        call, and depends on the named agent-tier addressing from §4.5 that
        hasn't landed yet). Crude, but reliably effective; the tradeoff is
        that we don't get opencode's precise `stopReason="cancelled"` report
        (a known M0 spike protocol defect where `cancelled` gets misreported
        as `end_turn` — see m0-acp-spike-evidence.md).
        """
        assert isinstance(handle, AcpxPromptHandle), f"AcpxTransport.cancel() got a foreign handle: {handle!r}"
        handle.cancel_requested = True
        handle.process.terminate()
        try:
            handle.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            handle.process.wait(timeout=5)
