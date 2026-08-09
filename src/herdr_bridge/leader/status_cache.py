"""Leader Harness 狀態快取：register_leader_harness() 的結果寫成小檔案，供
PreToolUse/PostToolUse 讀取決定是否加註 degraded 標記（可觀測性用途，不影響
deny 判斷本身——讀取失敗一律 fail-open，不報錯、不阻擋）。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from herdr_bridge.leader.harness import HarnessResult

_DEFAULT_MAX_AGE_SECONDS = 8 * 3600  # 8 小時容忍窗，覆蓋一個工作日的 session 週期


def write_harness_status(path: Path, result: HarnessResult) -> None:
    payload = {
        "status": result.status.value,
        "brand": result.brand,
        "degraded_reason": result.degraded.reason if result.degraded else None,
        "checked_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def read_harness_status(path: Path, *, max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS) -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    checked_at = data.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return None
    if time.time() - checked_at > max_age_seconds:
        return None
    return data
