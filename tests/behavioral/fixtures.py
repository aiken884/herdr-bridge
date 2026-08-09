"""BT-01~07 行為驗收測試的共用測試基礎設施（規格 §5，Wave 5 Task H1）。

不是自動化測試本身——BT-01~07 依規格 §5 定義為人工審閱對話記錄，不自動化。
這裡只提供「快速布置前提條件」的 helper，供人工執行測試前呼叫。
"""
from __future__ import annotations

from dataclasses import dataclass

from herdr_bridge.orchestration import task_board as tb


@dataclass
class MockWorker:
    """供 BT-01（平行任務派發）等案例使用的假艦隊成員。"""

    worker_id: str
    brand: str = "claude"
    status: str = "idle"


def spin_up_mock_workers(count: int) -> list[MockWorker]:
    """產生 count 個 idle 狀態的 mock worker（BT-01 前提條件：至少 3 個可用 Worker）。"""
    if count < 1:
        raise ValueError("count 必須 >= 1")
    return [MockWorker(worker_id=f"mock-worker-{i}") for i in range(1, count + 1)]


def seed_task_board(tasks: list[dict]) -> None:
    """依 tasks 直接布置 task_board.json 快照（BT-03/04/05 等案例的前提條件）。

    每個 task dict 至少需要 task_id 與 status 兩個欄位；status 必須是
    orchestration.task_board.VALID_STATES 之一，否則直接拒絕，避免布置出
    不合法的快照（regression 對照 test_task_board_state_machine.py 的合法值集合）。
    其餘欄位（claimed_by/claimed_at/blocked_reason/dispatch_receipt/heartbeat_ts/
    updated_at 等）依案例需求原樣帶入，seed 不做狀態機轉換檢查——它是「直接構造
    既定情境」的工具，不是模擬一次真實的狀態轉換（那是 apply_transition 的職責）。
    """
    board: dict = {"tasks": {}}
    for task in tasks:
        if "task_id" not in task:
            raise ValueError(f"task 缺少必填欄位 task_id: {task!r}")
        if "status" not in task:
            raise ValueError(f"task 缺少必填欄位 status: {task!r}")
        if task["status"] not in tb.VALID_STATES:
            raise ValueError(
                f"task_id={task['task_id']!r} 的 status={task['status']!r} 不是合法值，"
                f"必須是 {sorted(tb.VALID_STATES)} 之一"
            )
        board["tasks"][task["task_id"]] = dict(task)

    tb.write_snapshot_atomic(board, tb.default_snapshot_path())
