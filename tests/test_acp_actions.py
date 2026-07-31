# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.actions: unit tests for the `AcpActions` facade logic, using a fake
`AcpTransport` test double (no real acpx needed). Verifies session bookkeeping,
audit logging, idempotent ensure_session, exec_prompt's ensure+finally-close,
and error handling for unknown sessions. See test_acp_actions_integration.py
(using AcpxTransport + fake_acp_agent) for the real acpx end-to-end coverage.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from herdr_bridge.acp.actions import AcpActions, connect
from herdr_bridge.acp.adapter import write_session_config
from herdr_bridge.acp.errors import AcpAdapterError, AcpSessionError
from herdr_bridge.acp.models import AcpEvent, AcpPolicy, AcpSessionInfo, PromptResult
from herdr_bridge.audit import AuditLogger


class _FakeHandle:
    def __init__(self, session_name: str):
        self.session_name = session_name


class FakeTransport:
    def __init__(self):
        self.ensure_calls = []
        self.close_calls = []
        self.history_calls = []
        self.prompt_calls = []
        self.start_calls = []
        self.wait_calls = []
        self.cancel_calls = []
        self.next_result = PromptResult(
            reason="stop", stop_reason="end_turn", text="ok", session_name="", usage=None
        )

    def ensure_session(self, *, agent, workdir, session_name, policy):
        self.ensure_calls.append((agent, workdir, session_name, policy))
        return AcpSessionInfo(
            session_name=session_name, agent=agent, workdir=str(workdir), acp_session_id="fake-id", closed=False
        )

    def close_session(self, session):
        self.close_calls.append(session)

    def get_history(self, session):
        self.history_calls.append(session)
        return [AcpEvent(type="result", session_id=None, text=None, raw={})]

    def run_prompt(self, session, text, *, timeout_sec, on_event=None):
        self.prompt_calls.append((session, text, timeout_sec))
        if on_event:
            on_event(AcpEvent(type="agent_message_chunk", session_id=None, text="hi", raw={}))
        return replace(self.next_result, session_name=session.session_name)

    def start_prompt(self, session, text):
        self.start_calls.append((session, text))
        return _FakeHandle(session_name=session.session_name)

    def wait_done(self, handle, *, timeout_sec):
        self.wait_calls.append((handle, timeout_sec))
        return replace(self.next_result, session_name=handle.session_name)

    def cancel(self, handle):
        self.cancel_calls.append(handle)


class _SlowFileWritingTransport:
    """Simulates the two things `AcpxTransport.ensure_session()` really does:
    a separate subdirectory per `session_name`, and a call to the real
    `write_session_config()` that writes an actual file. Real files are needed
    to verify the concrete consequence of a race — "the file gets deleted by
    another concurrent call's orphan cleanup" — which an in-memory fake alone
    can't exercise. `time.sleep()` simulates the delay of a real
    `subprocess.run()`, widening the race window so that, without lock
    protection, multiple threads genuinely land in this window at the same
    time."""

    def __init__(self, session_dir: Path):
        self._session_dir = session_dir
        self.ensure_call_count = 0
        self._count_lock = threading.Lock()
        self.config_paths: list[Path] = []

    def ensure_session(self, *, agent, workdir, session_name, policy):
        with self._count_lock:
            self.ensure_call_count += 1
        this_session_dir = self._session_dir / session_name
        this_session_dir.mkdir(parents=True, exist_ok=True)
        config_path = write_session_config(policy, session_dir=this_session_dir)
        time.sleep(0.05)
        self.config_paths.append(config_path)
        return AcpSessionInfo(
            session_name=session_name, agent=agent, workdir=str(workdir), acp_session_id="fake-id", closed=False
        )

    def close_session(self, session):
        pass


class _SlowTransport:
    """Same purpose as `_SlowFileWritingTransport` (use `time.sleep()` to widen
    the race window so multiple threads genuinely land in it at the same time
    without lock protection), but this one tests the race across different
    `session_name`s sharing the same workdir, not config-file orphan cleanup —
    no need to write a real config file, so the simplest in-memory fake will
    do."""

    def __init__(self):
        self.ensure_call_count = 0
        self._count_lock = threading.Lock()

    def ensure_session(self, *, agent, workdir, session_name, policy):
        with self._count_lock:
            self.ensure_call_count += 1
        time.sleep(0.05)
        return AcpSessionInfo(
            session_name=session_name, agent=agent, workdir=str(workdir), acp_session_id="fake-id", closed=False
        )

    def close_session(self, session):
        pass


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def acp(transport: FakeTransport, audit: AuditLogger) -> AcpActions:
    return AcpActions(transport=transport, audit=audit)


def _read_audit(audit: AuditLogger) -> list[dict]:
    if not audit.path.exists():
        return []
    return [json.loads(line) for line in audit.path.read_text().splitlines() if line]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def primary_repo(tmp_path: Path) -> Path:
    """The primary worktree of a real scratch git repo (shared by the ADR 0003
    workdir isolation tests). Uses a real `git init` + commit rather than a
    plain string path or mocking out `git worktree list` — this way the test
    exercises `_enforce_workdir_isolation()` against a real environment
    instead of just going through the motions (explicitly required by the
    task spec)."""
    repo = tmp_path / "primary-repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.invalid", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("test\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def worktree(primary_repo: Path, tmp_path: Path) -> Path:
    """A valid, non-primary linked worktree under `primary_repo` — most tests
    use this as the opencode agent's valid workdir."""
    wt = tmp_path / "worktree-a"
    _git("worktree", "add", "-q", "-b", "wt-a", str(wt), cwd=primary_repo)
    return wt


@pytest.fixture
def worktree2(primary_repo: Path, tmp_path: Path) -> Path:
    """A second valid linked worktree, for tests that need two distinct valid
    workdirs."""
    wt = tmp_path / "worktree-b"
    _git("worktree", "add", "-q", "-b", "wt-b", str(wt), cwd=primary_repo)
    return wt


class TestListAcpAgents:
    def test_returns_the_opencode_builtin_tier(self, acp: AcpActions):
        specs = acp.list_acp_agents("gov:main")
        assert any(s.name == "opencode" and s.builtin for s in specs)

    def test_records_audit(self, acp: AcpActions, audit: AuditLogger):
        acp.list_acp_agents("gov:main")
        entries = _read_audit(audit)
        assert entries[0]["action"] == "acp.list_acp_agents"
        assert entries[0]["actor_id"] == "gov:main"


class TestEnsureSession:
    def test_delegates_to_transport_and_remembers_session(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path
    ):
        info = acp.ensure_session("gov:main", "opencode", str(worktree), "s1")

        assert len(transport.ensure_calls) == 1
        assert info.session_name == "s1"
        assert info.agent == "opencode"

    def test_is_idempotent_does_not_call_transport_again(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path
    ):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")

        assert len(transport.ensure_calls) == 1

    def test_default_policy_is_approve_all(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        _agent, _workdir, _name, policy = transport.ensure_calls[0]
        assert policy.mode == "approve-all"

    def test_records_audit_with_policy_mode(self, acp: AcpActions, audit: AuditLogger, worktree: Path):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1", policy=AcpPolicy(mode="deny-all"))
        entries = _read_audit(audit)
        assert entries[0]["policy_mode"] == "deny-all"
        assert entries[0]["idempotent_hit"] is False

    def test_policy_enforced_false_for_opencode(self, acp: AcpActions, audit: AuditLogger, worktree: Path):
        """opencode is a built-in tier — policy_enforced should be False (not enforced)."""
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        entries = _read_audit(audit)
        assert entries[0]["policy_enforced"] is False

    def test_policy_enforced_true_for_non_opencode(self, acp: AcpActions, audit: AuditLogger):
        """A non-opencode agent (e.g. claude) — policy_enforced should be True.
        FakeTransport never actually calls _default_agent_resolver(), but
        actions.py's decision logic (agent != "opencode") should still be
        reflected correctly in the audit record. claude is not subject to the
        ADR 0003 workdir isolation restriction, so a fake path like "/tmp/wd"
        is still valid for it (see scenario D in TestWorkdirIsolation)."""
        acp.ensure_session("gov:main", "claude", "/tmp/wd", "s1")
        entries = _read_audit(audit)
        assert entries[0]["policy_enforced"] is True


class TestEnsureSessionConcurrency:
    """PR #4 wrap-up review, finding 3: if `ensure_session()`'s check-then-act
    for the same `session_name` isn't atomic, concurrent calls let multiple
    threads all pass the "doesn't exist yet" check and each genuinely call the
    transport once — under `AcpxTransport` this causes repeated
    `write_session_config()` calls to delete each other's freshly-written
    config files (fail-open, not fail-closed — see the adapter.py docstring).
    This uses real threads (not a sequential simulation) to produce a genuine
    race."""

    def test_concurrent_calls_same_session_name_only_create_one_session(
        self, tmp_path: Path, audit: AuditLogger, worktree: Path
    ):
        transport = _SlowFileWritingTransport(tmp_path)
        acp = AcpActions(transport=transport, audit=audit)

        n_threads = 8
        barrier = threading.Barrier(n_threads)
        errors: list[BaseException] = []
        results: list[AcpSessionInfo] = []
        results_lock = threading.Lock()

        def worker():
            try:
                barrier.wait()
                info = acp.ensure_session("gov:main", "opencode", str(worktree), "same-session")
                with results_lock:
                    results.append(info)
            except BaseException as exc:  # noqa: BLE001  # thread worker: must catch everything (incl. AssertionError) so the main thread can observe and assert on it, not lose it silently
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

        # (a) In the end, only one session actually got created
        assert len(acp._sessions) == 1

        # (b) transport.ensure_session() (and thus the underlying
        # write_session_config()) was genuinely called exactly once, not once
        # per thread
        assert transport.ensure_call_count == 1
        assert len(transport.config_paths) == 1

        # (c) No thread ended up with a config_path pointing at a deleted
        # file — since there was only one real write_session_config() call,
        # no other concurrent call could trigger its orphan cleanup logic to
        # delete this file
        assert transport.config_paths[0].exists()

        # All 8 threads got back the same session (not each their own object)
        assert len(results) == n_threads
        assert all(r == results[0] for r in results)

    def test_locks_are_per_session_name_not_a_global_lock(
        self, audit: AuditLogger, worktree: Path, worktree2: Path
    ):
        """`_lock_for_session()` must lock per `session_name` — if it degraded
        to a single global lock, `ensure_session()` calls for unrelated
        session_names would block each other, needlessly slowing down totally
        unrelated concurrent creation (or even deadlocking each other when the
        transport call takes a while)."""
        entered_s1 = threading.Event()
        release_s1 = threading.Event()

        class _BlockingOnS1Transport:
            def ensure_session(self, *, agent, workdir, session_name, policy):
                if session_name == "s1":
                    entered_s1.set()
                    assert release_s1.wait(timeout=5), "release_s1 never signalled — test itself is broken"
                return AcpSessionInfo(
                    session_name=session_name, agent=agent, workdir=str(workdir), acp_session_id="fake-id", closed=False
                )

            def close_session(self, session):
                pass

        acp = AcpActions(transport=_BlockingOnS1Transport(), audit=audit)

        t1 = threading.Thread(target=lambda: acp.ensure_session("gov:main", "opencode", str(worktree), "s1"))
        t1.start()
        assert entered_s1.wait(timeout=5)

        start = time.monotonic()
        acp.ensure_session("gov:main", "opencode", str(worktree2), "s2")
        elapsed = time.monotonic() - start

        release_s1.set()
        t1.join(timeout=5)

        assert elapsed < 1.0, f"s2 waited {elapsed:.2f}s on s1's lock — lock is not per-session_name"

    def test_concurrent_calls_different_session_names_same_workdir_only_one_succeeds(
        self, audit: AuditLogger, worktree: Path
    ):
        """A known residual risk (see the `ensure_session()` docstring):
        `_session_locks` locks per `session_name`, so if two **different**
        `session_name`s call `ensure_session()` concurrently against the
        **same** workdir, both could pass `_enforce_workdir_isolation()`'s
        "shared workdir" check simultaneously, before either has written its
        own workdir into `self._sessions`. This uses real threads (a barrier
        to start them in sync, plus `time.sleep()` inside the transport to
        widen the race window) to produce a genuine race, not a sequential
        simulation — before the fix both would pass and each call the
        transport once; after the fix only one succeeds, and the other must
        be rejected for a SHARED workdir."""
        transport = _SlowTransport()
        acp = AcpActions(transport=transport, audit=audit)

        barrier = threading.Barrier(2)
        results: list[AcpSessionInfo] = []
        errors: list[AcpSessionError] = []
        results_lock = threading.Lock()

        def worker(session_name: str) -> None:
            try:
                barrier.wait()
                info = acp.ensure_session("gov:main", "opencode", str(worktree), session_name)
                with results_lock:
                    results.append(info)
            except AcpSessionError as exc:
                with results_lock:
                    errors.append(exc)

        t1 = threading.Thread(target=worker, args=("s1",))
        t2 = threading.Thread(target=worker, args=("s2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(results) == 1, f"expected exactly 1 success, got {len(results)}: {results}"
        assert len(errors) == 1, f"expected exactly 1 rejection, got {len(errors)}: {errors}"
        assert "SHARED" in str(errors[0])
        assert transport.ensure_call_count == 1


class TestCloseSession:
    def test_delegates_to_transport_and_forgets_session(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path
    ):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")

        acp.close_session("gov:main", "s1")

        assert len(transport.close_calls) == 1
        with pytest.raises(AcpSessionError):
            acp.close_session("gov:main", "s1")  # already closed — unknown now

    def test_unknown_session_fails_closed(self, acp: AcpActions):
        with pytest.raises(AcpSessionError):
            acp.close_session("gov:main", "never-existed")

    def test_removes_workdir_lock_entry(self, acp: AcpActions, worktree: Path):
        """Closing a session should also clear its corresponding workdir lock
        entry — otherwise `_workdir_locks` would grow unbounded with the
        number of workdirs ever used (a small memory leak). Since the ADR
        0003 isolation rule guarantees only one active session per workdir at
        a time, clearing the corresponding lock on close is safe."""
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        resolved_workdir = str(worktree.resolve())
        assert resolved_workdir in acp._workdir_locks

        acp.close_session("gov:main", "s1")

        assert resolved_workdir not in acp._workdir_locks

    def test_tolerates_already_missing_workdir_lock_entry(self, acp: AcpActions, worktree: Path):
        """The `None` default in `_workdir_locks.pop(resolved_workdir, None)`
        must actually take effect — manually remove the lock entry beforehand
        to simulate the edge case where "the key is already gone",
        `close_session()` must not raise `KeyError` because of it (it would,
        without the default)."""
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        resolved_workdir = str(worktree.resolve())
        del acp._workdir_locks[resolved_workdir]

        acp.close_session("gov:main", "s1")  # must not raise KeyError

        assert resolved_workdir not in acp._workdir_locks


class TestGetHistory:
    def test_unknown_session_fails_closed(self, acp: AcpActions):
        with pytest.raises(AcpSessionError):
            acp.get_history("gov:main", "never-existed")

    def test_returns_transport_events(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        events = acp.get_history("gov:main", "s1")
        assert len(events) == 1
        assert len(transport.history_calls) == 1


class TestPrompt:
    def test_unknown_session_fails_closed(self, acp: AcpActions):
        with pytest.raises(AcpSessionError):
            acp.prompt("gov:main", "never-existed", "hi")

    def test_delegates_and_returns_result(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        result = acp.prompt("gov:main", "s1", "hello")
        assert result.reason == "stop"
        assert result.session_name == "s1"

    def test_on_event_callback_reaches_transport(self, acp: AcpActions, worktree: Path):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        seen = []
        acp.prompt("gov:main", "s1", "hello", on_event=seen.append)
        assert len(seen) == 1

    def test_records_audit_with_reason(self, acp: AcpActions, audit: AuditLogger, worktree: Path):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        acp.prompt("gov:main", "s1", "hello", priority=5)
        entries = _read_audit(audit)
        prompt_entry = entries[-1]
        assert prompt_entry["action"] == "acp.prompt"
        assert prompt_entry["reason"] == "stop"
        assert prompt_entry["priority"] == 5


class TestExecPrompt:
    def test_ensures_then_closes_even_on_success(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        result = acp.exec_prompt("gov:main", "opencode", "hello", workdir=str(worktree))

        assert result.reason == "stop"
        assert len(transport.ensure_calls) == 1
        assert len(transport.close_calls) == 1

    def test_closes_even_when_run_prompt_raises(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        def boom(*_a, **_kw):
            raise RuntimeError("acpx exploded")

        transport.run_prompt = boom

        with pytest.raises(RuntimeError):
            acp.exec_prompt("gov:main", "opencode", "hello", workdir=str(worktree))

        assert len(transport.close_calls) == 1

    def test_does_not_leave_a_tracked_session(self, acp: AcpActions, worktree: Path):
        acp.exec_prompt("gov:main", "opencode", "hello", workdir=str(worktree))
        assert acp._sessions == {}

    def test_records_audit_with_policy_enforced(self, acp: AcpActions, audit: AuditLogger, worktree: Path):
        """exec_prompt should record policy_mode and policy_enforced
        (opencode=false, claude=true). The claude branch isn't subject to
        workdir isolation, so a fake path like "/tmp/wd" is fine (see
        scenario D)."""
        acp.exec_prompt("gov:main", "opencode", "hello", workdir=str(worktree))
        entries = _read_audit(audit)
        entry_open = entries[0]
        assert entry_open["action"] == "acp.exec_prompt"
        assert entry_open["policy_mode"] == "approve-all"
        assert entry_open["policy_enforced"] is False

        acp.exec_prompt("gov:main", "claude", "hello", workdir="/tmp/wd")
        entries = _read_audit(audit)
        entry_claude = entries[1]
        assert entry_claude["action"] == "acp.exec_prompt"
        assert entry_claude["policy_enforced"] is True


class TestStartPromptWaitDoneCancel:
    def test_start_wait_done_round_trip(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        handle = acp.start_prompt("gov:main", "s1", "hello")
        result = acp.wait_done("gov:main", handle)

        assert result.reason == "stop"
        assert len(transport.start_calls) == 1
        assert len(transport.wait_calls) == 1

    def test_wait_done_propagates_timeout_reason_without_raising(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path
    ):
        transport.next_result = replace(transport.next_result, reason="timeout", stop_reason=None)
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        handle = acp.start_prompt("gov:main", "s1", "hello")

        result = acp.wait_done("gov:main", handle)

        assert result.reason == "timeout"

    def test_cancel_delegates_to_transport(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        handle = acp.start_prompt("gov:main", "s1", "hello")

        acp.cancel("gov:main", handle)

        assert len(transport.cancel_calls) == 1


class TestClose:
    def test_closes_all_tracked_sessions(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path, worktree2: Path
    ):
        """s1/s2 use two distinct valid worktrees — the same workdir would now
        be rejected by the ADR 0003 isolation check (see scenario B in
        TestWorkdirIsolation), which is a separate concern from what this test
        verifies ("close() can close multiple sessions at once"), so distinct
        paths are used to avoid coupling the two."""
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        acp.ensure_session("gov:main", "opencode", str(worktree2), "s2")

        acp.close()

        assert len(transport.close_calls) == 2
        assert acp._sessions == {}


class TestWorkdirIsolation:
    """ADR 0003 Decision #2 + known boundary #2: mandatory workdir/git-worktree
    isolation for opencode-family tiers. Uses a real scratch git repo
    (`primary_repo`/`worktree`/`worktree2` fixtures, defined above the file)
    rather than mocking `git worktree list` output — this way the test
    exercises the real `git` subprocess and real path resolution, catching
    errors a mocked version would miss (e.g. whether `git -C` parsing or the
    `--porcelain` format assumptions actually hold)."""

    def test_rejects_primary_worktree_for_opencode(
        self, acp: AcpActions, transport: FakeTransport, primary_repo: Path, audit: AuditLogger
    ):
        """Scenario A: workdir is the primary worktree itself -> rejected, and
        the transport must not have been called."""
        with pytest.raises(AcpSessionError, match="PRIMARY"):
            acp.ensure_session("gov:main", "opencode", str(primary_repo), "s1")

        assert transport.ensure_calls == []
        assert acp._sessions == {}
        entries = _read_audit(audit)
        assert entries[-1]["action"] == "acp.ensure_session"
        assert entries[-1]["session_name"] == "s1"
        assert entries[-1]["agent"] == "opencode"
        assert entries[-1]["idempotent_hit"] is False
        assert "PRIMARY" in entries[-1]["rejected_reason"]

    def test_rejects_workdir_shared_with_active_session(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path, audit: AuditLogger
    ):
        """Scenario B: first claim the worktree by creating a session, then
        call ensure_session() on the same workdir (different session_name) ->
        rejected, and the transport isn't called a second time."""
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")

        with pytest.raises(AcpSessionError, match="SHARED"):
            acp.ensure_session("gov:main", "opencode", str(worktree), "s2")

        assert len(transport.ensure_calls) == 1  # only s1's call succeeded
        assert list(acp._sessions) == ["s1"]
        entries = _read_audit(audit)
        assert entries[-1]["session_name"] == "s2"
        assert "SHARED" in entries[-1]["rejected_reason"]
        assert "'s1'" in entries[-1]["rejected_reason"]

    def test_allows_isolated_worktree(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        """Scenario C: a valid, standalone worktree (distinct from both the
        primary worktree and any other session) -> passes through fine,
        unaffected."""
        info = acp.ensure_session("gov:main", "opencode", str(worktree), "s1")

        assert info.session_name == "s1"
        assert len(transport.ensure_calls) == 1

    def test_claude_not_restricted_to_primary_worktree(
        self, acp: AcpActions, transport: FakeTransport, primary_repo: Path
    ):
        """Scenario D: agent="claude" against the primary worktree -> not
        subject to this restriction, passes through fine."""
        info = acp.ensure_session("gov:main", "claude", str(primary_repo), "s1")

        assert info.agent == "claude"
        assert len(transport.ensure_calls) == 1

    def test_claude_not_restricted_from_sharing_workdir(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path
    ):
        """Scenario D: agent="claude" against a workdir already claimed by
        another session -> not subject to this restriction."""
        acp.ensure_session("gov:main", "claude", str(worktree), "s1")

        info = acp.ensure_session("gov:main", "claude", str(worktree), "s2")

        assert info.session_name == "s2"
        assert len(transport.ensure_calls) == 2

    def test_rejects_symlink_to_primary_worktree(
        self, acp: AcpActions, transport: FakeTransport, primary_repo: Path, tmp_path: Path
    ):
        """Scenario E: a symlink pointing at the primary worktree -> still
        blocked after resolving, so a symlink can't be used to bypass a plain
        string comparison."""
        link = tmp_path / "sneaky-link-to-primary"
        link.symlink_to(primary_repo)

        with pytest.raises(AcpSessionError, match="PRIMARY"):
            acp.ensure_session("gov:main", "opencode", str(link), "s1")

        assert transport.ensure_calls == []

    def test_rejects_symlink_to_active_session_workdir(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path, tmp_path: Path
    ):
        """Scenario E: a symlink pointing at a workdir already claimed by
        another active session -> still blocked after resolving."""
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        link = tmp_path / "sneaky-link-to-active-session"
        link.symlink_to(worktree)

        with pytest.raises(AcpSessionError, match="SHARED"):
            acp.ensure_session("gov:main", "opencode", str(link), "s2")

        assert len(transport.ensure_calls) == 1

    def test_fails_closed_when_workdir_is_not_a_git_repo(
        self, acp: AcpActions, transport: FakeTransport, tmp_path: Path
    ):
        """When `git worktree list --porcelain` fails (workdir isn't a git
        repo at all), fail closed and reject — we can't determine where the
        primary worktree is, so safety can't be guaranteed (ADR 0003 known
        boundary #2)."""
        not_a_repo = tmp_path / "not-a-git-repo"
        not_a_repo.mkdir()

        with pytest.raises(AcpSessionError):
            acp.ensure_session("gov:main", "opencode", str(not_a_repo), "s1")

        assert transport.ensure_calls == []

    def test_second_isolated_worktree_does_not_conflict_with_first(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path, worktree2: Path
    ):
        """Two distinct, valid standalone worktrees can each have their own
        active session at the same time."""
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")
        info2 = acp.ensure_session("gov:main", "opencode", str(worktree2), "s2")

        assert info2.session_name == "s2"
        assert len(transport.ensure_calls) == 2


class TestExecPromptWorkdirIsolation:
    """`exec_prompt()` creates an opencode session directly, just like
    `ensure_session()` — the same ADR 0003 Decision #2 check must also take
    effect here, or it would be a complete bypass of the `ensure_session()`
    check."""

    def test_rejects_primary_worktree(self, acp: AcpActions, transport: FakeTransport, primary_repo: Path):
        with pytest.raises(AcpSessionError, match="PRIMARY"):
            acp.exec_prompt("gov:main", "opencode", "hello", workdir=str(primary_repo))

        assert transport.ensure_calls == []

    def test_rejects_workdir_shared_with_active_ensure_session(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path
    ):
        acp.ensure_session("gov:main", "opencode", str(worktree), "s1")

        with pytest.raises(AcpSessionError, match="SHARED"):
            acp.exec_prompt("gov:main", "opencode", "hello", workdir=str(worktree))

        assert len(transport.ensure_calls) == 1  # only ensure_session's call

    def test_claude_bypasses_isolation(self, acp: AcpActions, transport: FakeTransport, primary_repo: Path):
        result = acp.exec_prompt("gov:main", "claude", "hello", workdir=str(primary_repo))

        assert result.reason == "stop"
        assert len(transport.ensure_calls) == 1

    def test_allows_isolated_worktree(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        result = acp.exec_prompt("gov:main", "opencode", "hello", workdir=str(worktree))

        assert result.reason == "stop"
        assert len(transport.ensure_calls) == 1


class TestIsolationConcurrentClose:
    """PR wrap-up review: while `_enforce_workdir_isolation()` is iterating
    `self._sessions`, another thread calling `close_session()` modifies the
    same dict via `pop()` — unless `.items()` is first converted to
    `list()`, CPython would raise `RuntimeError: dictionary changed size
    during iteration`, leaking through the error handling that's supposed to
    wrap it as `AcpSessionError`.
    """

    def test_ensure_session_unaffected_by_concurrent_close(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path, worktree2: Path
    ):
        """`ensure_session()`'s internal `_enforce_workdir_isolation()` (which
        now takes a snapshot via `list(self._sessions.items())`) is unaffected
        by another thread concurrently modifying the dict via
        `close_session()` — it must not raise `RuntimeError`."""
        # First create an active session so the `_sessions` dict has
        # something to iterate over.
        acp.ensure_session("gov:main", "opencode", str(worktree), "s-pre")

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def do_ensure_new():
            try:
                barrier.wait()
                # This call runs `_enforce_workdir_isolation()` while
                # holding the lock, iterating `list(self._sessions.items())`
                # — now a snapshot.
                acp.ensure_session("gov:main", "opencode", str(worktree2), "s-new")
            except BaseException as e:  # noqa: BLE001  # thread worker: must catch everything so the main thread can observe and assert on it, not lose it silently
                errors.append(e)

        def do_close_existing():
            try:
                barrier.wait()
                # This call modifies the dict directly via
                # `self._sessions.pop("s-pre")`.
                acp.close_session("gov:main", "s-pre")
            except BaseException as e:  # noqa: BLE001  # thread worker: must catch everything so the main thread can observe and assert on it, not lose it silently
                errors.append(e)

        t_ensure = threading.Thread(target=do_ensure_new)
        t_close = threading.Thread(target=do_close_existing)
        t_ensure.start()
        t_close.start()
        t_ensure.join()
        t_close.join()

        assert not errors, f"unexpected errors: {errors}"

    def test_iteration_snapshot_preserves_behavior(
        self, acp: AcpActions, transport: FakeTransport, worktree: Path, worktree2: Path
    ):
        """A subsequent `ensure_session()` call after `close_session()` no
        longer counts the closed session towards the workdir isolation check
        — the snapshot logic is consistent with the actual behavior."""
        # Create an active session first.
        acp.ensure_session("gov:main", "opencode", str(worktree), "s-pre")
        # Close it.
        acp.close_session("gov:main", "s-pre")
        # Create a new one on the same worktree — should not raise a SHARED
        # workdir error (since s-pre is no longer in `_sessions`,
        # active_workdirs now only contains s-new itself).
        info = acp.ensure_session("gov:main", "opencode", str(worktree), "s-new")
        assert info.session_name == "s-new"
        assert len(transport.ensure_calls) == 2  # s-pre + s-new


class TestPromptHooks:
    """Tests for the before_prompt / after_prompt hooks."""

    def test_before_prompt_transforms_text(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        def before(text: str) -> str:
            return "[MEM] " + text
        acp.ensure_session("gov", "opencode", str(worktree), "s1")
        acp.prompt("gov", "s1", "do task", before_prompt=before)
        assert transport.prompt_calls[0][1] == "[MEM] do task"

    def test_after_prompt_receives_result(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        received = []
        def after(res: PromptResult):
            received.append(res.reason)
        acp.ensure_session("gov", "opencode", str(worktree), "s1")
        acp.prompt("gov", "s1", "do task", after_prompt=after)
        assert received == ["stop"]

    def test_before_exception_propagates(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        def before(text):
            raise RuntimeError("before fail")
        acp.ensure_session("gov", "opencode", str(worktree), "s1")
        with pytest.raises(RuntimeError):
            acp.prompt("gov", "s1", "do", before_prompt=before)
        assert len(transport.prompt_calls) == 0

    def test_after_exception_propagates(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        def after(res):
            raise RuntimeError("after fail")
        acp.ensure_session("gov", "opencode", str(worktree), "s1")
        with pytest.raises(RuntimeError):
            acp.prompt("gov", "s1", "do", after_prompt=after)


class TestExecPromptHooks:
    """Tests for exec_prompt's hooks."""

    def test_before_after_on_exec(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        received = []
        def before(t): return "[M] " + t
        def after(r): received.append(r.reason)
        acp.exec_prompt("gov", "opencode", "task", workdir=str(worktree), before_prompt=before, after_prompt=after)
        assert "[M] task" in str(transport.prompt_calls)
        assert received == ["stop"]

    def test_on_event_on_exec(self, acp: AcpActions, transport: FakeTransport, worktree: Path):
        events = []
        def on_e(e): events.append(e)
        acp.exec_prompt("gov", "opencode", "task", workdir=str(worktree), on_event=on_e)
        assert len(events) >= 1


class TestConnect:
    """The `connect()` factory — with `strict_version=True` it should throw
    AcpAdapterError directly (fail loud), not just log a warning (see the
    docstring)."""

    def test_strict_version_raises_when_base_version_outside_compatible_range(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        monkeypatch.chdir(tmp_path)

        vendor_dir = tmp_path / ".vendor" / "opencode-patched"
        target_dir = vendor_dir / "darwin-arm64"
        target_dir.mkdir(parents=True)
        (target_dir / "opencode").write_text("#!/bin/sh\necho fake\n")
        manifest = {
            "target_triple": "darwin-arm64",
            "base_upstream_version": "1.99.0",
            "compatible_upstream_range": {"min_inclusive": "1.18.0", "max_inclusive": "1.18.99"},
        }
        (vendor_dir / "MANIFEST.json").write_text(json.dumps(manifest))

        with pytest.raises(AcpAdapterError):
            connect(strict_version=True)
