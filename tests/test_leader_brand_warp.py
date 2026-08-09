def test_warp_adapter_probe_returns_false():
    from herdr_bridge.leader.brands.warp import WarpLeaderAdapter
    adapter = WarpLeaderAdapter()
    assert adapter.probe() is False


def test_warp_adapter_registers_degraded_with_correct_reason():
    from herdr_bridge.leader.brands.warp import WarpLeaderAdapter
    adapter = WarpLeaderAdapter()
    result = adapter.register("/tmp/perms.json", None, "/tmp/root")
    assert result.status.value == "degraded"
    assert result.degraded.fallback_layers == ["layer_b_charter", "audit_frequency_2x"]
    assert "warp" in result.degraded.reason.lower()
