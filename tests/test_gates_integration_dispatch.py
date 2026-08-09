import json


def test_dispatch_rejected_when_gate_fails(monkeypatch, tmp_path):
    gates_path = tmp_path / "gates.json"
    gates_path.write_text(json.dumps({
        "gates": [{"id": "G01", "trigger": "DISPATCH", "check": "target_pane_and_agent_present",
                   "action_on_fail": "reject", "error_msg": "G01 FAIL: 缺 target"}]
    }))
    monkeypatch.setenv("HERDR_GATES_JSON", str(gates_path))

    from herdr_bridge.acp.router import create_herdr_router
    router = create_herdr_router(project="test-gate-integration", start_fleet_listener=False)

    # target=None 模擬缺 target 的派工請求
    result = router.dispatch_with_memory_confirm("do something", target=None, pane_id=None)
    assert result.get("ok") is False
    assert result.get("gate_rejected") is True
    assert "G01" in result.get("error", "")
