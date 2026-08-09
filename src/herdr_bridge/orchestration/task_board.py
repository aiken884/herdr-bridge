"""Task Board：事件日誌之上的 CQRS 投影層（規格 §4.1）。狀態機在程式碼層強制。"""
from __future__ import annotations

import fcntl
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from herdr_bridge.core.paths import resolve_project_root
from herdr_bridge.errors import HerdrBridgeError

VALID_STATES = {"pending", "claimed", "in_progress", "blocked", "done", "failed", "cancelled"}

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"claimed", "in_progress", "cancelled"},
    "claimed": {"in_progress", "cancelled"},
    "in_progress": {"blocked", "done", "failed", "cancelled"},
    "blocked": {"in_progress", "cancelled"},
    "done": set(),
    "failed": {"cancelled"},
    "cancelled": set(),
}


class InvalidTransitionError(HerdrBridgeError):
    """狀態轉換不合法，或缺少該轉換要求的必填欄位（規格 §4.1 狀態機）。"""


def apply_transition(task: dict[str, Any], new_status: str, **fields: Any) -> dict[str, Any]:
    """純函式：回傳新的 task dict，不寫檔。呼叫方負責寫入快照與事件日誌。"""
    current = task["status"]
    if new_status == "cancelled":
        pass  # 任何狀態都可以取消
    elif new_status not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(f"{current} -> {new_status} 不是合法轉換")

    if new_status == "blocked" and not fields.get("blocked_reason"):
        raise InvalidTransitionError("blocked 轉換必須附 blocked_reason")
    if new_status == "done" and not fields.get("dispatch_receipt"):
        raise InvalidTransitionError("done 轉換必須附 dispatch_receipt")

    updated = {**task, "status": new_status, **fields}
    return updated


def default_snapshot_path() -> Path:
    return resolve_project_root() / ".herdr" / "task_board.json"


def write_snapshot_atomic(board: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


_LEGACY_STATUS_MAP = {
    "in_progress": "claimed", "running": "claimed",
    "done": "completed", "finished": "done",
    "error": "failed", "blocked": "blocked",
}


def task_board_lock_path() -> Path:
    return default_snapshot_path().parent / ".task_board.lock"


def _project_event(event: dict[str, Any], board: dict[str, Any]) -> None:
    kind = event.get("kind", "")
    tasks = board.setdefault("tasks", {})

    if kind == "task_status_changed":
        task_id = event["task_id"]
        task = tasks.setdefault(task_id, {"task_id": task_id, "status": "pending"})
        task["status"] = event["new_status"]
        task["updated_at"] = event.get("ts")
        for key in ("claimed_by", "claimed_at", "blocked_reason", "dispatch_receipt"):
            if key in event:
                task[key] = event[key]
        return

    if kind == "status_update":
        synthetic_id = f"legacy_{event.get('agent_id', 'unknown')}_{event.get('ts', '')}"
        task = tasks.setdefault(synthetic_id, {"task_id": synthetic_id, "status": "pending", "_legacy": True})
        raw_status = event.get("status") or event.get("new_status")
        if raw_status:
            task["status"] = _LEGACY_STATUS_MAP.get(raw_status.lower(), raw_status.lower())
        return

    if kind == "task_handoff":
        task_id = event.get("task_id") or f"legacy_handoff_{event.get('ts', '')}"
        task = tasks.setdefault(task_id, {"task_id": task_id, "status": "pending", "_legacy": True})
        task["status"] = "claimed"
        task["claimed_by"] = event.get("to_agent")
        return

    # 未知格式：跳過，不中斷整個重播


def project_all_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    board: dict[str, Any] = {"tasks": {}}
    for event in events:
        _project_event(event, board)
    return board


def refresh_snapshot(events_source: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
    """取鎖 → 全量重播 → 原子寫入 → 回傳。鎖區間涵蓋整個重播與寫入（規格 §4.1 M-C）。

    全量重播覆寫語意：從空白 board 開始，只留下 events_source() 能重播出來的任務。只在
    事件日誌真的涵蓋任務完整生命週期（含建立）時才適合用這個——見
    reconcile_snapshot_from_events() 的說明。"""
    lock_path = task_board_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 鎖必須跨越整個 try/finally 存活，不能用 with open(...) 的區塊作用域收斂
    # （比照 signal/lock.py SingleInstanceLock.acquire() 既有的相同理由）。
    lock_fd = open(lock_path, "w")  # noqa: SIM115
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        board = project_all_events(events_source())
        write_snapshot_atomic(board, default_snapshot_path())
        return board
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def reconcile_snapshot_from_events(events_source: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
    """非破壞性校正（PPLX 共識，2026-08-09，見 docs/decisions 或 RemaGraph task_id
    command-tower-leader-refresh-remagraph-design）：跟 refresh_snapshot() 的「全量重播
    覆寫」語意不同——這裡以現有快照為底，只用事件去更新/新增有事件記錄涵蓋到的任務欄位，
    絕不刪除快照裡既有、但事件流沒有涵蓋到的任務。

    背景：目前只有 claim()/unclaim()（task_pool.py）會透過 RemaGraph 寫下事件，任務一開始
    被建立成 pending 狀態從未經過事件記錄。如果用 refresh_snapshot() 的全量重播覆寫，會把
    所有還沒被 claim 過的 pending 任務直接從快照裡刪掉（靜默資料流失，比什麼都不做更危險）。
    等 task_created 等完整生命週期事件補齊之後（PPLX 建議的中期方案），才適合換回
    refresh_snapshot() 的全量重播語意。"""
    lock_path = task_board_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 鎖必須跨越整個 try/finally 存活，不能用 with open(...) 的區塊作用域收斂
    # （比照 signal/lock.py SingleInstanceLock.acquire() 既有的相同理由）。
    lock_fd = open(lock_path, "w")  # noqa: SIM115
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        path = default_snapshot_path()
        try:
            board = json.loads(path.read_text()) if path.exists() else {"tasks": {}}
        except json.JSONDecodeError:
            board = {"tasks": {}}
        for event in events_source():
            _project_event(event, board)
        write_snapshot_atomic(board, path)
        return board
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
