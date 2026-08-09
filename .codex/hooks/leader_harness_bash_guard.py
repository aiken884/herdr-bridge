#!/usr/bin/env python3
"""Codex CLI PreToolUse hook（第二級防線，僅覆蓋 Bash/shell 工具——經查證 Codex 目前
不確定覆蓋 apply_patch/Edit/Write，保守假設不覆蓋，見 probe_apply_patch_coverage.py）。
Bash 指令本身不是「檔案路徑」，這裡只做最小化防護：偵測指令字串裡是否出現明顯高風險
樣式，無法做到跟 Edit/Write 一樣精確的路徑規則比對，file-write 類操作缺口靠 Codex
自己的 approval_policy 設定 + 章程自律補償。"""
import json
import os
import re
import sys

_DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bcurl\b.*\|\s*(sh|bash)\b"),
]


def main() -> None:
    event = json.load(sys.stdin)
    command = event.get("tool_input", {}).get("command", "")

    herdr_bridge_root = os.environ.get("HERDR_BRIDGE_ROOT")
    if not herdr_bridge_root:
        _emit_block("HARNESS-ENV-ERROR: HERDR_BRIDGE_ROOT 未設定，拒絕所有 shell 指令。")
        return

    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            _emit_block(f"[rule: bash-dangerous-pattern] 指令符合高風險樣式，拒絕執行：{pattern.pattern}")
            return

    sys.exit(0)


def _emit_block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[leader_harness] hook internal error: {exc}")
        sys.exit(1)
