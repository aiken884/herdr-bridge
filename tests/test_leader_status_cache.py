import json
import time

from herdr_bridge.leader.harness import HarnessResult, HarnessStatus
from herdr_bridge.leader.status_cache import read_harness_status, write_harness_status


def test_write_then_read_roundtrip(tmp_path):
    path = tmp_path / "harness_status.json"
    result = HarnessResult(status=HarnessStatus.DEGRADED, brand="claude", adapter=None,
                           degraded=None, detail="test detail")
    write_harness_status(path, result)
    data = read_harness_status(path)
    assert data is not None
    assert data["status"] == "degraded"
    assert data["brand"] == "claude"


def test_stale_status_returns_none(tmp_path):
    path = tmp_path / "harness_status.json"
    stale_ts = time.time() - 9 * 3600  # 9 小時前，超過 8 小時容忍窗
    path.write_text(json.dumps({"status": "active", "brand": "claude", "checked_at": stale_ts}))
    assert read_harness_status(path) is None


def test_missing_file_returns_none(tmp_path):
    assert read_harness_status(tmp_path / "does-not-exist.json") is None


def test_corrupt_file_returns_none(tmp_path):
    path = tmp_path / "harness_status.json"
    path.write_text("not valid json{{{")
    assert read_harness_status(path) is None
