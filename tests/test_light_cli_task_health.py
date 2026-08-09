import argparse
from datetime import UTC, datetime, timedelta


def _args(**overrides):
    base = {"verbose": False, "heartbeat_stale_seconds": 180, "error_count_threshold": 3}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_task_health_no_tasks_no_anomalies(tmp_path, monkeypatch, capsys):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.light.cli._fleet_health_seen_path", lambda: tmp_path / "seen.json")
    tb.write_snapshot_atomic({"tasks": {}}, board_path)

    from herdr_bridge.light.cli import cmd_task_health
    rc = cmd_task_health(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "無新異常" in out


def test_task_health_missing_snapshot_no_crash(tmp_path, monkeypatch, capsys):
    from herdr_bridge.orchestration import task_board as tb
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: tmp_path / "does-not-exist.json")
    monkeypatch.setattr("herdr_bridge.light.cli._fleet_health_seen_path", lambda: tmp_path / "seen.json")

    from herdr_bridge.light.cli import cmd_task_health
    rc = cmd_task_health(_args())
    assert rc == 0


def test_task_health_reports_stale_heartbeat(tmp_path, monkeypatch, capsys):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.light.cli._fleet_health_seen_path", lambda: tmp_path / "seen.json")

    stale_ts = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
    tb.write_snapshot_atomic(
        {"tasks": {"TASK-502": {"task_id": "TASK-502", "status": "in_progress",
                                 "claimed_by": "worker-b", "heartbeat_ts": stale_ts}}},
        board_path,
    )

    from herdr_bridge.light.cli import cmd_task_health
    rc = cmd_task_health(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "TASK-502" in out
    assert "heartbeat_stale" in out


def test_task_health_second_call_suppresses_unchanged_anomaly(tmp_path, monkeypatch, capsys):
    """跨兩次獨立呼叫（模擬自我重排迴圈的兩個 tick）驗證 seen 狀態真的有跨呼叫存續，
    第二次同一個未惡化的異常不該再刷屏。"""
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.light.cli._fleet_health_seen_path", lambda: tmp_path / "seen.json")

    stale_ts = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
    tb.write_snapshot_atomic(
        {"tasks": {"TASK-502": {"task_id": "TASK-502", "status": "in_progress",
                                 "claimed_by": "worker-b", "heartbeat_ts": stale_ts}}},
        board_path,
    )

    from herdr_bridge.light.cli import cmd_task_health
    cmd_task_health(_args())
    capsys.readouterr()  # 清掉第一次的輸出

    rc = cmd_task_health(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "無新異常" in out
