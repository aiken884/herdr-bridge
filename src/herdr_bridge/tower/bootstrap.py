"""Tower Bootstrap Protocol：SessionStart(compact) hook 的核心邏輯（規格 §4.2）。"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from herdr_bridge.core.paths import resolve_project_root
from herdr_bridge.orchestration import task_board as tb
from herdr_bridge.orchestration.memory import search_memories

logger = logging.getLogger("herdr_bridge.tower.bootstrap")

# task_pool.py（claim/unclaim）寫下的 learnings 固定格式，見 task_pool.py
# _append_event_with_retry 的 summary/learnings 組法（2026-08-09 BT-04 修復時定案）。
_TASK_POOL_TRANSITION_RE = re.compile(r"^task_pool transition: (?P<task_id>\S+) -> (?P<new_status>\S+)$")

_REFRESH_SUBSTEP_TIMEOUT_SECONDS = 10

FALLBACK_NO_SNAPSHOT = """[TOWER-CONTEXT: no-snapshot]
Task Board snapshot not found. This is likely the first session in this project.
Assume: no tasks are currently claimed. Proceed with normal triage.
Snapshot will be created at end of this refresh cycle."""

FALLBACK_SNAPSHOT_CORRUPT = """[TOWER-CONTEXT: snapshot-corrupt]
Task Board snapshot exists but failed integrity check (JSON parse error or schema mismatch).
Last known snapshot path: {snapshot_path}
Treat all task states as UNKNOWN. Do NOT assume any task is claimed or completed.
Recommended action: re-query Task Board via tool call before making any claim decisions.
Corrupt file has been renamed to: {corrupt_path}"""

FALLBACK_SNAPSHOT_STALE = """[TOWER-CONTEXT: snapshot-stale]
Task Board snapshot refresh timed out (>10s). Injecting last known snapshot from: {mtime_iso}
WARNING: Data may be up to {staleness_seconds}s stale. Verify critical task states before claiming.
{last_known_content}"""


def _resolve_bootstrap_project_id() -> str:
    """優先序比照 light/cli.py 的 _resolve_memory_project：HERDR_MEMORY_PROJECT →
    REMAGRAPH_PROJECT → project_root 目錄名稱（不寫死 "herdr-bridge"，Bootstrap Protocol
    本身是通用元件，不專屬這個 repo）。"""
    return (
        os.environ.get("HERDR_MEMORY_PROJECT")
        or os.environ.get("REMAGRAPH_PROJECT")
        or resolve_project_root().name
    )


def _task_pool_events_from_remagraph() -> list[dict[str, Any]]:
    """查真實 RemaGraph，把 task_pool.py claim()/unclaim() 寫下的事件轉成
    reconcile_snapshot_from_events() 看得懂的 task_status_changed 事件。只認得
    tags=["task-pool","claim"] 且 learnings 符合固定格式的紀錄（見 _TASK_POOL_TRANSITION_RE），
    其餘一律跳過，不猜測。"""
    project_id = _resolve_bootstrap_project_id()
    records = search_memories(
        kind="status_update", tags=["task-pool", "claim"],
        project_id=project_id, top_k=500,
    )
    events: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: str(r.get("timestamp", ""))):
        for learning in record.get("learnings") or []:
            match = _TASK_POOL_TRANSITION_RE.match(learning)
            if not match:
                continue
            event = {
                "kind": "task_status_changed",
                "task_id": match.group("task_id"),
                "new_status": match.group("new_status"),
                "ts": record.get("timestamp"),
            }
            if match.group("new_status") == "claimed":
                event["claimed_by"] = record.get("agent_id")
            events.append(event)
            break  # 一筆紀錄只會有一個 transition learning，找到就換下一筆
    return events


def _refresh_from_remagraph() -> None:
    """查真實 RemaGraph 事件、用 reconcile_snapshot_from_events()（非破壞性校正，PPLX
    共識 2026-08-09）對齊 task_board.json。刻意不用 tb.refresh_snapshot()（全量重播覆寫）
    ——目前只有 claim()/unclaim() 會寫事件，任務一開始被建立成 pending 從未經過事件記錄，
    全量重播覆寫會把所有還沒被 claim 過的 pending 任務靜默刪掉，比什麼都不做更危險。

    最佳努力語意（比照 orchestration/memory.py 其他 RemaGraph 呼叫的既有慣例）：任何例外
    都不外拋，只記 log 並直接返回——讓外層 ThreadPoolExecutor 的 10 秒逾時只保留給「真的卡住」
    這一種情境，不應該因為這裡拋出非逾時例外而讓整個 build_boot_packet() 崩潰。"""
    try:
        tb.reconcile_snapshot_from_events(_task_pool_events_from_remagraph)
    except Exception:
        logger.warning("_refresh_from_remagraph failed (best-effort, falling back to existing snapshot)", exc_info=True)


def build_boot_packet() -> str:
    board_path = tb.default_snapshot_path()

    if not board_path.exists():
        return FALLBACK_NO_SNAPSHOT

    try:
        raw = board_path.read_text()
        board = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        corrupt_path = board_path.with_suffix(f".json.corrupt.{int(time.time())}")
        try:
            board_path.rename(corrupt_path)
        except OSError:
            logger.warning("failed to rename corrupt snapshot", exc_info=True)
        return FALLBACK_SNAPSHOT_CORRUPT.format(snapshot_path=board_path, corrupt_path=corrupt_path)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_refresh_from_remagraph)
        try:
            future.result(timeout=_REFRESH_SUBSTEP_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            mtime_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(board_path.stat().st_mtime))
            staleness = time.time() - board_path.stat().st_mtime
            return FALLBACK_SNAPSHOT_STALE.format(
                mtime_iso=mtime_iso, staleness_seconds=int(staleness),
                last_known_content=json.dumps(board, ensure_ascii=False),
            )

    fresh_raw = board_path.read_text()
    fresh_board = json.loads(fresh_raw)
    lines = ["## Current Task Board"]
    for task in fresh_board.get("tasks", {}).values():
        line = f"- {task['task_id']}: {task['status']} (claimed_by={task.get('claimed_by', '-')}"
        if task["status"] == "blocked" and task.get("blocked_reason"):
            line += f", blocked_reason={task['blocked_reason']}"
        if task["status"] == "done" and task.get("dispatch_receipt"):
            line += f", dispatch_receipt={task['dispatch_receipt']}"
        line += ")"
        lines.append(line)
    return "\n".join(lines) if fresh_board.get("tasks") else "## Current Task Board\n(no active tasks)"
