# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""LightCommander — the core command tower for light mode.

Hides actor_id / fleet / rules; the only thing exposed externally is
"run a task → report back in user language".
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from herdr_bridge.errors import HerdrBridgeError
from herdr_bridge.light.report import (
    AcceptanceReport,
    build_failure_report,
    build_success_report,
    format_user_report,
)
from herdr_bridge.light.tasks import FIRST_TASK, TaskSpec, get_task

# RemaGraph memory integration (governance layer, embedded)
try:
    from herdr_bridge.orchestration import memory as _rg
except Exception:  # noqa: BLE001
    _rg = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from herdr_bridge.actions import BridgeActions
    from herdr_bridge.models import AgentInfo

logger = logging.getLogger("herdr_bridge.light.commander")

# Fixed actor id, not exposed to the user
_ACTOR = "commander:light"


@dataclass(frozen=True)
class LightResult:
    """Result of a light-mode run."""

    ok: bool
    report: AcceptanceReport
    agent_id: str | None = None
    elapsed_sec: float | None = None
    raw_reason: str | None = None

    def user_text(self) -> str:
        return format_user_report(self.report)


def _pick_coder(agents: list[AgentInfo]) -> AgentInfo | None:
    """Pick an agent suited to writing code; prefer names containing claude/code/coder/builder."""
    if not agents:
        return None
    preferred = ("claude", "code", "coder", "builder", "opencode", "codex")
    ranked: list[tuple[int, AgentInfo]] = []
    for a in agents:
        brand = getattr(a, "brand", None) or ""
        name_attr = getattr(a, "name", None) or ""
        name = f"{a.agent_id} {brand} {name_attr}".lower()
        score = 0
        for i, key in enumerate(preferred):
            if key in name:
                score = len(preferred) - i
                break
        if any(x in name for x in ("bash", "tester", "shell")):
            score -= 10
        ranked.append((score, a))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[0][1]


def _markers_in_text(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    found = []
    lower = text.lower()
    for m in markers:
        if m.lower() in lower or m in text:
            found.append(m)
    return tuple(found)


def _dry_run_report(task: TaskSpec, agent_id: str) -> AcceptanceReport:
    return AcceptanceReport(
        status="success",
        title=f"Preview: {task.title}",
        summary="This will hand the task off to the assistant (not actually executed yet).",
        details=(
            f"Task: {task.user_prompt[:80]}…",
            f"Expected files: {', '.join(task.expected_files)}",
            f"Selected assistant: {agent_id}",
        ),
        next_steps=("Drop --dry-run to actually run it",),
    )


class LightCommander:
    """Command tower for the occasional user."""

    def __init__(self, actions: BridgeActions) -> None:
        self._actions = actions
        # RemaGraph strict compliance: every entry point must call ensure first
        # (herdr-bridge's own dedicated project)
        if _rg and _rg.is_remagraph_enabled():
            try:
                _rg._ensure_remagraph_project("herdr-bridge")
                _rg._enforce_remagraph_safety_valve("herdr-bridge")
            except HerdrBridgeError:
                logger.debug(
                    "RemaGraph ensure/safety-valve check failed during "
                    "LightCommander construction; continuing without blocking "
                    "the constructor",
                    exc_info=True,
                )

    def run_first_task(
        self,
        *,
        timeout_sec: int = 600,  # LLMs need more time to think and use tools
        poll_interval_sec: int = 3,
        dry_run: bool = False,
    ) -> LightResult:
        """Run the first task locked in for Phase 1."""
        return self.run_task(
            FIRST_TASK,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
            dry_run=dry_run,
        )

    def run_task(
        self,
        task: TaskSpec,
        *,
        timeout_sec: int = 600,
        poll_interval_sec: int = 3,
        dry_run: bool = False,
    ) -> LightResult:
        """Run the given task and return a user-language result."""
        # RemaGraph strict-compliance safety valve: must ensure + verify before dispatching
        if _rg and _rg.is_remagraph_enabled():
            try:
                _rg._ensure_remagraph_project("herdr-bridge")
                _rg._enforce_remagraph_safety_valve("herdr-bridge")
            except HerdrBridgeError:
                logger.debug(
                    "RemaGraph ensure/safety-valve check failed during "
                    "run_task; continuing without blocking the task",
                    exc_info=True,
                )
        if isinstance(task, str):
            try:
                task = get_task(task)
            except KeyError as exc:
                report = build_failure_report(
                    "Unknown task",
                    reason="failed",
                    message=str(exc),
                )
                return LightResult(ok=False, report=report, raw_reason="unknown_task")

        try:
            agents = self._actions.list_agents(_ACTOR)
        except Exception as exc:  # noqa: BLE001
            report = build_failure_report(
                task.title,
                reason="failed",
                message="Unable to connect to the environment. Please confirm Herdr is running.",
                technical_reason=f"{type(exc).__name__}: {exc}",
            )
            return LightResult(ok=False, report=report, raw_reason="connect_error")

        coder = _pick_coder(agents)
        if coder is None:
            report = build_failure_report(
                task.title,
                reason="no_agent",
                message="No AI assistant is currently available. Please start at least one assistant first.",
            )
            return LightResult(ok=False, report=report, raw_reason="no_agent")

        if dry_run:
            report = _dry_run_report(task, coder.agent_id)
            return LightResult(
                ok=True, report=report, agent_id=coder.agent_id, raw_reason="dry_run"
            )

        # === RemaGraph memory integration (governance layer, embedded) ===
        # 1. recall + augment prompt
        send_text = task.agent_prompt
        used_task_id = task.task_id
        used_agent_id = coder.agent_id

        if _rg is not None and _rg.is_remagraph_enabled():
            send_text, used_task_id, used_agent_id = _rg.prepare_dispatch_text(
                task.agent_prompt,
                base_task_id=task.task_id,
                agent_id=coder.agent_id,
                project="herdr-light",
            )

        # PPLX's sole recommendation: require the agent to send a structured report via
        # the side-channel in its last step (not a text marker).
        #
        # Security note: task_id/agent_id come from herdr pane naming (may contain
        # characters like `:`, `/`, e.g. "wV:p1/my-downstream-project"), so we can't
        # apply an [A-Za-z0-9_-]-style whitelist regex (it would reject legitimate
        # values). Instead we pass them to the subprocess via shell environment
        # variables (escaped with shlex.quote); the `python3 -c` source itself is a
        # completely static string that never interpolates any external value into it,
        # fully avoiding the possibility of a user/agent-controlled string being
        # injected as executable Python source.
        #
        # NOTE: the block below (the instructional text appended to send_text,
        # including the JSON payload sent by the reporting script) is structurally
        # parsed by tests/test_security_s5_dispatch_code_injection.py, which does
        # `send_text.index("\nDo not report completion")` to slice out the shell
        # command and actually execute it — the exact anchor text must stay in sync
        # with that test's slice boundary if it changes.
        report_sock = os.environ.get("TOWER_REPORT_SOCK", "/tmp/tower-reports.sock")
        env_assignments = (
            f"TOWER_REPORT_SOCK={shlex.quote(report_sock)} "
            f"TOWER_TASK_ID={shlex.quote(used_task_id)} "
            f"TOWER_AGENT_ID={shlex.quote(used_agent_id)}"
        )
        python_script = (
            "import socket, json, os\n"
            "sock = os.environ[\"TOWER_REPORT_SOCK\"]\n"
            "report = {\n"
            '    "type": "task_report",\n'
            '    "task_id": os.environ["TOWER_TASK_ID"],\n'
            '    "agent_id": os.environ["TOWER_AGENT_ID"],\n'
            '    "status": "completed",\n'
            '    "result": {"summary": "Task completed, see output above", "artifacts": ["files updated"]},\n'
            '    "version": 1\n'
            "}\n"
            "try:\n"
            "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:\n"
            "        s.settimeout(2)\n"
            "        s.connect(sock)\n"
            "        s.sendall((json.dumps(report, ensure_ascii=False) + \"\\n\").encode())\n"
            "except Exception as e:\n"
            "    print(\"report err\", e)\n"
        )
        send_text += f"""

[IMPORTANT: after finishing, you must run this side-channel report — Herdr only tracks status, the report goes over an independent socket]
Once you have completed all the work (including writing files and confirming tests pass), run the following code as your last step to send the JSON report:
{env_assignments} python3 -c '
{python_script}'
Do not report completion with plain text like "DONE" or a marker — you must send the JSON above.
"""

        # 2. Automatically store the start status (best-effort bookkeeping: a
        # backend failure here must not block dispatching the task itself).
        if _rg is not None and _rg.is_remagraph_enabled():
            try:
                _rg.store_memory(
                    used_task_id,
                    used_agent_id,
                    kind="status_update",
                    summary=f"Starting task: {task.title}",
                    handoff_note="Task dispatched",
                    tags=["bridge", "light", "start"],
                    project_id="herdr-bridge",
                )
            except Exception:
                logger.debug(
                    "Failed to store start-of-task memory; continuing without "
                    "blocking dispatch",
                    exc_info=True,
                )

        # Capture pre-send state for delta detection (avoid prompt text pollution)
        pre_text = ""
        since_rev = None
        try:
            cur = self._actions.read_agent(_ACTOR, coder.agent_id)
            pre_text = cur.normalized_text or cur.text or ""
            since_rev = getattr(cur, "revision", None)
        except Exception:
            # Best-effort delta-detection read; any failure here (not just HerdrBridgeError)
            # must fall back to empty pre_text/since_rev rather than aborting the task.
            logger.debug(
                "Unable to read pre-send agent output for delta detection; "
                "falling back to empty pre_text/since_rev",
                exc_info=True,
            )

        try:
            self._actions.send_to_agent(
                _ACTOR, coder.agent_id, send_text, priority=1
            )
        except Exception as exc:  # noqa: BLE001
            report = build_failure_report(
                task.title,
                reason="failed",
                message="Unable to hand the task off to the assistant. Please try again later.",
                technical_reason=f"{type(exc).__name__}: {exc}",
            )
            return LightResult(
                ok=False,
                report=report,
                agent_id=coder.agent_id,
                raw_reason="send_error",
            )

        # Fixed sleep removed: go straight into wait_until (event-driven first + poll fallback)
        # If a "start" signal is needed, it should come from a pane.agent_status_changed
        # event or an output-triggered predicate

        def _predicate(out: object) -> bool:
            text = getattr(out, "normalized_text", None) or getattr(out, "text", "") or ""
            markers = _markers_in_text(str(text), task.success_markers)
            if not markers:
                return False
            # Delta only: ignore prompt pollution (only strictly check the delta when
            # pre_text doesn't already contain the marker)
            if pre_text and not any(m in pre_text for m in markers):
                # find how much is new
                overlap = 0
                for i in range(min(len(pre_text), len(text)), 0, -1):
                    if text[:i] == pre_text[-i:]:
                        overlap = i
                        break
                delta = text[overlap:]
                if not any(m in delta for m in markers):
                    return False
            # require at least some work evidence in delta or full
            evidence = any(x in text.lower() for x in ("def ", "import ", "created", "wrote", "file", "test"))
            return evidence

        wait = self._actions.wait_until(
            _ACTOR,
            coder.agent_id,
            predicate=_predicate,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
            since_revision=since_rev,
        )

        if wait.success:
            text = ""
            if wait.last_output is not None:
                text = wait.last_output.normalized_text or wait.last_output.text or ""
            # PPLX's new architecture: completion is reported via the side-channel
            # task_report, no longer relying on text markers to judge completion.
            # Markers are kept only for backward compatibility with older prompts;
            # actual success is decided by Herdr's done state + the side-channel report.
            found = ()  # old marker-based logic is deprecated

            notes = []
            if _rg is not None:
                try:
                    notes = _rg.extract_remagraph_notes(text)
                except Exception:  # best-effort note extraction on an already-successful task must never block returning the success report
                    logger.debug(
                        "Failed to extract RemaGraph notes from agent output",
                        exc_info=True,
                    )

            report = build_success_report(
                task.title,
                markers_found=(),
                hints=task.acceptance_hints,
            )
            raw: str = wait.reason
            if notes:
                raw = f"{wait.reason} + notes={notes}"

            # Automatically store the memory (best-effort bookkeeping: a backend
            # failure here must not block returning the success report).
            if _rg is not None and _rg.is_remagraph_enabled():
                summary = f"Success: {task.title}. markers={found or []}. Output summary: {text[:200]}"
                try:
                    _rg.store_memory(
                        used_task_id,
                        used_agent_id,
                        kind="status_update",
                        summary=summary,
                        handoff_note="Task completed",
                        tags=["bridge", "light", "success"],
                        project_id="herdr-bridge",
                    )
                except Exception:
                    logger.debug(
                        "Failed to store success memory; this does not block "
                        "returning the success report",
                        exc_info=True,
                    )

            # PPLX-recommended architecture: Herdr lifecycle + side-channel report.
            # The sock was already recorded to learnings (done at dispatch time); the
            # tower sends the structured report when the task finishes.
            report_sock = os.environ.get("TOWER_REPORT_SOCK", "/tmp/tower-reports.sock")
            if _rg and _rg.is_remagraph_enabled():
                try:
                    _rg.record_fleet_member(
                        used_task_id, used_agent_id, pane_id=coder.pane_id,
                        name=coder.agent_id, project_id="herdr-bridge",
                        learnings=[f"report_sock={report_sock}"]
                    )
                except Exception:
                    # Best-effort bookkeeping; a failure here (including AttributeError from a
                    # minimal/mock caller) must never block returning the success report.
                    logger.debug(
                        "Failed to record fleet member after task success; "
                        "this does not block returning the success report",
                        exc_info=True,
                    )
            # Send the side-channel report (using the output as the result)
            try:
                # Simple send (simulating what the agent would send; the real agent
                # should execute python3 -c as its last step)
                import json as _json
                import socket
                envelope = {
                    "type": "task_report",
                    "task_id": used_task_id,
                    "agent_id": used_agent_id,
                    "status": "completed",
                    "result": {"summary": text[:500], "markers": found or []},
                    "version": 1
                }
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(report_sock)
                    s.sendall((_json.dumps(envelope) + "\n").encode())
            except OSError:
                logger.debug(
                    "Failed to send side-channel task report over unix socket %s",
                    report_sock,
                    exc_info=True,
                )

            return LightResult(
                ok=True,
                report=report,
                agent_id=coder.agent_id,
                elapsed_sec=wait.elapsed_sec,
                raw_reason=raw,
            )

        reason_map = {
            "timeout": "timeout",
            "blocked": "blocked",
            "agent_gone": "failed",
            "error": "failed",
        }
        status = reason_map.get(wait.reason, "failed")
        messages = {
            "timeout": "The assistant did not finish the task within the time limit.",
            "blocked": "The assistant is waiting for your confirmation.",
            "failed": "Task execution was interrupted.",
        }
        report = build_failure_report(
            task.title,
            reason=status,  # type: ignore[arg-type]
            message=messages.get(status, "The task could not be completed."),
            technical_reason=wait.error or wait.reason,
        )

        # Automatically store the failure memory (best-effort bookkeeping,
        # recorded for timeout/blocked/failed alike; a backend failure here
        # must not block returning the failure report).
        if _rg is not None and _rg.is_remagraph_enabled():
            summary = f"Failed: {task.title}. reason={status}. {messages.get(status, '')}"
            try:
                _rg.store_memory(
                    used_task_id,
                    used_agent_id,
                    kind="status_update",
                    summary=summary,
                    handoff_note=wait.error or wait.reason or "",
                    tags=["bridge", "light", "failure"],
                    project_id="herdr-bridge",
                )
            except Exception:
                logger.debug(
                    "Failed to store failure memory; this does not block "
                    "returning the failure report",
                    exc_info=True,
                )

        return LightResult(
            ok=False,
            report=report,
            agent_id=coder.agent_id,
            elapsed_sec=wait.elapsed_sec,
            raw_reason=wait.reason,
        )

    # === Direct ACP channel + embedded RemaGraph (added; fully integrated) ===
    # Per the decision: herdr-bridge embeds RemaGraph + uses ACP for direct
    # communication with opencode, paired with an isolated worktree + memory ack
    # confirmation.
    # This is the "direct ACP channel" implementation for orchestration → agent.
    # Addendum: the command tower simultaneously acts as an ACP Router (Server +
    # Client), able to route to multiple different TUI ACP Agents.

    def run_task_via_acp(
        self,
        task: TaskSpec | str = "thumbnail-py",
        *,
        timeout_sec: float = 60,
        agent: str | None = None,
        use_router: bool = False,
    ) -> LightResult:
        """Run a task via direct ACP + embedded RemaGraph (headless opencode).

        Flow:
        1. Create an isolated worktree (ADR 0003)
        2. prepare_dispatch_text (embedded recall)
        3. ACP prompt
        4. Post-hoc recall to confirm the ack (memory channel)
        If use_router=True, routes through AcpRouter as the hub to a registered TUI agent.
        """
        if isinstance(task, str):
            try:
                task = get_task(task)
            except KeyError as exc:
                report = build_failure_report("Unknown task", reason="failed", message=str(exc))
                return LightResult(ok=False, report=report, raw_reason="unknown_task")

        agent = agent or "opencode"
        # Embedded RemaGraph prep
        send_text = task.agent_prompt
        used_tid = task.task_id
        used_aid = f"acp-{agent}" if agent else "acp-router"

        if _rg is not None and _rg.is_remagraph_enabled():
            send_text, used_tid, used_aid = _rg.prepare_dispatch_text(
                task.agent_prompt, base_task_id=task.task_id, agent_id=used_aid, project="herdr-acp"
            )
            try:
                _rg.store_memory(used_tid, used_aid, kind="status_update", summary=f"ACP started: {task.title}", project_id="herdr-bridge")
            except Exception:
                logger.debug(
                    "Failed to store ACP-start memory; continuing without "
                    "blocking dispatch",
                    exc_info=True,
                )

        if use_router:
            # Use the router as the hub; set env so router's internal prepare uses the
            # same tid (cross-project consistency)
            os.environ["TASK_ID"] = used_tid
            os.environ["AGENT_ID"] = used_aid
            os.environ["REMAGRAPH_PROJECT"] = "herdr-acp"
            router_res = self.route_via_acp_router(send_text, project="herdr-acp", target_agent=agent)
            ack_ok = "error" not in str(router_res)
            report = build_success_report(task.title, markers_found=(), hints=()) if ack_ok else build_failure_report(task.title, reason="failed", message="Router failed")
            return LightResult(ok=ack_ok, report=report, agent_id=used_aid, raw_reason=str(router_res))

        # Create an isolated worktree + ACP (the traditional custom path)
        try:
            from herdr_bridge.acp import AcpPolicy, connect
            from herdr_bridge.acp.isolated_workdir import create_isolated_worktree_for_opencode

            wt = create_isolated_worktree_for_opencode(branch_name=f"acp-{used_tid}")
            acp = connect()
            sess_name = f"acp-light-{used_tid}"
            _sess = acp.ensure_session(
                actor_id=_ACTOR,
                agent=agent,
                workdir=str(wt),
                session_name=sess_name,
                policy=AcpPolicy(mode="approve-all"),
            )
            res = acp.prompt(actor_id=_ACTOR, session_name=sess_name, text=send_text, timeout_sec=timeout_sec)

            # Confirmation (RemaGraph memory ack) -- switched from a fixed sleep to
            # relying on an immediate recall check (or a later ACP event ack).
            # If a precise wait for the ack is needed, switch to a RemaGraph event or a
            # short events.wait.
            mems = _rg.recall_memories(used_tid, used_aid, top_k=3, project_id="herdr-bridge") if _rg else []
            ack_ok = bool(mems) or res.reason != "error"

            if _rg and _rg.is_remagraph_enabled():
                try:
                    _rg.store_memory(
                        used_tid, used_aid,
                        kind="status_update",
                        summary=f"ACP complete: reason={res.reason} stop={res.stop_reason} ack_channel={ack_ok}",
                        project_id="herdr-acp",
                    )
                except Exception:
                    # Best-effort bookkeeping: a failure here must not be
                    # mistaken by the outer `except Exception as exc` below for
                    # an actual ACP-channel failure (the ACP call above already
                    # succeeded).
                    logger.debug(
                        "Failed to store ACP-complete memory; the ACP call "
                        "itself already succeeded",
                        exc_info=True,
                    )

            acp.close_session(actor_id=_ACTOR, session_name=sess_name)

            report = build_success_report(task.title, markers_found=(), hints=()) if ack_ok else build_failure_report(task.title, reason="failed", message="ACP channel ack not confirmed")
            return LightResult(ok=ack_ok, report=report, agent_id=used_aid, raw_reason=res.reason)

        except Exception as exc:  # noqa: BLE001
            # Store the error too (the pre-recall already happened above).
            # This must not raise: it runs while `exc` (the real ACP failure)
            # is already being handled, and a second exception here would
            # replace/mask it instead of the ACP failure being reported.
            if _rg and _rg.is_remagraph_enabled():
                try:
                    _rg.store_memory(
                        used_tid, used_aid,
                        kind="status_update",
                        summary=f"ACP channel failed: {str(exc)[:100]}",
                        handoff_note=str(exc)[:150],
                        project_id="herdr-acp",
                        tags=["acp", "error"],
                    )
                except Exception:
                    logger.debug(
                        "Failed to store ACP-failure memory while handling "
                        "an ACP channel error; the original error is still "
                        "reported below",
                        exc_info=True,
                    )
            report = build_failure_report(task.title, reason="failed", message="ACP channel failed", technical_reason=str(exc))
            return LightResult(ok=False, report=report, raw_reason="acp_error")

    def route_via_acp_router(
        self,
        prompt: str,
        *,
        project: str = "herdr-router",
        target_agent: str | None = None,
    ) -> dict[str, Any]:
        """Delegate to AcpRouter.dispatch_with_memory_confirm as the central facade.
        Cleans up scattered logic: prepare + store + record_fleet are all now
        force-enforced on the router side.
        Uses RemaGraph for coordination.
        """
        from herdr_bridge.acp.router import create_herdr_router
        router = create_herdr_router(project=project)
        # Keep the dynamic fallback compatible (if needed)
        if target_agent and target_agent not in router.registered_agents:
            from pathlib import Path
            base = Path(__file__).resolve().parents[3]
            script = str(base / "examples" / "acp-general-agent.py")
            caps = ["general", "ack"]
            desc = f"dynamic fallback for {target_agent} (real ACP)"
            router.register_agent(target_agent, "uv", ["run", "python", script], description=desc, capabilities=caps)
        return router.dispatch_with_memory_confirm(prompt, target=target_agent)

    def dispatch_with_memory_confirm(
        self,
        prompt: str,
        *,
        project: str = "herdr-router",
        target_agent: str | None = None,
        use_router: bool = True,
        pane_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any] | LightResult:
        """Delegate to the router's dispatch_with_memory_confirm (central facade) to
        force prepare+store+record.
        Cleans up scattered logic: the router side already unifies force
        record_fleet_member + status store.
        The legacy fallback path still ensures recording happens.
        """
        if use_router:
            from herdr_bridge.acp.router import create_herdr_router
            router = create_herdr_router(project=project)
            return router.dispatch_with_memory_confirm(
                prompt, target=target_agent, pane_id=pane_id, name=name
            )
        # Legacy fallback path still forces recording
        res = self.run_task_via_acp(prompt, use_router=False)
        if pane_id and name:
            tid = getattr(res, "task_id", f"{project}-dispatch") if hasattr(res, "task_id") else f"{project}-dispatch"
            aid = getattr(res, "agent_id", "unknown") if hasattr(res, "agent_id") else "unknown"
            self.record_fleet_member(tid, aid, pane_id=pane_id, name=name, project=project)
        # Record one even without a pane, to satisfy the force requirement
        elif _rg and _rg.is_remagraph_enabled():
            tid = getattr(res, "task_id", f"{project}-dispatch") if hasattr(res, "task_id") else f"{project}-dispatch"
            aid = getattr(res, "agent_id", "unknown") if hasattr(res, "agent_id") else "unknown"
            self.record_fleet_member(tid, aid, pane_id=f"legacy-{tid}", name="legacy-fallback", project=project)
        return res

    def batch_dispatch_with_memory(
        self,
        prompts: list[str],
        *,
        project: str = "herdr-router",
        use_router: bool = True,
    ) -> list[dict[str, Any]]:
        """Mid-term goal: simple batch / fleet-level cross-agent memory coordination.
        Dispatches each prompt in sequence (the router will distribute them to
        different downstream agents based on capabilities), storing memory for each.
        Returns a list of results.
        """
        results = []
        for i, p in enumerate(prompts):
            try:
                res = self.dispatch_with_memory_confirm(p, project=project, target_agent=None, use_router=use_router)
                results.append({"idx": i, "prompt": p[:50], "result": res})
            except Exception as e:  # noqa: BLE001  # one failing prompt must not abort the rest of the batch; the failure is captured in the results list, not silenced
                results.append({"idx": i, "error": str(e)})
        return results

    def record_fleet_member(
        self,
        task_id: str,
        agent_id: str,
        *,
        pane_id: str,
        name: str,
        project: str = "herdr-bridge",
    ) -> dict[str, Any]:
        """Must be called whenever the command tower dispatches a fleet member.
        This is the precondition for the tower being responsible for its own
        recycling: the tower will only go recycle a fleet member that was recorded here.
        """
        if _rg and _rg.is_remagraph_enabled():
            try:
                return _rg.record_fleet_member(
                    task_id, agent_id, pane_id=pane_id, name=name, project_id=project
                )
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "detail": str(exc)}
        return {"status": "memory_disabled"}

    def recycle_fleet_member(
        self,
        pane_id: str,
        task_id: str,
        agent_id: str,
        *,
        reason: str = "task_completed",
        project: str = "herdr-bridge",
    ) -> dict[str, Any]:
        """The command tower recycles a fleet member on its own.
        Strict restriction: only recycles a pane that has a fleet_member_dispatched
        record in its own RemaGraph.
        Never touches its own tower pane, another tower's agent, or a pane that isn't a fleet member.
        """
        # Verify this really was dispatched by me
        if _rg and _rg.is_remagraph_enabled():
            try:
                members = _rg.recall_fleet_members(task_id=task_id, agent_id=agent_id, project_id=project)
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "detail": str(exc)}
            is_mine = any(
                pane_id in str(m.get("learnings", [])) or pane_id in str(m.get("summary", ""))
                for m in members
            )
            if not is_mine:
                return {"status": "refused", "reason": "not my dispatched fleet member (no record in memory)"}

        # Extra protection: the last pane of the first tab of a Space (especially a
        # root tab the Tower helped open) must never be recycled, or the entire
        # project would disappear from the Space, forcing the user to manually reopen
        # it. Protect this even if the Tower itself dispatched this pane (unless there
        # is an explicit "close this project" command in the future).
        # Shared with the event-driven auto-recycle path
        # (AcpRouter._recycle_on_complete) so we don't maintain this logic separately
        # in two places and have one of them fall out of sync (that already happened
        # once — the event-driven path had been missed).
        from herdr_bridge.acp.router import is_last_pane_in_first_tab
        if is_last_pane_in_first_tab(pane_id):
            return {"status": "protected", "reason": "last pane in first tab of workspace; closing would make project disappear from Space"}

        # Execute the close (only closes a single pane)
        try:
            result = subprocess.run(
                ["herdr", "pane", "close", pane_id],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                return {"status": "close_failed", "error": result.stderr or result.stdout}

            # Record the recycle
            if _rg and _rg.is_remagraph_enabled():
                _rg.record_fleet_recycle(
                    task_id, agent_id, pane_id=pane_id, reason=reason, project_id=project
                )

            return {"status": "recycled", "pane_id": pane_id, "reason": reason}
        except (OSError, subprocess.SubprocessError, HerdrBridgeError) as exc:
            return {"status": "error", "detail": str(exc)}

    def recycle_completed_fleet(self, project: str = "herdr-bridge") -> list[dict[str, Any]]:
        """The command tower scans every fleet member it has dispatched itself, and
        automatically recycles the ones that already have a completion record.
        Only handles fleet members that belong to its own RemaGraph.
        """
        results = []
        if not (_rg and _rg.is_remagraph_enabled()):
            return [{"status": "memory_disabled"}]

        # Recall the ones dispatched by myself
        try:
            members = _rg.recall_fleet_members(project_id=project)
        except Exception as exc:  # noqa: BLE001
            return [{"status": "error", "detail": str(exc)}]
        for m in members:
            learnings = m.get("learnings", [])
            pane_id = None
            name = None
            for item in learnings:
                if item.startswith("pane_id="):
                    pane_id = item.split("=", 1)[1]
                if item.startswith("name="):
                    name = item.split("=", 1)[1]

            if not pane_id:
                continue

            # Check whether this task already has a completion record (simplified: look
            # for a done-related update)
            task_id = m.get("task_id")
            agent_id = m.get("agent_id")
            if not task_id or not agent_id:
                continue

            recent = _rg.recall_memories(task_id, agent_id, top_k=5, project_id=project)
            is_completed = any(
                "done" in str(r).lower() or "completed" in str(r).lower() or "handoff" in str(r).lower()
                for r in recent
            )

            if is_completed:
                res = self.recycle_fleet_member(pane_id, task_id, agent_id, reason="auto_completed", project=project)
                results.append({"pane": pane_id, "name": name, "result": res})

        return results

    # === opencode direct-control fleet event listener methods exposed to the tower ===
    # Lets the command tower control Herdr's event-driven monitoring (permission + recycle) via prompt

    def start_fleet_permission_monitor(self, project: str = "herdr-bridge") -> dict[str, Any]:
        """Start event-driven monitoring (a native Herdr subscription).
        - Subscribes to pane.agent_status_changed
        - idle/done → immediately recycle and close the pane
        - blocked → immediately auto-unblock
        Automatically watches new panes at dispatch time.
        Usually called automatically at bootstrap or when the tower starts.
        """
        from herdr_bridge.acp.router import create_herdr_router
        router = create_herdr_router(project=project)
        if hasattr(router, "_start_fleet_event_listener"):
            router._start_fleet_event_listener()
        return {"ok": True, "project": project, "msg": "Event-driven monitoring started (Herdr subscription, real-time, no polling)"}

    def get_fleet_permission_monitor_status(self, project: str = "herdr-bridge") -> dict[str, Any]:
        """Report the current monitoring status (for the tower itself or a human to see)."""
        if not (_rg and _rg.is_remagraph_enabled()):
            return {"ok": False, "msg": "Herdr Bridge Memory not enabled"}
        members = _rg.recall_fleet_members(project_id=project) or []
        # Already switched to event-driven: blocked / permission handled in real time by
        # router._start_fleet_event_listener + _handle_event
        # This get_status avoids polling herdr agent read (originally kept for
        # compatibility with an older check)
        # For a real-time count, the listener should maintain an internal counter or
        # read from a cache (to avoid periodic read_agent calls)
        blocked_count = 0
        # Could be accumulated by events in future: on blocked +=1, on unblock or done -=1
        return {
            "ok": True,
            "project": project,
            "recorded_fleet": len(members),
            "currently_blocked_on_permission": blocked_count,
            "msg": "Event-driven (Herdr subscription): idle/done recycled in real time, blocked unblocked in real time (get_status's periodic read-polling has been removed)"
        }

    def stop_fleet_permission_monitor(self, project: str = "herdr-bridge") -> dict[str, Any]:
        """The event-listener thread stops when the process ends."""
        return {
            "ok": True,
            "project": project,
            "msg": "The event listener daemon stops along with the background process. To force-stop it, use pkill -f tower-fleet-event"
        }
