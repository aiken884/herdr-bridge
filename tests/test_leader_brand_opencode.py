import json
import subprocess
import sys


def test_opencode_check_deny_downstream(tmp_path):
    root = tmp_path / "herdr-bridge"
    (root / ".herdr").mkdir(parents=True)
    downstream = tmp_path / "other-service" / "router.py"
    downstream.parent.mkdir()
    downstream.write_text("original")

    payload = json.dumps({"tool": "write", "file_path": str(downstream)})
    env = {"HERDR_BRIDGE_ROOT": str(root), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, ".opencode/plugins/leader_harness_check.py"],
        input=payload, capture_output=True, text=True, timeout=5, env=env, check=False,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["decision"] == "deny"
    assert "reason" in output


def test_opencode_check_allow_own_repo(tmp_path):
    root = tmp_path / "herdr-bridge"
    own_file = root / "src" / "foo.py"
    own_file.parent.mkdir(parents=True)
    own_file.write_text("x")

    payload = json.dumps({"tool": "edit", "file_path": str(own_file)})
    env = {"HERDR_BRIDGE_ROOT": str(root), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, ".opencode/plugins/leader_harness_check.py"],
        input=payload, capture_output=True, text=True, timeout=5, env=env, check=False,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["decision"] == "allow"
