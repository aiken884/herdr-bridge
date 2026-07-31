# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Input-box prompt pattern table for the five interactive TUIs (Claude Code / Grok /
Codex / OpenCode / Antigravity(agy)) — the single source of truth.

Blood-lesson from #65: `herdr_bridge.light.cli`'s notify-pane (the production path)
used to hard-code the prompt marker as a single `❯` (Claude Code only), which doesn't
apply at all to other TUIs like codex (›) or agy (>) — causing messages that were
still stuck in the input box to be misjudged as "submitted". `tests/dogfooding/harness/
screen_oracle.py` separately maintained its own `PROMPT_PATTERNS`, already verified
against real screens for all five TUIs, but the fact that the production path and the
test harness had unequal detection capability was itself a structural problem — this
module hoists the pattern table itself up under `src/`, so both sides (`cli.py`'s
production path and `screen_oracle.py`'s harness oracle) import the same object from
here instead of each maintaining their own copy.

Deliberately placed under `src/herdr_bridge/light/` (not `tests/`): `src/` must not
depend on `tests/`, but `tests/` depending on `src/` is the normal direction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DEFAULT_HEAD_LEN = 40


@dataclass(frozen=True)
class TuiPromptPattern:
    """Input-box prompt configuration for a single TUI.

    Two mutually exclusive (but co-existable, with `box_markers` taking priority)
    ways to locate the input box:
    - `markers`: a single prompt character (like ❯/›/>) — the input box is simply the
      last line containing that character.
    - `box_markers`: `(side border character, bottom border character)`, for TUIs like
      OpenCode that have no single prompt character and instead wrap the input region
      line by line with border characters (#53).
    """

    tui: str
    markers: tuple[str, ...]
    confirmed: bool
    notes: str = ""
    box_markers: tuple[str, str] | None = None
    reply_markers: tuple[str, ...] = ()
    """Fallback signal for when the history area shows a "truncated" send (#59): some
    TUIs (e.g. codex) actually truncate the content when displaying an already-sent
    message in the history area (this isn't line-wrapping — re-wrapping can't fix it),
    so a plain substring match against the full marker fails. In that case, we instead
    detect "a new line appears in `after` that starts with one of these strings (and
    wasn't in `before`)" as an alternative signal for `in_history` — a new reply block
    appearing means the downstream really did process it. An empty tuple means this
    TUI doesn't need this fallback (either it's known to display messages in full, or
    it hasn't been verified yet).

    **Scoping note**: this is extra insurance, not the root fix — on 2026-07-25,
    re-verifying codex with a short hex marker (8 characters) showed no truncation at
    all; the message displayed in full. opencode's `reply_markers` is an empty tuple
    and its detection is still correct — a second, independent piece of evidence. The
    real fix is the Runbook's recommendation to use a short, unique, pure-hex marker
    convention; this field is only a backstop for the rare case of truncation — don't
    mistake it for the primary mechanism."""


# Prompt patterns for all five TUIs — designed per the Runbook's requirement as a
# configurable table, rather than hard-coding a single pattern.
#
# confirmed=True: as of 2026-07-25, verified against a real pane via a read-only
# `herdr pane read`.
# confirmed=False: still pending verification; the marker is just carried over from an
# existing assumption or a reasonable guess — must be re-verified before running the T1
# series.
PROMPT_PATTERNS: dict[str, TuiPromptPattern] = {
    "claude": TuiPromptPattern(
        tui="claude",
        markers=("❯",),
        confirmed=True,
        notes=(
            "Established result from the notify-pane production path (cli.py). "
            "Whether the history area truncates already-sent messages (the issue #59 "
            "found for codex) hasn't been systematically verified — don't assume it "
            "displays in full like grok/agy."
        ),
    ),
    "grok": TuiPromptPattern(
        tui="grok",
        markers=("❯",),
        confirmed=True,
        notes=(
            "Verified against wT:p1A on 2026-07-25 (read-only `herdr pane read` only): "
            "the input box is wrapped in a border, with the prompt character `❯` inside "
            "the box. The history area displays sent messages in full, without "
            "truncation (confirmed by T1-01, 2026-07-25)."
        ),
    ),
    "codex": TuiPromptPattern(
        tui="codex",
        markers=("›",),
        confirmed=True,
        reply_markers=("•",),
        notes=(
            "Verified against wT:p1B on 2026-07-25 (read-only `herdr pane read` only): "
            "the input box prompt character is `›` (U+203A, not the U+276F ❯ used by "
            "claude); when idle, the input box shows a grayed-out placeholder (e.g. "
            "\"Implement {feature}\", \"Summarize recent commits\" — these rotate, "
            "they aren't a fixed string) — detection logic must be careful not to "
            "mistake the placeholder for actual user input. There's also a status bar "
            "at the bottom (e.g. model/usage), so the input line isn't necessarily "
            "among the last few lines on screen — a conservative check that only looks "
            "at a `fixed number of lines at the end of the screen` will miss it. This "
            "is exactly the root cause of #65."
            "\n"
            "**#59 (verified 2026-07-25 in T1-01): codex really does truncate "
            "already-sent messages in the history area** (this is not the wrap/reflow "
            "issue from #48 — `--source recent-unwrapped` doesn't help here). Sending "
            "\"Please reply with only this line, nothing else: HERDR_T_co53306 ACK\" to "
            "wT:p1B in practice left only a fragment like \"254 ACK\" in the history "
            "area — nearly impossible to match against the original marker with a "
            "plain substring check, so looking for a full/prefix marker match fails "
            "and misjudges in_history=False (a false FAIL). Switched to "
            "`reply_markers=(\"•\",)` as an alternative signal: when the marker "
            "substring can't be found, instead detect whether a new line starting with "
            "\"•\" appears in `after` (codex's own reply prefix) — if a new reply block "
            "shows up, treat it as in_history=True. The Runbook's test-marker "
            "recommendation is to switch to a short, unique, pure-hex value (e.g. 8 "
            "characters), to avoid a post-truncation fragment being completely "
            "unrecognizable."
        ),
    ),
    "opencode": TuiPromptPattern(
        tui="opencode",
        markers=(),
        box_markers=("┃", "╹"),
        confirmed=True,
        notes=(
            "Read (read-only) against wY:p1 on 2026-07-25: the input box at the bottom "
            "of the screen is wrapped line by line with the border character `┃`, and "
            "closed off at the bottom with `╹▀▀▀…` — no single prompt character ❯ was "
            "observed, which is inconsistent with the old assumption that "
            "'the common feature observed [across TUIs] is always ❯' (#53). Switched to "
            "a dedicated border-based detector: scan from the bottom up for the closing "
            "line (starting with `╹`), then collect the consecutive lines above it that "
            "start with `┃` as the input-box content, without relying on a single-"
            "character marker. When no closing line can be found (e.g. the screen "
            "hasn't finished rendering), it still falls back to the conservative "
            "whole-screen diff check, and won't misjudge as already-submitted. Whether "
            "the history area truncates already-sent messages (the issue #59 found for "
            "codex) hasn't been systematically verified — don't assume it displays in "
            "full like grok/agy."
        ),
    ),
    "agy": TuiPromptPattern(
        tui="agy",
        markers=(">",),
        confirmed=True,
        notes=(
            "Verified against wT:p1C on 2026-07-25 (after Aiken completed OAuth login; "
            "read-only `herdr pane read` only): Antigravity CLI 1.1.7, Gemini 3.5 Flash "
            "(Medium). The prompt is a plain ASCII `>` (different from claude's ❯, "
            "codex's ›, and not a unicode character either); the input box has a "
            "horizontal divider line above and below, and there's another status-bar "
            "line at the very bottom (time/directory/model/cost) — same as codex, the "
            "input line isn't necessarily among the last few lines on screen. Note: `>` "
            "is a very common ASCII character, so compared to the unicode markers of "
            "the other four TUIs it's much more prone to false-positive substring "
            "matches in prior output (e.g. code blocks, shell prompt characters); "
            "scanning from the bottom up for the first matching line greatly reduces "
            "this risk, but if the last few lines of history happen to also contain "
            "`>`, there's still some risk of misjudgment."
        ),
    ),
    "copilot": TuiPromptPattern(
        tui="copilot",
        markers=("❯",),
        confirmed=True,
        notes=(
            "Verified against wT:p1T on 2026-07-25 (a real notify-pane delivery test, "
            "not just a read-only observation): GitHub Copilot CLI 1.0.71. The screen "
            "layout is almost identical to claude's — the prompt is likewise `❯` "
            "(U+276F), and the input box is wrapped with a matching pair of `─` border "
            "lines, fully compatible with the border-based detection in "
            "`_extract_input_box_text` that it shares with claude — no extra "
            "box_markers needed. There's an additional status-bar line at the bottom "
            "(directory/git branch/session usage), so the input line isn't necessarily "
            "among the last few lines on screen, same as codex/agy. Verified end-to-end "
            "with a real notify-pane call (--tui claude hits it directly): sent a "
            "message with a marker and independently confirmed with --source visible "
            "that the message actually landed in the history area and the input box "
            "was cleared — this isn't just a read-only observation, it's an end-to-end "
            "verification including actual delivery confirmation."
        ),
    ),
    "gemini": TuiPromptPattern(
        tui="gemini",
        markers=(">",),
        confirmed=True,
        notes=(
            "Verified against wT:p1V on 2026-07-25 (a real notify-pane delivery test): "
            "Gemini CLI v0.52.0. The prompt is a plain ASCII `>` (same as agy, with the "
            "same risk — more prone to false-positive substring matches in prior output "
            "than the other TUIs' unicode markers). The input-box border characters "
            "differ from every other TUI: the top line uses `▄` (U+2584) and the bottom "
            "line uses `▀` (U+2580), not the common ─/━ — `_BORDER_LINE_RE` has been "
            "extended to cover both characters, so Claude's `_extract_input_box_text` "
            "border-based detection works directly on Gemini too, without a separate "
            "code path. When idle, the input box shows a grayed-out placeholder "
            "(\"Type your message or @path/to/file\"), similar to codex's placeholder "
            "situation — submission detection must be careful not to mistake this "
            "placeholder text for leftover user input. There's also a workspace/branch/"
            "model status bar at the bottom, so the input line isn't necessarily among "
            "the last few lines on screen. Verified end-to-end with a real notify-pane "
            "call sending a message with a marker, independently confirmed with "
            "--source visible: the message actually landed in the history area and the "
            "input box was cleared."
        ),
    ),
}


def find_input_line_index(
    lines: list[str], markers: tuple[str, ...]
) -> tuple[int, str, str] | None:
    """Scan from the bottom up for the last line containing any of the markers (the
    input box is usually at the very bottom of the screen).

    Returns `(line_index, matched_marker, content after the marker)`; returns None if
    not found.
    """
    for idx in range(len(lines) - 1, -1, -1):
        for marker in markers:
            pos = lines[idx].find(marker)
            if pos != -1:
                return idx, marker, lines[idx][pos + len(marker):]
    return None


def find_box_input_region(
    lines: list[str], side_marker: str, bottom_marker: str
) -> tuple[set[int], str] | None:
    """Scan from the bottom up for a border-style input box (e.g. OpenCode's
    `┃ ... ╹▀▀▀…`, #53).

    First find the last line starting with `bottom_marker` (the closing line), then
    collect the consecutive lines above it starting with `side_marker` as the box
    content. Returns `(set of indices of the box-content lines, merged box-content
    string)`; returns None if no closing line is found, or if there are no border lines
    above the closing line.
    """
    bottom_idx: int | None = None
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip().startswith(bottom_marker):
            bottom_idx = idx
            break
    if bottom_idx is None:
        return None

    box_indices: set[int] = set()
    box_lines: list[str] = []
    idx = bottom_idx - 1
    while idx >= 0:
        stripped = lines[idx].strip()
        if not stripped.startswith(side_marker):
            break
        content = lines[idx][lines[idx].find(side_marker) + len(side_marker):]
        box_lines.append(content)
        box_indices.add(idx)
        idx -= 1

    if not box_indices:
        return None

    box_lines.reverse()
    return box_indices, "\n".join(box_lines)


def locate_input_region(
    after_lines: list[str], pattern: TuiPromptPattern
) -> tuple[set[int], str] | None:
    """Unified interface: whether it's a single-character marker or a border-style
    input box, always returns `(set of indices of the input-box lines, input-box
    content)`; returns None if not found.
    """
    if pattern.box_markers is not None:
        side, bottom = pattern.box_markers
        return find_box_input_region(after_lines, side, bottom)
    found = find_input_line_index(after_lines, pattern.markers)
    if found is None:
        return None
    idx, _matched_marker, input_text = found
    return {idx}, input_text


def has_new_reply_marker_line(
    before_lines: list[str],
    after_lines: list[str],
    reply_markers: tuple[str, ...],
    exclude_indices: set[int],
) -> bool:
    """#59: some TUIs (e.g. codex) actually truncate content when displaying an
    already-sent message in the history area, so a plain substring match against the
    marker finds nothing. Here we instead detect "a line newly appearing in `after`
    that starts with any of `reply_markers`, and that line wasn't in `before`" as an
    alternative signal — a new reply block appearing means the downstream really did
    process this injection, rather than an old reply that happened to already be on
    screen and is unrelated to this injection.

    Just extra insurance, not the root fix — see the `TuiPromptPattern.reply_markers`
    docstring.

    `exclude_indices` are the line indices occupied by the input box itself, and are
    excluded from the reply-block search range.
    """
    before_set = set(before_lines)
    for idx, line in enumerate(after_lines):
        if idx in exclude_indices:
            continue
        stripped = line.strip()
        if any(stripped.startswith(m) for m in reply_markers) and line not in before_set:
            return True
    return False


def locate_any(
    after_lines: list[str], *, tui: str | None = None
) -> tuple[set[int], str, str] | None:
    """Depending on `tui`, either try the single specified pattern, or (when
    `tui=None`) try every known pattern in `PROMPT_PATTERNS` in turn, returning the
    first result that can locate an input box in `after_lines`:
    `(set of indices of the input-box lines, input-box content, name of the matched
    tui)`. Returns None if none of them match, leaving the caller to treat this as
    "uncertain" — don't pretend the input box has been located.
    """
    if tui is not None:
        pattern = PROMPT_PATTERNS.get(tui)
        candidates = [pattern] if pattern is not None else []
    else:
        candidates = list(PROMPT_PATTERNS.values())

    for pattern in candidates:
        found = locate_input_region(after_lines, pattern)
        if found is not None:
            box_indices, input_text = found
            return box_indices, input_text, pattern.tui
    return None


def marker_head(marker: str, *, head_len: int = _DEFAULT_HEAD_LEN) -> str:
    """Take the first `head_len` characters of the marker's first line as a comparison
    fingerprint (default 40):

    A marker can be multi-line or very long; comparing it character-for-character in
    full is easily thrown off by line wrapping, so we take a stable prefix instead.
    """
    probe = marker.strip()
    if not probe:
        return probe
    return probe.splitlines()[0][:head_len]


def ready_regex(*, tui: str | None = None) -> str:
    """Build the regex for `herdr pane wait-output --regex` (a Rust regex) — matches
    whether any known TUI's input-box prompt/border character has already appeared on
    screen (`tui=None` merges the markers of every known TUI, since the caller usually
    doesn't know in advance which TUI it's targeting).

    Blood-lesson from #69 (F1-04): right after `herdr agent start`, `agent_status`
    immediately reports `idle`, but the TUI clearly hasn't finished initializing yet —
    a message injected at that point gets silently dropped during initialization. The
    screen showing a known prompt is currently the only cheap readiness signal
    available (it doesn't guarantee keyboard input can actually be received, but it at
    least rules out the confirmed-to-cause-message-loss case of "the TUI hasn't
    rendered any interactive UI at all yet") — we can't rely on `agent_status` alone.

    These marker characters (❯/›/>/┃/╹) aren't special characters in either the Rust
    regex engine (used by herdr) or Python's `re`, so `re.escape()`-escaped syntax is
    compatible across both.
    """
    if tui is not None:
        pattern = PROMPT_PATTERNS.get(tui)
        candidates = [pattern] if pattern is not None else []
    else:
        candidates = list(PROMPT_PATTERNS.values())

    seen: set[str] = set()
    ordered_markers: list[str] = []
    for pattern in candidates:
        for m in (*pattern.markers, *(pattern.box_markers or ())):
            if m not in seen:
                seen.add(m)
                ordered_markers.append(m)

    return "|".join(re.escape(m) for m in ordered_markers)
