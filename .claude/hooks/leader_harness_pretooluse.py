#!/usr/bin/env python3
"""PreToolUse hook 進入點。deny 一律 exit code 0 + stdout JSON（R-6 最終定案）。

修正說明（實作計畫審查，2026-08-08）：exit code 2（阻斷 + stderr）本身也是合法的
deny 機制，不是「只保留給崩潰」——差異在於 exit 2 只能單純 deny、無法在同一次
判斷裡動態選 allow/deny/ask/defer；本 hook 需要依路徑規則動態判斷，所以選 JSON
路徑。exit code 2 + stderr 保留給 hook 腳本自身執行時發生例外（main() 拋出未預期
的例外）這條真正不同的錯誤路徑，不是「deny 的另一種寫法」。"""
import json
import os
import sys

sys.path.insert(0, "src")


def main() -> None:
    event = json.load(sys.stdin)
    tool_name = event.get("tool_name", "")
    file_path = event.get("tool_input", {}).get("file_path", "")

    if tool_name not in ("Edit", "Write", "MultiEdit"):
        sys.exit(0)

    herdr_bridge_root = os.environ.get("HERDR_BRIDGE_ROOT")
    if not herdr_bridge_root:
        _emit_deny("HARNESS-ENV-ERROR: HERDR_BRIDGE_ROOT 未設定，拒絕所有 Edit/Write。")
        return

    from herdr_bridge.leader.permissions import evaluate
    rule = evaluate(tool_name, file_path, herdr_bridge_root)

    if rule["decision"] == "deny":
        reason = rule.get("reason") or f"[rule: {rule['rule_id']}] 此操作被 Leader Harness 拒絕"
        _emit_deny(reason)
    else:
        sys.exit(0)


def _emit_deny(reason: str) -> None:
    from herdr_bridge.audit import AuditLogger
    AuditLogger().record("system:bridge", "pre_tool_use", layer="harness",
                         decision="deny", reason=reason)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    sys.exit(0)  # 必須是 0，不是 2（R-6）


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — hook 崩潰走完全不同的例外路徑
        sys.stderr.write(f"[leader_harness] hook internal error: {exc}")
        sys.exit(2)
