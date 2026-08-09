import json


def test_gate_missing_config_fails_open(tmp_path, monkeypatch):
    from herdr_bridge.gates import GateExecutor
    monkeypatch.setenv("HERDR_GATES_JSON", str(tmp_path / "does-not-exist.json"))
    executor = GateExecutor()
    result = executor.check("DISPATCH", {"task": {"assigned_to": {}}})
    assert result.passed is True  # H3：gates.json 缺失 → fail-open


def test_gate_rejects_dispatch_without_target(tmp_path, monkeypatch):
    gates_path = tmp_path / "gates.json"
    gates_path.write_text(json.dumps({
        "gates": [{
            "id": "G01", "trigger": "DISPATCH",
            "check": "target_pane_and_agent_present",
            "action_on_fail": "reject",
            "error_msg": "G01 FAIL: 派工缺少明確的 target_pane 或 agent，拒絕執行",
        }]
    }))
    monkeypatch.setenv("HERDR_GATES_JSON", str(gates_path))

    from herdr_bridge.gates import GateExecutor
    executor = GateExecutor()
    result = executor.check("DISPATCH", {"task": {"assigned_to": {}}})
    assert result.passed is False
    assert result.rejected_by == "G01"
    assert "target_pane" in result.error_msg


def test_gate_allows_dispatch_with_target(tmp_path, monkeypatch):
    gates_path = tmp_path / "gates.json"
    gates_path.write_text(json.dumps({
        "gates": [{
            "id": "G01", "trigger": "DISPATCH",
            "check": "target_pane_and_agent_present",
            "action_on_fail": "reject",
            "error_msg": "G01 FAIL",
        }]
    }))
    monkeypatch.setenv("HERDR_GATES_JSON", str(gates_path))

    from herdr_bridge.gates import GateExecutor
    executor = GateExecutor()
    result = executor.check("DISPATCH", {"task": {"assigned_to": {"pane": "p1", "agent": "a1"}}})
    assert result.passed is True


def test_gate_mtime_cache_avoids_reparse_within_one_second(tmp_path, monkeypatch):
    gates_path = tmp_path / "gates.json"
    gates_path.write_text(json.dumps({"gates": []}))
    monkeypatch.setenv("HERDR_GATES_JSON", str(gates_path))

    from herdr_bridge.gates import GateExecutor
    executor = GateExecutor()
    executor.check("DISPATCH", {"task": {}})
    call_count_before = executor._parse_call_count
    executor.check("DISPATCH", {"task": {}})  # 1 秒內第二次呼叫，應該用快取
    assert executor._parse_call_count == call_count_before


def test_gate_malformed_json_fails_open(tmp_path, monkeypatch, caplog):
    gates_path = tmp_path / "gates.json"
    gates_path.write_text("{not valid json")
    monkeypatch.setenv("HERDR_GATES_JSON", str(gates_path))

    from herdr_bridge.gates import GateExecutor
    executor = GateExecutor()
    with caplog.at_level("WARNING"):
        result = executor.check("DISPATCH", {"task": {"assigned_to": {}}})
    assert result.passed is True  # H3：gates.json 格式錯誤 → fail-open，不拋例外
    assert any("gates.json" in record.message for record in caplog.records)
