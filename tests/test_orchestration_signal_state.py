# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""orchestration.memory: Signal ACK state machine (design doc §3.1/§3.5/§3.7).

Uses a real signal_state_store.sqlite (via tmp_path monkeypatched into
_signal_state_dir), not a mock — matches this project's existing convention
for delivery_state_store's own tests.
"""

from __future__ import annotations

import pytest

from herdr_bridge.errors import SignalStateWriteFailed
from herdr_bridge.orchestration import memory as memory_mod


@pytest.fixture(autouse=True)
def _isolated_signal_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_mod, "_signal_state_dir", lambda project_id: tmp_path / project_id)


def test_mark_accepted_then_injected_then_seen_then_work_then_completed():
    mid = "msg-1"
    memory_mod.mark_accepted(
        "herdr-bridge", mid, from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1"
    )
    memory_mod.mark_injected("herdr-bridge", mid)
    memory_mod.mark_seen("herdr-bridge", mid)
    memory_mod.mark_accepted_for_work("herdr-bridge", mid)
    result = memory_mod.mark_completed("herdr-bridge", mid)
    assert result["state"] == "completed"

    row = memory_mod.get_signal_state("herdr-bridge", mid)
    assert row["state"] == "completed"
    assert row["accepted_at"] is not None
    assert row["injected_at"] is not None
    assert row["seen_at"] is not None


def test_get_signal_state_returns_none_for_unknown_message_id():
    assert memory_mod.get_signal_state("herdr-bridge", "never-seen") is None


def test_mark_injected_before_accepted_is_allowed():
    """daemon.py and outbound.py are independent processes racing to write the
    same row (no ordering guarantee between "sender recorded Accepted" and
    "receiver finished injecting") — mark_injected() with no prior mark_accepted()
    must succeed, not raise (see SIGNAL_STATE_TRANSITIONS's race-condition note)."""
    result = memory_mod.mark_injected("herdr-bridge", "msg-skip")
    assert result["state"] == "injected"


def test_late_mark_accepted_does_not_regress_an_already_injected_state():
    """The reverse ordering of the same race: mark_accepted() arriving after
    mark_injected() already landed must backfill accepted_at without
    regressing state back to "accepted"."""
    mid = "msg-race"
    memory_mod.mark_injected("herdr-bridge", mid)
    result = memory_mod.mark_accepted(
        "herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c"
    )
    assert result["state"] == "injected"
    assert result["accepted_at"] is not None


def test_mark_accepted_survives_daemon_racing_past_it_between_its_two_reads(monkeypatch):
    """2026-08-02 field incident (a real downstream deployment repro'd this
    twice, root-fix request): mark_accepted() reads current state once to decide target_state,
    but _write_signal_state() internally re-reads current state a SECOND time
    to validate the transition -- two independent reads, not one. Across two
    independent OS processes (outbound.py the sender, daemon.py the receiver)
    with no shared lock between them, the daemon can advance the row from
    whatever mark_accepted()'s own read saw all the way to "completed" in the
    gap before _write_signal_state()'s internal read lands. mark_accepted()'s
    target_state is then stale: _write_signal_state() validates it against the
    FRESH "completed" it just read, and _validate_signal_transition("completed",
    "accepted") is illegal (completed's allowed transitions are empty) --
    raising ValueError uncaught all the way up through outbound.send(),
    crashing the caller's whole `signal send` CLI process even though the send
    had already succeeded. Must instead retry once against the now-current
    state and backfill accepted_at without regressing or raising (PPLX
    consensus 2026-08-02: bounded retry local to mark_accepted(), not a
    general-purpose escape hatch in _write_signal_state() -- other callers
    like mark_injected()/mark_completed() must keep raising on a genuinely
    illegal transition)."""
    mid = "msg-race-toctou"
    # This is what the daemon has ALREADY finished writing by the time
    # mark_accepted()'s own write attempt lands, in the real race.
    memory_mod.mark_injected("herdr-bridge", mid)
    memory_mod.mark_completed("herdr-bridge", mid)

    real_read_state = memory_mod._signal_store.read_state
    calls = {"n": 0}

    def stale_first_read(state_dir, message_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # mark_accepted()'s own read: stale, predates the daemon's writes
        return real_read_state(state_dir, message_id)  # every read after: sees the real "completed"

    monkeypatch.setattr(memory_mod._signal_store, "read_state", stale_first_read)

    result = memory_mod.mark_accepted(
        "herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c"
    )

    assert result["state"] == "completed"
    row = memory_mod.get_signal_state("herdr-bridge", mid)
    assert row["state"] == "completed"
    assert row["accepted_at"] is not None
    assert calls["n"] >= 2


def test_mark_accepted_retry_budget_is_bounded_not_infinite(monkeypatch):
    """The retry added for the TOCTOU race above must not become an unbounded
    loop: if mark_accepted()'s own decision-read keeps seeing a stale value on
    every attempt (an extreme, contrived scenario -- real races settle within
    one retry, this is a defensive bound check, not a plausible production
    case), it must eventually give up and raise rather than retrying forever.
    Every ODD read_state() call simulates mark_accepted()'s own decision-read
    seeing a stale None; every EVEN call is _write_signal_state()'s internal
    validate-read seeing the real, already-completed row -- so every attempt
    computes target_state="accepted" and every validation rejects it against
    the real "completed" state, exactly like a race that never once lines up."""
    mid = "msg-persistent-race"
    memory_mod.mark_injected("herdr-bridge", mid)
    memory_mod.mark_completed("herdr-bridge", mid)

    real_read_state = memory_mod._signal_store.read_state
    calls = {"n": 0}

    def alternating_stale_read(state_dir, message_id):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            return None
        return real_read_state(state_dir, message_id)

    monkeypatch.setattr(memory_mod._signal_store, "read_state", alternating_stale_read)

    with pytest.raises(ValueError, match="Invalid Signal transition"):
        memory_mod.mark_accepted(
            "herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c"
        )
    # Bounded, not infinite: a handful of attempts at most, never a runaway loop.
    assert calls["n"] <= 10

    monkeypatch.setattr(memory_mod._signal_store, "read_state", real_read_state)
    # The row itself must be untouched by the failed attempts -- still "completed".
    row = memory_mod.get_signal_state("herdr-bridge", mid)
    assert row["state"] == "completed"


def test_daemon_unreachable_is_reachable_directly_from_none():
    """§3.5 rule 1/2: no Accepted was ever received — daemon_unreachable is a
    valid first state, not a transition off of accepted."""
    result = memory_mod.mark_escalated("herdr-bridge", "msg-unreachable", "daemon_unreachable")
    assert result["state"] == "daemon_unreachable"


def test_daemon_unreachable_is_terminal_against_further_escalation():
    memory_mod.mark_escalated("herdr-bridge", "msg-2", "daemon_unreachable")
    with pytest.raises(ValueError, match="Invalid Signal transition"):
        memory_mod.mark_injected("herdr-bridge", "msg-2")


def test_late_mark_accepted_does_not_regress_daemon_unreachable():
    """A stray/delayed Accepted reply arriving after the sender already gave up
    and marked daemon_unreachable must not resurrect the row as "accepted"."""
    memory_mod.mark_escalated("herdr-bridge", "msg-2b", "daemon_unreachable")
    result = memory_mod.mark_accepted(
        "herdr-bridge", "msg-2b", from_project="a", to_project="b", inbox_ref="c"
    )
    assert result["state"] == "daemon_unreachable"


def test_injection_unconfirmed_then_late_injected_still_allowed():
    """§3.1: injection_unconfirmed is an escalation flag, not a hard terminal
    state — a late Injected can still legally arrive afterward."""
    mid = "msg-3"
    memory_mod.mark_accepted("herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c")
    memory_mod.mark_escalated("herdr-bridge", mid, "injection_unconfirmed")
    result = memory_mod.mark_injected("herdr-bridge", mid)
    assert result["state"] == "injected"


def test_needs_attention_then_late_seen_still_allowed():
    mid = "msg-4"
    memory_mod.mark_accepted("herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c")
    memory_mod.mark_injected("herdr-bridge", mid)
    memory_mod.mark_escalated("herdr-bridge", mid, "needs_attention")
    result = memory_mod.mark_seen("herdr-bridge", mid)
    assert result["state"] == "seen"


def test_mark_escalated_rejects_unknown_reason():
    with pytest.raises(ValueError, match="mark_escalated reason must be"):
        memory_mod.mark_escalated("herdr-bridge", "msg-5", "made_up_reason")


def test_mark_escalated_is_a_noop_when_daemon_already_completed_the_send():
    """2026-08-02 field incident (multiple downstream deployments hit this
    live): daemon.py calls mark_injected() then mark_completed() back-to-back, so
    outbound.py's own poll loop can miss the single instant the state equals
    "injected" and observe "completed" instead, or simply still be mid-flight
    when the daemon finishes. Either way outbound.py's timeout fires and it
    calls mark_escalated(..., "injection_unconfirmed") for a send that
    actually succeeded. This used to raise ValueError (completed -> stuck at
    a terminal state does not allow transitioning to injection_unconfirmed)
    and crash the caller's whole CLI process. It must instead be a no-op that
    returns the already-completed row untouched."""
    mid = "msg-race-completed"
    memory_mod.mark_accepted("herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c")
    memory_mod.mark_injected("herdr-bridge", mid)
    memory_mod.mark_completed("herdr-bridge", mid)

    result = memory_mod.mark_escalated("herdr-bridge", mid, "injection_unconfirmed")

    assert result["state"] == "completed"
    row = memory_mod.get_signal_state("herdr-bridge", mid)
    assert row["state"] == "completed"


def test_mark_escalated_survives_daemon_completing_between_precheck_and_write(monkeypatch):
    """2026-08-02 field incident, the ACTUAL reported bug (repro'd
    twice in the field, traceback at signal/outbound.py's mark_escalated() call
    site: "Invalid Signal transition: completed -> injection_unconfirmed.
    Allowed: []", exit 1 -- yet `signal status` showed the envelope already
    state=completed). The 2026-08-01 fix (see
    test_mark_escalated_is_a_noop_when_daemon_already_completed_the_send)
    only closed the WIDE version of this race, where the daemon had already
    reached "completed" by the time mark_escalated()'s own pre-check read
    landed -- that pre-check correctly no-ops without ever calling
    _write_signal_state(). It did not close the NARROW version: if the
    pre-check's read lands BEFORE the daemon finishes (sees e.g. "accepted",
    where "injection_unconfirmed" is still a legal transition and the
    pre-check does not short-circuit), _write_signal_state() proceeds and
    performs its OWN internal re-read to validate -- and if the daemon
    finishes in the gap between those two reads, THAT read sees "completed",
    and the validation raises uncaught, exactly reproducing the field
    incident. Same underlying structural bug as mark_accepted()'s TOCTOU gap
    above, same PPLX-consensus fix: bounded local retry against the freshest
    state, re-running the no-op check against it rather than blindly retrying
    the write."""
    mid = "msg-race-narrow-window"
    memory_mod.mark_accepted("herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c")

    real_read_state = memory_mod._signal_store.read_state
    calls = {"n": 0}

    def precheck_sees_accepted_then_daemon_completes(state_dir, message_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "message_id": message_id, "from_project": "a", "to_project": "b", "inbox_ref": "c",
                "state": "accepted", "attempt_count": 0, "accepted_at": 1.0,
                "injected_at": None, "seen_at": None, "created_at": 1.0, "updated_at": 1.0,
            }
        # By the time _write_signal_state()'s internal read lands, the daemon
        # has actually already finished the whole chain -- reflect the real
        # row from here on.
        if calls["n"] == 2:
            memory_mod.mark_injected("herdr-bridge", mid)
            memory_mod.mark_completed("herdr-bridge", mid)
        return real_read_state(state_dir, message_id)

    monkeypatch.setattr(
        memory_mod._signal_store, "read_state", precheck_sees_accepted_then_daemon_completes
    )

    result = memory_mod.mark_escalated("herdr-bridge", mid, "injection_unconfirmed")

    assert result["state"] == "completed"
    monkeypatch.setattr(memory_mod._signal_store, "read_state", real_read_state)
    row = memory_mod.get_signal_state("herdr-bridge", mid)
    assert row["state"] == "completed"


def test_mark_escalated_needs_attention_is_a_noop_when_already_seen():
    """Same race, different escalation reason: a stray needs_attention firing
    after the message was already Seen must not regress or raise."""
    mid = "msg-race-seen"
    memory_mod.mark_accepted("herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c")
    memory_mod.mark_injected("herdr-bridge", mid)
    memory_mod.mark_seen("herdr-bridge", mid)

    result = memory_mod.mark_escalated("herdr-bridge", mid, "needs_attention")

    assert result["state"] == "seen"


def test_completed_is_terminal():
    mid = "msg-6"
    memory_mod.mark_accepted("herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c")
    memory_mod.mark_injected("herdr-bridge", mid)
    memory_mod.mark_seen("herdr-bridge", mid)
    memory_mod.mark_accepted_for_work("herdr-bridge", mid)
    memory_mod.mark_completed("herdr-bridge", mid)
    with pytest.raises(ValueError, match="Invalid Signal transition"):
        memory_mod.mark_seen("herdr-bridge", mid)


def test_timestamps_are_sticky_across_later_writes():
    """accepted_at recorded at mark_accepted() time must survive later writes
    that don't pass accepted_at again (write_state's COALESCE behavior)."""
    mid = "msg-7"
    memory_mod.mark_accepted("herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c")
    accepted_row = memory_mod.get_signal_state("herdr-bridge", mid)
    memory_mod.mark_injected("herdr-bridge", mid)
    injected_row = memory_mod.get_signal_state("herdr-bridge", mid)
    assert injected_row["accepted_at"] == accepted_row["accepted_at"]


def test_write_failure_raises_signal_state_write_failed(monkeypatch):
    from herdr_bridge.orchestration import signal_state_store

    def _broken_read(*args, **kwargs):
        return None  # simulates the write not actually landing

    monkeypatch.setattr(signal_state_store, "read_state", _broken_read)
    with pytest.raises(SignalStateWriteFailed):
        memory_mod.mark_escalated("herdr-bridge", "msg-broken", "daemon_unreachable")


def test_daemon_advances_injected_straight_to_completed():
    """2026-08-01 fix: injected -> completed must be a legal transition (daemon.py
    calls mark_completed() itself right after mark_injected(), see
    SIGNAL_STATE_TRANSITIONS's docstring) -- without this, every Signal was
    permanently stuck at "injected"."""
    mid = "msg-daemon-complete"
    memory_mod.mark_accepted("herdr-bridge", mid, from_project="a", to_project="b", inbox_ref="c")
    memory_mod.mark_injected("herdr-bridge", mid)
    result = memory_mod.mark_completed("herdr-bridge", mid)
    assert result["state"] == "completed"


# -- find_active_signal_by_target / inflight timeout (F1+F2 fix) ------------

def test_find_active_signal_by_target_finds_a_recent_non_completed_record():
    memory_mod.mark_accepted(
        "herdr-bridge", "msg-active-1", from_project="a", to_project="remagraph", inbox_ref="task-x",
    )
    active = memory_mod.find_active_signal_by_target("herdr-bridge", "remagraph", "task-x")
    assert active is not None
    assert active["message_id"] == "msg-active-1"


def test_find_active_signal_by_target_ignores_completed_records():
    memory_mod.mark_accepted(
        "herdr-bridge", "msg-done", from_project="a", to_project="remagraph", inbox_ref="task-y",
    )
    memory_mod.mark_injected("herdr-bridge", "msg-done")
    memory_mod.mark_completed("herdr-bridge", "msg-done")
    assert memory_mod.find_active_signal_by_target("herdr-bridge", "remagraph", "task-y") is None


def test_find_active_signal_by_target_ignores_records_past_the_inflight_timeout(monkeypatch):
    """Regression test for the F2 half of the production bug: a record stuck
    at a non-completed state (e.g. the daemon crashed between mark_injected()
    and mark_completed()) must not block resends forever."""
    from herdr_bridge.orchestration import signal_state_store

    real_time = signal_state_store.time.time
    long_ago = real_time() - (2 * signal_state_store.DEFAULT_INFLIGHT_TIMEOUT_SECONDS)
    monkeypatch.setattr(signal_state_store.time, "time", lambda: long_ago)
    memory_mod.mark_accepted(
        "herdr-bridge", "msg-stuck", from_project="a", to_project="remagraph", inbox_ref="task-z",
    )
    memory_mod.mark_injected("herdr-bridge", "msg-stuck")
    # restore the real clock (only this one attribute) so the query below
    # evaluates "now" correctly -- not a blanket monkeypatch.undo(), which
    # would also revert this file's autouse _isolated_signal_state_dir fixture
    monkeypatch.setattr(signal_state_store.time, "time", real_time)

    assert memory_mod.find_active_signal_by_target("herdr-bridge", "remagraph", "task-z") is None
    # not deleted -- the full record is still readable for audit purposes
    stuck = memory_mod.get_signal_state("herdr-bridge", "msg-stuck")
    assert stuck is not None
    assert stuck["state"] == "injected"
