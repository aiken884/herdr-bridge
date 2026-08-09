import json
import subprocess
import sys


def test_deny_hook_exits_zero_with_json_stdout(tmp_path, monkeypatch):
    """R-6 最終定案：deny 必須是 exit code 0 + stdout JSON，不是 exit code 2。"""
    root = tmp_path / "herdr-bridge"
    root.mkdir()
    (root / ".herdr").mkdir()
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")
    downstream = tmp_path / "other-service" / "router.py"
    downstream.parent.mkdir()
    downstream.write_text("original")

    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(downstream)}})
    env = {"HERDR_BRIDGE_ROOT": str(root), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, ".claude/hooks/leader_harness_pretooluse.py"],
        input=payload, capture_output=True, text=True, timeout=5, env=env, check=False,
    )
    assert result.returncode == 0  # 不是 2
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "hookEventName" in output["hookSpecificOutput"]
    assert "permissionDecisionReason" in output["hookSpecificOutput"]


def test_allow_own_repo_hook_exits_zero_no_deny(tmp_path):
    root = tmp_path / "herdr-bridge"
    root.mkdir()
    (root / ".herdr").mkdir()
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")
    own_file = root / "src" / "foo.py"
    own_file.parent.mkdir(parents=True)
    own_file.write_text("x")

    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(own_file)}})
    env = {"HERDR_BRIDGE_ROOT": str(root), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, ".claude/hooks/leader_harness_pretooluse.py"],
        input=payload, capture_output=True, text=True, timeout=5, env=env, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""  # allow：無 deny JSON 輸出


def test_detect_claude_version_parses_semver():
    from herdr_bridge.leader.brands.claude import _parse_version_string
    assert _parse_version_string("1.0.51 (Claude Code)") == (1, 0, 51)
    assert _parse_version_string("garbage output") is None


def test_register_degrades_when_version_below_fix(monkeypatch):
    from herdr_bridge.leader.brands.claude import ClaudeLeaderAdapter
    monkeypatch.setattr(
        "herdr_bridge.leader.brands.claude.detect_claude_version", lambda: (1, 0, 10)
    )
    adapter = ClaudeLeaderAdapter()
    result = adapter.register("/tmp/perms.json", None, "/tmp/root")
    from herdr_bridge.leader.harness import HarnessStatus
    assert result.status == HarnessStatus.DEGRADED
    assert "1.0.10" in result.detail or "版本" in result.detail
