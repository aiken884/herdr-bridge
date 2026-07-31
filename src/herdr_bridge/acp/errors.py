# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""ACP exception hierarchy. Deliberately independent of `herdr_bridge.errors.HerdrBridgeError`

(design doc §3: Option A, "cram both tracks into the old API," was rejected —
pane-centric and session-centric have different ontologies, and the error
semantics of two different-facing architectures shouldn't be mixed into one
tree; otherwise callers' `except` boundaries would silently couple two
independent contracts).
"""

from __future__ import annotations

from collections.abc import Sequence


class AcpError(Exception):
    """Base class for all `herdr_bridge.acp` exceptions."""


class AcpAdapterError(AcpError):
    """The acpx subprocess exited with a nonzero exit code, or failed to start (the single `AcpxAdapter` failure point)."""

    def __init__(self, message: str, *, argv: Sequence[str] = (),
                 exit_code: int | None = None) -> None:
        super().__init__(message)
        self.argv = list(argv)
        self.exit_code = exit_code


class AcpTransportError(AcpError):
    """The NDJSON read/parse layer failed (not an "unknown event type" — those are absorbed by the tolerant reader)."""


class AcpSessionError(AcpError):
    """Session addressing failed: `NO_SESSION`, workdir rejection (ADR 0003 C1/C2), etc."""


class AcpTimeoutError(AcpError):
    """`prompt()`/`wait_done()` did not obtain a stopReason within `timeout_sec`."""


class AcpVersionError(AcpError):
    """M0-V9 version gate: the acpx<->agent `protocolVersion` handshake didn't match."""

    def __init__(self, message: str, *, expected: int, actual: int) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual
