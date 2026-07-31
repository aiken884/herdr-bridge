# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Consistency check between our hand-rolled subset validator and the jsonschema
package (a dev-only safety fuse).

For a set of representative requests (half valid, half invalid), the two validators'
accept/reject decisions must agree, guarding against our hand-rolled validator
silently misjudging. jsonschema exists only as a dev dependency and is never
imported at runtime.
"""

import jsonschema
import pytest

from herdr_bridge.schema import SchemaStore
from tests.test_schema import MINI_SCHEMA

CASES = [
    # (method, params, should be valid?)
    ("ping", {}, True),
    ("pane.read", {"pane_id": "w1:p1", "source": "recent_unwrapped"}, True),
    ("pane.read", {"pane_id": "w1:p1", "source": "recent-unwrapped"}, False),  # hyphen
    ("pane.read", {"source": "visible"}, False),                               # missing pane_id
    ("pane.read", {"pane_id": 42, "source": "visible"}, False),                # wrong type
]


def _jsonschema_accepts(method: str, params: dict) -> bool:
    variant = next(v for v in MINI_SCHEMA["schemas"]["request"]["oneOf"]
                   if v["properties"]["method"]["const"] == method)
    resolver = jsonschema.RefResolver(base_uri="", referrer=MINI_SCHEMA)
    try:
        jsonschema.validate({"method": method, "params": params},
                            {**variant, "$defs": MINI_SCHEMA["schemas"]["request"]["$defs"]},
                            resolver=resolver)
        return True
    except jsonschema.ValidationError:
        return False


@pytest.mark.parametrize("method,params,should_pass", CASES)
def test_validators_agree(method, params, should_pass):
    store = SchemaStore.load(MINI_SCHEMA)
    try:
        store.validate_request(method, params)
        ours = True
    except ValueError:
        ours = False
    assert ours == should_pass
    assert _jsonschema_accepts(method, params) == should_pass
