from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from herdr_bridge.leader.harness import HarnessResult


class LeaderHarnessAdapter(ABC):
    brand: str

    @abstractmethod
    def register(self, policy_path: str, audit_sink: Any, herdr_bridge_root: str) -> HarnessResult:
        ...

    @abstractmethod
    def probe(self) -> bool:
        ...
