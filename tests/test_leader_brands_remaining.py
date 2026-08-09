import pytest


@pytest.mark.parametrize("module_path,class_name,expected_status", [
    ("herdr_bridge.leader.brands.codex", "CodexLeaderAdapter", "active"),
    ("herdr_bridge.leader.brands.copilot", "CopilotLeaderAdapter", "active"),
    ("herdr_bridge.leader.brands.gemini", "GeminiLeaderAdapter", "active"),
    ("herdr_bridge.leader.brands.opencode", "OpenCodeLeaderAdapter", "active"),
])
def test_supported_brand_adapter_registers_active(module_path, class_name, expected_status):
    import importlib
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    adapter = cls()
    result = adapter.register("/tmp/perms.json", None, "/tmp/root")
    assert result.status.value == expected_status


def test_codex_adapter_reports_bash_only_coverage():
    from herdr_bridge.leader.brands.codex import CodexLeaderAdapter
    adapter = CodexLeaderAdapter()
    result = adapter.register("/tmp/perms.json", None, "/tmp/root")
    assert "Bash" in result.detail or "bash" in result.detail.lower()


def test_grok_adapter_degrades_probe_false():
    from herdr_bridge.leader.brands.grok import GrokLeaderAdapter
    adapter = GrokLeaderAdapter()
    assert adapter.probe() is False
    result = adapter.register("/tmp/perms.json", None, "/tmp/root")
    assert result.status.value == "degraded"
    assert result.degraded.fallback_layers == ["layer_b_charter", "audit_frequency_2x"]
