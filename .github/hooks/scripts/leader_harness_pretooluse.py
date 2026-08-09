#!/usr/bin/env python3
"""GitHub Copilot CLI preToolUse hook：轉譯 Copilot 的 stdin/stdout JSON schema，
判斷邏輯完全委派給 herdr_bridge.leader.permissions.evaluate()（跟 Claude Code、
Gemini、Codex 共用同一份判斷邏輯，這裡只做 I/O 格式轉換）。"""
import json
import os
import sys


def main() -> None:
    event = json.load(sys.stdin)
    tool_name = event.get("toolName", "")
    file_path = event.get("toolArgs", {}).get("file_path", "")

    write_tools = {"edit", "write", "create"}
    if tool_name not in write_tools:
        sys.exit(0)

    herdr_bridge_root = os.environ.get("HERDR_BRIDGE_ROOT")
    if not herdr_bridge_root:
        _emit_deny("HARNESS-ENV-ERROR: HERDR_BRIDGE_ROOT 未設定，拒絕所有寫入操作。")
        return

    from herdr_bridge.leader.permissions import evaluate
    rule = evaluate("Edit", file_path, herdr_bridge_root)  # 統一映射到 Edit 語意

    if rule["decision"] == "deny":
        reason = rule.get("reason") or f"[rule: {rule['rule_id']}] 此操作被 Leader Harness 拒絕"
        _emit_deny(reason)
    else:
        sys.exit(0)


def _emit_deny(reason: str) -> None:
    output = {"permissionDecision": "deny", "permissionDecisionReason": reason}
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[leader_harness] hook internal error: {exc}")
        sys.exit(1)
