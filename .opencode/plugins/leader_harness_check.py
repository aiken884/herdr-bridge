#!/usr/bin/env python3
"""被 .opencode/plugins/leader-harness.js 用 child_process 呼叫的判斷子行程。
opencode plugin 系統是 in-process JS，無法直接 import Python 邏輯，所以用這個獨立子行程
維持跟其他品牌一致的單一判斷邏輯來源（herdr_bridge.leader.permissions.evaluate()）。
輸出永遠是 {"decision": "allow"|"deny", "reason": "..."}，不用 exit code 表達決策
（child_process 呼叫方只看 stdout）。"""
import json
import os
import sys


def main() -> None:
    event = json.load(sys.stdin)
    file_path = event.get("file_path", "")

    herdr_bridge_root = os.environ.get("HERDR_BRIDGE_ROOT")
    if not herdr_bridge_root:
        print(json.dumps({"decision": "deny",
                          "reason": "HARNESS-ENV-ERROR: HERDR_BRIDGE_ROOT 未設定"}))
        return

    from herdr_bridge.leader.permissions import evaluate
    rule = evaluate("Edit", file_path, herdr_bridge_root)

    if rule["decision"] == "deny":
        reason = rule.get("reason") or f"[rule: {rule['rule_id']}] 此操作被 Leader Harness 拒絕"
        print(json.dumps({"decision": "deny", "reason": reason}))
    else:
        print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"decision": "deny", "reason": f"hook internal error: {exc}"}))
