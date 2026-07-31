# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Pure functions mapping herdr panes to ACP sessions (docs/acp-command-plane-design.md
§4.4, D-6/D-7, policy-neutral).

The canonical join key is the herdr `pane_id` (empirically stable across server
runtime) plus the dispatch ledger — a lookup table of
(pane_id x session_name x dispatching actor x time) — not `session_name` on
its own. `session_name` is just a schema-free string bridge; once a session
is rebuilt, an old key silently goes stale. The ledger lets the governance
layer audit, at any moment, "which pane is currently carrying which dispatch
of which session," and drift is detected by reconciliation (`detect_drift`)
rather than by trusting a string.

Every function here is pure: `record_dispatch` returns a new list
(append-only), never mutating in place. Storing/persisting the ledger itself
is the caller's (`AcpActions`) responsibility — this module only handles data
shape and query logic, and makes no permission decisions (policy-neutral).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LedgerEntry:
    """A single dispatch record: some actor, at some point in time, assigned some session to some pane."""

    pane_id: str
    session_name: str
    actor_id: str
    dispatched_at: str  # ISO8601 UTC; supplied by the caller — this module never generates timestamps


def record_dispatch(
    ledger: list[LedgerEntry],
    *,
    pane_id: str,
    session_name: str,
    actor_id: str,
    dispatched_at: str,
) -> list[LedgerEntry]:
    """Append one dispatch record and return the new ledger (does not mutate the passed-in `ledger`)."""
    return [*ledger, LedgerEntry(pane_id=pane_id, session_name=session_name, actor_id=actor_id, dispatched_at=dispatched_at)]


def current_binding_for_pane(ledger: list[LedgerEntry], pane_id: str) -> LedgerEntry | None:
    """The session binding a pane currently carries (its most recent dispatch); `None` if not found."""
    matches = [entry for entry in ledger if entry.pane_id == pane_id]
    if not matches:
        return None
    return max(matches, key=lambda entry: entry.dispatched_at)


def current_binding_for_session(ledger: list[LedgerEntry], session_name: str) -> LedgerEntry | None:
    """The pane a session is currently bound to (its most recent dispatch); `None` if not found."""
    matches = [entry for entry in ledger if entry.session_name == session_name]
    if not matches:
        return None
    return max(matches, key=lambda entry: entry.dispatched_at)


def detect_drift(
    ledger: list[LedgerEntry], *, actual_pane_session_map: dict[str, str]
) -> list[str]:
    """Reconciliation: compare the "pane_id -> session_name" the governance layer
    actually observed against the latest bindings recorded in the ledger, and
    return the list of pane_ids where there's a mismatch (including panes the
    ledger has no record of at all).
    """
    drifted = []
    for pane_id, actual_session in actual_pane_session_map.items():
        binding = current_binding_for_pane(ledger, pane_id)
        if binding is None or binding.session_name != actual_session:
            drifted.append(pane_id)
    return drifted
