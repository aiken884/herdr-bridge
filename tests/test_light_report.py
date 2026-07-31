# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

from herdr_bridge.light.report import (
    AcceptanceReport,
    build_failure_report,
    build_success_report,
    format_user_report,
)


def test_format_success_report_user_language():
    r = build_success_report(
        "Create thumbnail",
        markers_found=("DONE-THUMBNAIL",),
        hints=("Please confirm the file",),
    )
    text = format_user_report(r)
    assert "Completed" in text
    assert "DONE-THUMBNAIL" in text
    assert "Please confirm the file" in text
    # should not leak technical jargon
    assert "actor_id" not in text
    assert "fleet" not in text.lower()


def test_format_no_agent_has_actionable_steps():
    r = build_failure_report(
        "Create thumbnail",
        reason="no_agent",
        message="No assistant",
    )
    text = format_user_report(r)
    assert "assistant" in text.lower()
    assert "Suggested next steps" in text
    assert r.status == "no_agent"


def test_format_timeout():
    r = build_failure_report(
        "Create thumbnail",
        reason="timeout",
        message="Timed out",
        technical_reason="timeout",
    )
    text = format_user_report(r)
    assert "Timed out" in text
    # technical_reason should not appear directly in the user-facing text
    assert "technical_reason" not in text


def test_format_blocked_has_actionable_steps():
    r = build_failure_report(
        "Create thumbnail",
        reason="blocked",
        message="Stuck",
    )
    text = format_user_report(r)
    assert "waiting for your confirmation" in text
    assert "Enter" in text
    assert r.status == "blocked"
    assert r.title == "Waiting: Create thumbnail"


def test_acceptance_report_frozen():
    r = AcceptanceReport(
        status="success",
        title="t",
        summary="s",
        details=(),
        next_steps=(),
    )
    assert r.status == "success"
