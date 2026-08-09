"""HERDR_LEADER_BRAND 未設定時的顯著警示（規格 §4.6 M4 最終定案）。"""
import os

_WARNING_TEMPLATE = """\
╔══════════════════════════════════════════════════════════╗
║  ⚠️  HERDR_LEADER_BRAND 未設定                           ║
║  指揮塔目前以「unknown brand」模式運行：                    ║
║    • 品牌專屬防護規則全部停用                              ║
║    • 以最低權限設定運行（最弱防線）                         ║
║  請設定 export HERDR_LEADER_BRAND=<品牌> 後重新啟動。       ║
║  輸入 [CONTINUE] 確認了解並接受風險，繼續啟動。              ║
╚══════════════════════════════════════════════════════════╝"""


def check_brand_env_set() -> bool:
    return bool(os.environ.get("HERDR_LEADER_BRAND", "").strip())


def build_warning_block() -> str:
    return _WARNING_TEMPLATE
