#!/usr/bin/env python3
"""SessionStart(matcher="compact") hook 進入點。stdout 內容會被 Claude Code
注入為對話上下文，不是可選讀取的檔案（規格 §4.2）。

2026-08-09 擴充：接上 register_leader_harness()（先前只有測試在呼叫，正式流程
從未觸發，degraded 狀態沒人看得到）、可選的 charter document 注入（opt-in，
未設定時靜默跳過）、HERDR_LEADER_BRAND 未設定警示（先前同樣零正式呼叫者）。"""
import os
import sys
from pathlib import Path

from herdr_bridge.audit import AuditLogger
from herdr_bridge.leader.brand_warning import build_warning_block, check_brand_env_set
from herdr_bridge.leader.harness import register_leader_harness
from herdr_bridge.leader.status_cache import write_harness_status
from herdr_bridge.tower.bootstrap import build_boot_packet
from herdr_bridge.tower.charter import read_charter_section

if __name__ == "__main__":
    herdr_bridge_root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    brand = os.environ.get("HERDR_LEADER_BRAND", "")

    sections = []

    if not check_brand_env_set():
        sections.append(build_warning_block())

    if brand:
        result = register_leader_harness(brand, policy_path="", audit_sink=None,
                                         herdr_bridge_root=herdr_bridge_root)
        AuditLogger().record("system:bridge", "harness_status", layer="harness",
                             brand=result.brand, status=result.status.value,
                             degraded_reason=result.degraded.reason if result.degraded else None)
        write_harness_status(Path(herdr_bridge_root) / ".herdr" / "harness_status.json", result)

    charter = read_charter_section()
    if charter:
        sections.append(charter)

    sections.append(build_boot_packet())
    print("\n\n".join(sections))
    sys.exit(0)
