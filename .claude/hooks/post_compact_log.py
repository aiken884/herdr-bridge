#!/usr/bin/env python3
"""PostCompact hook：只寫一行輕量 log，不做任何狀態刷新（規格 §4.2 C1 最終定案）。"""
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

if __name__ == "__main__":
    payload = json.load(sys.stdin)
    session_id = payload.get("session_id", "unknown")
    ts = datetime.now(UTC).isoformat()
    log_path = Path(os.environ.get("HERDR_COMPACT_LOG_PATH", os.path.expanduser(
        "~/.local/state/herdr-bridge/compact.log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(f"[PostCompact] session={session_id} ts={ts}\n")
    sys.exit(0)
