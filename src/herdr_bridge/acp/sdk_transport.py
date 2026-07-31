# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""AcpSdkTransport: a direct ACP Python SDK implementation (of the `AcpTransport` Protocol).

Bypasses the acpx CLI relay layer, talking directly to opencode's ACP
protocol endpoint using the PyPI `agent-client-protocol` package.

Import guard: protected by a top-level `try/except ImportError` — raises
`ImportError` with an install hint when the `herdr-bridge[acp-sdk]` extra is
missing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future
from pathlib import Path
from typing import Any, cast

try:
    from acp.client import ClientSideConnection
    from acp.schema import (
        AgentMessageChunk,
        AgentThoughtChunk,
        AllowedOutcome,
        DeniedOutcome,
        PermissionOption,
        PromptResponse,
        ReadTextFileResponse,
        TextContentBlock,
        ToolCallProgress,
        ToolCallStart,
        ToolCallUpdate,
        UserMessageChunk,
        WriteTextFileResponse,
    )

    _ACP_SDK_AVAILABLE = True
except ImportError:
    _ACP_SDK_AVAILABLE = False

from herdr_bridge.acp.adapter import resolve_claude_binary, resolve_patched_opencode_binary
from herdr_bridge.acp.errors import AcpSessionError, AcpTimeoutError
from herdr_bridge.acp.models import AcpEvent, AcpPolicy, AcpSessionInfo, PromptResult
from herdr_bridge.acp.transport import PromptHandle

logger = logging.getLogger(__name__)

# -- Permission handling aligned with AcpPolicy -------------------------------
_READ_KINDS: frozenset[str] = frozenset({"read", "search", "think", "fetch", "other"})


def _default_agent_resolver_sdk(vendor_dir: Path) -> Callable[[str], list[str]]:
    def resolve(agent: str) -> list[str]:
        if agent == "opencode":
            path, warnings = resolve_patched_opencode_binary(vendor_dir)
            for warning in warnings:
                logger.warning(warning)
            return [str(path), "acp"]
        if agent == "claude":
            return [str(resolve_claude_binary()), "acp"]
        raise AcpSessionError(
            f"unsupported agent {agent!r} — only 'opencode' and 'claude' are wired up"
        )

    return resolve


def _collect_text(events: list[AcpEvent]) -> str:
    return "".join(e.text for e in events if e.text)


# -- Client callback handler --------------------------------------------------


class _AcpSdkClient:
    """`acp.Client` Protocol implementation — handles agent callbacks and auto-answers permission requests."""

    def __init__(self, policy: AcpPolicy, events: list[AcpEvent]) -> None:
        self._policy = policy
        self._events = events

    def on_connect(self, conn: Any) -> None:
        pass

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> Any:
        """Auto-answer a permission request according to `AcpPolicy.mode`.

        - approve-all: picks `allow_always` (falls back to `allow_once`)
        - deny-all: returns `DeniedOutcome`
        - approve-reads: allows read-type tool kinds, denies everything else
        """
        if self._policy.mode == "deny-all":
            return DeniedOutcome(outcome="cancelled")

        if self._policy.mode == "approve-reads":
            kind = tool_call.kind or "other"
            if kind not in _READ_KINDS:
                return DeniedOutcome(outcome="cancelled")

        # approve-all, or approve-reads on a read-type kind: allow it
        for opt in options:
            if opt.kind == "allow_always":
                return AllowedOutcome(option_id=opt.option_id, outcome="selected")
        # fallback: if there's no "always" option, pick any "allow" option
        for opt in options:
            if opt.kind == "allow_once":
                return AllowedOutcome(option_id=opt.option_id, outcome="selected")
        # edge case: no allow option at all -> deny
        return DeniedOutcome(outcome="cancelled")

    async def session_update(
        self,
        session_id: str,
        update: (
            UserMessageChunk
            | AgentMessageChunk
            | AgentThoughtChunk
            | ToolCallStart
            | ToolCallProgress
            | Any
        ),
        **kwargs: Any,
    ) -> None:
        """Collect session update events into `_events`, for `get_history()` to return."""
        update_type = getattr(update, "session_update", None) or type(update).__name__

        text: str | None = None
        content = getattr(update, "content", None)
        if isinstance(content, TextContentBlock):
            text = content.text

        evt = AcpEvent(
            type=update_type,
            session_id=session_id,
            text=text,
            raw=update.model_dump() if hasattr(update, "model_dump") else {},
        )
        self._events.append(evt)

    async def read_text_file(
        self, session_id: str, path: str, line: int | None = None,
        limit: int | None = None, **kwargs: Any,
    ) -> ReadTextFileResponse:
        return ReadTextFileResponse(content="")

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any,
    ) -> WriteTextFileResponse | None:
        return WriteTextFileResponse()

    async def create_terminal(
        self, session_id: str, command: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        return type("_R", (), {"terminal_id": "term-1"})()

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any,
    ) -> Any:
        return type("_R", (), {"output": "", "truncated": False, "exit_status": None})()

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any,
    ) -> Any:
        return type("_R", (), {"exit_code": 0, "signal": None})()

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any,
    ) -> Any:
        return type("_R", (), {})()

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any,
    ) -> Any:
        return type("_R", (), {})()

    async def create_elicitation(
        self, message: str, mode: Any, **kwargs: Any,
    ) -> Any:
        return type("_R", (), {"elicitation_id": "el-0"})()

    async def complete_elicitation(
        self, elicitation_id: str, **kwargs: Any,
    ) -> None:
        pass

    async def ext_method(
        self, method: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        return {}

    async def ext_notification(
        self, method: str, params: dict[str, Any],
    ) -> None:
        pass


# -- Session entry (internal state) -------------------------------------------


class _SessionEntry:
    __slots__ = (
        "agent",
        "client",
        "conn",
        "policy",
        "process",
        "session_id",
        "workdir",
    )

    def __init__(
        self,
        *,
        conn: ClientSideConnection,
        process: asyncio.subprocess.Process,
        client: _AcpSdkClient,
        session_id: str,
        agent: str,
        workdir: Path,
        policy: AcpPolicy,
    ) -> None:
        self.conn = conn
        self.process = process
        self.client = client
        self.session_id = session_id
        self.agent = agent
        self.workdir = workdir
        self.policy = policy


# -- Prompt handle ------------------------------------------------------------


class _AcpSdkPromptHandle:
    """The handle returned by `start_prompt()` — holds a `concurrent.futures.Future`."""

    def __init__(self, *, session_name: str, text: str, future: Future[PromptResult]) -> None:
        self._session_name = session_name
        self.text = text
        self._future = future
        self.started_at = time.monotonic()
        self.cancel_requested: bool = False

    @property
    def session_name(self) -> str:
        return self._session_name

    def cancel_future(self) -> bool:
        return self._future.cancel()


# -- Transport ----------------------------------------------------------------


class AcpSdkTransport:
    """The direct ACP Python SDK implementation of `AcpTransport`.

    Maintains one background event-loop thread that handles all async ACP
    communication; the public-facing `AcpTransport` methods are all
    synchronous (to satisfy the Protocol signature).
    """

    def __init__(
        self,
        *,
        agent_resolver: Callable[[str], list[str]] | None = None,
        vendor_dir: Path | None = None,
    ) -> None:
        if not _ACP_SDK_AVAILABLE:
            raise ImportError(
                "agent-client-protocol is required for AcpSdkTransport. "
                "Install with: pip install herdr-bridge[acp-sdk]"
            )

        self._resolve_agent = agent_resolver or _default_agent_resolver_sdk(
            vendor_dir or Path.cwd() / ".vendor" / "opencode-patched"
        )
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._event_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("AcpSdkTransport event loop failed to start within 5 sec")

        self._sessions: dict[str, _SessionEntry] = {}

    # -- event loop ----------------------------------------------------------

    def _event_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def _run_async(
        self, coro: Any, *, timeout: float | None = None,
    ) -> Any:
        """Submit a coroutine to the background event loop and wait synchronously for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise AcpTimeoutError(
                f"async operation timed out after {timeout}s"
            ) from None

    # -- agent spawn ----------------------------------------------------------

    async def _spawn_and_connect(
        self, agent: str, workdir: Path,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.subprocess.Process]:
        argv = self._resolve_agent(agent)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
        )
        return process.stdout, process.stdin, process  # type: ignore[return-value]

    # -- ensure_session -------------------------------------------------------

    async def _async_ensure_session(
        self, *, agent: str, workdir: Path, session_name: str, policy: AcpPolicy,
    ) -> AcpSessionInfo:
        reader, writer, process = await self._spawn_and_connect(agent, workdir)

        events: list[AcpEvent] = []
        client = _AcpSdkClient(policy=policy, events=events)
        conn = ClientSideConnection(client, writer, reader)

        try:
            await conn.initialize(protocol_version=1)

            sess_resp = await conn.new_session(cwd=str(workdir))

            self._sessions[session_name] = _SessionEntry(
                conn=conn,
                process=process,
                client=client,
                session_id=sess_resp.session_id,
                agent=agent,
                workdir=workdir,
                policy=policy,
            )

            return AcpSessionInfo(
                session_name=session_name,
                agent=agent,
                workdir=str(workdir),
                acp_session_id=sess_resp.session_id,
                closed=False,
            )
        except BaseException:
            # Cleanup: close the connection and subprocess if setup failed
            try:
                await conn.close()
            except Exception as exc:  # noqa: BLE001 -- best-effort cleanup after failed setup; must not shadow the original exception being re-raised below
                logger.debug("conn.close() failed during ensure_session cleanup: %s", exc)
            try:
                process.terminate()
            except OSError as exc:
                logger.debug("process.terminate() failed during ensure_session cleanup: %s", exc)
            raise

    def ensure_session(
        self, *, agent: str, workdir: Path, session_name: str, policy: AcpPolicy,
    ) -> AcpSessionInfo:
        if session_name in self._sessions:
            entry = self._sessions[session_name]
            return AcpSessionInfo(
                session_name=session_name,
                agent=entry.agent,
                workdir=str(entry.workdir),
                acp_session_id=entry.session_id,
                closed=False,
            )
        return cast(
            AcpSessionInfo,
            self._run_async(
                self._async_ensure_session(
                    agent=agent, workdir=workdir, session_name=session_name, policy=policy,
                ),
                timeout=60,
            ),
        )

    # -- close_session --------------------------------------------------------

    async def _async_close_session(self, session_name: str, session_id: str) -> None:
        entry = self._sessions.pop(session_name, None)
        if entry is None:
            return
        try:
            await asyncio.wait_for(
                entry.conn.close_session(session_id),
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort session teardown; subsequent cleanup steps must still run regardless of failure here
            logger.debug("close_session() failed during teardown for %r: %s", session_name, exc)
        try:
            await asyncio.wait_for(entry.conn.close(), timeout=5)
        except Exception as exc:  # noqa: BLE001 -- best-effort teardown; the process-kill cleanup below must still run regardless of failure here
            logger.debug("conn.close() failed during teardown for %r: %s", session_name, exc)
        try:
            entry.process.terminate()
            try:
                await asyncio.wait_for(entry.process.wait(), timeout=5)
            except TimeoutError:
                entry.process.kill()
                await entry.process.wait()
        except Exception as exc:  # noqa: BLE001 -- last-resort process cleanup at the end of session teardown; nothing further to fall back to
            logger.debug("process cleanup failed during teardown for %r: %s", session_name, exc)

    def close_session(self, session: AcpSessionInfo) -> None:
        entry = self._sessions.get(session.session_name)
        if entry is None:
            raise AcpSessionError(
                f"cannot close unknown session: {session.session_name!r}"
            )
        self._run_async(
            self._async_close_session(session.session_name, entry.session_id),
            timeout=30,
        )

    # -- get_history ----------------------------------------------------------

    def get_history(self, session: AcpSessionInfo) -> list[AcpEvent]:
        entry = self._sessions.get(session.session_name)
        if entry is None:
            return []
        return list(entry.client._events)

    # -- run_prompt -----------------------------------------------------------

    async def _async_run_prompt(
        self,
        session_name: str,
        session_id: str,
        text: str,
        *,
        timeout_sec: float,
        on_event: Callable[[AcpEvent], None] | None,
    ) -> PromptResult:
        entry = self._sessions.get(session_name)
        if entry is None:
            return PromptResult(
                reason="error",
                stop_reason=None,
                text="",
                session_name=session_name,
                usage=None,
                error=f"unknown session: {session_name!r}",
            )

        try:
            resp: PromptResponse = await asyncio.wait_for(
                entry.conn.prompt(
                    session_id,
                    [TextContentBlock(type="text", text=text)],
                ),
                timeout=timeout_sec,
            )
        except TimeoutError:
            return PromptResult(
                reason="timeout",
                stop_reason=None,
                text="",
                session_name=session_name,
                usage=None,
                error=f"prompt did not complete within {timeout_sec}s",
            )

        # Make sure any pending session_update notification has been
        # processed: prompt()'s response can come back from await before the
        # session/update notification the agent sent, because the
        # notification runs on its own asyncio.create_task task. Yield control
        # back to the event loop to make sure the notification has been
        # dispatched and written to _events.
        await asyncio.sleep(0)

        # Collect this turn's new events (pre-prompt events are already in
        # `events`; new events during the prompt are appended in real time by
        # the session_update callback — we don't roll back to the pre-prompt
        # state here).
        events = entry.client._events

        if on_event:
            for evt in events:
                on_event(evt)

        stop_reason = resp.stop_reason
        reason: Any = "canceled" if stop_reason == "cancelled" else "stop"
        return PromptResult(
            reason=reason,
            stop_reason=stop_reason,
            text=_collect_text(events),
            session_name=session_name,
            usage=resp.usage.model_dump() if resp.usage is not None else None,
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
        entry = self._sessions.get(session.session_name)
        if entry is None:
            return PromptResult(
                reason="error",
                stop_reason=None,
                text="",
                session_name=session.session_name,
                usage=None,
                error=f"unknown session: {session.session_name!r}",
            )
        try:
            return cast(
                PromptResult,
                self._run_async(
                    self._async_run_prompt(
                        session_name=session.session_name,
                        session_id=entry.session_id,
                        text=text,
                        timeout_sec=timeout_sec,
                        on_event=on_event,
                    ),
                    timeout=timeout_sec + 60,
                ),
            )
        except AcpTimeoutError as exc:
            return PromptResult(
                reason="timeout",
                stop_reason=None,
                text="",
                session_name=session.session_name,
                usage=None,
                error=str(exc),
            )

    # -- start_prompt / wait_done / cancel ------------------------------------

    def start_prompt(self, session: AcpSessionInfo, text: str) -> PromptHandle:
        entry = self._sessions.get(session.session_name)
        if entry is None:
            raise AcpSessionError(
                f"unknown session: {session.session_name!r}"
            )

        coro = self._async_run_prompt(
            session_name=session.session_name,
            session_id=entry.session_id,
            text=text,
            timeout_sec=86400,  # the actual timeout is controlled by wait_done's own timeout_sec
            on_event=None,
        )
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return _AcpSdkPromptHandle(
            session_name=session.session_name,
            text=text,
            future=future,
        )

    def wait_done(self, handle: PromptHandle, *, timeout_sec: float = 60) -> PromptResult:
        assert isinstance(handle, _AcpSdkPromptHandle), (
            f"AcpSdkTransport.wait_done() got a foreign handle: {handle!r}"
        )
        try:
            return handle._future.result(timeout=timeout_sec)
        except TimeoutError:
            handle.cancel_future()
            return PromptResult(
                reason="timeout",
                stop_reason=None,
                text="",
                session_name=handle.session_name,
                usage=None,
                error=f"wait_done timed out after {timeout_sec}s",
            )
        except FutureCancelledError:
            return PromptResult(
                reason="canceled",
                stop_reason=None,
                text="",
                session_name=handle.session_name,
                usage=None,
                error="prompt was cancelled",
            )
        except Exception as exc:  # noqa: BLE001 -- final catch-all that turns any prompt-execution failure into a structured degraded PromptResult instead of crashing the caller
            return PromptResult(
                reason="error",
                stop_reason=None,
                text="",
                session_name=handle.session_name,
                usage=None,
                error=str(exc),
            )

    def cancel(self, handle: PromptHandle) -> None:
        assert isinstance(handle, _AcpSdkPromptHandle), (
            f"AcpSdkTransport.cancel() got a foreign handle: {handle!r}"
        )
        handle.cancel_requested = True

        entry = self._sessions.get(handle.session_name)
        if entry is not None:
            self._run_async(
                self._async_cancel_inner(handle.session_name, entry.session_id),
                timeout=10,
            )
        handle.cancel_future()

    async def _async_cancel_inner(self, session_name: str, session_id: str) -> None:
        entry = self._sessions.get(session_name)
        if entry is None:
            return
        try:
            await asyncio.wait_for(
                entry.conn.cancel(session_id),
                timeout=5,
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort cancel notification; cancel_future() already handles caller-visible cancellation regardless of whether this notification succeeds
            logger.debug("conn.cancel() failed for session %r: %s", session_name, exc)
