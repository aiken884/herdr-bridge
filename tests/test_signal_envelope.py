# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""signal.envelope: envelope construction, signing/verification, idempotency_key,
and the TTL/HMAC/replay checks from design doc §3.3/§3.3a."""

from __future__ import annotations

import time

import pytest

from herdr_bridge.errors import SignalEnvelopeError
from herdr_bridge.signal.envelope import Envelope, idempotency_key, verify


def _make(**overrides):
    kwargs = {
        "from_project": "herdr-bridge", "to_project": "remagraph",
        "inbox_ref": "task-abc/agent-1", "kind": "task_handoff", "sender_id": "herdr-bridge-tower",
    }
    kwargs.update(overrides)
    return Envelope(**kwargs)


def test_idempotency_key_only_depends_on_to_project_and_inbox_ref():
    a = idempotency_key("remagraph", "task-abc")
    b = idempotency_key("remagraph", "task-abc")
    assert a == b
    assert a != idempotency_key("remagraph", "task-xyz")
    assert a != idempotency_key("downstream-tower", "task-abc")


def test_idempotency_key_ignores_message_id_and_timestamp():
    e1 = _make()
    e2 = _make()
    assert e1.message_id != e2.message_id
    assert e1.idempotency_key == e2.idempotency_key


def test_sign_then_verify_succeeds():
    signed = _make().signed("shared-secret")
    assert signed.hmac
    verify(signed, "shared-secret")  # must not raise


def test_verify_rejects_wrong_secret():
    signed = _make().signed("shared-secret")
    with pytest.raises(SignalEnvelopeError, match="bad hmac"):
        verify(signed, "wrong-secret")


def test_verify_rejects_tampered_field():
    signed = _make().signed("shared-secret")
    tampered = Envelope(
        from_project=signed.from_project, to_project=signed.to_project,
        inbox_ref="task-tampered", kind=signed.kind, sender_id=signed.sender_id,
        message_id=signed.message_id, requires_processed_ack=signed.requires_processed_ack,
        nonce=signed.nonce, timestamp=signed.timestamp,
        sender_hostname=signed.sender_hostname, hmac=signed.hmac,
    )
    with pytest.raises(SignalEnvelopeError, match="bad hmac"):
        verify(tampered, "shared-secret")


def test_verify_rejects_tampered_sender_hostname():
    """2026-08-01 DEPLOYMENT CONSTRAINT fix: sender_hostname participates in
    the signed payload, so forging it to bypass daemon.py's same-host check
    (see signal/daemon.py) is caught the same way any other tampered field
    would be -- it isn't a separate trust boundary of its own, just a
    diagnostic field that happens to also be signed."""
    signed = _make().signed("shared-secret")
    tampered = Envelope(
        from_project=signed.from_project, to_project=signed.to_project,
        inbox_ref=signed.inbox_ref, kind=signed.kind, sender_id=signed.sender_id,
        message_id=signed.message_id, requires_processed_ack=signed.requires_processed_ack,
        nonce=signed.nonce, timestamp=signed.timestamp,
        sender_hostname="forged-hostname", hmac=signed.hmac,
    )
    with pytest.raises(SignalEnvelopeError, match="bad hmac"):
        verify(tampered, "shared-secret")


def test_sender_hostname_defaults_to_the_local_hostname():
    import socket

    assert _make().sender_hostname == socket.gethostname()


def test_from_json_falls_back_to_local_hostname_when_field_missing():
    """Backward compatibility: an envelope from before sender_hostname existed
    (or a hand-built one missing the field) must still parse -- falling back
    to this process's own hostname is a safe default (it makes the same-host
    check in daemon.py pass, i.e. "assume same host" for a value that
    predates the field existing at all, rather than refusing to parse)."""
    import json
    import socket

    signed = _make().signed("shared-secret")
    data = json.loads(signed.to_json())
    del data["sender_hostname"]
    restored = Envelope.from_json(json.dumps(data))
    assert restored.sender_hostname == socket.gethostname()


def test_verify_rejects_expired_envelope():
    signed = _make(timestamp=time.time() - 120).signed("shared-secret")
    with pytest.raises(SignalEnvelopeError, match="expired"):
        verify(signed, "shared-secret", ttl_seconds=60.0)


def test_verify_rejects_future_dated_envelope():
    signed = _make(timestamp=time.time() + 120).signed("shared-secret")
    with pytest.raises(SignalEnvelopeError, match="future"):
        verify(signed, "shared-secret", ttl_seconds=60.0)


def test_verify_accepts_envelope_within_ttl():
    signed = _make(timestamp=time.time() - 30).signed("shared-secret")
    verify(signed, "shared-secret", ttl_seconds=60.0)  # must not raise


def test_to_json_and_from_json_round_trip():
    signed = _make().signed("shared-secret")
    restored = Envelope.from_json(signed.to_json())
    assert restored == signed


def test_from_json_rejects_malformed_input():
    with pytest.raises(SignalEnvelopeError, match="malformed"):
        Envelope.from_json("not json")


def test_from_json_rejects_missing_required_fields():
    with pytest.raises(SignalEnvelopeError, match="malformed"):
        Envelope.from_json('{"from_project": "a"}')
