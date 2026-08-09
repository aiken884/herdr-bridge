"""Claude Code brand adapter：PreToolUse hook 註冊 + deny 表達（R-6）+ 版本探測。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.degraded import DegradedHarnessState
from herdr_bridge.leader.harness import HarnessResult, HarnessStatus

# 依 anthropics/claude-code#4669 追蹤的實際修復版本填入；issue 尚未標記修復版本前，
# 保守設為極大值，代表目前所有已知版本一律不信任 deny 機制（規格 §4.6 R-6 最終定案）。
#
# ⚠️ 維護提醒（實作計畫審查 M3，2026-08-08）：這個值目前是極大值，代表
# `register()` 在本計畫完成的當下會「永遠」回傳 DEGRADED，ClaudeLeaderAdapter
# 不會出現 ACTIVE 狀態——這是刻意的保守選擇（#4669 尚未確認修復），不是 bug。
# 這個值不會自己更新；#4669 在上游修復並經本專案驗證後，需要另開一個
# maintenance task 手動把這裡改成真實修復版本號，否則 Leader Harness 會無限期
# 停留在 degraded、F4 的 PostToolUse 補償會一直是主要防線而非次要防線。
HOOK_DENY_BROKEN_MAX_VERSION = (999, 999, 999)


def _parse_version_string(raw: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not match:
        return None
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


def detect_claude_version() -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_version_string(result.stdout.strip())


class ClaudeLeaderAdapter(LeaderHarnessAdapter):
    brand = "claude"

    def probe(self) -> bool:
        return detect_claude_version() is not None

    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        version = detect_claude_version()
        if version is None:
            return HarnessResult(
                status=HarnessStatus.DEGRADED, brand=self.brand, adapter=self,
                degraded=DegradedHarnessState.for_limited_hook(self.brand, "無法執行 claude --version"),
                detail="[LEADER-HARNESS] 無法確認 hook deny 可靠性，降級模式啟動",
            )
        if version <= HOOK_DENY_BROKEN_MAX_VERSION:
            return HarnessResult(
                status=HarnessStatus.DEGRADED, brand=self.brand, adapter=self,
                degraded=DegradedHarnessState.for_limited_hook(
                    self.brand, f"claude {version} 含已知 hook deny bug (#4669)"),
                detail=f"[LEADER-HARNESS] claude 版本 {version} 未確認修復 #4669，降級模式啟動",
            )
        return HarnessResult(status=HarnessStatus.ACTIVE, brand=self.brand, adapter=self,
                             degraded=None, detail="[LEADER-HARNESS] Claude Code PreToolUse 已註冊")


_FINGERPRINT_CACHE: dict[str, dict[str, Any]] = {}  # key: tool_call_id


def record_pre_deny_fingerprint(tool_call_id: str, file_path: str) -> None:
    """deny 前記錄目標檔案的原始內容快取（規格 §4.6 R-6 第一層）。"""
    p = Path(file_path)
    if not p.exists():
        return
    content = p.read_bytes()
    if len(content) < 64 * 1024:
        _FINGERPRINT_CACHE[tool_call_id] = {
            "file_path": file_path, "mtime": p.stat().st_mtime,
            "size": len(content), "content": content,
        }
    else:
        _FINGERPRINT_CACHE[tool_call_id] = {
            "file_path": file_path, "mtime": p.stat().st_mtime, "size": len(content), "content": None,
        }


def cross_check_and_compensate(tool_call_id: str, file_path: str) -> str:
    """PostToolUse 交叉核對（規格 §4.6 R-6 第二/三層）。回傳 clean/reverted/unrecoverable。"""
    cached = _FINGERPRINT_CACHE.get(tool_call_id)
    p = Path(file_path)
    if not p.exists():
        return "clean"

    current_mtime = p.stat().st_mtime
    current_size = p.stat().st_size

    if cached is None:
        return "unrecoverable"

    if current_mtime == cached["mtime"] and current_size == cached["size"]:
        return "clean"

    # deny 顯然沒有真的擋住（bug #4669 觸發）
    if cached["content"] is not None:
        p.write_bytes(cached["content"])
        return "reverted"

    return "unrecoverable"
