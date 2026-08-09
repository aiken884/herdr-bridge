#!/usr/bin/env python3
"""探針 hook：不做任何 allow/deny 判斷，只記錄 PreToolUse 實際觸發時收到的
tool_name，用真實環境驗證「Codex PreToolUse 是否也覆蓋 apply_patch」這個目前查證有
矛盾、無法從官方文件百分之百確認的問題。同時在 .codex/hooks.json 裡對 apply_patch
註冊這支探針——如果從未被觸發，代表官方文件裡「只覆蓋 Bash」的保守假設正確；如果被
觸發，代表這個品牌的覆蓋範圍比預期廣，需要回頭更新 leader_harness_bash_guard.py 的
覆蓋範圍判斷。"""
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

if __name__ == "__main__":
    event = json.load(sys.stdin)
    tool_name = event.get("tool_name", "unknown")
    log_path = Path(os.environ.get("HERDR_CODEX_PROBE_LOG",
                                    os.path.expanduser("~/.local/state/herdr-bridge/codex-probe.jsonl")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps({"tool_name": tool_name, "ts": datetime.now(UTC).isoformat()}) + "\n")
    sys.exit(0)
