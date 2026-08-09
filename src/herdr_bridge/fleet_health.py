"""艦隊健康監控：純函式判斷 task_board 快照裡的異常（規格 BT-07，2026-08-09 PPLX 共識設計）。

刻意跟排程/I/O 完全分離（PPLX 諮詢明確支持這個判斷）：check_fleet_health() 只讀一份
task_board 快照做判斷，不碰任何 wakeup/RemaGraph/檔案；排程（自我重排 ScheduleWakeup
迴圈）是呼叫端（對話式 agent session）自己的行為模式，不屬於這個模組的職責。
filter_new_anomalies() 負責跨輪次的重複告警抑制，同樣是純函式——呼叫端自己保管
`seen` 狀態在各輪之間的存續。

對任何單筆任務資料損壞（缺 task_id、heartbeat_ts 格式錯誤、error_count 非數字、
delivered_content 型別異常）一律容錯跳過或另外標記為異常本身，不能讓一筆壞資料
拖垮整個健康檢查（監控函式的首要原則：不能自己先掛掉）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Anomaly:
    rule_code: str
    severity: str  # "warning" | "critical"
    task_id: str
    worker: str | None
    message: str
    suggested_action: str
    detail: dict[str, Any] = field(default_factory=dict)
    # 只放「真的代表惡化」的欄位，用於 filter_new_anomalies 的簽章比對；跟 detail
    # 分開是因為 detail 常包含像 heartbeat_age_seconds 這種每次呼叫都會自然變動、
    # 但不代表狀況真的惡化的欄位——混進簽章會讓抑制機制對這類規則永久失效
    # （2026-08-09 對抗式審查抓到的真實 bug）。
    signature_detail: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        return (self.task_id, self.rule_code)


def _parse_ts_aware(ts: str) -> datetime:
    """回傳 timezone-aware datetime；naive（無時區）一律視為格式不合法而非默默假設
    UTC——寧可整條規則跳過並另外標記 heartbeat_malformed，也不要憑空猜測時區。"""
    dt = datetime.fromisoformat(ts)  # Python 3.11+ 原生支援 "Z" 尾綴（requires-python>=3.11）
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime (no tzinfo): {ts!r}")
    return dt


def check_fleet_health(
    board: dict[str, Any],
    *,
    now: datetime,
    heartbeat_stale_seconds: float = 180,
    error_count_threshold: int = 3,
) -> list[Anomaly]:
    """規則對應 BT-07 三種異常，逐項獨立判斷、互不排斥（同一任務可能同時觸發多條）。"""
    anomalies: list[Anomaly] = []

    for task in board.get("tasks", {}).values():
        task_id = task.get("task_id")
        if not task_id:
            continue  # 缺 task_id 的任務資料本身就損壞，無法標記歸屬，直接跳過
        worker = task.get("claimed_by")
        status = task.get("status")

        if status in ("claimed", "in_progress"):
            heartbeat_ts = task.get("heartbeat_ts")
            if heartbeat_ts:
                try:
                    age = (now - _parse_ts_aware(heartbeat_ts)).total_seconds()
                except (ValueError, TypeError, AttributeError):
                    # AttributeError：heartbeat_ts 是非字串的 truthy 值（int/dict/list 等）
                    # 時，_parse_ts_aware 內部呼叫 .replace() 會炸；2026-08-09 第二輪對抗式
                    # 審查抓到第一輪的 (ValueError, TypeError) 沒接住這個情況。
                    anomalies.append(Anomaly(
                        rule_code="heartbeat_malformed", severity="warning",
                        task_id=task_id, worker=worker,
                        message=f"{task_id} 的 heartbeat_ts 格式無法解析：{heartbeat_ts!r}",
                        suggested_action="檢查寫入 heartbeat_ts 的來源，這筆任務暫時無法判斷是否斷線",
                        detail={"heartbeat_ts": heartbeat_ts},
                        signature_detail={"heartbeat_ts": heartbeat_ts},
                    ))
                else:
                    if age > heartbeat_stale_seconds:
                        # 斷線分級（tier）：age 每跨過一倍門檻就升一級。只用 heartbeat_ts
                        # 當簽章會讓同一個永遠沒有新回報的 worker 只告警一次、之後不管斷線
                        # 多久都被永久抑制——不符合「持續主動監控」的精神；加上 tier 之後，
                        # 斷線時間每翻倍一次（3 分鐘→6 分鐘→9 分鐘...）就會重新告警一次，
                        # 但同一個 tier 內不會每輪刷屏。
                        tier = int(age // heartbeat_stale_seconds)
                        anomalies.append(Anomaly(
                            rule_code="heartbeat_stale", severity="critical",
                            task_id=task_id, worker=worker,
                            message=f"{worker or '未知 worker'} 執行 {task_id} 已 {int(age)} 秒未回報 heartbeat（門檻 {int(heartbeat_stale_seconds)} 秒，相當於 {tier}x 門檻）",
                            suggested_action="嘗試喚醒該 worker，或重新指派給 backup worker",
                            detail={"heartbeat_age_seconds": age, "heartbeat_ts": heartbeat_ts,
                                    "threshold_seconds": heartbeat_stale_seconds, "stale_tier": tier},
                            # 簽章含 heartbeat_ts（判斷是否為同一次斷線，不是誤觸發）+
                            # stale_tier（判斷有沒有跨級惡化）；不含連續變動的原始秒數。
                            signature_detail={"heartbeat_ts": heartbeat_ts, "stale_tier": tier},
                        ))

        error_count = task.get("error_count")
        if error_count is not None:
            try:
                ec = float(error_count) if not isinstance(error_count, bool) else None
            except (TypeError, ValueError):
                ec = None
            if ec is not None and ec > error_count_threshold:
                anomalies.append(Anomaly(
                    rule_code="error_count_exceeded", severity="critical",
                    task_id=task_id, worker=worker,
                    message=f"{task_id} 錯誤次數已達 {error_count}（門檻 {error_count_threshold}）",
                    suggested_action="暫停該任務／換一個 worker 重新指派／降低優先級，交由業主決定",
                    detail={"error_count": error_count, "threshold": error_count_threshold},
                    signature_detail={"error_count": ec},
                ))

        if status == "done" and "delivered_content" in task:
            content = task.get("delivered_content")
            empty = (
                content is None
                or (isinstance(content, str) and not content.strip())
                or (isinstance(content, (list, dict)) and not content)
            )
            if empty:
                anomalies.append(Anomaly(
                    rule_code="empty_delivery", severity="critical",
                    task_id=task_id, worker=worker,
                    message=f"{task_id} 回報 done 但交付內容為空",
                    suggested_action="不接受為有效完成，要求重工或人工確認",
                    detail={"dispatch_receipt": task.get("dispatch_receipt")},
                    signature_detail={},  # 空交付本身就是一次性事實，不會有「惡化」語意
                ))

    return anomalies


def filter_new_anomalies(
    current: list[Anomaly],
    *,
    seen: dict[tuple[str, str], str],
) -> tuple[list[Anomaly], dict[tuple[str, str], str]]:
    """重複告警抑制（PPLX 建議）：同一 (task_id, rule_code) 只在首次出現或
    signature_detail 明顯變化（代表真的惡化）時才回報；signature_detail 不變就抑制，
    不論 detail 裡其他欄位（例如持續累加的秒數）怎麼變。異常消失一輪後又復發，視為
    全新事件重新告警（不會被舊的 seen 紀錄永久壓制）。

    純函式：不修改傳入的 `seen`，回傳新的 seen 供呼叫端在下一輪帶入。
    """
    new_seen: dict[tuple[str, str], str] = {}
    fresh: list[Anomaly] = []
    for anomaly in current:
        key = anomaly.key()
        signature = repr(sorted(anomaly.signature_detail.items()))
        new_seen[key] = signature
        if seen.get(key) != signature:
            fresh.append(anomaly)
    return fresh, new_seen
