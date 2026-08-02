# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""S-3: SchemaStore $ref cycle protection -- A->B->A must not recurse infinitely."""

import pytest

from herdr_bridge.errors import SchemaError
from herdr_bridge.schema import SchemaStore

CYCLIC_SCHEMA = {
    "protocol": 16,
    "schema_version": 1,
    "schemas": {
        "request": {
            "oneOf": [
                {"properties": {"method": {"const": "test.op"},
                                "params": {"$ref": "#/schemas/request/$defs/A"}},
                 "required": ["method", "params"]},
            ],
            "$defs": {
                "A": {"$ref": "#/schemas/request/$defs/B"},
                "B": {"$ref": "#/schemas/request/$defs/A"},
            },
        }
    },
}

SELF_REF_SCHEMA = {
    "protocol": 16,
    "schema_version": 1,
    "schemas": {
        "request": {
            "oneOf": [
                {"properties": {"method": {"const": "self.ref"},
                                "params": {"$ref": "#/schemas/request/$defs/Self"}},
                 "required": ["method", "params"]},
            ],
            "$defs": {
                "Self": {"$ref": "#/schemas/request/$defs/Self"},
            },
        }
    },
}


@pytest.mark.timeout(5)
def test_cyclic_ref_a_b_a_raises_schema_error():
    """A cyclic A->B->A $ref raises SchemaError, doesn't hang. A short,
    explicit per-test timeout (rather than relying on the outer test-runner's
    own timeout) turns a broken cycle guard into a fast, clearly-attributed
    failure instead of a multi-minute hang."""
    store = SchemaStore.load(CYCLIC_SCHEMA)
    with pytest.raises(SchemaError, match="cyclic"):
        store.validate_request("test.op", {"x": 1})


@pytest.mark.timeout(5)
def test_self_ref_raises_schema_error():
    """A cyclic Self->Self $ref raises SchemaError. (kills _resolve
    mutmut_10: visited.add(ref)->visited.add(None), which breaks cycle
    detection entirely and hangs forever without the timeout marker above)"""
    store = SchemaStore.load(SELF_REF_SCHEMA)
    with pytest.raises(SchemaError, match="cyclic"):
        store.validate_request("self.ref", {"x": 1})


def test_non_cyclic_ref_still_works():
    """A normal, non-cyclic $ref is unaffected."""
    schema = {
        "protocol": 16, "schema_version": 1,
        "schemas": {"request": {
            "oneOf": [
                {"properties": {"method": {"const": "ok.op"},
                                "params": {"$ref": "#/schemas/request/$defs/P"}},
                 "required": ["method", "params"]},
            ],
            "$defs": {
                "P": {"type": "object",
                       "properties": {"name": {"$ref": "#/schemas/request/$defs/Str"}},
                       "required": ["name"]},
                "Str": {"type": "string"},
            },
        }},
    }
    store = SchemaStore.load(schema)
    store.validate_request("ok.op", {"name": "hello"})
