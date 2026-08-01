# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Schema-driven request validation (spec §3.6/§4.1).

**An opt-in tool, not a live guard**: `SchemaStore` / `validate_request` (including
`_validate_node`) are request-validation tools for probing/testing — they are
**not on the connect() execution path**. Don't treat this as a live safety
barrier. Live compatibility checks are the job of this module's
`check_server_compat` (the only one connect() runs). See BOUNDARIES.md for the
same boundary statement.

Validation data comes 100% from `herdr api schema --json` fetched at runtime;
this module hardcodes no method names or field assumptions.
It implements only the subset of JSON Schema that herdr's schema actually uses:
oneOf (const-dispatch) / $ref / required / enum / type.
The client's schema may be newer than the server's (environment validation notes
§3.5), so this only blocks requests that are "already known to be invalid on the
client side" — server-side rejection still defers to the server's own error.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, cast

from herdr_bridge.client import SocketClient
from herdr_bridge.errors import SchemaError, SchemaVersionError

logger = logging.getLogger("herdr_bridge.schema")

MIN_SUPPORTED_PROTOCOL = 16
MAX_TESTED_PROTOCOL = 16


def fetch_schema_via_cli(herdr_bin: str = "herdr") -> dict[str, Any]:
    out = subprocess.run(
        [herdr_bin, "api", "schema", "--json"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    # cast() is a runtime no-op (it just returns its second argument unchanged), so a
    # mutation of its first argument is an equivalent mutant mutmut can never kill.
    return cast(dict[str, Any], json.loads(out.stdout))


def check_server_compat(client: SocketClient) -> dict[str, Any]:
    """Two-tier protocol check (D8).

    Older than MIN_SUPPORTED_PROTOCOL: known incompatible, explicitly rejected.
    Newer than MAX_TESTED_PROTOCOL: untested, allowed through after a warning
    (a warning means it's never a silent downgrade).
    """
    info = dict(client.ping())
    protocol = info.get("protocol")
    if not isinstance(protocol, int) or protocol < MIN_SUPPORTED_PROTOCOL:
        raise SchemaVersionError(
            f"herdr server protocol {protocol!r} is below minimum supported "
            f"{MIN_SUPPORTED_PROTOCOL}; please upgrade herdr"
        )
    if protocol > MAX_TESTED_PROTOCOL:
        logger.warning(
            "herdr server protocol %d is newer than the last tested protocol %d; "
            "continuing in untested-compat mode — update herdr-bridge if you hit issues",
            protocol, MAX_TESTED_PROTOCOL)
        info["protocol_compat"] = "untested"
    else:
        info["protocol_compat"] = "tested"
    return info


class SchemaStore:
    def __init__(self, root: dict[str, Any], variants: dict[str, dict[str, Any]]) -> None:
        self._root = root
        self._variants = variants
        self.methods = frozenset(variants)

    @classmethod
    def load(cls, schema_json: dict[str, Any]) -> SchemaStore:
        request = schema_json["schemas"]["request"]
        variants: dict[str, dict[str, Any]] = {}
        for variant in request.get("oneOf", []):
            method = variant.get("properties", {}).get("method", {}).get("const")
            if method:
                variants[method] = variant
        return cls(root=schema_json, variants=variants)

    # -- $ref -------------------------------------------------------------
    def _resolve(self, node: dict[str, Any]) -> dict[str, Any]:
        visited: set[str] = set()
        while "$ref" in node:
            ref = node["$ref"]
            if ref in visited:
                raise SchemaError(f"cyclic $ref detected: {ref}")
            visited.add(ref)
            path = ref.removeprefix("#/").split("/")
            cur: Any = self._root
            for part in path:
                cur = cur[part]
            node = cur
        return node

    # -- validation -------------------------------------------------------
    def validate_request(self, method: str, params: dict[str, Any]) -> None:
        if method not in self._variants:
            raise ValueError(f"unknown method {method!r} (not in herdr schema)")
        params_schema = self._variants[method].get("properties", {}).get("params")
        if params_schema is None:
            return
        self._validate_node(self._resolve(params_schema), params, path="params")

    def _validate_node(self, schema: dict[str, Any], value: Any, path: str) -> None:
        schema = self._resolve(schema)
        if "oneOf" in schema:
            # every oneOf in herdr's schema is a const-dispatch discriminator; passing any one variant is enough
            errors = []
            for variant in schema["oneOf"]:
                try:
                    self._validate_node(variant, value, path)
                    return
                except ValueError as exc:
                    errors.append(str(exc))
            raise ValueError(f"{path}: no oneOf variant matched ({errors[:2]}…)")
        if "const" in schema and value != schema["const"]:
            raise ValueError(f"{path}: {value!r} != const {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path}: {value!r} not in {schema['enum']}")
        expected = schema.get("type")
        if expected:
            allowed = expected if isinstance(expected, list) else [expected]
            if not _type_matches(value, allowed):
                raise ValueError(f"{path}: expected {allowed}, got {type(value).__name__}")
        if isinstance(value, dict):
            for req_key in schema.get("required", []):
                if req_key not in value:
                    raise ValueError(f"{path}: missing required field {req_key!r}")
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    self._validate_node(sub, value[key], path=f"{path}.{key}")
        if isinstance(value, list) and "items" in schema:
            for i, item in enumerate(value):
                self._validate_node(schema["items"], item, path=f"{path}[{i}]")


_TYPE_MAP = {
    "object": dict, "array": list, "string": str,
    "boolean": bool, "null": type(None),
}


def _type_matches(value: Any, allowed: list[str]) -> bool:
    for name in allowed:
        if name == "integer":
            if isinstance(value, int) and not isinstance(value, bool):
                return True
        elif name == "number":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return True
        else:
            py = _TYPE_MAP.get(name)
            if py and isinstance(value, py):
                return True
    return False
