#!/usr/bin/env python3
"""PostToolUse hook：交叉核對 deny 是否真的生效（#4669 bug 補償，第二/三層）。"""
import json
import sys

sys.path.insert(0, "src")
from herdr_bridge.leader.brands.claude import cross_check_and_compensate  # noqa: E402

if __name__ == "__main__":
    event = json.load(sys.stdin)
    tool_call_id = event.get("tool_call_id", "")
    file_path = event.get("tool_input", {}).get("file_path", "")
    if file_path:
        outcome = cross_check_and_compensate(tool_call_id, file_path)
        if outcome == "unrecoverable":
            sys.stderr.write(
                f"[leader_harness] SECURITY_VIOLATION: deny bypass detected and unrecoverable "
                f"for {file_path}; Leader Harness switching to degraded.\n"
            )
    sys.exit(0)
