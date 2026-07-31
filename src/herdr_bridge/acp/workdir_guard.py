# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Implements ADR 0003 Decision #2 + Known Limitation #2: mandatory workdir/git
worktree isolation checks for the opencode-family tier.

This only runs once, right before `AcpActions` calls
`self._transport.ensure_session()` when creating a session (`ensure_session()`,
`exec_prompt()`) — ADR 0003 Known Limitation #2 explicitly scopes this as a
one-time, workdir-level check whose timing "covers at least the moment
`ensure_session()` is called," not a process-level/runtime interceptor
(Known Limitation #1: an opencode process later `cd`-ing / `git checkout`-ing
/ `git reset`-ing its way out of the workdir boundary on its own is outside
the scope of this line of defense).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from herdr_bridge.acp.errors import AcpSessionError

_GIT_WORKTREE_LIST_TIMEOUT_SEC = 15


def resolve_primary_worktree(workdir: Path) -> Path:
    """Return the primary worktree path (already `resolve()`d) of the git repo `workdir` belongs to.

    Obtained via `git -C <workdir> worktree list --porcelain` — the first
    `worktree` line is always the primary worktree (`git-worktree(1)`; ADR
    0003 Known Limitation #2). `workdir` itself doesn't have to be the
    primary worktree — as long as it's inside the same repo (including any
    linked worktree), the full list can still be queried.

    Fail-closed (explicitly required by ADR 0003 Known Limitation #2): if the
    git call fails (`workdir` isn't a git repo, `git` isn't installed, it
    times out, the porcelain output has no `worktree` lines to parse, etc.),
    this always raises `AcpSessionError` — when we can't determine where the
    primary worktree is, we can't guarantee this session is safe, so we don't
    try to guess or let it through by some other means.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(workdir), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_GIT_WORKTREE_LIST_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcpSessionError(
            f"cannot determine git worktree layout for {workdir} ({exc}) — "
            f"failing closed per ADR 0003 Known Limitation #2"
        ) from exc

    if result.returncode != 0:
        raise AcpSessionError(
            f"cannot determine git worktree layout for {workdir} — "
            f"'git worktree list --porcelain' exited {result.returncode}: "
            f"{result.stderr.strip()} — failing closed per ADR 0003 Known Limitation #2"
        )

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):]).resolve()

    raise AcpSessionError(
        f"cannot determine git worktree layout for {workdir} — "
        f"'git worktree list --porcelain' returned no 'worktree' entries — "
        f"failing closed per ADR 0003 Known Limitation #2"
    )


def check_opencode_workdir_isolation(
    *,
    agent: str,
    workdir: Path,
    active_workdirs: dict[str, Path],
) -> None:
    """The concrete check implementing ADR 0003 Decision #2. Only takes effect
    for `agent == "opencode"` — the claude family isn't subject to this
    restriction (Decision #3: ACP permission negotiation has been confirmed
    to work for the claude adapter, which can evolve along the established
    S0->S1->S2 path without needing this defense-in-depth workdir isolation).

    - Rejects the case where `workdir` equals the primary worktree of its repo.
    - Rejects the case where `workdir` is shared with any existing active
      session (`active_workdirs`, which the caller populates with only
      `closed=False` sessions).

    Both comparisons `Path.resolve()` first rather than doing a plain
    string-prefix comparison (to prevent symlink/hardlink bypasses — ADR 0003
    Known Limitation #2 explicitly forbids taking the string-comparison
    shortcut here). Raises `AcpSessionError` on rejection, with a message that
    clearly distinguishes which kind of violation occurred.
    """
    if agent != "opencode":
        return

    resolved = workdir.resolve()

    primary = resolve_primary_worktree(resolved)
    if resolved == primary:
        raise AcpSessionError(
            f"refusing opencode-tier session: workdir {resolved} is the "
            f"PRIMARY git worktree of its repo — opencode is an untrusted FS "
            f"actor (ADR 0003 Decision #1) and must run in an isolated, "
            f"non-primary git worktree, never the primary working tree "
            f"(ADR 0003 Decision #2)"
        )

    for other_session_name, other_workdir in active_workdirs.items():
        if other_workdir.resolve() == resolved:
            raise AcpSessionError(
                f"refusing opencode-tier session: workdir {resolved} is "
                f"SHARED with active session {other_session_name!r} — "
                f"opencode-tier sessions must not share a workdir with "
                f"another active session (ADR 0003 Decision #2)"
            )
