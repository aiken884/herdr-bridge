"""Warp Agents CLI adapter：已查證不支援可程式化外部攔截（2026-08-08 研究結論——
Warp 的權限控管是 App 內建 UI「Agent Profiles & Permissions」，僅 shell 指令有
regex allowlist/denylist，無檔案路徑層級規則，無結構化拒絕回饋），
比照 Grok 的降級模式處理，非自創第三種規則（規格 §4.6）。"""
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.degraded import DegradedHarnessState
from herdr_bridge.leader.harness import HarnessResult, HarnessStatus


class WarpLeaderAdapter(LeaderHarnessAdapter):
    brand = "warp"

    def probe(self) -> bool:
        return False

    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        return HarnessResult(
            status=HarnessStatus.DEGRADED, brand=self.brand, adapter=self,
            degraded=DegradedHarnessState.for_unknown_brand(self.brand),
            detail=(
                "[LEADER-HARNESS] Warp 的權限控管是 App 內建 UI（Agent Profiles & "
                "Permissions），非可程式化外部攔截機制，無檔案路徑層級規則、"
                "無結構化拒絕回饋——降級模式啟動"
            ),
        )
