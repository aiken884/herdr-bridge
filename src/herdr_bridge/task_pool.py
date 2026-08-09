"""認領式任務池：機制層，無政策判斷（規格 §4.5，H1 + R-5 最終定案）。"""
from __future__ import annotations

import fcntl
import json
import logging
import time
from datetime import UTC, datetime
from random import uniform
from typing import Any

from herdr_bridge.errors import HerdrBridgeError
from herdr_bridge.orchestration import task_board as tb
from herdr_bridge.orchestration.memory import store_memory


class EventLogWriteError(HerdrBridgeError):
    """claim() 附加寫入事件日誌失敗（重試耗盡後），已觸發快照回滾。"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _append_event_with_retry(task_id: str, agent_id: str, project_id: str, new_status: str) -> None:
    """R-5：最多 3 次重試，指數退避 50/100/200/400ms（各加 jitter），總計上限 1 秒。"""
    delays = [0.05, 0.1, 0.2, 0.4]
    deadline = time.monotonic() + 1.0
    last_exc: Exception | None = None
    for delay in delays:
        if time.monotonic() > deadline:
            break
        try:
            result = store_memory(
                task_id, agent_id, kind="status_update",
                # RemaGraph 拒收 summary < 30 字元（見 remagraph/arbitration.py
                # summary_too_short）；短 task_id（如 "TASK-201"）配上原本精簡的
                # f"task {task_id} -> {new_status}" 常常不足 30 字元，導致 claim()
                # 100% 失敗（2026-08-09 BT-04 行為驗收測試發現，非競態問題）。
                # 固定的英文骨架長度已確保任何合法 task_id/status 組合都 >= 30 字元。
                summary=f"task_pool: task {task_id} status changed to {new_status} (agent={agent_id})",
                project_id=project_id,
                tags=["task-pool", "claim"],
                # RemaGraph 規則 #2（arbitration.py validate_learnings）要求 learnings
                # 至少一筆非空白元素，否則一律拒收（learnings_empty）。task_pool 原本
                # 完全沒傳這個參數（預設 None -> store_memory 內部轉成 []），同樣是
                # 2026-08-09 BT-04 發現的成因之一，claim()/unclaim() 因此 100% 失敗。
                learnings=[f"task_pool transition: {task_id} -> {new_status}"],
            )
            if result.get("status") in ("stored", "ok"):
                return
            last_exc = RuntimeError(str(result))
        except OSError as exc:
            last_exc = exc
        time.sleep(delay + uniform(-0.01, 0.01))
    raise EventLogWriteError(f"event log write failed after retries: {last_exc}") from last_exc


class TaskPool:
    def __init__(self, *, project_id: str) -> None:
        self.project_id = project_id

    def claim(self, task_id: str, agent_id: str) -> bool:
        lock_path = tb.task_board_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 鎖必須跨越整個 try/finally 存活，不能用 with open(...) 的區塊作用域收斂
        # （比照 signal/lock.py SingleInstanceLock.acquire() 既有的相同理由）。
        lock_fd = open(lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            board_path = tb.default_snapshot_path()
            old_snapshot_bytes = board_path.read_bytes()
            board = json.loads(old_snapshot_bytes)
            task = board["tasks"].get(task_id)
            if task is None or task["status"] != "pending":
                return False

            task["status"] = "claimed"
            task["claimed_by"] = agent_id
            task["claimed_at"] = _now_iso()
            tb.write_snapshot_atomic(board, board_path)

            try:
                _append_event_with_retry(task_id, agent_id, self.project_id, "claimed")
            except EventLogWriteError:
                tb.write_snapshot_atomic(json.loads(old_snapshot_bytes), board_path)
                raise
            return True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def unclaim(self, task_id: str, agent_id: str) -> None:
        lock_path = tb.task_board_lock_path()
        # 鎖必須跨越整個 try/finally 存活，不能用 with open(...) 的區塊作用域收斂
        # （比照 signal/lock.py SingleInstanceLock.acquire() 既有的相同理由）。
        lock_fd = open(lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            board_path = tb.default_snapshot_path()
            board = json.loads(board_path.read_text())
            task = board["tasks"].get(task_id)
            if task is None:
                return
            task["status"] = "pending"
            task.pop("claimed_by", None)
            task.pop("heartbeat_ts", None)
            tb.write_snapshot_atomic(board, board_path)
            # M4 修正（實作計畫審查）：unclaim 不像 claim 有回滾對象——快照已經
            # 正確回到 pending，這本身就是語意正確的終點；event log 寫入失敗只代表
            # 「這次 unclaim 的稽核記錄」缺失，不代表狀態本身錯誤，所以只 log
            # warning，不 raise、不回滾（回滾快照反而會把已經正確的 pending 狀態
            # 錯誤地改回 claimed）。
            try:
                _append_event_with_retry(task_id, agent_id, self.project_id, "pending")
            except EventLogWriteError as exc:
                logging.getLogger("herdr_bridge.task_pool").warning(
                    "unclaim(%s) succeeded but event log write failed (audit trail incomplete): %s",
                    task_id, exc,
                )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def heartbeat(self, task_id: str, agent_id: str) -> None:
        lock_path = tb.task_board_lock_path()
        # 鎖必須跨越整個 try/finally 存活，不能用 with open(...) 的區塊作用域收斂
        # （比照 signal/lock.py SingleInstanceLock.acquire() 既有的相同理由）。
        lock_fd = open(lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            board_path = tb.default_snapshot_path()
            board = json.loads(board_path.read_text())
            task = board["tasks"].get(task_id)
            if task is not None and task.get("claimed_by") == agent_id:
                task["heartbeat_ts"] = _now_iso()
                tb.write_snapshot_atomic(board, board_path)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def list_claimable(self) -> list[dict[str, Any]]:
        board_path = tb.default_snapshot_path()
        if not board_path.exists():
            return []
        board = json.loads(board_path.read_text())
        return [t for t in board["tasks"].values() if t["status"] == "pending"]
