import json
import os
import subprocess
import sys


def test_session_start_writes_harness_status(tmp_path):
    root = tmp_path / "herdr-bridge"
    (root / ".herdr").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")

    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(root)
    env["HERDR_LEADER_BRAND"] = "claude"
    env["HERDR_MEMORY_MODE"] = "off"
    env.pop("HERDR_TOWER_PROMPT", None)
    env.pop("HERDR_ORG_ROOT", None)

    result = subprocess.run(
        [sys.executable, ".claude/hooks/session_start_compact.py"],
        input="{}", capture_output=True, text=True, timeout=10, env=env, check=False,
    )
    assert result.returncode == 0
    status_path = root / ".herdr" / "harness_status.json"
    assert status_path.exists()
    data = json.loads(status_path.read_text())
    assert data["brand"] == "claude"
    assert data["status"] in ("active", "degraded")


def test_session_start_warns_when_brand_unset(tmp_path):
    root = tmp_path / "herdr-bridge"
    (root / ".herdr").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")

    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(root)
    env["HERDR_MEMORY_MODE"] = "off"
    env.pop("HERDR_LEADER_BRAND", None)
    env.pop("HERDR_TOWER_PROMPT", None)
    env.pop("HERDR_ORG_ROOT", None)

    result = subprocess.run(
        [sys.executable, ".claude/hooks/session_start_compact.py"],
        input="{}", capture_output=True, text=True, timeout=10, env=env, check=False,
    )
    assert result.returncode == 0
    assert "HERDR_LEADER_BRAND" in result.stdout
    assert not (root / ".herdr" / "harness_status.json").exists()
