import pytest


@pytest.fixture(autouse=True)
def _clear_fingerprint_cache():
    """M5 修正（實作計畫審查）：_FINGERPRINT_CACHE 是模組級別字典，會跨測試污染
    （前一個測試寫入的 cache 會影響後一個測試判讀 clean/reverted/unrecoverable）。
    每個測試前後都清空，保證測試之間互不影響。"""
    from herdr_bridge.leader.brands.claude import _FINGERPRINT_CACHE
    _FINGERPRINT_CACHE.clear()
    yield
    _FINGERPRINT_CACHE.clear()


def test_cross_check_clean_when_file_unchanged(tmp_path):
    from herdr_bridge.leader.brands.claude import (
        cross_check_and_compensate,
        record_pre_deny_fingerprint,
    )
    target = tmp_path / "protected.py"
    target.write_text("original content")

    record_pre_deny_fingerprint("call-1", str(target))
    result = cross_check_and_compensate("call-1", str(target))
    assert result == "clean"


def test_cross_check_reverts_when_file_was_bypassed(tmp_path):
    from herdr_bridge.leader.brands.claude import (
        cross_check_and_compensate,
        record_pre_deny_fingerprint,
    )
    target = tmp_path / "protected.py"
    target.write_text("original content")

    record_pre_deny_fingerprint("call-2", str(target))
    target.write_text("BYPASSED — bug #4669 triggered")  # 模擬 deny 沒擋住

    result = cross_check_and_compensate("call-2", str(target))
    assert result == "reverted"
    assert target.read_text() == "original content"


def test_cross_check_unrecoverable_when_no_cached_original(tmp_path):
    from herdr_bridge.leader.brands.claude import cross_check_and_compensate
    target = tmp_path / "never-recorded.py"
    target.write_text("changed without prior record")
    result = cross_check_and_compensate("call-unknown", str(target))
    assert result == "unrecoverable"
