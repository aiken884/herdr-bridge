# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `herdr_bridge.acp.isolated_workdir` (ADR 0003 opencode isolated worktree).

`create_isolated_worktree_for_opencode()` only does `git clone --depth 1` +
`git worktree add`, both local, network-free git operations, so we can verify
it by running the real flow against a freshly created minimal git repo --
no mocking needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from herdr_bridge.acp.isolated_workdir import (
    cleanup_isolated_worktree,
    create_isolated_worktree_for_opencode,
)


def _make_minimal_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "primary-repo"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-q"], cwd=repo)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "test"], cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "README.md"], cwd=repo)
    subprocess.check_call(["git", "commit", "-q", "-m", "init"], cwd=repo)
    return repo


def test_create_isolated_worktree_returns_resolved_new_worktree_path(tmp_path):
    repo = _make_minimal_git_repo(tmp_path)

    wt_dir = create_isolated_worktree_for_opencode(base_repo=repo, branch_name="acp-test-branch")

    assert wt_dir.exists()
    assert wt_dir.is_absolute()
    # the git worktree directory itself is a file (pointing back at the
    # primary repo's .git directory), not a folder
    assert (wt_dir / ".git").exists()
    # confirm it actually checked out to the specified branch, and that
    # README.md's content matches the original commit
    assert (wt_dir / "README.md").read_text(encoding="utf-8") == "hello\n"
    branch = subprocess.check_output(
        ["git", "-C", str(wt_dir), "branch", "--show-current"], text=True
    ).strip()
    assert branch == "acp-test-branch"


def test_create_isolated_worktree_defaults_base_repo_to_cwd(tmp_path, monkeypatch):
    repo = _make_minimal_git_repo(tmp_path)
    monkeypatch.chdir(repo)

    wt_dir = create_isolated_worktree_for_opencode()

    assert wt_dir.exists()
    assert (wt_dir / "README.md").exists()


def test_create_isolated_worktree_uses_custom_prefix(tmp_path):
    repo = _make_minimal_git_repo(tmp_path)

    wt_dir = create_isolated_worktree_for_opencode(base_repo=repo, prefix="custom-prefix-")

    # tmp_root's name carries the prefix; wt_dir is tmp_root/isolated-wt
    assert wt_dir.parent.name.startswith("custom-prefix-")


def test_cleanup_isolated_worktree_does_not_raise_on_normal_worktree(tmp_path):
    repo = _make_minimal_git_repo(tmp_path)
    wt_dir = create_isolated_worktree_for_opencode(base_repo=repo)

    cleanup_isolated_worktree(wt_dir)  # currently just a best-effort no-op; this verifies it doesn't raise


def test_cleanup_isolated_worktree_does_not_raise_on_nonexistent_path(tmp_path):
    cleanup_isolated_worktree(tmp_path / "does" / "not" / "exist")
