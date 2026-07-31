# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""M1 acceptance 2: an external policy rule can complete a full call flow
without modifying the tool layer's source code."""

import json
import threading
import time

from herdr_bridge import BridgeActions
from herdr_bridge.audit import AuditLogger
from herdr_bridge.cache import SessionCache
from herdr_bridge.client import SocketClient
from tests.test_cache import SNAPSHOT

TWO_AGENT_SNAPSHOT = {
    "type": "session_snapshot",
    "snapshot": {
        **SNAPSHOT["snapshot"],
        "panes": SNAPSHOT["snapshot"]["panes"] + [
            {"pane_id": "w1:p3", "terminal_id": "term_rev", "workspace_id": "w1",
             "tab_id": "w1:t1", "focused": False, "cwd": "/tmp/r",
             "foreground_cwd": "/tmp/r", "agent": "claude",
             "agent_status": "idle",
             "agent_session": None, "revision": 0,
             "scroll": {"offset_from_bottom": 0, "max_offset_from_bottom": 0,
                        "viewport_rows": 50}},
        ],
        "agents": SNAPSHOT["snapshot"]["agents"] + [
            {"terminal_id": "term_rev", "agent": "claude", "agent_status": "idle",
             "agent_session": None, "workspace_id": "w1", "tab_id": "w1:t1",
             "pane_id": "w1:p3", "focused": False, "cwd": "/tmp/r",
             "foreground_cwd": "/tmp/r", "revision": 0},
        ],
    },
}


def test_failed_forwarder_flow_without_touching_source(fake_herdr, tmp_path):
    # The current wait_until implementation is event-driven via
    # subscribe(pane.agent_status_changed) and no longer calls events.wait
    # (an old mechanism removed after the PPLX priority-1 refactor -- setting a
    # handler for it would be dead code anyway) -- use fake_herdr.push_event to
    # simulate the event push that triggers a re-read.
    fake_herdr.set_handler("session.snapshot", lambda p: TWO_AGENT_SNAPSHOT)
    tester_outputs = iter(["collecting tests…", "FAILED tests/test_login.py"])
    fake_herdr.set_handler(
        "agent.read",
        lambda p: {"type": "pane_read", "read": {"text": next(tester_outputs, "FAILED tests/test_login.py")}}
        if p["target"] == "term_a" else {"type": "pane_read", "read": {"text": ""}})

    def push_events():
        for _ in range(2):
            time.sleep(0.05)
            fake_herdr.push_event("pane.agent_status_changed",
                                  {"pane_id": "w1:p1", "agent_status": "working"})

    threading.Thread(target=push_events, daemon=True).start()
    sent: list[dict] = []

    def send_handler(p):
        sent.append(p)
        return {"type": "ok"}

    fake_herdr.set_handler("agent.send", send_handler)

    client = SocketClient(fake_herdr.socket_path)
    cache = SessionCache(client)
    cache.refresh_snapshot()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    actions = BridgeActions(client, cache, audit)

    # ---- Policy rule body (same logic as examples/failed_forwarder.py) ----
    actor = "rule:policy-demo"  # memo v1.0 format; the short actor used
                                    # elsewhere in unit tests (e.g. "ruler-1")
                                    # is deliberately kept to verify that a
                                    # malformed flag doesn't interrupt execution
    agents = {a.agent_id for a in actions.list_agents(actor)}
    assert {"term_a", "term_rev"} <= agents
    result = actions.wait_until(actor, "term_a",
                                lambda o: "FAILED" in o.text,
                                timeout_sec=10, poll_interval_sec=0)
    assert result.success is True
    context = actions.read_agent(actor, "term_a")
    actions.send_to_agent(actor, "term_rev",
                          f"failure:\n{context.text[-2000:]}", priority=1)
    # ---- Verification ----
    assert sent and sent[0]["target"] == "term_rev"
    assert "FAILED" in sent[0]["text"]
    entries = [json.loads(line) for line in
               audit.path.read_text().strip().splitlines()]
    assert all(e["actor_id"] == actor for e in entries)
    assert {"list_agents", "wait_until", "read_agent",
            "send_to_agent"} <= {e["action"] for e in entries}
