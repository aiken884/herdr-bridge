# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""User-language acceptance reports (hides technical detail)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReportStatus = Literal["success", "partial", "failed", "blocked", "timeout", "no_agent"]


@dataclass(frozen=True)
class AcceptanceReport:
    """Acceptance result shown to an occasional user."""

    status: ReportStatus
    title: str
    summary: str
    details: tuple[str, ...]
    next_steps: tuple[str, ...]
    technical_reason: str | None = None


def format_user_report(report: AcceptanceReport) -> str:
    """Format an acceptance report into user-readable text."""
    icon = {
        "success": "✅",
        "partial": "⚠️",
        "failed": "❌",
        "blocked": "⏸️",
        "timeout": "⏱️",
        "no_agent": "🔌",
    }.get(report.status, "•")

    lines = [
        f"{icon} {report.title}",
        "",
        report.summary,
    ]
    if report.details:
        lines.append("")
        lines.append("Details:")
        for d in report.details:
            lines.append(f"  • {d}")
    if report.next_steps:
        lines.append("")
        lines.append("Suggested next steps:")
        for s in report.next_steps:
            lines.append(f"  → {s}")
    return "\n".join(lines)


def build_success_report(
    task_title: str,
    *,
    markers_found: tuple[str, ...],
    hints: tuple[str, ...],
) -> AcceptanceReport:
    details = []
    if markers_found:
        details.append(f"Completion signal: {', '.join(markers_found)}")
    details.append("The command tower has finished dispatching and completed a preliminary check")
    return AcceptanceReport(
        status="success",
        title=f"Completed: {task_title}",
        summary="Task complete. The command tower handed the work off to the assistant and confirmed the initial result.",
        details=tuple(details),
        next_steps=hints,
    )


def build_failure_report(
    task_title: str,
    *,
    reason: ReportStatus,
    message: str,
    hints: tuple[str, ...] = (),
    technical_reason: str | None = None,
) -> AcceptanceReport:
    titles = {
        "timeout": f"Timed out: {task_title}",
        "blocked": f"Waiting: {task_title}",
        "no_agent": "No assistant available",
        "failed": f"Not completed: {task_title}",
        "partial": f"Partially completed: {task_title}",
    }
    next_steps = list(hints) if hints else []
    if reason == "no_agent":
        next_steps = [
            "Please confirm Herdr is running and at least one AI assistant is active",
            "You can run: bash scripts/commander-start.sh",
            *next_steps,
        ]
    elif reason == "timeout":
        next_steps = [
            "You can try again, or make your request more specific",
            "If the assistant seems stuck, check the Herdr window to see whether it's waiting for your confirmation",
            *next_steps,
        ]
    elif reason == "blocked":
        next_steps = [
            "The assistant may be waiting for your confirmation (e.g. approving a trust-folder prompt)",
            "Go to the Herdr window, press Enter or follow the on-screen instructions, then try again",
            *next_steps,
        ]
    return AcceptanceReport(
        status=reason,
        title=titles.get(reason, f"Not completed: {task_title}"),
        summary=message,
        details=(),
        next_steps=tuple(next_steps),
        technical_reason=technical_reason,
    )
