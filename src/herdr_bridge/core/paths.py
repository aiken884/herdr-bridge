"""project_root 唯一解析入口（規格 §4.0 O-1 最終定案）。

所有其他模組一律從這裡 import；禁止各自實作 project_root 解析邏輯。
HERDR_BRIDGE_ROOT（Leader Harness 專屬錨點）不併入這裡——語意不同，見規格 §4.6。
"""
from __future__ import annotations

import logging
import os
import tomllib
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("herdr_bridge.core.paths")


def _find_herdr_config_toml(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        config = candidate / ".herdr" / "config.toml"
        if config.exists():
            return config
    return None


def _walk_up_for_herdr_dir(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".herdr").is_dir():
            return candidate
    return None


@lru_cache(maxsize=1)
def resolve_project_root() -> Path:
    """優先序：HERDR_PROJECT_ROOT 環境變數 → .herdr/config.toml 的
    project_root 欄位 → 往上找含 .herdr/ 的父目錄 → CWD（記錄 WARNING）。
    """
    env_val = os.environ.get("HERDR_PROJECT_ROOT")
    if env_val:
        return Path(env_val).resolve()

    config_toml = _find_herdr_config_toml(Path.cwd())
    if config_toml is not None:
        with open(config_toml, "rb") as f:
            cfg = tomllib.load(f)
        raw = cfg.get("project_root")
        if isinstance(raw, str) and raw:
            resolved: Path = (config_toml.parent.parent / raw).resolve()
            return resolved

    candidate = _walk_up_for_herdr_dir(Path.cwd())
    if candidate is not None:
        return candidate

    logger.warning(
        "project_root 無法自動解析，fallback 至 CWD=%s；建議設定 HERDR_PROJECT_ROOT 環境變數。",
        Path.cwd(),
    )
    return Path.cwd().resolve()
