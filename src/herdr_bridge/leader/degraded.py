from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class DegradedHarnessState:
    brand: str
    reason: str
    fallback_layers: list[str]
    degraded_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def for_unknown_brand(cls, brand: str) -> DegradedHarnessState:
        return cls(brand=brand, reason=f"brand={brand!r} 未在 brand_registry 中登錄",
                   fallback_layers=["layer_b_charter", "audit_frequency_2x"])

    @classmethod
    def for_limited_hook(cls, brand: str, limitation: str) -> DegradedHarnessState:
        return cls(brand=brand, reason=f"brand={brand!r} hook 能力受限：{limitation}",
                   fallback_layers=["layer_b_charter", "audit_frequency_2x"])
