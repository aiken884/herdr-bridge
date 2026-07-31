# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import subprocess

import pytest

from herdr_bridge.client import SocketClient
from herdr_bridge.errors import SchemaVersionError
from herdr_bridge.schema import SchemaStore, check_server_compat, fetch_schema_via_cli

MINI_SCHEMA = {
    "protocol": 16,
    "schema_version": 1,
    "schemas": {
        "request": {
            "oneOf": [
                {"properties": {"method": {"const": "ping"},
                                "params": {"type": "object"}},
                 "required": ["method"]},
                {"properties": {"method": {"const": "pane.read"},
                                "params": {"$ref": "#/schemas/request/$defs/PaneReadParams"}},
                 "required": ["method", "params"]},
            ],
            "$defs": {
                "PaneReadParams": {
                    "type": "object",
                    "properties": {
                        "pane_id": {"type": "string"},
                        "source": {"$ref": "#/schemas/request/$defs/ReadSource"},
                    },
                    "required": ["pane_id", "source"],
                },
                "ReadSource": {"enum": ["visible", "recent", "recent_unwrapped", "detection"]},
            },
        }
    },
}


def test_methods_extracted():
    store = SchemaStore.load(MINI_SCHEMA)
    assert store.methods == frozenset({"ping", "pane.read"})


def test_validate_ok():
    store = SchemaStore.load(MINI_SCHEMA)
    store.validate_request("pane.read", {"pane_id": "w1:p1", "source": "recent_unwrapped"})


def test_validate_unknown_method():
    store = SchemaStore.load(MINI_SCHEMA)
    with pytest.raises(ValueError, match="unknown method"):
        store.validate_request("no.such", {})


def test_validate_missing_required():
    store = SchemaStore.load(MINI_SCHEMA)
    with pytest.raises(ValueError, match="pane_id"):
        store.validate_request("pane.read", {"source": "recent_unwrapped"})


def test_validate_bad_enum():
    store = SchemaStore.load(MINI_SCHEMA)
    with pytest.raises(ValueError, match="source"):
        store.validate_request("pane.read", {"pane_id": "w1:p1", "source": "recent-unwrapped"})


def test_server_compat_ok(fake_herdr):
    info = check_server_compat(SocketClient(fake_herdr.socket_path))
    assert info["protocol"] == 16
    assert info["protocol_compat"] == "tested"


def test_server_compat_rejects_older_protocol(fake_herdr):
    fake_herdr.set_handler(
        "ping", lambda p: {"type": "pong", "version": "0.6", "protocol": 15,
                           "capabilities": {}})
    with pytest.raises(SchemaVersionError):
        check_server_compat(SocketClient(fake_herdr.socket_path))


def test_server_compat_warns_but_allows_newer_protocol(fake_herdr, caplog):
    fake_herdr.set_handler(
        "ping", lambda p: {"type": "pong", "version": "0.9", "protocol": 18,
                           "capabilities": {}})
    import logging
    with caplog.at_level(logging.WARNING, logger="herdr_bridge.schema"):
        info = check_server_compat(SocketClient(fake_herdr.socket_path))
    assert info["protocol_compat"] == "untested"
    assert any("untested" in r.message for r in caplog.records)


ITEMS_SCHEMA = {
    "protocol": 16, "schema_version": 1,
    "schemas": {"request": {
        "oneOf": [
            {"properties": {"method": {"const": "events.subscribe"},
                            "params": {"$ref": "#/schemas/request/$defs/SubParams"}},
             "required": ["method", "params"]},
        ],
        "$defs": {
            "SubParams": {"type": "object",
                          "properties": {"subscriptions": {
                              "type": "array",
                              "items": {"$ref": "#/schemas/request/$defs/Subscription"}}},
                          "required": ["subscriptions"]},
            "Subscription": {"oneOf": [
                {"type": "object", "properties": {"type": {"const": "pane.created"}},
                 "required": ["type"]},
                {"type": "object",
                 "properties": {"type": {"const": "pane.agent_status_changed"},
                                "pane_id": {"type": "string"}},
                 "required": ["type", "pane_id"]},
            ]},
        },
    }},
}


def test_items_recursion_validates_each_element():
    """Review MINOR-4 regression: items sub-schema validates each element."""
    store = SchemaStore.load(ITEMS_SCHEMA)
    store.validate_request("events.subscribe", {"subscriptions": [
        {"type": "pane.created"},
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1"},
    ]})
    with pytest.raises(ValueError, match="no oneOf variant"):
        store.validate_request("events.subscribe", {"subscriptions": [
            {"typ": "pane.created"},  # wrong field name, matches no variant
        ]})


def test_oneof_dispatch_rejects_missing_required():
    """Review MINOR-4 regression: a oneOf variant's missing required field is rejected."""
    store = SchemaStore.load(ITEMS_SCHEMA)
    with pytest.raises(ValueError, match="no oneOf variant"):
        store.validate_request("events.subscribe", {"subscriptions": [
            {"type": "pane.agent_status_changed"},  # missing required pane_id
        ]})


# -- mutation-killing regression tests ----------------------------------------


def test_oneof_error_path_in_message():
    """When oneOf fails, the error message includes the path. (kills validate_node mutmut_13: path->None)"""
    store = SchemaStore.load(ITEMS_SCHEMA)
    with pytest.raises(ValueError) as exc:
        store.validate_request("events.subscribe", {"subscriptions": [
            {"bad": True},
        ]})
    assert "params.subscriptions[0]" in str(exc.value)


def test_oneof_error_contains_variant_details():
    """When oneOf fails, the error message contains the variant's error details. (kills validate_node mutmut_17/18: errors->None)"""
    store = SchemaStore.load(ITEMS_SCHEMA)
    with pytest.raises(ValueError) as exc:
        store.validate_request("events.subscribe", {"subscriptions": [
            {"bad": True},
        ]})
    msg = str(exc.value)
    assert "missing required field" in msg or "expected" in msg


def test_type_mismatch_error_message():
    """A type-mismatch error message includes the expected type and the actual type. (kills validate_node mutmut_51: ValueError(None))"""
    store = SchemaStore.load(MINI_SCHEMA)
    with pytest.raises(ValueError) as exc:
        store.validate_request("pane.read", {"pane_id": 123, "source": "recent_unwrapped"})
    assert "str" in str(exc.value) or "expected" in str(exc.value)


def test_method_variant_without_properties_is_valid():
    """Should not crash when a method variant has no properties key (kills validate_request mutmut_6: {}->None)."""
    no_props_schema = {
        "protocol": 16, "schema_version": 1,
        "schemas": {"request": {
            "oneOf": [
                {"properties": {"method": {"const": "noop"}},
                 "required": ["method"]},
            ],
        }},
    }
    store = SchemaStore.load(no_props_schema)
    store.validate_request("noop", {})


def test_validate_node_const_mismatch_message():
    """A const-mismatch error message includes the actual value. (kills validate_node const-related mutant)"""
    const_schema = {
        "protocol": 16, "schema_version": 1,
        "schemas": {"request": {
            "oneOf": [
                {"properties": {
                    "method": {"const": "txn"},
                    "params": {"type": "object",
                               "properties": {"action": {"const": "commit"}},
                               "required": ["action"]}},
                 "required": ["method", "params"]},
            ],
        }},
    }
    store = SchemaStore.load(const_schema)
    with pytest.raises(ValueError) as exc:
        store.validate_request("txn", {"action": "rollback"})
    assert "rollback" in str(exc.value)


def test_validate_node_int_type_matches():
    """integer type validation correctly matches int values."""
    int_schema = {
        "protocol": 16, "schema_version": 1,
        "schemas": {"request": {
            "oneOf": [
                {"properties": {
                    "method": {"const": "count"},
                    "params": {"type": "object",
                               "properties": {"n": {"type": "integer"}},
                               "required": ["n"]}},
                 "required": ["method", "params"]},
            ],
        }},
    }
    store = SchemaStore.load(int_schema)
    store.validate_request("count", {"n": 42})
    with pytest.raises(ValueError, match="str"):
        store.validate_request("count", {"n": "42"})


def test_validate_node_bool_not_int():
    """bool should not be mistaken for integer. (kills _type_matches mutmut: bool->int misjudgment)"""
    bool_schema = {
        "protocol": 16, "schema_version": 1,
        "schemas": {"request": {
            "oneOf": [
                {"properties": {
                    "method": {"const": "flag"},
                    "params": {"type": "object",
                               "properties": {"enabled": {"type": "boolean"}},
                               "required": ["enabled"]}},
                 "required": ["method", "params"]},
            ],
        }},
    }
    store = SchemaStore.load(bool_schema)
    store.validate_request("flag", {"enabled": True})
    with pytest.raises(ValueError, match="bool"):
        store.validate_request("flag", {"enabled": 1})


def test_validate_node_number_type_accepts_int_and_float_but_not_bool():
    """number type accepts int/float but excludes bool (same bool trap as the integer branch)."""
    number_schema = {
        "protocol": 16, "schema_version": 1,
        "schemas": {"request": {
            "oneOf": [
                {"properties": {
                    "method": {"const": "measure"},
                    "params": {"type": "object",
                               "properties": {"v": {"type": "number"}},
                               "required": ["v"]}},
                 "required": ["method", "params"]},
            ],
        }},
    }
    store = SchemaStore.load(number_schema)
    store.validate_request("measure", {"v": 3.14})
    store.validate_request("measure", {"v": 42})
    with pytest.raises(ValueError, match="bool"):
        store.validate_request("measure", {"v": True})


def test_fetch_schema_via_cli_parses_stdout_json(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout='{"protocol": 16, "schema_version": 1}', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_schema_via_cli("herdr")

    assert result == {"protocol": 16, "schema_version": 1}
    assert captured["argv"] == ["herdr", "api", "schema", "--json"]


def test_schema_load_skip_variant_without_const():
    """$ref dispatching: a variant without method.const should be skipped, not crash."""
    no_const_schema = {
        "protocol": 16, "schema_version": 1,
        "schemas": {"request": {
            "oneOf": [
                {"properties": {"method": {"const": "ping"}},
                 "required": ["method"]},
                {"description": "this variant has no method const",
                 "required": ["other"]},
            ],
        }},
    }
    store = SchemaStore.load(no_const_schema)
    assert store.methods == frozenset({"ping"})
