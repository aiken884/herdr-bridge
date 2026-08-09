from herdr_bridge.leader.harness import HarnessStatus, register_leader_harness


def test_unknown_brand_returns_degraded():
    result = register_leader_harness(
        brand="totally-unknown-brand", policy_path="/tmp/x.json",
        audit_sink=None, herdr_bridge_root="/tmp/root",
    )
    assert result.status == HarnessStatus.DEGRADED
    assert result.degraded is not None
    assert "totally-unknown-brand" in result.degraded.reason


def test_known_brand_resolves_to_adapter(monkeypatch):
    from herdr_bridge.leader import brand_registry

    class FakeAdapter:
        brand = "fake"
        def register(self, *a, **k):
            from herdr_bridge.leader.harness import HarnessResult, HarnessStatus
            return HarnessResult(status=HarnessStatus.ACTIVE, brand="fake",
                                 adapter=self, degraded=None, detail="ok")
        def probe(self):
            return True

    monkeypatch.setitem(brand_registry._REGISTRY_INSTANCES, "fake", FakeAdapter())
    result = register_leader_harness(
        brand="fake", policy_path="/tmp/x.json", audit_sink=None, herdr_bridge_root="/tmp/root",
    )
    assert result.status == HarnessStatus.ACTIVE
