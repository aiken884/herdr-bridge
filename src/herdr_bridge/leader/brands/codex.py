"""Codex CLI adapter：有 PreToolUse，但只覆蓋 Bash（規格 §4.6 M3——不得暗示與
Claude Code 防護強度相同，file-write 類操作僅有章程自律補償）。"""
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.harness import HarnessResult, HarnessStatus


class CodexLeaderAdapter(LeaderHarnessAdapter):
    brand = "codex"

    def probe(self) -> bool:
        return True  # Bash 攔截能力視為可用；file-write 攔截缺口見下方 detail

    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        return HarnessResult(
            status=HarnessStatus.ACTIVE, brand=self.brand, adapter=self, degraded=None,
            detail=(
                "[LEADER-HARNESS] Codex PreToolUse 已註冊（設定檔：.codex/hooks.json），"
                "覆蓋範圍僅 Bash 高風險樣式偵測；file-write 類操作（apply_patch/Edit/Write）"
                "無技術攔截，僅靠章程小抄自律 + 建議使用者搭配 Codex 原生 approval_policy "
                "（workspace-write 等）補償——第二級防線，非等同 Claude Code 的技術強制（M3）"
            ),
        )
