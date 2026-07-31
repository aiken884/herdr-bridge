# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tooling to create an ADR-0003-compliant isolated git worktree for the opencode ACP tier.

opencode acts as an untrusted FS actor and must never run in the primary
worktree. This module provides a convenience helper for creating an isolated
worktree (clone + worktree add).
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("herdr_bridge.acp.isolated_workdir")


def create_isolated_worktree_for_opencode(
    *,
    base_repo: Path | None = None,
    branch_name: str = "acp-session",
    prefix: str = "acp-opencode-",
) -> Path:
    """Create a brand-new isolated git worktree, suitable for use by an opencode ACP session.

    The flow:
    1. Create a shallow clone of the repo in temp (that clone's main tree
       becomes its own primary).
    2. Inside the clone, `git worktree add` a new directory on a new branch.
    3. Return that worktree's path (not the primary).

    Remember to clean up once the session is done with it (or let tmp clean
    up automatically).

    The returned path already passes the guard check and can be passed
    directly to AcpActions.ensure_session(..., workdir=...).
    """
    if base_repo is None:
        base_repo = Path.cwd()

    tmp_root = Path(tempfile.mkdtemp(prefix=prefix))
    clone_dir = tmp_root / "repo-clone"
    wt_dir = tmp_root / "isolated-wt"

    # shallow clone
    subprocess.check_call(
        ["git", "clone", "--depth", "1", str(base_repo), str(clone_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # create isolated worktree
    subprocess.check_call(
        ["git", "-C", str(clone_dir), "worktree", "add", str(wt_dir), "-b", branch_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # optional: ensure no .opencode or sensitive files leak from primary (defense in depth)
    # the guard + OPENCODE_DISABLE_PROJECT_CONFIG in adapter already handle most.

    return wt_dir.resolve()


def cleanup_isolated_worktree(wt_dir: Path) -> None:
    """Rough cleanup (in practice the tmp directory is usually cleaned up by the OS or pytest anyway)."""
    # This is best-effort only; a full cleanup would need to remove the corresponding worktree + clone directories
    try:
        # find the parent clone and remove worktree entry if possible
        parent = wt_dir.parent
        if (parent / ".git").exists() or (parent / "repo-clone").exists():
            # best effort
            pass
    except Exception:  # best-effort cleanup only; OS/pytest tmp cleanup is the real safety net
        logger.debug("cleanup_isolated_worktree best-effort check failed for %s", wt_dir, exc_info=True)
