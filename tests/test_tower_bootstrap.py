import time


def test_boot_packet_includes_task_board_when_snapshot_exists(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic({"tasks": {"t1": {"task_id": "t1", "status": "in_progress"}}}, board_path)
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.tower.bootstrap._task_pool_events_from_remagraph", list)

    from herdr_bridge.tower.bootstrap import build_boot_packet
    packet = build_boot_packet()
    assert "t1" in packet
    assert "in_progress" in packet
    assert "TOWER-CONTEXT" not in packet  # 正常路徑不應出現降級標記


def test_boot_packet_includes_blocked_reason_for_blocked_task(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic(
        {"tasks": {"t2": {"task_id": "t2", "status": "blocked", "blocked_reason": "等待 API key 核發"}}},
        board_path,
    )
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.tower.bootstrap._task_pool_events_from_remagraph", list)

    from herdr_bridge.tower.bootstrap import build_boot_packet
    packet = build_boot_packet()
    assert "等待 API key 核發" in packet


def test_boot_packet_includes_dispatch_receipt_for_done_task(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic(
        {"tasks": {"t3": {"task_id": "t3", "status": "done", "dispatch_receipt": "commit abc123"}}},
        board_path,
    )
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.tower.bootstrap._task_pool_events_from_remagraph", list)

    from herdr_bridge.tower.bootstrap import build_boot_packet
    packet = build_boot_packet()
    assert "commit abc123" in packet


def test_boot_packet_fallback_a_when_no_snapshot(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: tmp_path / "does-not-exist.json")

    from herdr_bridge.tower.bootstrap import build_boot_packet
    packet = build_boot_packet()
    assert "no-snapshot" in packet
    assert "Assume: no tasks are currently claimed" in packet


def test_boot_packet_fallback_b_when_snapshot_corrupt(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    board_path.write_text("{not valid json")
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)

    from herdr_bridge.tower.bootstrap import build_boot_packet
    packet = build_boot_packet()
    assert "snapshot-corrupt" in packet
    assert "Treat all task states as UNKNOWN" in packet
    assert board_path.with_suffix(".json.corrupt") is not None  # 損毀檔應被改名，見下方 step3 實作


def test_boot_packet_fallback_c_when_refresh_times_out(tmp_path, monkeypatch):
    from herdr_bridge.orchestration import task_board as tb
    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic({"tasks": {}}, board_path)
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)

    def slow_refresh(*a, **k):
        time.sleep(11)  # 超過 10 秒子步驟 timeout
    monkeypatch.setattr("herdr_bridge.tower.bootstrap._refresh_from_remagraph", slow_refresh)

    from herdr_bridge.tower.bootstrap import build_boot_packet
    packet = build_boot_packet()
    assert "snapshot-stale" in packet


def test_boot_packet_fallback_c_real_stall_via_lock_contention(tmp_path, monkeypatch):
    """S3 強制待辦（實作計畫審查，2026-08-08）：上面那個 fallback_c 測試整個
    monkeypatch 掉 _refresh_from_remagraph 本身，只驗證『executor 逾時機制』，沒有驗證
    真實實作接上 RemaGraph 之後、在真實環境下真的會卡住。這裡改用真實 fcntl 鎖競爭
    （比照 test_task_board_projector.py 既有的 lock-contention 測試手法）製造一個
    _refresh_from_remagraph 真的會卡住的情境：只 mock 掉會打真實網路的
    _task_pool_events_from_remagraph（回傳空事件源，CI 不應依賴真的 RemaGraph 連線），
    但 _refresh_from_remagraph 本身、reconcile_snapshot_from_events()、以及它裡面真正的
    fcntl.flock(LOCK_EX) 全部是真實程式碼路徑，沒有被 monkeypatch 掉——鎖是被另一個真實
    thread 真的持有超過 10 秒，逼真正的程式碼真的卡住，不是模擬。"""
    import fcntl
    import threading

    from herdr_bridge.orchestration import task_board as tb

    board_path = tmp_path / "task_board.json"
    tb.write_snapshot_atomic({"tasks": {"t1": {"task_id": "t1", "status": "pending"}}}, board_path)
    monkeypatch.setattr(tb, "default_snapshot_path", lambda: board_path)
    monkeypatch.setattr("herdr_bridge.tower.bootstrap._task_pool_events_from_remagraph", list)

    lock_path = tb.task_board_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = threading.Event()
    release = threading.Event()

    def holder():
        fd = open(lock_path, "w")  # noqa: SIM115 -- 鎖需跨越整個 holder() 執行期存活
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.set()
        release.wait(timeout=15)  # 至少撐過子步驟的 10 秒 timeout
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()

    t = threading.Thread(target=holder)
    t.start()
    held.wait(timeout=2)

    try:
        from herdr_bridge.tower.bootstrap import build_boot_packet
        start = time.monotonic()
        packet = build_boot_packet()
        elapsed = time.monotonic() - start
    finally:
        release.set()
        t.join(timeout=2)

    assert "snapshot-stale" in packet
    assert elapsed >= 10  # 真的卡滿 10 秒逾時才降級，不是提早判斷


def test_task_pool_events_from_remagraph_parses_real_record_shape(monkeypatch):
    """比照 BT-04 修復後真實查到的 RemaGraph 紀錄形狀（見
    command-tower-leader-bt-04 稽核紀錄）：task_id 是真實欄位、learnings[0] 固定格式
    'task_pool transition: {task_id} -> {new_status}'，兩者都要能正確轉成
    reconcile_snapshot_from_events() 看得懂的 task_status_changed 事件。"""
    from herdr_bridge.tower import bootstrap as boot

    fake_records = [
        {
            "task_id": "TASK-201", "agent_id": "worker-a", "kind": "status_update",
            "timestamp": "2026-08-09T04:46:47Z",
            "learnings": ["task_pool transition: TASK-201 -> claimed"],
            "tags": ["task-pool", "claim"],
        },
        # 不符合固定格式的紀錄應該被忽略，不能讓 parser 崩潰或亂猜
        {
            "task_id": "TASK-999", "agent_id": "someone", "kind": "status_update",
            "timestamp": "2026-08-09T04:00:00Z",
            "learnings": ["不是我們寫的格式，來自別的用途"],
            "tags": ["task-pool", "claim"],
        },
    ]
    monkeypatch.setattr(boot, "search_memories", lambda **kw: fake_records)

    events = boot._task_pool_events_from_remagraph()
    assert events == [{
        "kind": "task_status_changed", "task_id": "TASK-201", "new_status": "claimed",
        "ts": "2026-08-09T04:46:47Z", "claimed_by": "worker-a",
    }]


def test_resolve_bootstrap_project_id_priority(monkeypatch):
    from herdr_bridge.tower import bootstrap as boot

    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)
    monkeypatch.setattr(boot, "resolve_project_root", lambda: __import__("pathlib").Path("/x/fallback-name"))
    assert boot._resolve_bootstrap_project_id() == "fallback-name"

    monkeypatch.setenv("REMAGRAPH_PROJECT", "from-remagraph-env")
    assert boot._resolve_bootstrap_project_id() == "from-remagraph-env"

    monkeypatch.setenv("HERDR_MEMORY_PROJECT", "from-memory-env")
    assert boot._resolve_bootstrap_project_id() == "from-memory-env"  # 優先序最高
