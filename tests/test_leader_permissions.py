import os

from herdr_bridge.leader.permissions import evaluate

ROOT = "/workspace/herdr-bridge"


def test_dispatch_path_allowed():
    r = evaluate("Edit", f"{ROOT}/dispatch/router.py", ROOT)
    assert r["decision"] == "allow"
    assert r["rule_id"] == "allow-dispatch-own"


def test_leader_permissions_json_itself_denied():
    r = evaluate("Edit", f"{ROOT}/config/leader-permissions.json", ROOT)
    assert r["decision"] == "deny"
    assert r["rule_id"] == "deny-self-permissions"


def test_charter_path_denied_even_though_it_is_inside_root():
    """M-A 迴歸測試：charter/** 必須在萬用規則之前被攔截，不能被 {ROOT}/** ALLOW 搶先命中。"""
    r = evaluate("Edit", f"{ROOT}/charter/command-tower-charter.md", ROOT)
    assert r["decision"] == "deny"
    assert r["rule_id"] == "deny-charter"


def test_other_config_allowed_with_confirmation_flag():
    r = evaluate("Edit", f"{ROOT}/config/other-setting.json", ROOT)
    assert r["decision"] == "allow"
    assert r.get("require_human_confirmation") is True


def test_other_own_repo_path_allowed():
    r = evaluate("Edit", f"{ROOT}/src/herdr_bridge/foo.py", ROOT)
    assert r["decision"] == "allow"
    assert r["rule_id"] == "allow-own-repo"


def test_downstream_path_denied_by_default():
    r = evaluate("Edit", "/workspace/payments-service/router.py", ROOT)
    assert r["decision"] == "deny"
    assert r["rule_id"] == "deny-downstream-edit"


def test_read_tool_not_subject_to_rules():
    r = evaluate("Read", "/workspace/payments-service/router.py", ROOT)
    assert r["decision"] == "allow"
    assert r["rule_id"] == "not-write-tool"


def test_claude_code_memory_dir_allowed_as_tui_native_infra():
    """2026-08-09 Aiken 業務決策：TUI 原生基礎設施路徑不受 deny-downstream-edit 影響。"""
    path = os.path.expanduser("~/.claude/projects/some-project/memory/note.md")
    r = evaluate("Write", path, ROOT)
    assert r["decision"] == "allow"
    assert r["rule_id"] == "allow-tui-native-infra"


def test_unrelated_downstream_path_still_denied_by_default():
    """新增的 TUI 原生基礎設施白名單不能連帶放寬真正的下游業務 repo。"""
    r = evaluate("Edit", "/workspace/payments-service/router.py", ROOT)
    assert r["decision"] == "deny"
    assert r["rule_id"] == "deny-downstream-edit"
