import json
from datetime import UTC, datetime, timedelta


def test_scan_reaps_expired_claim(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    stale_ts = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    tb.write_snapshot_atomic(
        {"tasks": {"t1": {"task_id": "t1", "status": "claimed",
                           "claimed_by": "agent-dead", "heartbeat_ts": stale_ts}}},
        board_path,
    )
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", lambda *a, **k: None)

    from herdr_bridge.signal.daemon import SignalDaemon
    daemon = SignalDaemon.__new__(SignalDaemon)  # 繞過建構子，只測 GC 邏輯
    daemon.project = "test-proj"
    reaped = daemon._scan_and_reap_expired_claims()

    assert reaped == 1
    board = json.loads(board_path.read_text())
    assert board["tasks"]["t1"]["status"] == "pending"


def test_scan_does_not_reap_fresh_heartbeat(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    fresh_ts = datetime.now(UTC).isoformat()
    tb.write_snapshot_atomic(
        {"tasks": {"t1": {"task_id": "t1", "status": "claimed",
                           "claimed_by": "agent-alive", "heartbeat_ts": fresh_ts}}},
        board_path,
    )
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)

    from herdr_bridge.signal.daemon import SignalDaemon
    daemon = SignalDaemon.__new__(SignalDaemon)
    daemon.project = "test-proj"
    reaped = daemon._scan_and_reap_expired_claims()
    assert reaped == 0


def test_scan_skips_malformed_heartbeat_without_aborting_rest_of_scan(tmp_path, monkeypatch):
    """2026-08-09 CodeGraph 全專案診斷發現的迴歸測試：datetime.fromisoformat(hb) 原本
    完全沒有 try/except 保護，任何一筆任務的 heartbeat_ts 格式異常（或缺時區）就會拋例外
    中斷整個 for 迴圈——不是跳過那一筆，是這次掃描『之後所有任務』都不檢查了。這裡把壞
    資料放在 dict literal 的前兩個 key，確保它們會先被迭代到（Python dict 自 3.7 起保證
    插入順序，write_snapshot_atomic 用 json.dumps 未加 sort_keys、json.loads 會保留原始
    key 順序，全程不重排序，所以測試能可靠依賴這個順序，不是靠 task_id 字面上的字母序）。
    驗證後面一筆真的逾時的任務仍然會被正確回收，不會被前面的壞資料拖累。"""
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    stale_ts = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    tb.write_snapshot_atomic(
        {"tasks": {
            "a-bad": {"task_id": "a-bad", "status": "claimed",
                      "claimed_by": "agent-x", "heartbeat_ts": "not-a-timestamp"},
            "b-naive": {"task_id": "b-naive", "status": "claimed",
                        "claimed_by": "agent-y", "heartbeat_ts": "2026-08-09T00:00:00"},  # 無時區
            "z-expired": {"task_id": "z-expired", "status": "claimed",
                          "claimed_by": "agent-dead", "heartbeat_ts": stale_ts},
        }},
        board_path,
    )
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", lambda *a, **k: None)

    from herdr_bridge.signal.daemon import SignalDaemon
    daemon = SignalDaemon.__new__(SignalDaemon)
    daemon.project = "test-proj"
    reaped = daemon._scan_and_reap_expired_claims()  # 不應該拋例外

    assert reaped == 1
    board = json.loads(board_path.read_text())
    assert board["tasks"]["z-expired"]["status"] == "pending"  # 沒被壞資料拖累
    assert board["tasks"]["a-bad"]["status"] == "claimed"  # 壞資料本身維持原樣，不誤判成逾時
    assert board["tasks"]["b-naive"]["status"] == "claimed"


def test_scan_skips_empty_string_heartbeat(tmp_path, monkeypatch):
    """空字串是資料損毀常見型態，跟 None（正常視為『尚未回報過』而跳過）語意不同，
    要走同一條防護路徑而不是拋例外。"""
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic(
        {"tasks": {"t1": {"task_id": "t1", "status": "claimed",
                           "claimed_by": "agent-x", "heartbeat_ts": ""}}},
        board_path,
    )
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)

    from herdr_bridge.signal.daemon import SignalDaemon
    daemon = SignalDaemon.__new__(SignalDaemon)
    daemon.project = "test-proj"
    reaped = daemon._scan_and_reap_expired_claims()  # 不應該拋例外
    assert reaped == 0


def test_scan_skips_non_string_heartbeat_type(tmp_path, monkeypatch):
    """heartbeat_ts 若是非字串型別（例如整數），fromisoformat 會丟 TypeError 而非
    ValueError，確保這條路徑也被涵蓋。"""
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic(
        {"tasks": {"t1": {"task_id": "t1", "status": "claimed",
                           "claimed_by": "agent-x", "heartbeat_ts": 12345}}},
        board_path,
    )
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)

    from herdr_bridge.signal.daemon import SignalDaemon
    daemon = SignalDaemon.__new__(SignalDaemon)
    daemon.project = "test-proj"
    reaped = daemon._scan_and_reap_expired_claims()  # 不應該拋例外
    assert reaped == 0
