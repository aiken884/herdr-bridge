# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""signal.outbound: send() + the §3.5 escalation rules (1/2/3 synchronous,
4 as a lazy pull-check — see outbound.py's module docstring for why)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest

from herdr_bridge.orchestration import get_signal_state
from herdr_bridge.orchestration import memory as memory_mod
from herdr_bridge.signal import outbound
from herdr_bridge.signal.envelope import Envelope


@pytest.fixture(autouse=True)
def _isolated_signal_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_mod, "_signal_state_dir", lambda project_id: tmp_path / "state" / project_id)


@pytest.fixture
def short_socket_dir():
    with tempfile.TemporaryDirectory(prefix="hbsig-out-", dir="/tmp") as d:
        yield Path(d)


def _run(coro):
    return asyncio.run(coro)


async def _serve_once(socket_path: Path, reply: dict | None, *, delay: float = 0.0):
    """Minimal fake daemon: accept one connection, read one line, optionally
    reply after `delay` seconds (None reply = never reply, simulating a dead
    daemon)."""
    async def handler(reader, writer):
        await reader.readline()
        if delay:
            await asyncio.sleep(delay)
        if reply is not None:
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    return server


def test_send_success_when_daemon_marks_injected_in_time(short_socket_dir, monkeypatch):
    monkeypatch.setattr(outbound, "INJECTED_TIMEOUT_SECONDS", 2.0)
    socket_path = short_socket_dir / "d.sock"

    async def _body():
        message_id_holder = {}

        async def handler(reader, writer):
            raw = await reader.readline()
            env = Envelope.from_json(raw.decode().strip())
            message_id_holder["id"] = env.message_id
            writer.write((json.dumps({"status": "accepted", "message_id": env.message_id}) + "\n").encode())
            await writer.drain()
            writer.close()
            # simulate the daemon marking Injected shortly after Accepted
            await asyncio.sleep(0.05)
            memory_mod.mark_injected("remagraph", env.message_id)

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        try:
            result = await outbound.send(
                "herdr-bridge", "remagraph", "task-1", "task_handoff",
                "herdr-bridge-tower", "s3cret", socket_path,
            )
            return result
        finally:
            server.close()
            await server.wait_closed()

    result = _run(_body())
    assert result.status == "injected"
    row = get_signal_state("remagraph", result.message_id)
    assert row["state"] == "injected"


def test_send_success_when_daemon_races_straight_to_completed(short_socket_dir, monkeypatch):
    """2026-08-02 field incident regression: daemon.py calls mark_injected()
    then mark_completed() back-to-back with no gap, so a real daemon can
    advance past "injected" before send()'s poll loop ever observes that
    exact state. This must still report "injected" (the send succeeded), not
    crash trying to escalate an already-completed message."""
    monkeypatch.setattr(outbound, "INJECTED_TIMEOUT_SECONDS", 2.0)
    socket_path = short_socket_dir / "d.sock"

    async def _body():
        async def handler(reader, writer):
            raw = await reader.readline()
            env = Envelope.from_json(raw.decode().strip())
            writer.write((json.dumps({"status": "accepted", "message_id": env.message_id}) + "\n").encode())
            await writer.drain()
            writer.close()
            await asyncio.sleep(0.05)
            # simulate daemon.py's real back-to-back write ordering
            memory_mod.mark_injected("remagraph", env.message_id)
            memory_mod.mark_completed("remagraph", env.message_id)

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        try:
            return await outbound.send(
                "herdr-bridge", "remagraph", "task-1", "task_handoff",
                "herdr-bridge-tower", "s3cret", socket_path,
            )
        finally:
            server.close()
            await server.wait_closed()

    result = _run(_body())
    assert result.status == "injected"
    row = get_signal_state("remagraph", result.message_id)
    assert row["state"] == "completed"


def test_send_returns_daemon_unreachable_when_socket_missing(tmp_path):
    async def _body():
        return await outbound.send(
            "herdr-bridge", "remagraph", "task-1", "task_handoff",
            "herdr-bridge-tower", "s3cret", tmp_path / "no-such.sock",
        )

    result = _run(_body())
    assert result.status == "daemon_unreachable"
    row = get_signal_state("remagraph", result.message_id)
    assert row["state"] == "daemon_unreachable"


def test_send_returns_injection_failed_transient_when_daemon_never_injects(short_socket_dir, monkeypatch):
    """No other in-flight signal for this target exists -- this is a plain,
    retry-worthy transient miss, not a dedup drop (2026-08-01 status split)."""
    monkeypatch.setattr(outbound, "INJECTED_TIMEOUT_SECONDS", 0.2)
    socket_path = short_socket_dir / "d.sock"

    async def _body():
        async def handler(reader, writer):
            raw = await reader.readline()
            env = Envelope.from_json(raw.decode().strip())
            writer.write((json.dumps({"status": "accepted", "message_id": env.message_id}) + "\n").encode())
            await writer.drain()
            writer.close()
            # never calls mark_injected — simulates a daemon crash after Accepted

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        try:
            return await outbound.send(
                "herdr-bridge", "remagraph", "task-1", "task_handoff",
                "herdr-bridge-tower", "s3cret", socket_path,
            )
        finally:
            server.close()
            await server.wait_closed()

    result = _run(_body())
    assert result.status == "injection_failed_transient"
    row = get_signal_state("remagraph", result.message_id)
    assert row["state"] == "injection_unconfirmed"  # underlying state-machine reason is unchanged


def test_send_returns_deduplicated_inflight_when_another_message_owns_the_target(short_socket_dir, monkeypatch):
    """2026-08-01 status-split regression test: when the daemon's own dedup
    check drops this send because another message_id already owns the same
    (to_project, inbox_ref), the caller must see a status that says so --
    not the same string as a plain transient miss (see SendResult.status's
    docstring for why these need different caller behavior)."""
    from herdr_bridge.orchestration import mark_accepted, mark_injected

    monkeypatch.setattr(outbound, "INJECTED_TIMEOUT_SECONDS", 0.2)
    socket_path = short_socket_dir / "d.sock"

    # Simulate an earlier, still in-flight signal for the same target.
    mark_accepted(
        "remagraph", "earlier-message-id",
        from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-shared",
    )
    mark_injected("remagraph", "earlier-message-id")

    async def _body():
        async def handler(reader, writer):
            raw = await reader.readline()
            env = Envelope.from_json(raw.decode().strip())
            # Accept it (matches the daemon's real behavior: Accepted is sent
            # before the dedup check runs), but never inject it.
            writer.write((json.dumps({"status": "accepted", "message_id": env.message_id}) + "\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        try:
            return await outbound.send(
                "herdr-bridge", "remagraph", "task-shared", "task_handoff",
                "herdr-bridge-tower", "s3cret", socket_path,
            )
        finally:
            server.close()
            await server.wait_closed()

    result = _run(_body())
    assert result.status == "deduplicated_inflight"


def test_send_retries_before_giving_up(short_socket_dir, monkeypatch):
    """§3.5 rule 1: the socket doesn't exist on the first attempt but appears
    before the retries are exhausted -- send() must succeed, not give up early."""
    monkeypatch.setattr(outbound, "ACCEPT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(outbound, "ACCEPT_RETRY_DELAYS_SECONDS", (0.1, 0.1))
    monkeypatch.setattr(outbound, "INJECTED_TIMEOUT_SECONDS", 1.0)
    socket_path = short_socket_dir / "d.sock"

    async def _body():
        async def start_server_late():
            await asyncio.sleep(0.15)  # miss the first attempt's window

            async def handler(reader, writer):
                raw = await reader.readline()
                env = Envelope.from_json(raw.decode().strip())
                writer.write((json.dumps({"status": "accepted", "message_id": env.message_id}) + "\n").encode())
                await writer.drain()
                writer.close()
                memory_mod.mark_injected("remagraph", env.message_id)

            return await asyncio.start_unix_server(handler, path=str(socket_path))

        server_task = asyncio.ensure_future(start_server_late())
        try:
            result = await outbound.send(
                "herdr-bridge", "remagraph", "task-1", "task_handoff",
                "herdr-bridge-tower", "s3cret", socket_path,
            )
            return result
        finally:
            server = await server_task
            server.close()
            await server.wait_closed()

    result = _run(_body())
    assert result.status == "injected"


# -- check_needs_attention (rule 4, lazy pull-check) -------------------------

def test_check_needs_attention_not_due_yet():
    memory_mod.mark_accepted(
        "remagraph", "msg-1", from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1"
    )
    memory_mod.mark_injected("remagraph", "msg-1")
    assert outbound.check_needs_attention("remagraph", "msg-1") is None
    assert get_signal_state("remagraph", "msg-1")["state"] == "injected"


def test_check_needs_attention_escalates_when_overdue(monkeypatch):
    memory_mod.mark_accepted(
        "remagraph", "msg-2", from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1"
    )
    memory_mod.mark_injected("remagraph", "msg-2")
    # backdate injected_at past the threshold
    from herdr_bridge.orchestration import signal_state_store
    from herdr_bridge.orchestration._state_paths import signal_state_dir

    state_dir = signal_state_dir("remagraph")
    monkeypatch.setattr(memory_mod, "_signal_state_dir", lambda project_id: state_dir)
    signal_state_store.write_state(
        state_dir, "msg-2", from_project="herdr-bridge", to_project="remagraph",
        inbox_ref="task-1", state="injected", injected_at=time.time() - 120,
    )

    assert outbound.check_needs_attention("remagraph", "msg-2") == "needs_attention"
    assert get_signal_state("remagraph", "msg-2")["state"] == "needs_attention"


def test_check_needs_attention_noop_when_already_seen():
    memory_mod.mark_accepted(
        "remagraph", "msg-3", from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1"
    )
    memory_mod.mark_injected("remagraph", "msg-3")
    memory_mod.mark_seen("remagraph", "msg-3")
    assert outbound.check_needs_attention("remagraph", "msg-3") is None
    assert get_signal_state("remagraph", "msg-3")["state"] == "seen"


def test_check_needs_attention_noop_for_unknown_message():
    assert outbound.check_needs_attention("remagraph", "never-existed") is None
