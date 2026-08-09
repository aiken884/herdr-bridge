# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Layer 0 oracle: purely mechanical screen-diff verification (no LLM reasoning involved).

Corresponds to `tests/dogfooding/RUNBOOK.md` Section 4's "layered oracle
strategy" Layer 0 — it only verifies whether the TUI input mechanism
actually received and submitted a message (has the marker disappeared from
the input box, has it shown up in the conversation history region), and
makes no semantic judgment about the agent's reasoning output. This is the
primary oracle for the P0/T1 series TUI injection tests.

The judgment logic shares its origin with `src/herdr_bridge/light/cli.py`'s
existing `_looks_submitted()` (the production path for the fourth-layer
notify-pane communication). The five TUIs' pattern table
(`PROMPT_PATTERNS`) and the locator logic itself have already been factored
out into `herdr_bridge.light.tui_patterns` (#65: the production path and the
harness side used to have mismatched detection capability, which was a
structural problem — now both sides import from the same shared source
instead of maintaining it separately). This file keeps only the
harness-specific bits: `verify_submitted()`'s structured return value,
`capture()`'s pane-read wrapper, and `read_until()`'s polling wait (#67).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from herdr_bridge.light.tui_patterns import (
    PROMPT_PATTERNS,
    TuiPromptPattern,
)
from herdr_bridge.light.tui_patterns import (
    has_new_reply_marker_line as _has_new_reply_marker_line,
)
from herdr_bridge.light.tui_patterns import (
    locate_input_region as _locate_input_region,
)
from herdr_bridge.light.tui_patterns import (
    marker_head as _marker_head,
)

__all__ = [
    "PROMPT_PATTERNS",
    "TuiPromptPattern",
    "VerifyResult",
    "capture",
    "read_until",
    "verify_submitted",
]

_READ_TIMEOUT_SECONDS = 10


@dataclass
class VerifyResult:
    """The structured return value of `verify_submitted()`."""

    submitted: bool
    still_in_input_box: bool
    in_history: bool
    raw_before: str
    raw_after: str
    marker: str
    tui: str | None = None
    ambiguous: bool = False
    detail: str = ""


def capture(pane_id: str, *, source: str = "recent-unwrapped", lines: int = 60) -> str:
    """Wraps `herdr pane read` to get a plain-text snapshot of a pane's screen.

    Defaults to `recent-unwrapped` (rather than `recent`) — `recent` wraps
    lines based on screen width, and on a narrow pane (e.g. a 41-42 column
    split view) that easily breaks a marker's fingerprint across two lines,
    so no single-line `in` comparison can find it, producing a false
    "not submitted" verdict (#48). `recent-unwrapped` gets the unwrapped
    version directly, sidestepping the problem entirely. Callers can still
    explicitly override this back to `"recent"`/`"visible"`/another source.

    A failed read (nonzero exit, timeout, any exception) always returns an
    empty string, leaving it to the caller to treat that as "undeterminable"
    — this never raises and interrupts the test flow (same conservative
    design as cli.py's `_pane_read()`).
    """
    try:
        res = subprocess.run(
            [
                "herdr", "pane", "read", pane_id,
                "--source", source,
                "--lines", str(lines),
                "--format", "text",
            ],
            capture_output=True,
            text=True,
            timeout=_READ_TIMEOUT_SECONDS,
            check=False,
        )
        if res.returncode != 0:
            return ""
        return res.stdout
    except Exception:  # noqa: BLE001
        return ""


def read_until(
    pane_id: str,
    predicate: Callable[[str], bool],
    *,
    source: str = "visible",
    timeout: float = 30.0,
    interval: float = 2.0,
    lines: int = 60,
) -> tuple[bool, str]:
    """Poll `pane_id`'s screen until `predicate(snapshot)` holds or it times
    out, and return `(success?, last snapshot read)` (#67).

    Motivation: verification scripts the tower wrote in a single day produced
    four false failures, and at least two of those traced back directly to
    the problem this function solves:

    1. **Reading the screen too early** — right after `herdr pane
       send-text`, the screen needs time to render. A single `capture()`
       call can catch a screen that hasn't finished updating yet, and
       misread "hasn't finished rendering" as "the message wasn't
       delivered/processed" (H0-06's false-failure case: it read the screen
       only 1 second after sending, when the content was in fact fully
       visible).
    2. **A one-shot read can't tell "still waiting" from "genuinely never
       happened"** — only polling with a reasonable timeout can separate
       these two cases, instead of gambling on the timing of a single read.

    `read_until()` itself doesn't own the semantics of `predicate` — its job
    is only "repeatedly read + check + enforce a timeout". It's up to the
    caller to decide whether they're waiting for content to "appear" or to
    "disappear" (for the latter, always confirm the content appeared at some
    point first — otherwise you can't tell "it disappeared" from "it never
    showed up in the first place". This is a rule spelled out in Runbook
    Section 4; this function won't make that call for you).

    Args:
        pane_id: the target pane.
        predicate: takes a screen snapshot string, returns whether the
            expected condition is already met.
        source: passed through to `capture()`'s `--source`. Defaults to
            `"visible"` (the live screen most commonly used for manual
            review); opencode's conversation history can scroll out of the
            visible region, so `"recent"` is recommended when reviewing
            opencode (see Runbook Section 4).
        timeout: timeout in seconds, defaults to 30.
        interval: seconds between polls, defaults to 2.
        lines: passed through to `capture()`'s `--lines`.

    Returns:
        `(True, snapshot)`: the snapshot at the moment `predicate` became
        true, before the timeout.
        `(False, snapshot)`: the last snapshot read as of the timeout
        (regardless of whether `predicate` was ever true and then stopped
        being true — this function only tracks whether `predicate` holds at
        the moment of its most recent call, it does not track history).
    """
    deadline = time.time() + timeout
    snapshot = ""
    while True:
        snapshot = capture(pane_id, source=source, lines=lines)
        if predicate(snapshot):
            return True, snapshot
        if time.time() >= deadline:
            return False, snapshot
        time.sleep(interval)


_DEFAULT_HEAD_LEN = 40


def verify_submitted(
    before: str,
    after: str,
    marker: str,
    *,
    tui: str | None = None,
    expect_empty: bool = False,
    head_len: int = _DEFAULT_HEAD_LEN,
) -> VerifyResult:
    """Determine whether `marker` was really submitted from the input box
    into the conversation history region.

    Args:
        before: the screen snapshot before injection.
        after: the screen snapshot after injection (including any fallback
            attempt to send Enter, etc.).
        marker: the message content that was sent (or an identifiable
            marker string within it, e.g. `HERDR_TEST_<uuid>`).
        tui: a known TUI name (see the keys of `PROMPT_PATTERNS`). If
            omitted, tries every known pattern in order and uses the first
            one that can locate an input box in `after`.
        expect_empty: set to True when the caller explicitly declares "this
            is deliberately testing an empty message" (Runbook Section 3
            axis 3, "only send `\\n`") — only then is a blank fingerprint
            treated as necessarily submitted. Defaults to False — a marker
            that unexpectedly ends up empty (e.g. a special-character test's
            marker happens to consist entirely of whitespace/control
            characters, or starts with a newline) does not by itself mean it
            was submitted; see #47.
        head_len: fingerprint length, defaults to 40. Can be shortened on a
            narrow pane to dodge the line-wrap point (#48).

    Returns:
        A structured `VerifyResult`. When no known marker pattern can be
        found at all (e.g. for a TUI whose pattern hasn't been confirmed
        yet), or the marker's fingerprint unexpectedly ends up empty without
        `expect_empty` being declared, `ambiguous=True`, and it falls back
        to a conservative whole-screen diff verdict — better to falsely call
        it "not submitted" than to falsely call it "submitted" and miss a
        message that's genuinely stuck.
    """
    head = _marker_head(marker, head_len=head_len)
    after_lines = after.splitlines()

    if tui is not None:
        pattern = PROMPT_PATTERNS.get(tui)
        candidates = [pattern] if pattern is not None else []
    else:
        candidates = list(PROMPT_PATTERNS.values())

    located: tuple[set[int], str] | None = None
    matched_tui = tui
    for pattern in candidates:
        found = _locate_input_region(after_lines, pattern)
        if found is not None:
            located = found
            matched_tui = pattern.tui
            break

    if not head:
        if expect_empty:
            # The caller has explicitly declared that this is deliberately
            # testing an empty message (Runbook Section 3 axis 3): there's
            # no content to track, so treat it as necessarily "submitted".
            return VerifyResult(
                submitted=True,
                still_in_input_box=False,
                in_history=False,
                raw_before=before,
                raw_after=after,
                marker=marker,
                tui=matched_tui,
            )
        # No declaration that an empty message was expected, but the
        # fingerprint unexpectedly came out empty — can't unconditionally
        # call this a success (#47); fall back to a conservative verdict and
        # leave it for manual/downstream analysis rather than pretending it
        # was submitted.
        return VerifyResult(
            submitted=False,
            still_in_input_box=False,
            in_history=False,
            raw_before=before,
            raw_after=after,
            marker=marker,
            tui=matched_tui,
            ambiguous=True,
            detail=(
                "The marker's fingerprint unexpectedly came out empty (it "
                "may be entirely whitespace/control characters after strip), "
                "and the caller did not declare expect_empty=True — not "
                "treated as submitted, to avoid masking a message that's "
                "genuinely stuck"
            ),
        )

    if located is not None:
        box_indices, input_text = located
        still_in_input_box = head in input_text
        in_history = any(head in line for i, line in enumerate(after_lines) if i not in box_indices)
        detail = ""

        if not in_history and matched_tui is not None:
            matched_pattern = PROMPT_PATTERNS.get(matched_tui)
            if matched_pattern is not None and matched_pattern.reply_markers:
                # A direct substring match couldn't find it, but this TUI is
                # known to truncate its history display (#59): fall back to
                # checking whether `after` has a newly-appeared reply block
                # as an alternative signal.
                before_lines = before.splitlines()
                if _has_new_reply_marker_line(
                    before_lines, after_lines, matched_pattern.reply_markers, box_indices
                ):
                    in_history = True
                    detail = (
                        "in_history was determined via the reply_markers "
                        "fallback signal (the marker was likely truncated by "
                        "the TUI, a direct substring match couldn't find it, "
                        "so a newly-appeared reply block was detected "
                        "instead, see #59)"
                    )

        return VerifyResult(
            submitted=in_history and not still_in_input_box,
            still_in_input_box=still_in_input_box,
            in_history=in_history,
            raw_before=before,
            raw_after=after,
            marker=marker,
            tui=matched_tui,
            detail=detail,
        )

    # No known marker pattern found at all: fall back to a conservative
    # whole-screen diff verdict.
    changed = after != before
    tail = "\n".join(after_lines[-3:])
    still_in_tail = head in tail
    return VerifyResult(
        submitted=changed and not still_in_tail,
        still_in_input_box=still_in_tail,
        in_history=changed,
        raw_before=before,
        raw_after=after,
        marker=marker,
        tui=matched_tui,
        ambiguous=True,
        detail="No known TUI prompt pattern found, falling back to a conservative whole-screen diff verdict",
    )
