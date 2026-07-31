# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from herdr_bridge.actions import BridgeActions
from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from herdr_bridge.errors import AgentNotFoundError, ControlLeaseError
from tests.test_cache import SNAPSHOT


@pytest.fixture()
def acts(fake_herdr, tmp_path):
    fake_herdr.set_handler("session.snapshot", lambda p: SNAPSHOT)
    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    return BridgeActions(client, cache, AuditLogger(tmp_path / "a.jsonl"))


def test_control_is_exclusive_until_release(acts):
    h1 = acts.acquire_control("actor-a", "w1:p1", mode="control")
    with pytest.raises(ControlLeaseError):
        acts.acquire_control("actor-b", "w1:p1", mode="control")
    h1.release()
    h2 = acts.acquire_control("actor-b", "w1:p1", mode="control")
    assert h2.released is False
    h2.release()


def test_observe_is_shared(acts):
    h1 = acts.acquire_control("actor-a", "w1:p1", mode="observe")
    h2 = acts.acquire_control("actor-b", "w1:p1", mode="observe")
    h1.release()
    h2.release()


def test_release_is_idempotent_and_context_manager(acts):
    with acts.acquire_control("actor-a", "w1:p1") as h:
        assert h.mode == "control"
    assert h.released is True
    h.release()  # a second release must not raise


def test_unknown_pane_raises(acts):
    with pytest.raises(AgentNotFoundError):
        acts.acquire_control("actor-a", "w9:p9")


def test_invalid_mode_raises(acts):
    with pytest.raises(ValueError):
        acts.acquire_control("actor-a", "w1:p1", mode="steal")


def test_lease_key_canonicalized_via_agent_id(acts):
    """M1 gate X2 regression: passing the agent_id by mistake must not create a
    second lease that bypasses mutual exclusion."""
    h1 = acts.acquire_control("actor-a", "w1:p1", mode="control")
    with pytest.raises(ControlLeaseError):
        acts.acquire_control("actor-b", "term_a", mode="control")  # agent_id of the same pane
    h1.release()


def test_denied_acquire_is_audited(acts, tmp_path):
    """M1 gate X3 regression: a denied takeover attempt must leave an audit trail."""
    import json
    h1 = acts.acquire_control("actor-a", "w1:p1", mode="control")
    with pytest.raises(ControlLeaseError):
        acts.acquire_control("actor-b", "w1:p1", mode="control")
    entries = [json.loads(line) for line in
               acts._audit.path.read_text().strip().splitlines()]
    denied = [e for e in entries if e["action"] == "acquire_control_denied"]
    assert denied and denied[-1]["actor_id"] == "actor-b"
    assert denied[-1]["held_by"] == "actor-a"
    h1.release()


def test_concurrent_double_release_single_audit(acts):
    """M1 gate CC4 regression: a concurrent double-release must produce only one release audit entry."""
    import json
    import threading
    h = acts.acquire_control("actor-a", "w1:p1", mode="control")
    barrier = threading.Barrier(2)

    def rel():
        barrier.wait()
        h.release()

    threads = [threading.Thread(target=rel) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    entries = [json.loads(line) for line in
               acts._audit.path.read_text().strip().splitlines()]
    releases = [e for e in entries if e["action"] == "release_control"]
    assert len(releases) == 1
