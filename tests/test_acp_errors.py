# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.errors: the exception hierarchy is additive, kept independent of the
existing HerdrBridgeError tree."""

from __future__ import annotations

from herdr_bridge.acp.errors import (
    AcpAdapterError,
    AcpError,
    AcpSessionError,
    AcpTimeoutError,
    AcpTransportError,
    AcpVersionError,
)


def test_acp_error_is_own_base_not_herdr_bridge_error():
    """The ACP exception tree is independent (a new contract; it does not
    inherit the existing six-function exception hierarchy)."""
    assert issubclass(AcpError, Exception)
    from herdr_bridge.errors import HerdrBridgeError
    assert not issubclass(AcpError, HerdrBridgeError)


def test_five_subclasses_inherit_acp_error():
    for cls in (AcpAdapterError, AcpTransportError, AcpSessionError,
                AcpTimeoutError, AcpVersionError):
        assert issubclass(cls, AcpError)


def test_acp_adapter_error_carries_argv_for_debugging():
    exc = AcpAdapterError("acpx exited non-zero", argv=["acpx", "--approve-all"],
                          exit_code=4)
    assert exc.exit_code == 4
    assert "acpx" in exc.argv


def test_acp_version_error_carries_versions():
    exc = AcpVersionError("protocol version mismatch",
                          expected=1, actual=2)
    assert exc.expected == 1
    assert exc.actual == 2
