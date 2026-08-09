from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from herdr_bridge.leader.base import LeaderHarnessAdapter
from herdr_bridge.leader.brand_registry import resolve_adapter
from herdr_bridge.leader.degraded import DegradedHarnessState


class HarnessStatus(str, Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass(frozen=True)
class HarnessResult:
    status: HarnessStatus
    brand: str
    adapter: LeaderHarnessAdapter | None
    degraded: DegradedHarnessState | None
    detail: str


def register_leader_harness(brand: str, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
    adapter = resolve_adapter(brand)
    if adapter is None:
        return HarnessResult(
            status=HarnessStatus.DEGRADED, brand=brand, adapter=None,
            degraded=DegradedHarnessState.for_unknown_brand(brand),
            detail=f"[LEADER-HARNESS] brand={brand!r} 無對應 adapter，降級模式啟動",
        )
    return adapter.register(policy_path, audit_sink, herdr_bridge_root)
