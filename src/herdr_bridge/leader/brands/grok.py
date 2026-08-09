"""Grok CLI adapter：2026-08-09 重新查證更正——先前規格判斷「無可信資料確認等價機制，
預設按不支援處理」已過時，官方文件（docs.x.ai/build/features/hooks）確認 Grok CLI 已
支援 PreToolUse hook，schema 與 Claude Code 高度相似（stdin toolName/toolInput，
stdout decision/reason，exit 0=allow/2=deny）。改列為 ACTIVE，設定檔：
.grok/hooks/leader-harness.json + .grok/hooks/leader_harness_pretooluse.py。

已知限制：Grok 的專案層級 hooks 需要使用者先在 ~/.grok/trusted_folders.toml 信任這個
專案資料夾才會生效——這是使用者本機全域設定，herdr-bridge 不能、也不會自動修改，需要
在文件裡明確告知使用者這個手動步驟。"""
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.harness import HarnessResult, HarnessStatus


class GrokLeaderAdapter(LeaderHarnessAdapter):
    brand = "grok"

    def probe(self) -> bool:
        return True

    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        return HarnessResult(
            status=HarnessStatus.ACTIVE, brand=self.brand, adapter=self, degraded=None,
            detail=(
                "[LEADER-HARNESS] Grok PreToolUse 已註冊（全工具覆蓋，設定檔："
                ".grok/hooks/leader-harness.json）——需要使用者先在 "
                "~/.grok/trusted_folders.toml 信任這個專案資料夾，否則 project-level "
                "hooks 不會生效（Grok 官方信任機制，herdr-bridge 不自動修改此檔案）"
            ),
        )
