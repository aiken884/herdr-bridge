# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.binding: pure functions for the pane<->session dispatch ledger
(docs/acp-command-plane-design.md §4.4, canonical join key = herdr pane_id +
dispatch ledger)."""

from __future__ import annotations

from herdr_bridge.acp.binding import (
    LedgerEntry,
    current_binding_for_pane,
    current_binding_for_session,
    detect_drift,
    record_dispatch,
)


def test_record_dispatch_returns_new_list_not_mutated_in_place():
    ledger: list[LedgerEntry] = []
    result = record_dispatch(
        ledger, pane_id="pane-1", session_name="s1", actor_id="gov:main", dispatched_at="2026-07-20T10:00:00Z"
    )

    assert ledger == []  # pure function: original list unchanged
    assert len(result) == 1
    assert result[0].pane_id == "pane-1"
    assert result[0].session_name == "s1"
    assert result[0].actor_id == "gov:main"


def test_ledger_entry_is_frozen():
    entry = LedgerEntry(pane_id="p", session_name="s", actor_id="a", dispatched_at="2026-07-20T10:00:00Z")
    try:
        entry.pane_id = "other"  # type: ignore[misc]
        assert False, "should not be settable"
    except AttributeError:
        pass


def test_current_binding_for_pane_returns_none_when_absent():
    assert current_binding_for_pane([], "pane-1") is None


def test_current_binding_for_pane_returns_the_latest_entry():
    ledger: list[LedgerEntry] = []
    ledger = record_dispatch(ledger, pane_id="p1", session_name="s1", actor_id="a", dispatched_at="2026-07-20T10:00:00Z")
    ledger = record_dispatch(ledger, pane_id="p1", session_name="s2", actor_id="a", dispatched_at="2026-07-20T11:00:00Z")
    ledger = record_dispatch(ledger, pane_id="p2", session_name="s3", actor_id="a", dispatched_at="2026-07-20T09:00:00Z")

    binding = current_binding_for_pane(ledger, "p1")

    assert binding is not None
    assert binding.session_name == "s2"  # the more recent entry


def test_current_binding_for_session_returns_the_latest_entry():
    ledger: list[LedgerEntry] = []
    ledger = record_dispatch(ledger, pane_id="p1", session_name="s1", actor_id="a", dispatched_at="2026-07-20T10:00:00Z")
    ledger = record_dispatch(ledger, pane_id="p2", session_name="s1", actor_id="a", dispatched_at="2026-07-20T11:00:00Z")

    binding = current_binding_for_session(ledger, "s1")

    assert binding is not None
    assert binding.pane_id == "p2"


def test_current_binding_for_session_returns_none_when_absent():
    assert current_binding_for_session([], "s1") is None


def test_detect_drift_finds_no_drift_when_actual_matches_ledger():
    ledger: list[LedgerEntry] = []
    ledger = record_dispatch(ledger, pane_id="p1", session_name="s1", actor_id="a", dispatched_at="2026-07-20T10:00:00Z")

    drifted = detect_drift(ledger, actual_pane_session_map={"p1": "s1"})

    assert drifted == []


def test_detect_drift_flags_a_pane_whose_actual_session_differs_from_the_ledger():
    ledger: list[LedgerEntry] = []
    ledger = record_dispatch(ledger, pane_id="p1", session_name="s1", actor_id="a", dispatched_at="2026-07-20T10:00:00Z")

    drifted = detect_drift(ledger, actual_pane_session_map={"p1": "s-different"})

    assert drifted == ["p1"]


def test_detect_drift_flags_a_pane_with_no_ledger_entry_at_all():
    drifted = detect_drift([], actual_pane_session_map={"p-unknown": "s1"})

    assert drifted == ["p-unknown"]
