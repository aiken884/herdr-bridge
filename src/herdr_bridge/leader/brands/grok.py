"""Grok CLI adapter：無可信資料確認等價機制，預設按不支援處理，雙層補償
（規格 §4.6——不預設完全禁止 Grok 擔任指揮塔，只如實標記降級）。"""
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.degraded import DegradedHarnessState
from herdr_bridge.leader.harness import HarnessResult, HarnessStatus


class GrokLeaderAdapter(LeaderHarnessAdapter):
    brand = "grok"

    def probe(self) -> bool:
        return False

    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        return HarnessResult(
            status=HarnessStatus.DEGRADED, brand=self.brand, adapter=self,
            degraded=DegradedHarnessState.for_unknown_brand(self.brand),
            detail="[LEADER-HARNESS] Grok 無已驗證的工具呼叫攔截機制，降級模式啟動",
        )
