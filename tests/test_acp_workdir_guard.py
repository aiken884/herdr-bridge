# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.workdir_guard: unit tests directly against the two pure functions
`resolve_primary_worktree()`/`check_opencode_workdir_isolation()` --
`test_acp_actions.py`'s `TestWorkdirIsolation`/`TestExecPromptWorkdirIsolation`
already cover the integration path via `AcpActions`; this fills in three
fail-closed branches that are hard to trigger naturally through `AcpActions`
(`git` missing, timeout, porcelain output that parses to no `worktree` lines
at all), plus pure-function edge cases that don't go through `AcpActions`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from herdr_bridge.acp.errors import AcpSessionError
from herdr_bridge.acp.workdir_guard import (
    _GIT_WORKTREE_LIST_TIMEOUT_SEC,
    check_opencode_workdir_isolation,
    resolve_primary_worktree,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def primary_repo(tmp_path: Path) -> Path:
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
    wt = tmp_path / "worktree-a"
    _git("worktree", "add", "-q", "-b", "wt-a", str(wt), cwd=primary_repo)
    return wt


class _FakeCompletedProcess:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestResolvePrimaryWorktree:
    def test_returns_the_repo_root_itself(self, primary_repo: Path):
        assert resolve_primary_worktree(primary_repo) == primary_repo.resolve()

    def test_returns_primary_when_called_from_a_linked_worktree(self, primary_repo: Path, worktree: Path):
        """Querying from a linked worktree should also resolve to the primary worktree (not the worktree itself)."""
        assert resolve_primary_worktree(worktree) == primary_repo.resolve()

    def test_fails_closed_when_not_a_git_repo(self, tmp_path: Path):
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        with pytest.raises(AcpSessionError):
            resolve_primary_worktree(not_a_repo)

    def test_fails_closed_when_git_binary_is_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        def _raise_not_found(*_args, **_kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(subprocess, "run", _raise_not_found)

        with pytest.raises(AcpSessionError, match="cannot determine git worktree layout"):
            resolve_primary_worktree(tmp_path)

    def test_fails_closed_on_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        def _raise_timeout(*_args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout", 15))

        monkeypatch.setattr(subprocess, "run", _raise_timeout)

        with pytest.raises(AcpSessionError, match="cannot determine git worktree layout"):
            resolve_primary_worktree(tmp_path)

    def test_fails_closed_when_porcelain_output_has_no_worktree_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Defensive branch: even if git returns exit 0, if the porcelain output
        has no `worktree ` line at all (a real git wouldn't behave this way,
        but we can't assume the output format will always match expectations),
        it must still fail-closed rather than silently allow it through."""

        def _empty_success(*_args, **_kwargs):
            return _FakeCompletedProcess(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _empty_success)

        with pytest.raises(AcpSessionError, match="no 'worktree' entries"):
            resolve_primary_worktree(tmp_path)

    def test_error_message_on_git_failure_includes_returncode_and_stderr(self, tmp_path: Path):
        """The message content itself must be meaningful (it's not enough to
        just raise AcpSessionError with empty/None content) -- `git worktree
        list` reliably returns 128 for a non-repo path and explains why in
        stderr, so the message should surface both, making it easy to
        determine the real failure reason from audit logs."""
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()

        with pytest.raises(AcpSessionError) as excinfo:
            resolve_primary_worktree(not_a_repo)

        message = str(excinfo.value)
        assert "128" in message
        assert ("not a git repository" in message) or ("不是一個 git 版本庫" in message) or ("git" in message.lower() and "worktree" in message.lower())

    def test_invokes_the_real_git_binary_with_the_expected_timeout(
        self, primary_repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Spies on `subprocess.run` (still delegating to the real
        implementation, so behavior is unchanged) to confirm the call uses
        lowercase `"git"` (not just any casing variant slipping through --
        on a case-insensitive filesystem, a variant like `"GIT"` might
        accidentally resolve to the same executable, which "did it raise or
        not" alone can't catch), and that
        `_GIT_WORKTREE_LIST_TIMEOUT_SEC` really is passed as `timeout=`
        (not dropped or replaced with `None`, which would leave a runaway
        subprocess without timeout protection)."""
        calls: list[tuple[list[str], dict[str, object]]] = []
        real_run = subprocess.run

        def _spy(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)

        resolve_primary_worktree(primary_repo)

        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv[0] == "git"
        assert argv[1:] == ["-C", str(primary_repo), "worktree", "list", "--porcelain"]
        assert kwargs["timeout"] == _GIT_WORKTREE_LIST_TIMEOUT_SEC


class TestCheckOpencodeWorkdirIsolation:
    def test_noop_for_non_opencode_agent_even_with_bogus_workdir(self, tmp_path: Path):
        """When agent != "opencode", it's always allowed through directly,
        without even needing to check git -- it shouldn't raise even if
        workdir is a path that doesn't exist at all."""
        bogus = tmp_path / "does-not-exist"
        check_opencode_workdir_isolation(agent="claude", workdir=bogus, active_workdirs={})

    def test_rejects_primary_worktree(self, primary_repo: Path):
        with pytest.raises(AcpSessionError, match="PRIMARY"):
            check_opencode_workdir_isolation(agent="opencode", workdir=primary_repo, active_workdirs={})

    def test_rejects_shared_workdir(self, worktree: Path):
        with pytest.raises(AcpSessionError, match="SHARED"):
            check_opencode_workdir_isolation(
                agent="opencode", workdir=worktree, active_workdirs={"other-session": worktree}
            )

    def test_allows_isolated_worktree_with_no_active_sessions(self, worktree: Path):
        check_opencode_workdir_isolation(agent="opencode", workdir=worktree, active_workdirs={})

    def test_allows_isolated_worktree_when_active_sessions_use_other_paths(
        self, primary_repo: Path, worktree: Path, tmp_path: Path
    ):
        other_wt = tmp_path / "worktree-b"
        _git("worktree", "add", "-q", "-b", "wt-b", str(other_wt), cwd=primary_repo)

        check_opencode_workdir_isolation(
            agent="opencode", workdir=worktree, active_workdirs={"other-session": other_wt}
        )
