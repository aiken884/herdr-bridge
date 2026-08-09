"""可選的 charter document 載入（規格 §4.3 H2，2026-08-09 改為 opt-in 語意）。

原始設計假設任何部署都一定有這份文件、找不到就該中止 SessionStart；但公開開源版的一般
使用者不會有這份文件，維持原設計會讓每個全新 clone 的使用者 SessionStart hook 直接失敗。
改為 opt-in：兩個環境變數都沒設定 → 視為「這個部署沒有 charter，正常」，不注入、不報錯；
HERDR_TOWER_PROMPT 明確設定但讀取失敗 → 使用者明確想用這個功能但設定有誤，維持硬性錯誤；
HERDR_ORG_ROOT 有設定但衍生路徑找不到 → 非阻斷，回傳 None（HERDR_ORG_ROOT 可能是為了別的
用途設定的，不代表使用者一定要有 charter）。"""
from __future__ import annotations

import os
from pathlib import Path

_CHARTER_SUBPATH = "agents/leader/tower-prompt.md"


def resolve_charter_path() -> Path | None:
    explicit = os.environ.get("HERDR_TOWER_PROMPT")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise RuntimeError(
                f"HERDR_TOWER_PROMPT 已設定為 {explicit!r}，但檔案不存在或不可讀。"
            )
        return path

    org_root = os.environ.get("HERDR_ORG_ROOT")
    if org_root:
        candidate = Path(org_root) / _CHARTER_SUBPATH
        return candidate if candidate.is_file() else None

    return None


def read_charter_section() -> str | None:
    path = resolve_charter_path()
    if path is None:
        return None
    return path.read_text()
