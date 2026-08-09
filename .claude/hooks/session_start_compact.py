#!/usr/bin/env python3
"""SessionStart(matcher="compact") hook 進入點。stdout 內容會被 Claude Code
注入為對話上下文，不是可選讀取的檔案（規格 §4.2）。"""
import json
import sys

sys.path.insert(0, "src")
from herdr_bridge.tower.bootstrap import build_boot_packet  # noqa: E402

if __name__ == "__main__":
    packet = build_boot_packet()
    print(packet)
    sys.exit(0)
