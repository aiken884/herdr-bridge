# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.sdk_transport: behavior tests for `AcpSdkTransport` against a fake ACP
agent (`tests/fixtures/fake_acp_agent.py`). Covers the four core capabilities
of the AcpTransport Protocol (session creation, prompt round-trip, session
close, automated request_permission responses) plus the
start_prompt/wait_done/cancel flow.

Also uses unit tests to verify the correctness of the import guard and the
policy mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from herdr_bridge.acp.errors import AcpSessionError
from herdr_bridge.acp.models import AcpPolicy, AcpSessionInfo

_FAKE_AGENT = (Path(__file__).parent / "fixtures" / "fake_acp_agent.py").resolve()

# -- if the ACP SDK isn't installed (e.g. CI without the acp-sdk extra), mark the whole module skipped
_acp_sdk_available = False
try:
    from herdr_bridge.acp.sdk_transport import (
        _ACP_SDK_AVAILABLE,
        AcpSdkTransport,
        _AcpSdkClient,
        _AcpSdkPromptHandle,
    )

    _acp_sdk_available = _ACP_SDK_AVAILABLE
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    not _acp_sdk_available,
    reason="requires agent-client-protocol (install herdr-bridge[acp-sdk])",
)


def _transport(agent_resolver: Any = None) -> AcpSdkTransport:
    if agent_resolver is None:
        def _default_resolver(agent: str) -> list[str]:
            return ["python3", str(_FAKE_AGENT)]

        agent_resolver = _default_resolver
    return AcpSdkTransport(agent_resolver=agent_resolver)


def _make_workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "wd"
    wd.mkdir()
    return wd


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


class TestImportGuard:
    def test_import_error_when_sdk_not_installed(self) -> None:
        """Simulating agent-client-protocol not being installed, constructing AcpSdkTransport should raise ImportError."""
        with patch(
            "herdr_bridge.acp.sdk_transport._ACP_SDK_AVAILABLE", False
        ), pytest.raises(ImportError, match="herdr-bridge\\[acp-sdk\\]"):
            AcpSdkTransport()


# ---------------------------------------------------------------------------
# Permission policy unit tests (no subprocess needed)
# ---------------------------------------------------------------------------


class TestPermissionPolicy:
    def test_approve_all_returns_allowed(self) -> None:
        """approve-all: should pick the allow_always option and return an AllowedOutcome."""
        from acp.schema import (
            AllowedOutcome,
            PermissionOption,
            ToolCallUpdate,
        )

        client = _AcpSdkClient(policy=AcpPolicy(mode="approve-all"), events=[])
        options = [
            PermissionOption(option_id="o1", name="Allow", kind="allow_always"),
            PermissionOption(option_id="o2", name="Reject", kind="reject_always"),
        ]
        tc = ToolCallUpdate(tool_call_id="tc1")

        import asyncio
        result = asyncio.run(
            client.request_permission("s1", tc, options)
        )
        assert isinstance(result, AllowedOutcome)
        assert result.outcome == "selected"
        assert result.option_id == "o1"

    def test_deny_all_returns_denied(self) -> None:
        """deny-all: should return a DeniedOutcome."""
        from acp.schema import (
            DeniedOutcome,
            PermissionOption,
            ToolCallUpdate,
        )

        client = _AcpSdkClient(policy=AcpPolicy(mode="deny-all"), events=[])
        options = [
            PermissionOption(option_id="o1", name="Allow", kind="allow_always"),
        ]
        tc = ToolCallUpdate(tool_call_id="tc1")

        import asyncio
        result = asyncio.run(
            client.request_permission("s1", tc, options)
        )
        assert isinstance(result, DeniedOutcome)
        assert result.outcome == "cancelled"

    def test_approve_reads_allows_read_kind(self) -> None:
        """approve-reads: tool kind='read' should be allowed through."""
        from acp.schema import (
            AllowedOutcome,
            PermissionOption,
            ToolCallUpdate,
        )

        client = _AcpSdkClient(policy=AcpPolicy(mode="approve-reads"), events=[])
        options = [
            PermissionOption(option_id="o1", name="Allow", kind="allow_always"),
        ]
        tc = ToolCallUpdate(tool_call_id="tc1", kind="read")

        import asyncio
        result = asyncio.run(
            client.request_permission("s1", tc, options)
        )
        assert isinstance(result, AllowedOutcome)

    def test_approve_reads_denies_edit_kind(self) -> None:
        """approve-reads: tool kind='edit' should be denied."""
        from acp.schema import (
            DeniedOutcome,
            PermissionOption,
            ToolCallUpdate,
        )

        client = _AcpSdkClient(policy=AcpPolicy(mode="approve-reads"), events=[])
        options = [
            PermissionOption(option_id="o1", name="Reject", kind="reject_always"),
        ]
        tc = ToolCallUpdate(tool_call_id="tc1", kind="edit")

        import asyncio
        result = asyncio.run(
            client.request_permission("s1", tc, options)
        )
        assert isinstance(result, DeniedOutcome)

    def test_approve_reads_allows_search_kind(self) -> None:
        """approve-reads: tool kind='search' should be allowed through."""
        from acp.schema import (
            AllowedOutcome,
            PermissionOption,
            ToolCallUpdate,
        )

        client = _AcpSdkClient(policy=AcpPolicy(mode="approve-reads"), events=[])
        options = [
            PermissionOption(option_id="o1", name="Allow", kind="allow_once"),
        ]
        tc = ToolCallUpdate(tool_call_id="tc1", kind="search")

        import asyncio
        result = asyncio.run(
            client.request_permission("s1", tc, options)
        )
        assert isinstance(result, AllowedOutcome)

    def test_approve_reads_denies_execute_kind(self) -> None:
        """approve-reads: tool kind='execute' should be denied."""
        from acp.schema import (
            DeniedOutcome,
            PermissionOption,
            ToolCallUpdate,
        )

        client = _AcpSdkClient(policy=AcpPolicy(mode="approve-reads"), events=[])
        options = [
            PermissionOption(option_id="o1", name="Reject", kind="reject_once"),
        ]
        tc = ToolCallUpdate(tool_call_id="tc1", kind="execute")

        import asyncio
        result = asyncio.run(
            client.request_permission("s1", tc, options)
        )
        assert isinstance(result, DeniedOutcome)

    def test_fallback_allow_once_when_no_allow_always(self) -> None:
        """approve-all: when there's no allow_always option, falls back to picking allow_once."""
        from acp.schema import (
            AllowedOutcome,
            PermissionOption,
            ToolCallUpdate,
        )

        client = _AcpSdkClient(policy=AcpPolicy(mode="approve-all"), events=[])
        options = [
            PermissionOption(option_id="o1", name="Once", kind="allow_once"),
        ]
        tc = ToolCallUpdate(tool_call_id="tc1")

        import asyncio
        result = asyncio.run(
            client.request_permission("s1", tc, options)
        )
        assert isinstance(result, AllowedOutcome)
        assert result.option_id == "o1"


# ---------------------------------------------------------------------------
# Session lifecycle integration tests
# ---------------------------------------------------------------------------


class TestEnsureSession:
    def test_returns_session_info(self, tmp_path: Path) -> None:
        t = _transport()
        wd = _make_workdir(tmp_path)
        info = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1", policy=AcpPolicy(mode="deny-all"),
        )
        assert info.session_name == "s1"
        assert info.agent == "opencode"
        assert info.workdir == str(wd)
        assert info.closed is False
        assert info.acp_session_id is not None
        t.close_session(info)

    def test_ensure_session_is_idempotent(self, tmp_path: Path) -> None:
        """Calling with the same session_name repeatedly should return the same session info."""
        t = _transport()
        wd = _make_workdir(tmp_path)
        info1 = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s_idem", policy=AcpPolicy(),
        )
        info2 = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s_idem", policy=AcpPolicy(),
        )
        assert info1.session_name == info2.session_name
        t.close_session(info1)

    def test_two_sessions_independent(self, tmp_path: Path) -> None:
        """Two sessions with different session_names are independent of each other."""
        t = _transport()
        wd1 = tmp_path / "wd1"
        wd1.mkdir()
        wd2 = tmp_path / "wd2"
        wd2.mkdir()

        info1 = t.ensure_session(
            agent="opencode", workdir=wd1, session_name="s_a", policy=AcpPolicy(),
        )
        info2 = t.ensure_session(
            agent="opencode", workdir=wd2, session_name="s_b", policy=AcpPolicy(),
        )

        assert info1.session_name == "s_a"
        assert info2.session_name == "s_b"
        assert info1.acp_session_id != info2.acp_session_id

        t.close_session(info1)
        t.close_session(info2)


class TestCloseSession:
    def test_close_removes_session(self, tmp_path: Path) -> None:
        t = _transport()
        wd = _make_workdir(tmp_path)
        info = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s_close", policy=AcpPolicy(),
        )
        assert "s_close" in t._sessions

        t.close_session(info)
        assert "s_close" not in t._sessions

    def test_close_unknown_raises(self, tmp_path: Path) -> None:
        t = _transport()
        fake = AcpSessionInfo(
            session_name="ghost", agent="opencode", workdir="/tmp",
            acp_session_id=None, closed=False,
        )
        with pytest.raises(AcpSessionError, match="ghost"):
            t.close_session(fake)


# ---------------------------------------------------------------------------
# run_prompt
# ---------------------------------------------------------------------------


class TestRunPrompt:
    def test_run_prompt_returns_stop_result(self, tmp_path: Path) -> None:
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )

        result = t.run_prompt(session, "hello", timeout_sec=10)

        assert result.reason == "stop"
        assert result.stop_reason == "end_turn"
        assert result.session_name == "s1"
        assert result.error is None

        t.close_session(session)

    def test_on_event_callback_is_invoked(self, tmp_path: Path) -> None:
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )

        seen: list[Any] = []
        t.run_prompt(session, "hello", timeout_sec=10, on_event=seen.append)
        assert len(seen) > 0

        t.close_session(session)

    def test_cancelled_stop_reason_returns_canceled(self, tmp_path: Path) -> None:
        """stopReason=='cancelled' → PromptResult.reason=='canceled'."""
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )

        result = t.run_prompt(session, "$CANCEL$", timeout_sec=10)

        assert result.reason == "canceled"
        assert result.stop_reason == "cancelled"
        assert result.session_name == "s1"
        assert result.error is None

        t.close_session(session)

    def test_run_prompt_unknown_session_returns_error(self, tmp_path: Path) -> None:
        t = _transport()
        fake = AcpSessionInfo(
            session_name="ghost", agent="opencode", workdir="/tmp",
            acp_session_id=None, closed=False,
        )
        result = t.run_prompt(fake, "hello", timeout_sec=1)
        assert result.reason == "error"


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


class TestGetHistory:
    def test_returns_events_after_prompt(self, tmp_path: Path) -> None:
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )
        t.run_prompt(session, "hello", timeout_sec=10)

        history = t.get_history(session)
        assert isinstance(history, list)
        assert len(history) > 0

        t.close_session(session)

    def test_unknown_session_returns_empty(self, tmp_path: Path) -> None:
        t = _transport()
        fake = AcpSessionInfo(
            session_name="ghost", agent="opencode", workdir="/tmp",
            acp_session_id=None, closed=False,
        )
        assert t.get_history(fake) == []


# ---------------------------------------------------------------------------
# start_prompt / wait_done / cancel
# ---------------------------------------------------------------------------


class TestStartPromptWaitDone:
    def test_start_then_wait_done_completes(self, tmp_path: Path) -> None:
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )

        handle = t.start_prompt(session, "hello")
        result = t.wait_done(handle, timeout_sec=10)

        assert result.reason == "stop"
        assert result.session_name == "s1"

        t.close_session(session)

    def test_wait_done_never_raises_on_timeout(self, tmp_path: Path) -> None:
        """wait_done convention: never raises, everything is expressed via reason.
        Uses the $HANG$ prompt to force the agent not to respond, guaranteeing a timeout is triggered."""
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )

        handle = t.start_prompt(session, "$HANG$")
        # $HANG$ makes the fake agent sleep 60s -- use a short timeout to trigger wait_done's timeout
        result = t.wait_done(handle, timeout_sec=0.5)

        assert result.reason == "timeout"
        t.cancel(handle)

        t.close_session(session)

    def test_cancel_sets_cancel_requested(self, tmp_path: Path) -> None:
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )

        handle = t.start_prompt(session, "hello")
        t.cancel(handle)
        assert isinstance(handle, _AcpSdkPromptHandle)
        assert handle.cancel_requested is True

        t.close_session(session)

    def test_start_cancel_wait_yields_canceled(self, tmp_path: Path) -> None:
        """Genuine mid-flight cancellation: cancel() must land while the
        prompt future is still pending, not after it already completed.
        Using a plain "hello" prompt (which the fake agent answers near-
        instantly) made this a real race against subprocess/event-loop
        scheduling -- flaky under CI load, where the future could already be
        done by the time cancel() ran (yielding reason="stop" instead of
        "canceled"). $HANG$ makes the fake agent sleep well past this test's
        timeout without responding, guaranteeing the future is still pending
        when cancel() is called, so this deterministically exercises the
        FutureCancelledError path in wait_done() instead of racing it.
        """
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )

        handle = t.start_prompt(session, "$HANG$")
        t.cancel(handle)
        result = t.wait_done(handle, timeout_sec=10)

        assert result.reason == "canceled"
        assert result.session_name == "s1"

        t.close_session(session)

    def test_cancelled_stop_reason_returns_canceled_via_wait_done(self, tmp_path: Path) -> None:
        """stopReason=='cancelled' → wait_done() also returns reason=='canceled'."""
        t = _transport()
        wd = _make_workdir(tmp_path)
        session = t.ensure_session(
            agent="opencode", workdir=wd, session_name="s1",
            policy=AcpPolicy(mode="approve-all"),
        )

        handle = t.start_prompt(session, "$CANCEL$")
        result = t.wait_done(handle, timeout_sec=10)

        assert result.reason == "canceled"
        assert result.stop_reason == "cancelled"
        assert result.session_name == "s1"
        assert result.error is None

        t.close_session(session)

    def test_unknown_session_raises_on_start_prompt(self, tmp_path: Path) -> None:
        t = _transport()
        fake = AcpSessionInfo(
            session_name="ghost", agent="opencode", workdir="/tmp",
            acp_session_id=None, closed=False,
        )
        with pytest.raises(AcpSessionError, match="ghost"):
            t.start_prompt(fake, "hello")
