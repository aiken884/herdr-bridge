def test_warning_block_contains_key_phrases():
    from herdr_bridge.leader.brand_warning import build_warning_block
    text = build_warning_block()
    assert "HERDR_LEADER_BRAND" in text
    assert "unknown brand" in text
    assert "CONTINUE" in text


def test_check_brand_env_returns_true_when_set(monkeypatch):
    from herdr_bridge.leader.brand_warning import check_brand_env_set
    monkeypatch.setenv("HERDR_LEADER_BRAND", "claude")
    assert check_brand_env_set() is True


def test_check_brand_env_returns_false_when_missing(monkeypatch):
    from herdr_bridge.leader.brand_warning import check_brand_env_set
    monkeypatch.delenv("HERDR_LEADER_BRAND", raising=False)
    assert check_brand_env_set() is False
