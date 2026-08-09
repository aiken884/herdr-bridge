"""Layer C 機械化強制檢核：GateExecutor（規格 §4.4，C4 動態載入 + H3 fail-open 語意）。"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

from herdr_bridge.audit import AuditLogger
from herdr_bridge.core.paths import resolve_project_root

_CACHE_TTL_SECONDS = 1.0

_CHECK_FUNCS = {
    "target_pane_and_agent_present": lambda ctx: bool(
        ctx.get("task", {}).get("assigned_to", {}).get("pane")
        and ctx.get("task", {}).get("assigned_to", {}).get("agent")
    ),
    "dispatch_receipt_present": lambda ctx: bool(ctx.get("task", {}).get("dispatch_receipt")),
    "blocked_reason_present": lambda ctx: bool(ctx.get("task", {}).get("blocked_reason")),
}


@dataclass
class GateResult:
    passed: bool
    rejected_by: str | None = None
    error_msg: str | None = None


def resolve_gates_json_path() -> Path | None:
    """C4 路徑解析順序：環境變數 → project_root/.herdr → XDG_CONFIG_HOME。

    2026-08-09 移除了原本第三順位、寫死子目錄名稱的猜測式 fallback：沒有任何
    bootstrap 腳本依賴那個自動探測，而 HERDR_GATES_JSON（優先權最高、完全通用）
    已經能達成同樣效果，只是需要明確設定而非自動猜路徑。移除後 private/public
    兩邊程式碼維持完全一致，不產生分岔；需要從其他來源讀取 gates.json 時，改為
    在部署環境明確設定 HERDR_GATES_JSON 指向該檔案。"""
    env_val = os.environ.get("HERDR_GATES_JSON")
    if env_val and Path(env_val).exists():
        return Path(env_val)

    root = resolve_project_root()
    for candidate in (
        root / ".herdr" / "gates.json",
        Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "herdr" / "gates.json",
    ):
        if candidate.exists():
            return candidate
    return None


class GateExecutor:
    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float | None = None
        self._cache_loaded_at: float = 0.0
        self._parse_call_count = 0

    def _load_gates(self) -> list[dict[str, Any]]:
        path = resolve_gates_json_path()
        if path is None:
            return []  # H3：找不到檔案 → fail-open（等同無 gates）

        now = time.monotonic()
        mtime = path.stat().st_mtime
        if self._cache is not None and now - self._cache_loaded_at < _CACHE_TTL_SECONDS and mtime == self._cache_mtime:
            cached_gates: list[dict[str, Any]] = self._cache["gates"]
            return cached_gates

        self._parse_call_count += 1
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _logger.warning("gates.json 讀取/解析失敗（%s），fail-open 視同無 gates：%s", path, e)
            return []  # H3：格式錯誤/讀取失敗 → fail-open（等同無 gates），不讓派工流程崩潰

        self._cache = data
        self._cache_mtime = mtime
        self._cache_loaded_at = now
        gates: list[dict[str, Any]] = data.get("gates", [])
        return gates

    def check(self, trigger: str, context: dict[str, Any]) -> GateResult:
        gates = self._load_gates()
        for gate in gates:
            if gate["trigger"] != trigger:
                continue
            check_fn = _CHECK_FUNCS.get(gate["check"])
            if check_fn is None:
                continue
            if not check_fn(context):
                AuditLogger().record(
                    "system:bridge", "gate_check", layer="gate",
                    trigger=trigger, rejected_by=gate["id"], error_msg=gate["error_msg"],
                )
                return GateResult(passed=False, rejected_by=gate["id"], error_msg=gate["error_msg"])
        return GateResult(passed=True)
