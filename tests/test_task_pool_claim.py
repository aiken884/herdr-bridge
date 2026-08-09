import json
import threading

import pytest

from herdr_bridge.orchestration import task_board as tb
from herdr_bridge.task_pool import EventLogWriteError, TaskPool


@pytest.fixture
def board_env(tmp_path, monkeypatch):
    board_path = tmp_path / "task_board.json"
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    tb.write_snapshot_atomic(
        {"tasks": {"t1": {"task_id": "t1", "status": "pending"}}}, board_path
    )
    return board_path


def test_claim_success_marks_task_claimed(board_env, monkeypatch):
    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", lambda *a, **k: None)
    pool = TaskPool(project_id="test-proj")
    assert pool.claim("t1", "agent-a") is True
    board = json.loads(board_env.read_text())
    assert board["tasks"]["t1"]["status"] == "claimed"
    assert board["tasks"]["t1"]["claimed_by"] == "agent-a"


def test_claim_already_claimed_returns_false(board_env, monkeypatch):
    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", lambda *a, **k: None)
    pool = TaskPool(project_id="test-proj")
    assert pool.claim("t1", "agent-a") is True
    assert pool.claim("t1", "agent-b") is False  # 第二個搶不到，不重試


def test_claim_nonexistent_task_returns_false(board_env, monkeypatch):
    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", lambda *a, **k: None)
    pool = TaskPool(project_id="test-proj")
    assert pool.claim("does-not-exist", "agent-a") is False


def test_claim_rolls_back_snapshot_when_event_write_fails(board_env, monkeypatch):
    def _always_fail(*a, **k):
        raise EventLogWriteError("simulated RemaGraph outage")
    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", _always_fail)

    pool = TaskPool(project_id="test-proj")
    with pytest.raises(EventLogWriteError):
        pool.claim("t1", "agent-a")

    board = json.loads(board_env.read_text())
    assert board["tasks"]["t1"]["status"] == "pending"  # 已回滾


def test_second_agent_can_claim_after_first_agents_rollback(board_env, monkeypatch):
    """S3 補強（實作計畫審查）：第一個 agent claim 成功後 event log 寫入失敗、
    觸發回滾到 pending，第二個 agent 必須能在回滾後成功接手——先前只測了
    threading 競態（event log 永遠 mock 成功），沒測「回滾後還能不能被別人搶到」。"""
    call_count = {"n": 0}

    def _fail_first_agent_only(task_id, agent_id, project_id, new_status):
        call_count["n"] += 1
        if agent_id == "agent-a":
            raise EventLogWriteError("simulated outage for agent-a only")
        # agent-b 的呼叫成功，不做事

    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", _fail_first_agent_only)

    pool = TaskPool(project_id="test-proj")
    with pytest.raises(EventLogWriteError):
        pool.claim("t1", "agent-a")  # 快照先被寫成 claimed，隨後失敗回滾成 pending

    assert pool.claim("t1", "agent-b") is True  # 回滾後 t1 應可被 agent-b 成功搶到
    board = json.loads(board_env.read_text())
    assert board["tasks"]["t1"]["status"] == "claimed"
    assert board["tasks"]["t1"]["claimed_by"] == "agent-b"


def test_two_concurrent_claims_only_one_wins(board_env, monkeypatch):
    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", lambda *a, **k: None)
    pool = TaskPool(project_id="test-proj")
    results = []

    def worker(agent_id):
        results.append(pool.claim("t1", agent_id))

    threads = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == 4


def test_append_event_satisfies_remagraph_validation_rules(board_env, monkeypatch):
    """2026-08-09 BT-04 行為驗收測試發現的迴歸測試：真實 RemaGraph 對 store_memory
    請求有兩條驗證規則，task_pool 原本兩條都沒滿足，導致 claim()/unclaim() 對任何
    task_id 都 100% 失敗（不是競態問題，是每次必然失敗）：
    - summary_too_short（remagraph/arbitration.py）：summary 需 >= 30 字元，原本
      f"task {task_id} -> {new_status}" 對短 task_id（如 "t1"/"TASK-201"）常常不夠。
    - learnings_empty（同上，規則 #2）：learnings 需至少一筆非空白元素，原本
      task_pool 完全沒傳這個參數。
    這裡只 mock store_memory 本身（不 mock 掉 _append_event_with_retry），驗證組出來
    的請求本身就滿足這兩條外部套件的驗證規則。
    """
    captured = {}

    def _fake_store_memory(task_id, agent_id, *, kind, summary, project_id, tags, learnings=None, **kw):
        captured["summary"] = summary
        captured["learnings"] = learnings
        return {"status": "stored"}

    monkeypatch.setattr("herdr_bridge.task_pool.store_memory", _fake_store_memory)

    pool = TaskPool(project_id="test-proj")
    assert pool.claim("t1", "agent-a") is True
    assert len(captured["summary"]) >= 30, (
        f"summary 只有 {len(captured['summary'])} 字元，RemaGraph 會以 "
        f"summary_too_short 拒收：{captured['summary']!r}"
    )
    assert captured["learnings"] and any(s.strip() for s in captured["learnings"]), (
        f"learnings 是空的，RemaGraph 會以 learnings_empty 拒收：{captured['learnings']!r}"
    )


def test_unclaim_returns_task_to_pending(board_env, monkeypatch):
    monkeypatch.setattr("herdr_bridge.task_pool._append_event_with_retry", lambda *a, **k: None)
    pool = TaskPool(project_id="test-proj")
    pool.claim("t1", "agent-a")
    pool.unclaim("t1", "agent-a")
    board = json.loads(board_env.read_text())
    assert board["tasks"]["t1"]["status"] == "pending"
    assert board["tasks"]["t1"].get("claimed_by") is None
