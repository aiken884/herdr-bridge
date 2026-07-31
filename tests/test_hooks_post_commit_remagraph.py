# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Verify `.githooks/post-commit`: after a commit completes, a summary should
be automatically written back to RemaGraph.

Charter §3.5.9 point 4: "must write before recycling" doesn't rely on the
agent remembering to manually call `remagraph store` -- instead, this action
is baked into the commit flow itself.

This test copies and runs the real `.githooks/post-commit` file directly (not
a reimplementation of its logic), actually runs `git commit` once in an
isolated temp git repo with an isolated REMAGRAPH_STATE_DIR, then reads the
database back with `remagraph search` and asserts that "RemaGraph really does
have one more matching status_update record after the commit" -- rather than
just checking the exit code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POST_COMMIT_HOOK = REPO_ROOT / ".githooks" / "post-commit"

pytestmark = pytest.mark.skipif(
    shutil.which("remagraph") is None,
    reason="the remagraph CLI is required to verify the post-commit hook's actual write-back behavior",
)


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=30,
        check=False,
    )


def _search(project: str, task_id: str, env: dict[str, str]) -> list[dict]:
    result = subprocess.run(
        ["remagraph", "search", "--project", project, "--task-id", task_id],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"remagraph search failed: {result.stderr}"
    payload = json.loads(result.stdout)
    return payload["results"]


def _install_hook(hooks_dir: Path) -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "post-commit"
    shutil.copy(POST_COMMIT_HOOK, dest)
    dest.chmod(0o755)


def _init_repo(repo_dir: Path, hooks_dir: Path, env: dict[str, str], name: str = "Test Committer",
               email: str = "test@example.com") -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], repo_dir, env)
    _run(["git", "config", "user.name", name], repo_dir, env)
    _run(["git", "config", "user.email", email], repo_dir, env)
    _run(["git", "config", "core.hooksPath", str(hooks_dir)], repo_dir, env)


@pytest.fixture()
def base_env(tmp_path) -> dict[str, str]:
    """An isolated REMAGRAPH_STATE_DIR, to keep the test from polluting real data."""
    env = os.environ.copy()
    env["REMAGRAPH_STATE_DIR"] = str(tmp_path / "remagraph-state")
    env.pop("REMAGRAPH_PROJECT", None)
    env.pop("AGENT_ID", None)
    return env


def test_post_commit_hook_stores_status_update_memory(tmp_path, base_env):
    """Common case: after a commit, the matching status_update record should
    be found in RemaGraph, with project_id derived from the repo directory
    name and agent_id derived from git user.name.
    """
    repo_dir = tmp_path / "sample-project"
    hooks_dir = tmp_path / "hooks"
    _install_hook(hooks_dir)
    _init_repo(repo_dir, hooks_dir, base_env)

    (repo_dir / "file1.txt").write_text("hello\n")
    _run(["git", "add", "file1.txt"], repo_dir, base_env)
    commit = _run(["git", "commit", "-q", "-m", "fix: short subject line"], repo_dir, base_env)
    assert commit.returncode == 0, f"commit failed: {commit.stderr}"

    short_hash = _run(
        ["git", "rev-parse", "--short", "HEAD"], repo_dir, base_env
    ).stdout.strip()
    task_id = f"sample-project-commit-{short_hash}"

    results = _search("sample-project", task_id, base_env)
    assert len(results) == 1, f"expected exactly one record, got: {results}"
    record = results[0]
    assert record["kind"] == "status_update"
    assert record["task_id"] == task_id
    assert record["project_id"] == "sample-project"
    assert record["agent_id"] == "test-committer"
    assert "fix: short subject line" in record["summary"]


def test_post_commit_hook_project_id_is_worktree_safe(tmp_path, base_env):
    """When committing from within a git worktree, project_id must be the
    main repo's directory name, not the worktree's own (unrelated) directory
    name -- this is a pitfall explicitly called out in the charter's
    requirements.
    """
    main_repo = tmp_path / "herdr-bridge"
    hooks_dir = tmp_path / "hooks"
    _install_hook(hooks_dir)
    _init_repo(main_repo, hooks_dir, base_env, name="Tower", email="tower@example.com")
    init_commit = _run(
        ["git", "commit", "-q", "--allow-empty", "-m", "chore: init"], main_repo, base_env
    )
    assert init_commit.returncode == 0

    worktree_dir = tmp_path / "some-unrelated-worktree-dirname"
    add_worktree = _run(
        ["git", "worktree", "add", "-q", "-b", "feat/x", str(worktree_dir)],
        main_repo,
        base_env,
    )
    assert add_worktree.returncode == 0, add_worktree.stderr

    # A worktree doesn't automatically inherit anything beyond core.hooksPath,
    # but hooksPath is shared config for the whole repo (including all
    # worktrees), so this is just a confirmation check.
    (worktree_dir / "f.txt").write_text("content\n")
    _run(["git", "add", "f.txt"], worktree_dir, base_env)
    commit = _run(["git", "commit", "-q", "-m", "feat: add f"], worktree_dir, base_env)
    assert commit.returncode == 0, f"commit failed: {commit.stderr}"

    short_hash = _run(
        ["git", "rev-parse", "--short", "HEAD"], worktree_dir, base_env
    ).stdout.strip()
    expected_task_id = f"herdr-bridge-commit-{short_hash}"

    results = _search("herdr-bridge", expected_task_id, base_env)
    assert len(results) == 1, f"expected to find the record under the main repo's project_id 'herdr-bridge': {results}"
    assert results[0]["project_id"] == "herdr-bridge"

    # A query using the worktree's own directory name should find nothing at
    # all -- proving the worktree's directory name was never mistakenly used.
    wrong_project_results = _search(
        "some-unrelated-worktree-dirname", expected_task_id, base_env
    )
    assert wrong_project_results == []


def test_post_commit_hook_respects_agent_id_env_override(tmp_path, base_env):
    """The AGENT_ID env var should take priority over git config user.name
    (following the existing AGENT_ID convention)."""
    repo_dir = tmp_path / "env-override-project"
    hooks_dir = tmp_path / "hooks"
    _install_hook(hooks_dir)
    _init_repo(repo_dir, hooks_dir, base_env, name="Should Not Be Used")

    env = dict(base_env)
    env["AGENT_ID"] = "claude-headless-03"

    (repo_dir / "g.txt").write_text("g\n")
    _run(["git", "add", "g.txt"], repo_dir, env)
    commit = _run(["git", "commit", "-q", "-m", "feat: env agent id"], repo_dir, env)
    assert commit.returncode == 0

    short_hash = _run(["git", "rev-parse", "--short", "HEAD"], repo_dir, env).stdout.strip()
    task_id = f"env-override-project-commit-{short_hash}"

    results = _search("env-override-project", task_id, env)
    assert len(results) == 1
    assert results[0]["agent_id"] == "claude-headless-03"


def test_post_commit_hook_pads_short_summary_past_arbitration_minimum(tmp_path, base_env):
    """When the commit subject is extremely short (e.g. a single character),
    RemaGraph's arbitration rule requires the summary to be at least 30
    characters; the hook must ensure the record still actually gets stored in
    this case, rather than being silently rejected by the arbitration rule
    (remagraph store also returns exit 0 for a rejection, so exit code alone
    can't tell the difference -- you must actually query the database back
    to confirm the record exists).
    """
    repo_dir = tmp_path / "shortsub"
    hooks_dir = tmp_path / "hooks"
    _install_hook(hooks_dir)
    _init_repo(repo_dir, hooks_dir, base_env)

    (repo_dir / "a.txt").write_text("a\n")
    _run(["git", "add", "a.txt"], repo_dir, base_env)
    commit = _run(["git", "commit", "-q", "-m", "x"], repo_dir, base_env)
    assert commit.returncode == 0

    short_hash = _run(
        ["git", "rev-parse", "--short", "HEAD"], repo_dir, base_env
    ).stdout.strip()
    task_id = f"shortsub-commit-{short_hash}"

    results = _search("shortsub", task_id, base_env)
    assert len(results) == 1, (
        "an extremely short commit subject should still write successfully "
        "(summary should have been padded past the arbitration threshold), "
        f"but no record was found: {results}"
    )
    assert len(results[0]["summary"]) >= 30


def test_post_commit_hook_does_not_hang_when_remagraph_store_never_returns(tmp_path, base_env):
    """The post-commit hook's RemaGraph write must have timeout protection:
    even if remagraph store hangs without responding, the commit itself must
    still complete within a reasonable time -- commit success must take
    priority over memory write success.

    Background (2026-07-25 hard-won evidence, #62): even with zero other
    concurrent git/pytest operations, running this file's tests alone still
    reproduced a 30-second timeout (TimeoutExpired). Investigation found that
    internally, `remagraph store` makes a network request to huggingface.co
    via model2vec's
    `StaticModel.from_pretrained("minishlab/potion-multilingual-128M")` (even
    with the model already cached locally, it still does an online
    verification request), and this code path has zero timeout protection.
    Here we simulate the most extreme case with a fake remagraph CLI that
    "never responds," to verify the hook's own timeout protection actually
    kicks in, rather than just happening to finish within the limit.
    """
    repo_dir = tmp_path / "hang-repro"
    hooks_dir = tmp_path / "hooks"
    _install_hook(hooks_dir)

    fake_bin = tmp_path / "fakebin-hang"
    fake_bin.mkdir()
    for tool in ("git", "bash", "sh", "sed", "tr", "dirname", "basename", "cat", "env", "sleep", "kill"):
        real = shutil.which(tool)
        if real:
            (fake_bin / tool).symlink_to(real)
    fake_remagraph = fake_bin / "remagraph"
    fake_remagraph.write_text("#!/usr/bin/env bash\nsleep 300\n")
    fake_remagraph.chmod(0o755)

    env = dict(base_env)
    env["PATH"] = str(fake_bin)

    _init_repo(repo_dir, hooks_dir, env)
    (repo_dir / "c.txt").write_text("c\n")
    _run(["git", "add", "c.txt"], repo_dir, env)

    import time

    start = time.time()
    commit = subprocess.run(
        ["git", "commit", "-q", "-m", "chore: hang test"],
        cwd=str(repo_dir), env=env, capture_output=True, text=True, timeout=25,
        check=False,
    )
    elapsed = time.time() - start

    assert commit.returncode == 0, f"commit should not fail when remagraph store hangs: {commit.stderr}"
    assert elapsed < 20, (
        f"commit should finish well within the outer test timeout cap (25s), "
        f"but actually took {elapsed:.1f}s -- meaning the hook isn't actually "
        f"enforcing a timeout on remagraph store"
    )


def test_post_commit_hook_noop_when_remagraph_not_installed(tmp_path, base_env):
    """When remagraph isn't installed, the hook must skip silently: commit
    succeeds normally, only a single hint line is printed to stderr, and no
    uncaught error surfaces or blocks the commit.
    """
    repo_dir = tmp_path / "norema"
    hooks_dir = tmp_path / "hooks"
    _install_hook(hooks_dir)
    _init_repo(repo_dir, hooks_dir, base_env)

    # Build a PATH that deliberately excludes remagraph, keeping only the minimal toolset git needs.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    for tool in ("git", "bash", "sh", "sed", "tr", "dirname", "basename", "cat", "env"):
        real = shutil.which(tool)
        if real:
            (fake_bin / tool).symlink_to(real)

    env = dict(base_env)
    env["PATH"] = str(fake_bin)

    (repo_dir / "b.txt").write_text("b\n")
    add = _run(["git", "add", "b.txt"], repo_dir, env)
    assert add.returncode == 0
    commit = _run(["git", "commit", "-q", "-m", "chore: no remagraph installed"], repo_dir, env)

    assert commit.returncode == 0, f"commit should not fail when remagraph isn't installed: {commit.stderr}"
    assert "remagraph not installed" in commit.stderr
    assert "Traceback" not in commit.stderr
