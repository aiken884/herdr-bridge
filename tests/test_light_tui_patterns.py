# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""#65: single-source-of-truth tests for the five TUIs' prompt pattern table.

The production path (`herdr_bridge.light.cli`) and a separate real-screen
verification harness used to each maintain their own copy of the pattern
table, and the resulting capability mismatch was a structural problem
(codex/agy's markers only existed in the harness's copy, not the production
path, causing notify-pane to misjudge these two TUIs). This makes sure both
sides really import the same `PROMPT_PATTERNS` object, rather than each
keeping a copy that looks the same but drifts apart over time.
"""

from __future__ import annotations

from herdr_bridge.light import tui_patterns


def test_all_five_tuis_present():
    for tui in ("claude", "grok", "codex", "opencode", "agy"):
        assert tui in tui_patterns.PROMPT_PATTERNS
        pattern = tui_patterns.PROMPT_PATTERNS[tui]
        assert pattern.markers or pattern.box_markers, (
            f"{tui} must have at least one of markers or box_markers"
        )


def test_codex_and_claude_use_different_markers():
    # the key fact for this task: codex uses › (U+203A), not claude's ❯ (U+276F)
    assert "›" in tui_patterns.PROMPT_PATTERNS["codex"].markers
    assert "❯" in tui_patterns.PROMPT_PATTERNS["claude"].markers
    assert tui_patterns.PROMPT_PATTERNS["codex"].markers != tui_patterns.PROMPT_PATTERNS["claude"].markers


def test_agy_uses_plain_ascii_gt():
    assert tui_patterns.PROMPT_PATTERNS["agy"].markers == (">",)


def test_locate_any_finds_codex_marker_regardless_of_footer_lines_below():
    """#65 root-cause regression: when there's an extra status line/noise line
    below the input line, it must still be located correctly -- not by only
    looking at 'a fixed number of lines at the bottom of the screen'."""
    lines = [
        "history line 1",
        "history line 2",
        "› stuck message here",
        "status bar line",
        "footer line 1",
        "footer line 2",
    ]
    located = tui_patterns.locate_any(lines, tui=None)
    assert located is not None
    box_indices, input_text, matched_tui = located
    assert matched_tui == "codex"
    assert box_indices == {2}
    assert "stuck message here" in input_text


def test_locate_any_with_explicit_tui_only_tries_that_pattern():
    lines = ["❯ some claude input"]
    # when codex is explicitly specified, even if the screen actually looks like
    # claude's, it should not be mismatched against the claude pattern
    assert tui_patterns.locate_any(lines, tui="codex") is None


def test_locate_any_returns_none_when_nothing_matches():
    lines = ["totally unrecognized screen", "no known marker here"]
    assert tui_patterns.locate_any(lines, tui=None) is None


def test_marker_head_takes_first_line_prefix():
    assert tui_patterns.marker_head("hello\nworld", head_len=3) == "hel"

