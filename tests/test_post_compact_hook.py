import json
import subprocess
import sys


def test_post_compact_hook_writes_one_log_line(tmp_path, monkeypatch):
    log_path = tmp_path / "compact.log"
    monkeypatch.setenv("HERDR_COMPACT_LOG_PATH", str(log_path))

    payload = json.dumps({"session_id": "sess-123", "compaction_reason": "auto"})
    result = subprocess.run(
        [sys.executable, ".claude/hooks/post_compact_log.py"],
        input=payload, capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode == 0
    content = log_path.read_text()
    assert "[PostCompact]" in content
    assert "sess-123" in content
