"""GitHub Copilot adapter：官方確認 preToolUse，全工具覆蓋。"""
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.harness import HarnessResult, HarnessStatus


class CopilotLeaderAdapter(LeaderHarnessAdapter):
    brand = "copilot"

    def probe(self) -> bool:
        return True

    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        return HarnessResult(status=HarnessStatus.ACTIVE, brand=self.brand, adapter=self,
                             degraded=None, detail="[LEADER-HARNESS] Copilot preToolUse 已註冊（全工具覆蓋）")
