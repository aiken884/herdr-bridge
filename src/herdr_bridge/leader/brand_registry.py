from __future__ import annotations

from herdr_bridge.leader.base import LeaderHarnessAdapter

_REGISTRY: dict[str, str] = {
    "claude": "herdr_bridge.leader.brands.claude:ClaudeLeaderAdapter",
    "codex": "herdr_bridge.leader.brands.codex:CodexLeaderAdapter",
    "copilot": "herdr_bridge.leader.brands.copilot:CopilotLeaderAdapter",
    "gemini": "herdr_bridge.leader.brands.gemini:GeminiLeaderAdapter",
    "opencode": "herdr_bridge.leader.brands.opencode:OpenCodeLeaderAdapter",
    "grok": "herdr_bridge.leader.brands.grok:GrokLeaderAdapter",
    "warp": "herdr_bridge.leader.brands.warp:WarpLeaderAdapter",  # 2026-08-08 追加第七品牌
}
_REGISTRY_INSTANCES: dict[str, LeaderHarnessAdapter] = {}  # 測試專用覆蓋點


def resolve_adapter(brand: str) -> LeaderHarnessAdapter | None:
    key = brand.lower()
    if key in _REGISTRY_INSTANCES:
        return _REGISTRY_INSTANCES[key]
    entry = _REGISTRY.get(key)
    if entry is None:
        return None
    import importlib
    module_path, class_name = entry.rsplit(":", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    instance: LeaderHarnessAdapter = cls()
    return instance
