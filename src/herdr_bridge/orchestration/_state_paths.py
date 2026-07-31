# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-project state dir path computation shared by RemaGraph and the FSM's dedicated store.

Extracted out of `orchestration/memory.py` into its own module so that
`orchestration/delivery_state_store.py` (#72) can reuse the same path rules
without importing `memory.py` (avoiding a circular import: `memory.py`
needs to import `delivery_state_store.py` for the FSM's terminal-state
dual-write).
"""

from __future__ import annotations

from pathlib import Path


def slugify_project(project_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in project_id)
    return safe or "default"


def project_state_dir(project_id: str) -> Path:
    """Compute the herdr-bridge state dir for this project_id (shared by the RemaGraph DB and the FSM's dedicated store).

    Self-protection measure (2026-07-25 #66 postmortem): deliberately
    deviates from RemaGraph's standard `remagraph-<project_id>` naming
    convention by adding the herdr-bridge-specific prefix "hb-live-".

    Reason: a long-running `remagraph serve` process (PID 3760) that
    doesn't belong to herdr-bridge — started by another project's
    (MegaNote's) agent — derives paths using the standard convention and
    runs cleanup/rebuild maintenance on any directory it recognizes as its
    own. This was wiping out herdr-bridge's live memory within minutes
    (measured: under the standard path, the file got unlinked and rebuilt
    within 3.75 minutes; after switching to this deviating path, a control
    run survived 6.75 minutes with the inode completely unchanged). This
    isn't cosmetic — it's an environment-level self-protection measure for
    as long as we can't require the external process to add project
    isolation (it isn't ours, and the cross-project collaboration boundary
    doesn't allow us to touch it directly). Once #66's root cause is fixed
    (that serve process gets project isolation), this workaround can be
    evaluated for removal — until then, every caller must obtain the path
    through this function; don't recompute it elsewhere with the standard
    rule, or you'll bypass this protection and land back in the same trap.

    #72: the FSM's dedicated store (delivery_state_store.py) also puts its
    own sqlite file in this same directory (alongside RemaGraph's
    remagraph.db, under a different filename) — reusing the same
    self-protection layer instead of opening a new top-level directory that
    an external serve process could recognize.
    """
    safe = slugify_project(project_id)
    return Path.home() / ".local" / "state" / f"remagraph-hb-live-{safe}"
