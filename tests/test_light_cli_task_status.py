

def test_task_status_prints_current_states(tmp_path, monkeypatch, capsys):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    tb.write_snapshot_atomic(
        {"tasks": {"t1": {"task_id": "t1", "status": "in_progress", "claimed_by": "agent-a"}}},
        board_path,
    )

    import argparse

    from herdr_bridge.light.cli import cmd_task_status
    args = argparse.Namespace(project=None, verbose=False)
    rc = cmd_task_status(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "t1" in out
    assert "in_progress" in out


def test_task_status_empty_board_says_no_tasks(tmp_path, monkeypatch, capsys):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    tb.write_snapshot_atomic({"tasks": {}}, board_path)

    import argparse

    from herdr_bridge.light.cli import cmd_task_status
    args = argparse.Namespace(project=None, verbose=False)
    rc = cmd_task_status(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "沒有" in out or "no tasks" in out.lower()
