from herdr_bridge.leader.brand_warning import build_warning_block

_BANNED_TERMS = ["治理層", "herdr-gov", "herdr_gov", "govd", "open-core", "herdr-org"]


def test_warning_block_has_no_named_leak():
    text = build_warning_block()
    for term in _BANNED_TERMS:
        assert term not in text, f"警示文字包含禁詞：{term}"
