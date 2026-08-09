"""Task #32：艦隊健康監控純函式（BT-07 P1 架構缺口，PPLX 共識設計 2026-08-09）。

check_fleet_health() 只做判斷、不碰排程/I/O；filter_new_anomalies() 負責跨輪次的
重複告警抑制。排程（自我重排 ScheduleWakeup 迴圈）刻意不在這個模組裡，屬於呼叫端
（對話式 agent session 自己的行為模式），這裡只測純邏輯。
"""
from datetime import UTC, datetime, timedelta

from herdr_bridge.fleet_health import Anomaly, check_fleet_health, filter_new_anomalies

NOW = datetime(2026, 8, 9, 5, 0, 0, tzinfo=UTC)


def _iso(dt):
    return dt.isoformat()


def test_healthy_board_has_no_anomalies():
    board = {"tasks": {
        "TASK-501": {"task_id": "TASK-501", "status": "in_progress", "claimed_by": "worker-a",
                     "heartbeat_ts": _iso(NOW)},
    }}
    assert check_fleet_health(board, now=NOW) == []


def test_stale_heartbeat_flagged_for_in_progress_task():
    stale_ts = datetime(2026, 8, 9, 4, 56, 0, tzinfo=UTC)  # 4 分鐘前
    board = {"tasks": {
        "TASK-502": {"task_id": "TASK-502", "status": "in_progress", "claimed_by": "worker-b",
                     "heartbeat_ts": _iso(stale_ts)},
    }}
    anomalies = check_fleet_health(board, now=NOW, heartbeat_stale_seconds=180)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.rule_code == "heartbeat_stale"
    assert a.task_id == "TASK-502"
    assert a.worker == "worker-b"
    assert a.severity == "critical"
    assert a.detail["heartbeat_age_seconds"] >= 180


def test_fresh_heartbeat_within_threshold_not_flagged():
    fresh_ts = datetime(2026, 8, 9, 4, 59, 0, tzinfo=UTC)  # 1 分鐘前
    board = {"tasks": {
        "TASK-502": {"task_id": "TASK-502", "status": "in_progress", "claimed_by": "worker-b",
                     "heartbeat_ts": _iso(fresh_ts)},
    }}
    assert check_fleet_health(board, now=NOW, heartbeat_stale_seconds=180) == []


def test_pending_task_never_flagged_for_heartbeat():
    """還沒開始執行的任務沒有 heartbeat 語意，不該被誤判成斷線。"""
    board = {"tasks": {
        "TASK-503": {"task_id": "TASK-503", "status": "pending"},
    }}
    assert check_fleet_health(board, now=NOW) == []


def test_error_count_exceeding_threshold_flagged():
    board = {"tasks": {
        "TASK-503": {"task_id": "TASK-503", "status": "in_progress", "claimed_by": "worker-c",
                     "heartbeat_ts": _iso(NOW), "error_count": 5},
    }}
    anomalies = check_fleet_health(board, now=NOW, error_count_threshold=3)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.rule_code == "error_count_exceeded"
    assert a.task_id == "TASK-503"
    assert a.detail["error_count"] == 5
    assert a.detail["threshold"] == 3


def test_error_count_within_threshold_not_flagged():
    board = {"tasks": {
        "TASK-503": {"task_id": "TASK-503", "status": "in_progress", "claimed_by": "worker-c",
                     "heartbeat_ts": _iso(NOW), "error_count": 2},
    }}
    assert check_fleet_health(board, now=NOW, error_count_threshold=3) == []


def test_error_count_as_numeric_string_still_counted():
    """上游若把 error_count 寫成字串（例如 "5"）不該讓規則靜默失效。"""
    board = {"tasks": {
        "TASK-503": {"task_id": "TASK-503", "status": "in_progress", "claimed_by": "worker-c",
                     "heartbeat_ts": _iso(NOW), "error_count": "5"},
    }}
    anomalies = check_fleet_health(board, now=NOW, error_count_threshold=3)
    assert len(anomalies) == 1
    assert anomalies[0].rule_code == "error_count_exceeded"


def test_error_count_garbage_value_skipped_without_crash():
    board = {"tasks": {
        "TASK-503": {"task_id": "TASK-503", "status": "in_progress", "claimed_by": "worker-c",
                     "heartbeat_ts": _iso(NOW), "error_count": "not-a-number"},
    }}
    assert check_fleet_health(board, now=NOW, error_count_threshold=3) == []


def test_empty_delivery_on_done_task_flagged():
    board = {"tasks": {
        "TASK-504": {"task_id": "TASK-504", "status": "done", "claimed_by": "worker-d",
                     "dispatch_receipt": "r-504", "delivered_content": ""},
    }}
    anomalies = check_fleet_health(board, now=NOW)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.rule_code == "empty_delivery"
    assert a.task_id == "TASK-504"
    assert a.severity == "critical"


def test_done_task_with_real_content_not_flagged():
    board = {"tasks": {
        "TASK-504": {"task_id": "TASK-504", "status": "done", "claimed_by": "worker-d",
                     "dispatch_receipt": "r-504", "delivered_content": "實際交付內容"},
    }}
    assert check_fleet_health(board, now=NOW) == []


def test_done_task_without_delivered_content_field_not_flagged():
    """delivered_content 這個欄位本來就是選填——沒有這個欄位不代表空交付，
    只有『有這個欄位但是空的』才算異常。"""
    board = {"tasks": {
        "TASK-504": {"task_id": "TASK-504", "status": "done", "dispatch_receipt": "r-504"},
    }}
    assert check_fleet_health(board, now=NOW) == []


def test_delivered_content_non_string_empty_container_flagged_without_crash():
    board = {"tasks": {
        "TASK-504": {"task_id": "TASK-504", "status": "done", "dispatch_receipt": "r-504",
                     "delivered_content": []},
    }}
    anomalies = check_fleet_health(board, now=NOW)
    assert len(anomalies) == 1
    assert anomalies[0].rule_code == "empty_delivery"


def test_delivered_content_non_string_non_empty_container_not_flagged():
    board = {"tasks": {
        "TASK-504": {"task_id": "TASK-504", "status": "done", "dispatch_receipt": "r-504",
                     "delivered_content": {"files": ["a.py"]}},
    }}
    assert check_fleet_health(board, now=NOW) == []


def test_task_missing_task_id_skipped_without_crash():
    board = {"tasks": {
        "broken-entry": {"status": "in_progress", "claimed_by": "worker-x",
                          "heartbeat_ts": _iso(datetime(2026, 8, 9, 4, 0, 0, tzinfo=UTC))},
    }}
    assert check_fleet_health(board, now=NOW) == []


def test_malformed_heartbeat_ts_flagged_not_crash_and_other_tasks_still_checked():
    board = {"tasks": {
        "TASK-BAD": {"task_id": "TASK-BAD", "status": "in_progress", "claimed_by": "worker-x",
                     "heartbeat_ts": "not-a-timestamp"},
        "TASK-503": {"task_id": "TASK-503", "status": "in_progress", "claimed_by": "worker-c",
                     "heartbeat_ts": _iso(NOW), "error_count": 5},
    }}
    anomalies = check_fleet_health(board, now=NOW, error_count_threshold=3)
    codes = sorted((a.task_id, a.rule_code) for a in anomalies)
    assert codes == [("TASK-503", "error_count_exceeded"), ("TASK-BAD", "heartbeat_malformed")]


def test_non_string_heartbeat_ts_treated_as_malformed_not_crash():
    """2026-08-09 第二輪對抗式審查抓到的殘留 crash 風險：非字串的 truthy 值
    （int/dict/list）呼叫 .replace() 會 AttributeError，第一輪的 except 沒接住。"""
    for bad_value in (12345, {"not": "a-string"}, ["also", "not"]):
        board = {"tasks": {
            "TASK-BAD": {"task_id": "TASK-BAD", "status": "in_progress", "claimed_by": "worker-x",
                         "heartbeat_ts": bad_value},
        }}
        anomalies = check_fleet_health(board, now=NOW)
        assert len(anomalies) == 1
        assert anomalies[0].rule_code == "heartbeat_malformed"


def test_error_count_boolean_not_miscounted_as_one():
    """bool 是 int 子類，float(True)==1.0 不會拋例外，若不特判會被誤判成合法 error_count=1。"""
    board = {"tasks": {
        "TASK-503": {"task_id": "TASK-503", "status": "in_progress", "claimed_by": "worker-c",
                     "heartbeat_ts": _iso(NOW), "error_count": True},
    }}
    assert check_fleet_health(board, now=NOW, error_count_threshold=0) == []


def test_naive_heartbeat_ts_treated_as_malformed_not_crash():
    """沒有時區資訊的 timestamp 不能默默假設 UTC——寧可標記 malformed 也不要猜。"""
    board = {"tasks": {
        "TASK-BAD": {"task_id": "TASK-BAD", "status": "in_progress", "claimed_by": "worker-x",
                     "heartbeat_ts": "2026-08-09T04:00:00"},  # 無 tzinfo
    }}
    anomalies = check_fleet_health(board, now=NOW)
    assert len(anomalies) == 1
    assert anomalies[0].rule_code == "heartbeat_malformed"


def test_multiple_anomalies_across_tasks_all_reported():
    stale_ts = datetime(2026, 8, 9, 4, 55, 0, tzinfo=UTC)
    board = {"tasks": {
        "TASK-501": {"task_id": "TASK-501", "status": "in_progress", "claimed_by": "worker-a",
                     "heartbeat_ts": _iso(NOW)},
        "TASK-502": {"task_id": "TASK-502", "status": "in_progress", "claimed_by": "worker-b",
                     "heartbeat_ts": _iso(stale_ts)},
        "TASK-503": {"task_id": "TASK-503", "status": "in_progress", "claimed_by": "worker-c",
                     "heartbeat_ts": _iso(NOW), "error_count": 9},
        "TASK-504": {"task_id": "TASK-504", "status": "done", "claimed_by": "worker-d",
                     "dispatch_receipt": "r-504", "delivered_content": ""},
    }}
    anomalies = check_fleet_health(board, now=NOW, heartbeat_stale_seconds=180, error_count_threshold=3)
    codes = sorted((a.task_id, a.rule_code) for a in anomalies)
    assert codes == [
        ("TASK-502", "heartbeat_stale"),
        ("TASK-503", "error_count_exceeded"),
        ("TASK-504", "empty_delivery"),
    ]


def test_filter_new_anomalies_first_tick_all_new():
    a = Anomaly(rule_code="heartbeat_stale", severity="critical", task_id="T1", worker="w1",
                message="msg", suggested_action="act", detail={"heartbeat_age_seconds": 200},
                signature_detail={"heartbeat_ts": "2026-08-09T04:00:00+00:00"})
    new, seen = filter_new_anomalies([a], seen={})
    assert new == [a]
    assert ("T1", "heartbeat_stale") in seen


def test_filter_new_anomalies_suppresses_unchanged_repeat():
    """PPLX 建議：同一異常沒有惡化就不該每輪重複告警刷屏。"""
    a = Anomaly(rule_code="error_count_exceeded", severity="critical", task_id="T1", worker="w1",
                message="msg", suggested_action="act", detail={"error_count": 5, "threshold": 3},
                signature_detail={"error_count": 5})
    _, seen = filter_new_anomalies([a], seen={})
    new_again, seen2 = filter_new_anomalies([a], seen=seen)
    assert new_again == []
    assert seen2 == seen


def test_filter_new_anomalies_reports_escalation():
    """錯誤次數從 5 惡化到 8，即使 rule_code/task_id 相同，也要重新告警。"""
    a1 = Anomaly(rule_code="error_count_exceeded", severity="critical", task_id="T1", worker="w1",
                 message="msg", suggested_action="act", detail={"error_count": 5, "threshold": 3},
                 signature_detail={"error_count": 5})
    a2 = Anomaly(rule_code="error_count_exceeded", severity="critical", task_id="T1", worker="w1",
                 message="msg", suggested_action="act", detail={"error_count": 8, "threshold": 3},
                 signature_detail={"error_count": 8})
    _, seen = filter_new_anomalies([a1], seen={})
    new_again, _ = filter_new_anomalies([a2], seen=seen)
    assert new_again == [a2]


def test_filter_new_anomalies_resolved_then_recurring_is_new_again():
    """異常消失過一輪（例如任務被重派修好了），之後又重新出現，要視為全新事件重新告警。"""
    a = Anomaly(rule_code="heartbeat_stale", severity="critical", task_id="T1", worker="w1",
                message="msg", suggested_action="act", detail={"heartbeat_age_seconds": 200},
                signature_detail={"heartbeat_ts": "2026-08-09T04:00:00+00:00"})
    _, seen_tick1 = filter_new_anomalies([a], seen={})
    _, seen_tick2_resolved = filter_new_anomalies([], seen=seen_tick1)  # 這一輪沒有異常了
    assert ("T1", "heartbeat_stale") not in seen_tick2_resolved
    new_tick3, _ = filter_new_anomalies([a], seen=seen_tick2_resolved)  # 又復發
    assert new_tick3 == [a]


def test_integration_stale_heartbeat_suppressed_across_real_advancing_time():
    """2026-08-09 對抗式審查抓到的關鍵迴歸測試：同一個 stale worker，check_fleet_health()
    真的用兩個不同、往前推進的 now 呼叫兩次（模擬自我重排迴圈的兩個 tick），中間串接
    filter_new_anomalies()——heartbeat_age_seconds 兩次呼叫必然不同，但因為
    worker 沒有送出新的 heartbeat（heartbeat_ts 本身沒變），第二次一定要被抑制，
    不能因為「age 數字變大了」就誤判成新事件。這個情境如果只用寫死的 Anomaly 物件測，
    抓不到 signature 誤用 detail 而非 signature_detail 的 bug。"""
    stale_ts = NOW - timedelta(seconds=200)  # age=200s at tick1 → tier 1（門檻 180s）
    board = {"tasks": {
        "TASK-502": {"task_id": "TASK-502", "status": "in_progress", "claimed_by": "worker-b",
                     "heartbeat_ts": _iso(stale_ts)},
    }}

    tick1 = check_fleet_health(board, now=NOW, heartbeat_stale_seconds=180)
    fresh1, seen = filter_new_anomalies(tick1, seen={})
    assert len(fresh1) == 1
    assert fresh1[0].detail["stale_tier"] == 1

    now_tick2 = NOW + timedelta(seconds=90)  # age=290s，仍在 tier 1（<360s）
    tick2 = check_fleet_health(board, now=now_tick2, heartbeat_stale_seconds=180)
    fresh2, seen2 = filter_new_anomalies(tick2, seen=seen)
    assert fresh2 == []  # 沒有惡化（heartbeat_ts 沒變、tier 沒變），不該重新告警

    # 再往前推進到跨過下一個 tier（門檻的兩倍 = 360s）——同一個 worker持續斷線很久，
    # 不該被第一次的抑制永久壓制，應該在明顯惡化時重新告警。
    now_tick3 = NOW + timedelta(seconds=200)  # age=400s → tier 2
    tick3 = check_fleet_health(board, now=now_tick3, heartbeat_stale_seconds=180)
    fresh3, _ = filter_new_anomalies(tick3, seen=seen2)
    assert len(fresh3) == 1
    assert fresh3[0].detail["stale_tier"] == 2
