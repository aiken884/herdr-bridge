"""OpenCode adapter：tool.execute.before（plugin 路徑），Cupcake 案例已驗證可 deny。"""
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.harness import HarnessResult, HarnessStatus


class OpenCodeLeaderAdapter(LeaderHarnessAdapter):
    brand = "opencode"

    def probe(self) -> bool:
        return True

    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        return HarnessResult(status=HarnessStatus.ACTIVE, brand=self.brand, adapter=self,
                             degraded=None,
                             detail="[LEADER-HARNESS] OpenCode tool.execute.before 已註冊（plugin 路徑）")
