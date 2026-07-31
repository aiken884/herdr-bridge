# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.transport: behavior tests for `AcpxTransport` against the real acpx CLI
+ a stdlib-only fake ACP agent (`tests/fixtures/fake_acp_agent.py`). Same
philosophy as test_acp_adapter_contract.py -- using a fake agent means no
real opencode binary or model credentials are needed, only the acpx CLI
itself (a lightweight npm package), so it can run every time in CI, adding
regression coverage for the session-lifecycle-management layer above
argv/env assembly.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from herdr_bridge.acp.errors import AcpSessionError
from herdr_bridge.acp.models import AcpPolicy, AcpSessionInfo, PromptResult
from herdr_bridge.acp.transport import (
    AcpxPromptHandle,
    AcpxTransport,
    _default_agent_resolver,
    _is_needs_reconnect,
)

_FAKE_AGENT = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"

requires_acpx = pytest.mark.skipif(
    shutil.which("acpx") is None,
    reason="requires the acpx CLI on PATH (npm i -g acpx@0.12.0)",
)


def _transport(tmp_path: Path) -> AcpxTransport:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    return AcpxTransport(session_dir=session_dir, agent_resolver=lambda agent: _FAKE_AGENT, ttl_sec=5)


@requires_acpx
class TestEnsureSession:
    def test_returns_session_info(self, tmp_path: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        transport = _transport(tmp_path)

        info = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="deny-all")
        )

        assert info.session_name == "s1"
        assert info.agent == "opencode"
        assert info.workdir == str(workdir)
        assert info.closed is False

    def test_two_sessions_get_isolated_config_dirs(self, tmp_path: Path):
        """N5: different sessions' orphan-cleanup scopes must not step on each other -- each gets its own independent subdirectory."""
        transport = _transport(tmp_path)
        workdir1 = tmp_path / "wd1"
        workdir1.mkdir()
        workdir2 = tmp_path / "wd2"
        workdir2.mkdir()

        transport.ensure_session(agent="opencode", workdir=workdir1, session_name="s1", policy=AcpPolicy())
        transport.ensure_session(agent="opencode", workdir=workdir2, session_name="s2", policy=AcpPolicy())

        assert transport._configs["s1"].parent != transport._configs["s2"].parent
        assert transport._configs["s1"].exists()
        assert transport._configs["s2"].exists()


@requires_acpx
def test_unsupported_agent_fails_closed(tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    transport = AcpxTransport(session_dir=session_dir, vendor_dir=tmp_path / "nonexistent-vendor", ttl_sec=5)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    with pytest.raises(AcpSessionError):
        transport.ensure_session(agent="unknown-agent", workdir=workdir, session_name="s1", policy=AcpPolicy())


@requires_acpx
class TestRunPrompt:
    def test_run_prompt_returns_stop_result(self, tmp_path: Path):
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )

        result = transport.run_prompt(session, "hello", timeout_sec=10)

        assert result.reason == "stop"
        assert result.stop_reason == "end_turn"
        assert result.session_name == "s1"
        assert result.error is None

    def test_on_event_callback_is_invoked(self, tmp_path: Path):
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )

        seen = []
        transport.run_prompt(session, "hello", timeout_sec=10, on_event=seen.append)

        assert len(seen) > 0

    def test_cancelled_stop_reason_returns_canceled(self, tmp_path: Path):
        """stopReason=="cancelled" → PromptResult.reason=="canceled" (§11.2 dual-detection path A)."""
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )

        result = transport.run_prompt(session, "$CANCEL$", timeout_sec=10)

        assert result.reason == "canceled"
        assert result.stop_reason == "cancelled"
        assert result.session_name == "s1"
        assert result.error is None


@requires_acpx
class TestGetHistory:
    def test_returns_a_list_of_events_after_a_prompt(self, tmp_path: Path):
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )
        transport.run_prompt(session, "hello", timeout_sec=10)

        history = transport.get_history(session)

        assert isinstance(history, list)


@requires_acpx
class TestCloseSession:
    def test_close_removes_config_file_and_forgets_session(self, tmp_path: Path):
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy()
        )
        config_path = transport._configs["s1"]
        assert config_path.exists()

        transport.close_session(session)

        assert not config_path.exists()
        assert "s1" not in transport._configs
        assert "s1" not in transport._policies
        assert "s1" not in transport._agents


@requires_acpx
class TestStartPromptWaitDone:
    def test_start_then_wait_done_completes(self, tmp_path: Path):
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )

        handle = transport.start_prompt(session, "hello")
        result = transport.wait_done(handle, timeout_sec=15)

        assert result.reason == "stop"
        assert result.session_name == "s1"

    def test_wait_done_never_raises_on_timeout(self, tmp_path: Path):
        """wait_done convention: never raises, everything is expressed via reason
        (echoing the wait_until philosophy)."""
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )

        handle = transport.start_prompt(session, "hello")
        result = transport.wait_done(handle, timeout_sec=0.001)

        assert result.reason == "timeout"
        transport.cancel(handle)

    def test_cancel_terminates_the_process(self, tmp_path: Path):
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )

        handle = transport.start_prompt(session, "hello")
        transport.cancel(handle)

        assert handle.process.poll() is not None

    def test_start_cancel_wait_yields_canceled_pre_generation(self, tmp_path: Path):
        """Pre-generation cancel edge case: cancel_requested flag fallback → reason=="canceled".

        Uses the $HANG$ prompt (the fake agent sleeps 60s without responding
        with a result once it receives it) to simulate the scenario where
        cancel happens pre-generation and there's no stopReason at all --
        this path is decided purely by the cancel_requested flag, not the
        stopReason path (§11.2 dual-detection path B)."""
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )

        handle = transport.start_prompt(session, "$HANG$")
        transport.cancel(handle)
        result = transport.wait_done(handle, timeout_sec=10)

        assert result.reason == "canceled"
        assert result.session_name == "s1"

    def test_cancelled_stop_reason_returns_canceled_via_wait_done(self, tmp_path: Path):
        """stopReason=="cancelled" → wait_done() also returns reason=="canceled" (the wait_done version of dual-detection path A)."""
        transport = _transport(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = transport.ensure_session(
            agent="opencode", workdir=workdir, session_name="s1", policy=AcpPolicy(mode="approve-all")
        )

        handle = transport.start_prompt(session, "$CANCEL$")
        result = transport.wait_done(handle, timeout_sec=15)

        assert result.reason == "canceled"
        assert result.stop_reason == "cancelled"
        assert result.session_name == "s1"
        assert result.error is None


# ---------------------------------------------------------------------------
# needs-reconnect retry logic unit tests (no dependency on the acpx CLI -- return sequences are controlled via mocks)
# ---------------------------------------------------------------------------


def _make_error_result(session_name: str, *, error: str) -> PromptResult:
    return PromptResult(
        reason="error", stop_reason=None, text="",
        session_name=session_name, usage=None, error=error,
    )


def _make_stop_result(session_name: str) -> PromptResult:
    return PromptResult(
        reason="stop", stop_reason="end_turn", text="ok",
        session_name=session_name, usage=None, error=None,
    )


def _setup_retry_transport(tmp_path: Path) -> AcpxTransport:
    """Build a transport whose internal state is sufficient for the retry path's _base_argv() to work correctly."""
    (tmp_path / "sessions").mkdir(exist_ok=True)
    (tmp_path / "workdir").mkdir(exist_ok=True)
    config_path = tmp_path / "config.json"
    config_path.touch()
    transport = AcpxTransport(
        session_dir=tmp_path / "sessions",
        agent_resolver=lambda _agent: Path("/bin/echo"),
        ttl_sec=5,
    )
    transport._agents["s1"] = "opencode"
    transport._policies["s1"] = AcpPolicy(mode="approve-all")
    transport._workdirs["s1"] = tmp_path / "workdir"
    transport._configs["s1"] = config_path
    return transport


class TestIsNeedsReconnect:
    def test_matches_needs_reconnect_in_error(self):
        result = _make_error_result("s1", error="stderr=...agent needs reconnect")
        assert _is_needs_reconnect(result) is True

    def test_matches_needs_reconnect_at_start_of_error(self):
        result = _make_error_result("s1", error="needs reconnect: socket probe failed")
        assert _is_needs_reconnect(result) is True

    def test_no_match_for_other_error(self):
        result = _make_error_result("s1", error="some other failure")
        assert _is_needs_reconnect(result) is False

    def test_no_match_for_empty_error(self):
        result = _make_error_result("s1", error="")
        assert _is_needs_reconnect(result) is False

    def test_no_match_for_none_error(self):
        result = PromptResult(
            reason="error", stop_reason=None, text="",
            session_name="s1", usage=None, error=None,
        )
        assert _is_needs_reconnect(result) is False

    def test_no_match_for_stop_result(self):
        result = _make_stop_result("s1")
        assert _is_needs_reconnect(result) is False

    def test_no_match_for_timeout_result(self):
        result = PromptResult(
            reason="timeout", stop_reason=None, text="",
            session_name="s1", usage=None, error="timeout",
        )
        assert _is_needs_reconnect(result) is False


class TestRunPromptRetry:
    """needs-reconnect automatic retry (hard cap of one) -- the run_prompt() path."""

    def test_retry_on_needs_reconnect_succeeds(self, tmp_path, caplog):
        """First attempt hits needs reconnect → ensure_session() + succeeds after retry."""
        transport = _setup_retry_transport(tmp_path)
        session = AcpSessionInfo(
            session_name="s1", agent="opencode",
            workdir=str(tmp_path / "workdir"), acp_session_id=None, closed=False,
        )
        error_result = _make_error_result("s1", error="stderr=...agent needs reconnect")
        success_result = _make_stop_result("s1")

        caplog.set_level(logging.WARNING)
        with (
            patch.object(transport, "_run_prompt_once", side_effect=[error_result, success_result]),
            patch.object(transport, "ensure_session") as mock_ensure,
        ):
            result = transport.run_prompt(session, "hello", timeout_sec=10)

        assert result.reason == "stop"
        assert result.error is None
        mock_ensure.assert_called_once()
        assert "needs reconnect" in caplog.text
        assert "s1" in caplog.text

    def test_retry_count_is_exactly_one(self, tmp_path, caplog):
        """When the second attempt also fails, it must not go on to a third result -- hard cap of one retry."""
        transport = _setup_retry_transport(tmp_path)
        session = AcpSessionInfo(
            session_name="s1", agent="opencode",
            workdir=str(tmp_path / "workdir"), acp_session_id=None, closed=False,
        )
        error1 = _make_error_result("s1", error="needs reconnect: first")
        error2 = _make_error_result("s1", error="needs reconnect: second")

        caplog.set_level(logging.WARNING)
        with (
            patch.object(transport, "_run_prompt_once", side_effect=[error1, error2]),
            patch.object(transport, "ensure_session"),
        ):
            result = transport.run_prompt(session, "hello", timeout_sec=10)

        # _run_prompt_once is called twice (the initial attempt + one retry), never a third time
        assert result.reason == "error"
        # the returned result is the second (retry) error -- the key point is retrying only once, not looping
        assert "needs reconnect" in caplog.text

    def test_original_error_preserved_on_retry_failure(self, tmp_path):
        """When it still fails after retrying, the real error message is returned, not wrapped or swallowed."""
        transport = _setup_retry_transport(tmp_path)
        session = AcpSessionInfo(
            session_name="s1", agent="opencode",
            workdir=str(tmp_path / "workdir"), acp_session_id=None, closed=False,
        )
        first_error = _make_error_result(
            "s1", error="no final result observed; acpx exit_code=0; stderr=...agent needs reconnect"
        )
        second_error = _make_error_result(
            "s1", error="no final result observed; acpx exit_code=0; stderr=...agent needs reconnect (retry)"
        )

        with (
            patch.object(transport, "_run_prompt_once", side_effect=[first_error, second_error]),
            patch.object(transport, "ensure_session"),
        ):
            result = transport.run_prompt(session, "hello", timeout_sec=10)

        # the error message really does contain the original diagnostic info, not None or a generic string
        assert result.error is not None
        assert "needs reconnect" in result.error
        assert result.reason == "error"

    def test_non_reconnect_error_not_retried(self, tmp_path, caplog):
        """Errors that aren't needs-reconnect (e.g. a plain NDJSON parse failure) don't trigger a retry."""
        transport = _setup_retry_transport(tmp_path)
        session = AcpSessionInfo(
            session_name="s1", agent="opencode",
            workdir=str(tmp_path / "workdir"), acp_session_id=None, closed=False,
        )
        other_error = _make_error_result(
            "s1", error="no final result observed; acpx exit_code=1; stderr=some other error"
        )

        caplog.set_level(logging.WARNING)
        with (
            patch.object(transport, "_run_prompt_once", side_effect=[other_error]),
            patch.object(transport, "ensure_session") as mock_ensure,
        ):
            result = transport.run_prompt(session, "hello", timeout_sec=10)

        assert result is other_error
        mock_ensure.assert_not_called()
        assert "needs reconnect" not in caplog.text

    def test_ensure_session_is_called_on_retry(self, tmp_path):
        """ensure_session() is really called on retry, and with the correct arguments."""
        transport = _setup_retry_transport(tmp_path)
        session = AcpSessionInfo(
            session_name="s1", agent="opencode",
            workdir=str(tmp_path / "workdir"), acp_session_id=None, closed=False,
        )
        error_result = _make_error_result("s1", error="needs reconnect")
        success_result = _make_stop_result("s1")

        with (
            patch.object(transport, "_run_prompt_once", side_effect=[error_result, success_result]),
            patch.object(transport, "ensure_session") as mock_ensure,
        ):
            transport.run_prompt(session, "hello", timeout_sec=10)

        mock_ensure.assert_called_once_with(
            agent="opencode",
            workdir=tmp_path / "workdir",
            session_name="s1",
            policy=transport._policies["s1"],
        )


# ---------------------------------------------------------------------------
# wait_done() retry logic unit tests -- symmetric to TestRunPromptRetry
# ---------------------------------------------------------------------------


def _make_retry_handle(session_name: str, *, text: str = "hello", process: MagicMock | None = None) -> AcpxPromptHandle:
    """Build an AcpxPromptHandle with a mock process, for wait_done() retry tests."""
    return AcpxPromptHandle(session_name=session_name, text=text, process=process or MagicMock())


class TestWaitDoneRetry:
    """needs-reconnect automatic retry (hard cap of one) -- the wait_done() path.

    Symmetric to TestRunPromptRetry: run_prompt() already covers the full
    retry logic, and this covers wait_done()'s retry path (rebuilding
    AcpSessionInfo, calling start_prompt(), handling the new handle).
    """

    def test_retry_on_needs_reconnect_succeeds(self, tmp_path, caplog):
        """First call to _wait_done_once returns needs-reconnect → ensure_session + start_prompt + succeeds after retry."""
        transport = _setup_retry_transport(tmp_path)
        handle = _make_retry_handle("s1")
        new_handle = _make_retry_handle("s1")
        error_result = _make_error_result("s1", error="stderr=...agent needs reconnect")
        success_result = _make_stop_result("s1")

        caplog.set_level(logging.WARNING)
        with (
            patch.object(transport, "_wait_done_once", side_effect=[error_result, success_result]),
            patch.object(transport, "ensure_session") as mock_ensure,
            patch.object(transport, "start_prompt", return_value=new_handle) as mock_start,
        ):
            result = transport.wait_done(handle, timeout_sec=10)

        assert result.reason == "stop"
        assert result.error is None
        mock_ensure.assert_called_once()
        mock_start.assert_called_once()
        # start_prompt receives the rebuilt AcpSessionInfo + the original prompt text
        call_args = mock_start.call_args
        assert call_args[0][0].session_name == "s1"
        assert call_args[0][1] == "hello"
        assert "needs reconnect" in caplog.text
        assert "s1" in caplog.text

    def test_retry_count_is_exactly_one(self, tmp_path, caplog):
        """When the second attempt also fails, it must not go on to a third result -- hard cap of one retry."""
        transport = _setup_retry_transport(tmp_path)
        handle = _make_retry_handle("s1")
        new_handle = _make_retry_handle("s1")
        error1 = _make_error_result("s1", error="needs reconnect: first")
        error2 = _make_error_result("s1", error="needs reconnect: second")

        caplog.set_level(logging.WARNING)
        with (
            patch.object(transport, "_wait_done_once", side_effect=[error1, error2]),
            patch.object(transport, "ensure_session"),
            patch.object(transport, "start_prompt", return_value=new_handle),
        ):
            result = transport.wait_done(handle, timeout_sec=10)

        # _wait_done_once is called twice (the initial attempt + one retry), never a third time
        assert result.reason == "error"
        assert "needs reconnect: second" in (result.error or "")
        assert "needs reconnect" in caplog.text

    def test_logger_warning_emitted_on_retry(self, tmp_path, caplog):
        """logger.warning is really called on retry, and includes the "needs reconnect" keyword."""
        transport = _setup_retry_transport(tmp_path)
        handle = _make_retry_handle("s1")
        new_handle = _make_retry_handle("s1")
        error_result = _make_error_result("s1", error="needs reconnect")
        success_result = _make_stop_result("s1")

        caplog.set_level(logging.WARNING)
        with (
            patch.object(transport, "_wait_done_once", side_effect=[error_result, success_result]),
            patch.object(transport, "ensure_session"),
            patch.object(transport, "start_prompt", return_value=new_handle),
        ):
            transport.wait_done(handle, timeout_sec=10)

        assert any(
            "needs reconnect" in r.message and "wait_done()" in r.message
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    def test_original_error_preserved_on_retry_failure(self, tmp_path):
        """When it still fails after retrying, the real error is returned rather than being swallowed."""
        transport = _setup_retry_transport(tmp_path)
        handle = _make_retry_handle("s1")
        new_handle = _make_retry_handle("s1")
        first_error = _make_error_result(
            "s1", error="no final result observed; acpx exit_code=0; stderr=...agent needs reconnect"
        )
        second_error = _make_error_result(
            "s1", error="no final result observed; acpx exit_code=0; stderr=...agent needs reconnect (retry)"
        )

        with (
            patch.object(transport, "_wait_done_once", side_effect=[first_error, second_error]),
            patch.object(transport, "ensure_session"),
            patch.object(transport, "start_prompt", return_value=new_handle),
        ):
            result = transport.wait_done(handle, timeout_sec=10)

        assert result.error is not None
        assert "needs reconnect" in result.error
        assert result.reason == "error"

    def test_non_reconnect_error_not_retried(self, tmp_path, caplog):
        """Errors that aren't needs-reconnect don't trigger a retry."""
        transport = _setup_retry_transport(tmp_path)
        handle = _make_retry_handle("s1")
        other_error = _make_error_result("s1", error="some other IPC failure")

        caplog.set_level(logging.WARNING)
        with (
            patch.object(transport, "_wait_done_once", return_value=other_error),
            patch.object(transport, "ensure_session") as mock_ensure,
            patch.object(transport, "start_prompt") as mock_start,
        ):
            result = transport.wait_done(handle, timeout_sec=10)

        assert result is other_error
        mock_ensure.assert_not_called()
        mock_start.assert_not_called()
        assert "needs reconnect" not in caplog.text

    def test_stale_process_reaped_on_retry(self, tmp_path):
        """If the old process hasn't finished yet before the retry (poll()==None), call wait(timeout=5) to reap it."""
        transport = _setup_retry_transport(tmp_path)
        mock_process = MagicMock()
        mock_process.poll.return_value = None  # simulate a not-yet-reaped zombie process
        handle = _make_retry_handle("s1", process=mock_process)
        new_handle = _make_retry_handle("s1")
        error_result = _make_error_result("s1", error="needs reconnect")
        success_result = _make_stop_result("s1")

        with (
            patch.object(transport, "_wait_done_once", side_effect=[error_result, success_result]),
            patch.object(transport, "ensure_session"),
            patch.object(transport, "start_prompt", return_value=new_handle),
        ):
            transport.wait_done(handle, timeout_sec=10)

        mock_process.poll.assert_called_once()
        mock_process.wait.assert_called_once_with(timeout=5)

    def test_already_done_process_not_waited_on_retry(self, tmp_path):
        """When poll() returns something other than None (already finished), don't call wait() -- avoid unnecessary blocking."""
        transport = _setup_retry_transport(tmp_path)
        mock_process = MagicMock()
        mock_process.poll.return_value = 0  # already finished normally
        handle = _make_retry_handle("s1", process=mock_process)
        new_handle = _make_retry_handle("s1")
        error_result = _make_error_result("s1", error="needs reconnect")
        success_result = _make_stop_result("s1")

        with (
            patch.object(transport, "_wait_done_once", side_effect=[error_result, success_result]),
            patch.object(transport, "ensure_session"),
            patch.object(transport, "start_prompt", return_value=new_handle),
        ):
            transport.wait_done(handle, timeout_sec=10)

        mock_process.poll.assert_called_once()
        mock_process.wait.assert_not_called()


# ---------------------------------------------------------------------------
# _default_agent_resolver — claude support
# ---------------------------------------------------------------------------


class TestDefaultAgentResolverClaude:
    def test_resolves_claude_via_env(self, tmp_path: Path, monkeypatch):
        fake_claude = tmp_path / "fake-claude"
        fake_claude.write_text("#!/bin/sh\necho claude\n")
        fake_claude.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(fake_claude))

        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        resolve = _default_agent_resolver(vendor_dir)

        path = resolve("claude")
        assert path == fake_claude

    def test_resolves_claude_via_which(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        resolve = _default_agent_resolver(vendor_dir)

        path = resolve("claude")
        assert path is not None

    def test_resolves_opencode_still_works(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        vendor_dir = tmp_path / "vendor"
        target_dir = vendor_dir / "darwin-arm64"
        target_dir.mkdir(parents=True)
        (target_dir / "opencode").write_text("#!/bin/sh\necho fake\n")
        (vendor_dir / "MANIFEST.json").write_text(
            json.dumps({
                "target_triple": "darwin-arm64",
                "base_upstream_version": "1.18.3",
                "compatible_upstream_range": {"min_inclusive": "1.18.0", "max_inclusive": "1.18.99"},
            })
        )

        resolve = _default_agent_resolver(vendor_dir)
        path = resolve("opencode")
        assert path == vendor_dir / "darwin-arm64" / "opencode"

    def test_unknown_agent_fails_closed(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        resolve = _default_agent_resolver(vendor_dir)

        with pytest.raises(AcpSessionError, match="unsupported agent"):
            resolve("unknown-agent")


# ---------------------------------------------------------------------------
# AcpxTransport — claude agent integration (requires the real claude CLI + acpx)
# ---------------------------------------------------------------------------


@requires_acpx
class TestAcpxTransportClaudeIntegration:
    """Integration tests against the real acpx CLI + real claude. Marked `integration` so CI can deselect them."""

    pytestmark = pytest.mark.integration

    def test_ensure_session_with_claude_succeeds(self, tmp_path: Path):
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        transport = AcpxTransport(session_dir=session_dir, ttl_sec=5)

        info = transport.ensure_session(
            agent="claude", workdir=workdir, session_name="claude-s1", policy=AcpPolicy()
        )

        assert info.agent == "claude"
        assert info.session_name == "claude-s1"
        assert info.closed is False
        assert transport._configs.get("claude-s1") is None

        transport.close_session(info)

    def test_run_prompt_with_claude_returns_result(self, tmp_path: Path):
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        transport = AcpxTransport(session_dir=session_dir, ttl_sec=5)

        session = transport.ensure_session(
            agent="claude", workdir=workdir, session_name="claude-s2", policy=AcpPolicy(mode="approve-all")
        )

        result = transport.run_prompt(session, "say hello in one word", timeout_sec=30)

        assert result.reason in ("stop", "timeout", "error")
        assert result.session_name == "claude-s2"

        transport.close_session(session)
