import json

import pytest

from herdr_bridge.orchestration.task_board import (
    VALID_STATES,
    InvalidTransitionError,
    apply_transition,
    write_snapshot_atomic,
)


def test_pending_to_claimed_allowed():
    task = {"task_id": "t1", "status": "pending"}
    result = apply_transition(task, "claimed", claimed_by="agent-x")
    assert result["status"] == "claimed"
    assert result["claimed_by"] == "agent-x"


def test_in_progress_to_blocked_requires_reason():
    task = {"task_id": "t1", "status": "in_progress"}
    with pytest.raises(InvalidTransitionError, match="blocked_reason"):
        apply_transition(task, "blocked")


def test_in_progress_to_blocked_with_reason_ok():
    task = {"task_id": "t1", "status": "in_progress"}
    result = apply_transition(task, "blocked", blocked_reason="waiting on staging")
    assert result["status"] == "blocked"


def test_done_requires_dispatch_receipt():
    task = {"task_id": "t1", "status": "in_progress"}
    with pytest.raises(InvalidTransitionError, match="dispatch_receipt"):
        apply_transition(task, "done")


def test_cancel_allowed_from_any_state():
    for start in VALID_STATES - {"cancelled"}:
        task = {"task_id": "t1", "status": start}
        result = apply_transition(task, "cancelled")
        assert result["status"] == "cancelled"


def test_done_to_pending_rejected():
    task = {"task_id": "t1", "status": "done"}
    with pytest.raises(InvalidTransitionError):
        apply_transition(task, "pending")


def test_write_snapshot_atomic_survives_partial_write(tmp_path):
    path = tmp_path / "task_board.json"
    write_snapshot_atomic({"tasks": {}}, path)
    assert json.loads(path.read_text()) == {"tasks": {}}
    # tmp 檔不應殘留
    assert not path.with_suffix(".tmp").exists()
