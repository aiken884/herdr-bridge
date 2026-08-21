# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Structural checks for scripts/sandbox-up.sh (no live Claude, no default session)."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = (REPO / "scripts" / "sandbox-up.sh").read_text(encoding="utf-8")


def test_sandbox_up_treats_trust_screen_start_errors_as_already_up():
    assert "agent_not_ready" in SCRIPT
    assert "agent_name_taken" in SCRIPT
    assert "set -euo pipefail" in SCRIPT
    # Unknown start errors must still be able to fail the script.
    assert "return \"$rc\"" in SCRIPT or "return $rc" in SCRIPT
    assert "exit 1" in SCRIPT


def test_sandbox_up_does_not_abort_bare_agent_start_on_nonzero():
    """The old `herdr agent start ... || { retry }` with set -e aborted when
    both attempts returned agent_not_ready / agent_name_taken."""
    # Must capture start output rather than sending it to /dev/null-only || retry.
    assert "herdr agent start test-claude" in SCRIPT
    assert "_start_agent" in SCRIPT


def test_gating_docs_do_not_claim_restore_read_fix_is_unshipped():
    """Verification plan step 4: README and capability-notes must not still
    claim the #2065/#2088 restore-read fix is unshipped."""
    stale = (
        "修復尚未隨任何已發布版本出貨",
        "目前沒有任何已發布版本（含 preview）包含此修復",
        "the fix has not shipped yet",
    )
    for rel in ("README.md", "docs/capability-notes.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for phrase in stale:
            assert phrase not in text, f"{rel} still claims restore-read is unshipped: {phrase!r}"
