#!/usr/bin/env python3
"""Grok CLI PreToolUse hook：轉譯 Grok 的 stdin/stdout JSON schema，判斷邏輯委派給
herdr_bridge.leader.permissions.evaluate()。2026-08-09 補充：先前規格判斷 Grok「無可信
資料確認等價機制，預設按不支援處理」，重新查證後發現官方已支援 PreToolUse hook（stdin
toolName/toolInput、stdout decision/reason、exit 0=allow/2=deny），schema 幾乎與
Claude Code 相同（官方文件確認 Bash/Read/Write/Edit 等工具名稱直接對應 Claude Code 命名）。
toolInput 裡檔案路徑欄位名稱（file_path）採官方「與 Claude Code 工具名稱自動對應」的說法
推論，未見官方逐工具列出完整 toolInput schema，屬 best-effort，如有出入以實測為準。"""
import json
import os
import sys


def main() -> None:
    event = json.load(sys.stdin)
    tool_name = event.get("toolName", "")
    file_path = event.get("toolInput", {}).get("file_path", "")

    write_tools = {"Write", "Edit", "MultiEdit"}
    if tool_name not in write_tools:
        sys.exit(0)

    herdr_bridge_root = os.environ.get("HERDR_BRIDGE_ROOT")
    if not herdr_bridge_root:
        _emit_deny("HARNESS-ENV-ERROR: HERDR_BRIDGE_ROOT 未設定，拒絕所有寫入操作。")
        return

    from herdr_bridge.leader.permissions import evaluate
    rule = evaluate("Edit", file_path, herdr_bridge_root)

    if rule["decision"] == "deny":
        reason = rule.get("reason") or f"[rule: {rule['rule_id']}] 此操作被 Leader Harness 拒絕"
        _emit_deny(reason)
    else:
        sys.exit(0)


def _emit_deny(reason: str) -> None:
    print(json.dumps({"decision": "deny", "reason": reason}))
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[leader_harness] hook internal error: {exc}")
        sys.exit(1)  # 非阻擋性錯誤（Grok fail-open 語意：只有 exit 2 才算 deny）
