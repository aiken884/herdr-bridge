import fcntl
import json
import threading
import time

from herdr_bridge.orchestration.task_board import (
    project_all_events,
    reconcile_snapshot_from_events,
    refresh_snapshot,
    task_board_lock_path,
)


def test_project_all_events_builds_correct_states():
    events = [
        {"kind": "task_status_changed", "task_id": "t1", "new_status": "pending", "ts": "1"},
        {"kind": "task_status_changed", "task_id": "t1", "new_status": "claimed",
         "claimed_by": "agent-a", "ts": "2"},
    ]
    board = project_all_events(events)
    assert board["tasks"]["t1"]["status"] == "claimed"
    assert board["tasks"]["t1"]["claimed_by"] == "agent-a"


def test_refresh_snapshot_acquires_shared_lock(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: tmp_path / "task_board.json")

    def fake_events():
        return [{"kind": "task_status_changed", "task_id": "t1", "new_status": "pending", "ts": "1"}]

    board = refresh_snapshot(fake_events)
    assert board["tasks"]["t1"]["status"] == "pending"
    written = json.loads((tmp_path / "task_board.json").read_text())
    assert written == board


def test_refresh_snapshot_blocks_concurrent_writer(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)

    lock_path = task_board_lock_path()
    held = threading.Event()
    release = threading.Event()

    def holder():
        fd = open(lock_path, "w")  # noqa: SIM115 -- 鎖需跨越整個 holder() 執行期存活
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.set()
        release.wait(timeout=2)
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()

    t = threading.Thread(target=holder)
    t.start()
    held.wait(timeout=2)

    start = time.monotonic()
    release.set()  # 讓 holder 盡快放手，只驗證流程不死鎖
    refresh_snapshot(list)
    elapsed = time.monotonic() - start
    t.join(timeout=2)
    assert elapsed < 2


def test_reconcile_preserves_pending_tasks_not_covered_by_events(tmp_path, monkeypatch):
    """PPLX 共識（2026-08-09）的核心安全屬性：從未被 claim 過的 pending 任務(沒有對應
    RemaGraph 事件)必須在 reconcile 之後原封不動地留著，不能因為事件流沒提到它就被刪除
    ——這正是跟 refresh_snapshot()（全量重播覆寫）語意上的差異所在。"""
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic(
        {"tasks": {
            "TASK-104": {"task_id": "TASK-104", "status": "pending"},
            "TASK-105": {"task_id": "TASK-105", "status": "pending"},
        }},
        board_path,
    )
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)

    # 事件流只提到 TASK-105（例如它剛被 claim），完全沒提到 TASK-104
    board = reconcile_snapshot_from_events(lambda: [
        {"kind": "task_status_changed", "task_id": "TASK-105", "new_status": "claimed",
         "claimed_by": "worker-a", "ts": "2026-08-09T00:00:00Z"},
    ])

    assert board["tasks"]["TASK-104"]["status"] == "pending"  # 沒被事件覆蓋，原樣保留
    assert board["tasks"]["TASK-105"]["status"] == "claimed"  # 有事件的正確更新
    assert board["tasks"]["TASK-105"]["claimed_by"] == "worker-a"


def test_reconcile_creates_task_when_event_references_unknown_task_id(tmp_path, monkeypatch):
    """有事件明確提到某個 task_id 時，即使快照裡原本沒有這筆任務，也要建立出來
    （事件本身就是這筆任務存在過的證據，不是「無中生有刪資料」的反面情境）。"""
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic({"tasks": {}}, board_path)
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)

    board = reconcile_snapshot_from_events(lambda: [
        {"kind": "task_status_changed", "task_id": "TASK-201", "new_status": "claimed",
         "claimed_by": "worker-a", "ts": "1"},
    ])
    assert board["tasks"]["TASK-201"]["status"] == "claimed"


def test_reconcile_no_snapshot_starts_empty_not_crash(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: tmp_path / "does-not-exist.json")

    board = reconcile_snapshot_from_events(list)
    assert board == {"tasks": {}}
