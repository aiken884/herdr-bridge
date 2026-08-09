import json
import subprocess
import sys


def test_codex_bash_guard_denies_rm_rf(tmp_path):
    root = tmp_path / "herdr-bridge"
    (root / ".herdr").mkdir(parents=True)

    payload = json.dumps({"tool_name": "shell", "tool_input": {"command": "rm -rf /other-service"}})
    env = {"HERDR_BRIDGE_ROOT": str(root), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, ".codex/hooks/leader_harness_bash_guard.py"],
        input=payload, capture_output=True, text=True, timeout=5, env=env, check=False,
    )
    assert result.returncode == 1  # Codex 用 exit 1 = 拒絕
    output = json.loads(result.stdout)
    assert output["decision"] == "block"


def test_codex_bash_guard_allows_safe_command(tmp_path):
    root = tmp_path / "herdr-bridge"
    (root / ".herdr").mkdir(parents=True)

    payload = json.dumps({"tool_name": "shell", "tool_input": {"command": "ls -la"}})
    env = {"HERDR_BRIDGE_ROOT": str(root), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, ".codex/hooks/leader_harness_bash_guard.py"],
        input=payload, capture_output=True, text=True, timeout=5, env=env, check=False,
    )
    assert result.returncode == 0


def test_probe_records_tool_name(tmp_path):
    log_path = tmp_path / "probe.jsonl"
    payload = json.dumps({"tool_name": "apply_patch", "tool_input": {}})
    env = {"HERDR_CODEX_PROBE_LOG": str(log_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, ".codex/hooks/probe_apply_patch_coverage.py"],
        input=payload, capture_output=True, text=True, timeout=5, env=env, check=False,
    )
    assert result.returncode == 0
    assert log_path.exists()
    assert "apply_patch" in log_path.read_text()
