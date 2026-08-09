#!/usr/bin/env python3
"""Gemini CLI BeforeTool hook：轉譯 Gemini 的 stdin/stdout JSON schema，判斷邏輯
委派給 herdr_bridge.leader.permissions.evaluate()。"""
import json
import os
import sys


def main() -> None:
    event = json.load(sys.stdin)
    tool_name = event.get("tool_name", "")
    file_path = event.get("tool_input", {}).get("file_path", "")

    write_tools = {"write_file", "replace"}
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
    output = {"decision": "deny", "reason": reason}
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[leader_harness] hook internal error: {exc}")
        sys.exit(2)
