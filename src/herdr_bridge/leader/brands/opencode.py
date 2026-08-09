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
                             detail="[LEADER-HARNESS] opencode tool.execute.before 已註冊"
                                    "（plugin：.opencode/plugins/leader-harness.js，"
                                    "拒絕訊息可見性為 best-effort——throw Error 是否被 "
                                    "LLM 讀到官方未保證）")
