# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Wake envelope: the minimal control message carried over the Signal socket.

Per docs/herdr-bridge-signal-design.md §3.3: the socket only ever carries this
small control envelope — full content always stays in RemaGraph. §3.3a defines
the security model (HMAC + timestamp + nonce) this module implements.

DEPLOYMENT CONSTRAINT (2026-08-01, PPLX-reviewed adversarial-review finding):
the "shared secret" HMAC uses is not a real key-exchange result -- it is
whatever `_load_or_create_shared_secret()` (light/cli.py) finds at a path
computed from `Path.home()`, so sender and receiver only end up with the
SAME secret because they happen to run as the same OS user on the same
machine. That's fine for the current same-host, same-user deployment (the
socket and the secret file are both already 0600 -- anyone who can read the
secret can already connect to the socket directly, so HMAC isn't adding a
real authentication boundary there), but it silently breaks the moment any
tower moves to a different host: each side would generate its own
independent secret, and every verification would fail with a "bad hmac"
that looks like tampering rather than a deployment mismatch. `sender_hostname`
below exists so daemon.py can check for that mismatch BEFORE running HMAC
verification and give an honest diagnostic instead. See
`_load_or_create_shared_secret()`'s docstring for the same-user assertion
this pairs with. Real cross-host key distribution is intentionally out of
scope for now (PPLX consensus 2026-08-01): the recommended future approach
is an operator-driven pre-shared-key exchange (fingerprint-verified, SSH
known_hosts-style), not automatic key agreement.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import json
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field

from herdr_bridge.errors import SignalEnvelopeError

#: §3.3a: packets older (or newer, clock skew) than this are rejected outright.
DEFAULT_TTL_SECONDS = 60.0


def idempotency_key(to_project: str, inbox_ref: str) -> str:
    """§2.4 defect 2: hash only the stable fields (target + RemaGraph reference).

    Deliberately excludes message_id/timestamp/nonce (those vary per send attempt
    and belong to replay protection, a different concern — see §3.3a). Two sends
    for the same (to_project, inbox_ref), no matter how far apart in time, collapse
    to the same idempotency_key so the receiver can dedupe repeat processing.
    """
    return hashlib.sha256(f"{to_project}:{inbox_ref}".encode()).hexdigest()


@dataclass(frozen=True)
class Envelope:
    """The wake packet sent over the Signal socket. See §3.3 for the field table."""

    from_project: str
    to_project: str
    inbox_ref: str
    kind: str
    sender_id: str
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requires_processed_ack: bool = True
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    #: See this module's DEPLOYMENT CONSTRAINT docstring. Not a security
    #: control (a forged value can't gain anything an attacker who can
    #: already reach this socket doesn't have) -- purely a diagnostic so
    #: daemon.py can report "you're on a different host" instead of a
    #: confusing "bad hmac" when the shared-secret-by-coincidence breaks.
    sender_hostname: str = field(default_factory=socket.gethostname)
    hmac: str = ""

    @property
    def idempotency_key(self) -> str:
        return idempotency_key(self.to_project, self.inbox_ref)

    def _signing_payload(self) -> bytes:
        """Canonical (sorted-key, no-whitespace) JSON of every field except `hmac`
        itself — sign() and verify() must serialize identically or verification
        would fail for reasons unrelated to tampering.
        """
        data = asdict(self)
        data.pop("hmac", None)
        data["idempotency_key"] = self.idempotency_key
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    def signed(self, shared_secret: str) -> Envelope:
        """Return a copy with `hmac` computed over every other field."""
        mac = hmac_lib.new(shared_secret.encode(), self._signing_payload(), hashlib.sha256).hexdigest()
        return Envelope(
            from_project=self.from_project, to_project=self.to_project,
            inbox_ref=self.inbox_ref, kind=self.kind, sender_id=self.sender_id,
            message_id=self.message_id, requires_processed_ack=self.requires_processed_ack,
            nonce=self.nonce, timestamp=self.timestamp,
            sender_hostname=self.sender_hostname, hmac=mac,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> Envelope:
        try:
            data = json.loads(raw)
            return cls(**{k: v for k, v in data.items() if k != "idempotency_key"})
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise SignalEnvelopeError(f"malformed envelope: {exc}") from exc


def verify(
    envelope: Envelope, shared_secret: str, *, ttl_seconds: float = DEFAULT_TTL_SECONDS
) -> None:
    """Raise SignalEnvelopeError if the envelope fails HMAC, is expired, or is
    dated in the future beyond one TTL window (clock skew tolerance).

    Nonce replay detection is NOT this function's job — it needs a stateful
    seen-set (§3.3a: in-memory, TTL-bounded, deliberately not persisted), which
    only makes sense scoped to one daemon instance; see signal/daemon.py.
    """
    expected = hmac_lib.new(
        shared_secret.encode(), envelope._signing_payload(), hashlib.sha256
    ).hexdigest()
    if not hmac_lib.compare_digest(expected, envelope.hmac):
        raise SignalEnvelopeError(f"bad hmac for message_id={envelope.message_id}")
    age = time.time() - envelope.timestamp
    if age > ttl_seconds:
        raise SignalEnvelopeError(
            f"expired envelope message_id={envelope.message_id}: {age:.1f}s old, TTL={ttl_seconds}s"
        )
    if age < -ttl_seconds:
        raise SignalEnvelopeError(
            f"envelope message_id={envelope.message_id} timestamped {-age:.1f}s in the future"
        )
