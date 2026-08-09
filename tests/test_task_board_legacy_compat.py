from herdr_bridge.orchestration.task_board import project_all_events


def test_legacy_status_update_projected_with_legacy_flag():
    events = [
        {"kind": "status_update", "agent_id": "agent-x", "status": "in_progress", "ts": "100"},
    ]
    board = project_all_events(events)
    task = next(iter(board["tasks"].values()))
    assert task["_legacy"] is True
    assert task["status"] == "claimed"


def test_legacy_task_handoff_projected_as_claimed():
    events = [
        {"kind": "task_handoff", "task_id": "old-1", "to_agent": "agent-y", "ts": "200"},
    ]
    board = project_all_events(events)
    assert board["tasks"]["old-1"]["status"] == "claimed"
    assert board["tasks"]["old-1"]["claimed_by"] == "agent-y"
    assert board["tasks"]["old-1"]["_legacy"] is True


def test_unknown_event_kind_skipped_not_crashing():
    events = [{"kind": "totally_unknown_kind", "ts": "1"}]
    board = project_all_events(events)  # 不應拋例外
    assert board["tasks"] == {}


def test_mixed_legacy_and_new_events_coexist():
    events = [
        {"kind": "status_update", "agent_id": "a1", "status": "done", "ts": "1"},
        {"kind": "task_status_changed", "task_id": "new-1", "new_status": "pending", "ts": "2"},
    ]
    board = project_all_events(events)
    assert len(board["tasks"]) == 2
