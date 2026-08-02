# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Exception hierarchy. Layers outside Bridge Actions express failure via exceptions."""

from __future__ import annotations


class HerdrBridgeError(Exception):
    """Base class for all herdr-bridge exceptions."""


class HerdrConnectionError(HerdrBridgeError):
    """Could not connect to the herdr socket, or the connection dropped unexpectedly."""


class HerdrTimeoutError(HerdrBridgeError):
    """The request received no response within the timeout."""


class HerdrApiError(HerdrBridgeError):
    """herdr replied with an error envelope: {"error": {"code", "message"}}."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AgentNotFoundError(HerdrApiError):
    """The target agent/pane doesn't exist (code: pane_not_found / agent_not_found, etc.)."""


class SchemaVersionError(HerdrBridgeError):
    """The server's protocol/schema version is outside what this library supports."""


class SchemaError(HerdrBridgeError):
    """The schema definition itself is malformed (e.g. a cyclic $ref) — not a request validation failure."""


class ControlLeaseError(HerdrBridgeError):
    """acquire_control conflict: that pane already has an unreleased control lease."""


class HerdrMemoryError(HerdrBridgeError):
    """Herdr Bridge Memory (the embedded memory backend) failed to store, recall,
    or initialize. Raised with `from e` so the original underlying exception stays
    on `__cause__` for logs/-v; the default CLI print path shows only str(exc).
    """


class DeliveryStateWriteFailed(HerdrBridgeError):
    """update_delivery_state() wrote a value, but its self-verification read-back
    couldn't find the state it had just written.

    Background (2026-07-25 #71, hard-won evidence): store_memory() always returns
    gracefully as a dict for rejected/error cases instead of raising, so if the
    caller (update_delivery_state) doesn't check the returned status, it can
    mistakenly assume the write succeeded — in practice the most common cause is
    that the summary f"delivery_state={new_state}" is too short for most state
    names to meet RemaGraph arbitration rule #1's 30-character threshold, so it
    gets rejected. This exception keeps "write failed" from being a silent success,
    and distinguishes it from a plain call-order mistake in _validate_transition(),
    so debugging doesn't get misdirected toward "the caller's logic is wrong".
    """


class SignalEnvelopeError(HerdrBridgeError):
    """A Signal wake envelope failed verification (bad HMAC, expired timestamp,
    replayed nonce, or malformed fields). The daemon drops the packet silently
    (no ACK) on this error — see docs/herdr-bridge-signal-design.md §3.3a.
    """


class SignalStateWriteFailed(HerdrBridgeError):
    """A Signal ACK-state write (mark_injected/mark_seen/mark_accepted_for_work/
    mark_completed) failed its self-verification read-back — mirrors
    DeliveryStateWriteFailed's rationale for the Signal state machine.
    """
