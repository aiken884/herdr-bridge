"""leader-permissions.json 路徑規則引擎（規格 §4.6，M-A 修正後順序：精確規則必須排在
萬用規則之前，否則萬用 ALLOW 會搶先命中、讓後面的 DENY 規則形同虛設）。"""
from __future__ import annotations

import fnmatch
import os
from typing import Any

_WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}

# 各 TUI 品牌自己的原生基礎設施路徑（session 記憶、設定等）——這些不是「下游業務
# repo」，Leader 對它們的寫入（例如 Claude Code 的跨 session 記憶檔）不屬於
# deny-downstream-edit 想防的範疇（Leader 逕自修改下游程式碼），放行時不需要
# require_human_confirmation。2026-08-09 Aiken 業務決策：各 TUI 原生指令/機制不能
# 被 Harness 綁死，否則沒人要用這個專案；但 Harness 對「真正下游業務 repo」的
# deny-by-default 防線（BT-02/05/06 驗證的核心）必須維持，所以用精確 allowlist
# 而非全面放寬。之後支援其他品牌時，把該品牌的原生路徑加進這裡即可。
# 路徑樣板中的 {HOME} 於 evaluate() 呼叫當下才展開（不可在 import 時就快取展開結果
# ——HOME 在測試環境或不同執行環境下可能與 import 當時不同，快取會鎖死成錯誤值）。
_TUI_NATIVE_INFRA_PATTERNS: list[str] = [
    "{HOME}/.claude/projects/**",  # Claude Code：session 記憶/專案中繼資料
]

_RULES: list[dict[str, Any]] = [
    {"id": "allow-dispatch-own", "pattern": "{ROOT}/dispatch/**", "decision": "allow"},
    {"id": "deny-self-permissions", "pattern": "{ROOT}/config/leader-permissions.json", "decision": "deny"},
    {"id": "deny-charter", "pattern": "{ROOT}/charter/**", "decision": "deny"},
    {"id": "allow-own-config-with-confirm", "pattern": "{ROOT}/config/**", "decision": "allow",
     "require_human_confirmation": True},
    {"id": "allow-own-repo", "pattern": "{ROOT}/**", "decision": "allow"},
    *(
        {"id": "allow-tui-native-infra", "pattern": pattern, "decision": "allow"}
        for pattern in _TUI_NATIVE_INFRA_PATTERNS
    ),
]


def evaluate(tool_name: str, file_path: str, herdr_bridge_root: str) -> dict[str, Any]:
    if tool_name not in _WRITE_TOOLS:
        return {"decision": "allow", "rule_id": "not-write-tool", "reason": "非寫入類工具，不受此規則約束"}

    for rule in _RULES:
        pattern = rule["pattern"].replace("{ROOT}", herdr_bridge_root).replace("{HOME}", os.path.expanduser("~"))
        if fnmatch.fnmatch(file_path, pattern):
            result = {"decision": rule["decision"], "rule_id": rule["id"]}
            if rule.get("require_human_confirmation"):
                result["require_human_confirmation"] = True
            return result

    return {
        "decision": "deny",
        "rule_id": "deny-downstream-edit",
        "reason": f"路徑 {file_path} 不屬於 herdr-bridge 自身 repo（{herdr_bridge_root}）",
    }
