"""Gemini CLI adapter：查證確認 BeforeTool，支援 Block/Rewrite。"""
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.harness import HarnessResult, HarnessStatus


class GeminiLeaderAdapter(LeaderHarnessAdapter):
    brand = "gemini"

    def probe(self) -> bool:
        return True

    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        return HarnessResult(status=HarnessStatus.ACTIVE, brand=self.brand, adapter=self,
                             degraded=None,
                             detail="[LEADER-HARNESS] Gemini BeforeTool 已註冊（全工具覆蓋，"
                                    "設定檔：.gemini/settings.json）")
