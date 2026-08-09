import json


def test_gate_rejection_writes_audit_with_layer_gate(tmp_path, monkeypatch):
    from herdr_bridge.audit import AuditLogger
    audit_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path=audit_path)

    from herdr_bridge.gates import GateResult
    result = GateResult(passed=False, rejected_by="G01", error_msg="缺 target")
    logger.record("system:bridge", "gate_check", layer="gate", trigger="DISPATCH",
                  passed=False, rejected_by=result.rejected_by, error_msg=result.error_msg)

    line = json.loads(audit_path.read_text().strip().splitlines()[-1])
    assert line["layer"] == "gate"
    assert line["rejected_by"] == "G01"


def test_harness_deny_writes_audit_with_layer_harness(tmp_path):
    from herdr_bridge.audit import AuditLogger
    audit_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path=audit_path)

    logger.record("system:bridge", "pre_tool_use", layer="harness", decision="deny",
                  tool="Edit", rule_id="deny-downstream-edit")

    line = json.loads(audit_path.read_text().strip().splitlines()[-1])
    assert line["layer"] == "harness"
    assert line["decision"] == "deny"


def test_jq_can_filter_by_layer(tmp_path):
    """驗證兩層寫進同一檔案後，可以用 layer 欄位區分查詢（規格 §4.6 稽核串接）。"""
    from herdr_bridge.audit import AuditLogger
    audit_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path=audit_path)
    logger.record("system:bridge", "gate_check", layer="gate")
    logger.record("system:bridge", "pre_tool_use", layer="harness")

    lines = [json.loads(l) for l in audit_path.read_text().strip().splitlines()]
    assert sum(1 for l in lines if l["layer"] == "gate") == 1
    assert sum(1 for l in lines if l["layer"] == "harness") == 1
