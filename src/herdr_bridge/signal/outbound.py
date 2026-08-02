# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Sender side: build + sign a wake envelope, deliver it to the target tower's
Signal daemon, and apply the §3.5 escalation rules.

Write ownership: this module writes `accepted` and the three escalation
markers (`daemon_unreachable`/`injection_unconfirmed`/`needs_attention`) —
never `injected`/`seen`/`accepted_for_work`/`completed`, which belong to the
receiving tower's daemon/agent respectively (see signal/daemon.py and
orchestration/memory.py's write-ownership note).

State home: every write in this module targets `envelope.to_project`'s ACK
state store, not the sender's own project — there is exactly one canonical
state row per message_id, and it lives where the receiving daemon already
writes `injected`/`seen`/etc, not a second sender-side copy that could drift.

Escalation-window implementation note (deviates from the design doc's literal
"outbound.py holds a resident 15s/60s timer" wording — design doc §3.7 v5.2):
- Rule 1/2 (Accepted) and rule 3 (Injected, calibrated ~2s per §4 M0-A) are
  short enough to wait for synchronously inside `send()` — a caller willing to
  block ~22s worst case for delivery confirmation is the normal CLI usage
  pattern (`herdr-commander notify-pane` already blocks similarly for its own
  retries).
- Rule 4 (Seen, 60s) is NOT implemented as a literal blocking timer — a
  60-second blocking CLI call is impractical, and a timer object only lives as
  long as the process holding it (exactly the problem the whole Signal design
  exists to solve). Instead, `check_needs_attention()` is a lazy/pull check:
  given a message_id, it computes elapsed time since `injected_at` and marks
  `needs_attention` if over the threshold, callable on demand (by
  `herdr-commander signal status`/`doctor`, a scheduled loop, or anything
  else) rather than needing a specific process to stay alive holding a timer.
  This satisfies rule 4's intent (escalate a real gap) more robustly than a
  literal timer would (survives process restarts; nothing to lose).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from herdr_bridge.orchestration import (
    find_active_signal_by_target,
    get_signal_state,
    mark_accepted,
    mark_escalated,
)
from herdr_bridge.signal.daemon import MERGE_WINDOW_SECONDS
from herdr_bridge.signal.envelope import Envelope

logger = logging.getLogger(__name__)

#: §3.5 rule 1: wait this long for the Accepted ACK before retrying.
ACCEPT_TIMEOUT_SECONDS = 5.0
#: §3.5 rule 1: retry delays after the first attempt (2 retries, increasing interval).
ACCEPT_RETRY_DELAYS_SECONDS = (5.0, 10.0)
#: §3.5 rule 3. 2026-08-02 field incident fix: this used to be a
#: separately-tracked "2.0s, calibrated per M0-A's measured notify-pane
#: worst case" constant that never accounted for daemon.py's
#: MERGE_WINDOW_SECONDS -- the daemon deliberately waits up to that long to
#: batch same-window wakes BEFORE it ever calls notify-pane, so a sender
#: waiting only 2s would time out and report "injection_failed_transient"
#: on almost every send that was actually about to succeed a few seconds
#: later (confirmed live: message state was "completed" within 11s of a
#: send that reported unconfirmed at the 2s mark). Derived directly from
#: MERGE_WINDOW_SECONDS, not a second independent constant, so the two can
#: never drift apart again the way they did here -- +3.0s covers M0-A's
#: calibrated worst-case notify-pane time (~1.0s) plus margin.
INJECTED_TIMEOUT_SECONDS = MERGE_WINDOW_SECONDS + 3.0
#: §3.5 rule 4.
NEEDS_ATTENTION_THRESHOLD_SECONDS = 60.0
#: States that mean "the daemon did inject this" for _wait_for_state's
#: purposes -- see its 2026-08-02 docstring note. "injected" itself plus every
#: state SIGNAL_STATE_TRANSITIONS allows it to reach.
_INJECTED_OR_LATER_STATES = frozenset({"injected", "seen", "accepted_for_work", "completed"})

_MAX_LINE_BYTES = 64 * 1024


@dataclass(frozen=True)
class SendResult:
    message_id: str
    # "injected" | "daemon_unreachable" | "injection_failed_transient" | "deduplicated_inflight"
    #
    # 2026-08-01 fix (PPLX-reviewed): a single "injection_unconfirmed" status
    # used to mean two entirely different things a caller needs to react to
    # differently -- a plain transient miss (worth retrying) vs. this send
    # being silently dropped because another in-flight send for the same
    # (to_project, inbox_ref) already owns it (retrying immediately won't
    # help; the caller should wait for the existing one or check its state).
    # Kept the same underlying `injection_unconfirmed` escalation reason in
    # the ACK state store (see mark_escalated calls below) -- this split is
    # about what the caller sees, not a new state-machine state.
    status: str


async def _try_once(envelope: Envelope, socket_path: Path, *, timeout: float) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)), timeout=timeout
        )
    except (OSError, TimeoutError):
        return False
    try:
        writer.write((envelope.to_json() + "\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not raw or len(raw) > _MAX_LINE_BYTES:
            return False
        reply = json.loads(raw.decode())
        return bool(reply.get("status") == "accepted" and reply.get("message_id") == envelope.message_id)
    except (OSError, TimeoutError, json.JSONDecodeError):
        return False
    finally:
        writer.close()


async def _send_with_retries(envelope: Envelope, socket_path: Path) -> bool:
    """§3.5 rule 1: initial attempt + up to 2 retries with increasing delay."""
    if await _try_once(envelope, socket_path, timeout=ACCEPT_TIMEOUT_SECONDS):
        return True
    for delay in ACCEPT_RETRY_DELAYS_SECONDS:
        await asyncio.sleep(delay)
        if await _try_once(envelope, socket_path, timeout=ACCEPT_TIMEOUT_SECONDS):
            return True
    return False


async def _wait_for_state(
    to_project: str, message_id: str, target_states: frozenset[str], *, timeout: float, poll_interval: float = 0.1
) -> bool:
    """Polls until the recorded state is any of `target_states`.

    2026-08-02 field incident: daemon.py calls mark_injected() then
    mark_completed() back-to-back with no delay between them, so a poll can
    easily land between two checks and never observe the single instant the
    state equals "injected" -- it jumps straight to "completed" instead. A
    caller waiting on the exact string "injected" would then wrongly conclude
    the send failed and escalate it, even though it plainly succeeded. Taking
    a set of "this-state-or-any-known-downstream-state" values makes this
    poll accept whichever one it happens to observe.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = get_signal_state(to_project, message_id)
        if row and row["state"] in target_states:
            return True
        await asyncio.sleep(poll_interval)
    row = get_signal_state(to_project, message_id)
    return bool(row and row["state"] in target_states)


async def send(
    from_project: str, to_project: str, inbox_ref: str, kind: str,
    sender_id: str, shared_secret: str, socket_path: Path,
) -> SendResult:
    """§3.2 step 2 onward + §3.5 rules 1-3. Assumes the caller already did
    §3.4's Primary-layer write (`store_memory()`) — this function only ever
    sends the wake control signal, never the content itself (§3.3)."""
    envelope = Envelope(
        from_project=from_project, to_project=to_project, inbox_ref=inbox_ref,
        kind=kind, sender_id=sender_id,
    ).signed(shared_secret)

    accepted = await _send_with_retries(envelope, socket_path)
    if not accepted:
        mark_escalated(
            to_project, envelope.message_id, "daemon_unreachable",
            from_project=from_project, to_project=to_project, inbox_ref=inbox_ref,
        )
        return SendResult(envelope.message_id, "daemon_unreachable")

    mark_accepted(
        to_project, envelope.message_id,
        from_project=from_project, to_project=to_project, inbox_ref=inbox_ref,
    )

    injected = await _wait_for_state(
        to_project, envelope.message_id, _INJECTED_OR_LATER_STATES, timeout=INJECTED_TIMEOUT_SECONDS
    )
    if not injected:
        mark_escalated(to_project, envelope.message_id, "injection_unconfirmed")
        # Distinguish "daemon just didn't get to it in time" (retry-worthy)
        # from "daemon deliberately dropped it as a duplicate of another
        # in-flight send for the same target" (retrying immediately won't
        # help -- see this dataclass field's docstring).
        active = find_active_signal_by_target(
            to_project, to_project, inbox_ref, exclude_message_id=envelope.message_id
        )
        if active is not None:
            return SendResult(envelope.message_id, "deduplicated_inflight")
        return SendResult(envelope.message_id, "injection_failed_transient")

    return SendResult(envelope.message_id, "injected")


def check_needs_attention(to_project: str, message_id: str) -> str | None:
    """§3.5 rule 4, implemented as a lazy pull-check rather than a resident
    timer (see module docstring). Call this from `signal status`/`doctor`/a
    periodic loop. Returns "needs_attention" if it just escalated, else None
    (not yet due, already Seen, or in a state this rule doesn't apply to).
    """
    row = get_signal_state(to_project, message_id)
    if not row or row["state"] != "injected" or row["injected_at"] is None:
        return None
    if time.time() - row["injected_at"] < NEEDS_ATTENTION_THRESHOLD_SECONDS:
        return None
    mark_escalated(to_project, message_id, "needs_attention")
    return "needs_attention"
