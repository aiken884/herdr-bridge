# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""signal.daemon: pane_id resolution (§3.4) and the socket server's
verify/merge/inject/mark_injected pipeline (§3.2/§3.6/§3.7).

No pytest-asyncio/anyio test plugin is configured in this project, so async
test bodies are run via asyncio.run() from plain sync test functions —
consistent with not adding a new test-framework dependency for this alone.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest

from herdr_bridge.orchestration import get_signal_state
from herdr_bridge.orchestration import memory as memory_mod
from herdr_bridge.signal.daemon import (
    PaneIdResolutionError,
    SignalDaemon,
    resolve_own_pane_id,
)
from herdr_bridge.signal.envelope import Envelope


@pytest.fixture(autouse=True)
def _isolated_signal_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_mod, "_signal_state_dir", lambda project_id: tmp_path / "state" / project_id)
    monkeypatch.setattr(
        "herdr_bridge.signal.daemon.signal_state_dir", lambda project_id: tmp_path / "state" / project_id
    )


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def short_socket_dir():
    """macOS caps AF_UNIX sun_path at 104 bytes; pytest's tmp_path can exceed
    that (same TD-001 P0 workaround as testing/_server.py's FakeHerdrServer)."""
    with tempfile.TemporaryDirectory(prefix="hbsig-", dir="/tmp") as d:
        yield Path(d)


# -- resolve_own_pane_id (§3.4 three-tier resolution) ------------------------

def test_tier1_env_var_wins_and_writes_pin_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    result = resolve_own_pane_id("herdr-bridge")
    assert result == "w1:p1"
    pin_path = tmp_path / "state" / "herdr-bridge" / "pane_id.pin"
    assert pin_path.read_text() == "w1:p1"


def test_tier2_pin_file_used_when_no_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    pin_path = tmp_path / "state" / "herdr-bridge" / "pane_id.pin"
    pin_path.parent.mkdir(parents=True)
    pin_path.write_text("w2:p3")
    assert resolve_own_pane_id("herdr-bridge") == "w2:p3"


def test_tier3_dynamic_scan_single_match(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    cwd = str(tmp_path)
    monkeypatch.chdir(tmp_path)
    fake_output = json.dumps({"result": {"panes": [
        {"pane_id": "w9:p1", "cwd": cwd},
        {"pane_id": "w9:p2", "cwd": "/somewhere/else"},
    ]}})

    class _FakeResult:
        stdout = fake_output

    monkeypatch.setattr(
        "herdr_bridge.signal.daemon.subprocess.run", lambda *a, **k: _FakeResult()
    )
    assert resolve_own_pane_id("herdr-bridge") == "w9:p1"


def test_tier3_refuses_to_start_on_multiple_matches(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    cwd = str(tmp_path)
    monkeypatch.chdir(tmp_path)
    fake_output = json.dumps({"result": {"panes": [
        {"pane_id": "w9:p1", "cwd": cwd},
        {"pane_id": "w9:p2", "cwd": cwd},
    ]}})

    class _FakeResult:
        stdout = fake_output

    monkeypatch.setattr(
        "herdr_bridge.signal.daemon.subprocess.run", lambda *a, **k: _FakeResult()
    )
    with pytest.raises(PaneIdResolutionError, match="found 2 candidate"):
        resolve_own_pane_id("herdr-bridge")


def test_tier3_refuses_to_start_on_zero_matches(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    fake_output = json.dumps({"result": {"panes": []}})

    class _FakeResult:
        stdout = fake_output

    monkeypatch.setattr(
        "herdr_bridge.signal.daemon.subprocess.run", lambda *a, **k: _FakeResult()
    )
    with pytest.raises(PaneIdResolutionError, match="found 0 candidate"):
        resolve_own_pane_id("herdr-bridge")


# -- SignalDaemon connection handling -----------------------------------------

class _FakeStreamWriter:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeStreamReader:
    def __init__(self, line: bytes):
        self._line = line

    async def readline(self) -> bytes:
        return self._line


def _envelope_line(project="herdr-bridge", to="remagraph", inbox="task-1", secret="s3cret") -> bytes:
    env = Envelope(
        from_project=project, to_project=to, inbox_ref=inbox,
        kind="task_handoff", sender_id="sender-tower",
    ).signed(secret)
    return (env.to_json() + "\n").encode()


def test_valid_envelope_gets_accepted_ack(monkeypatch, tmp_path):
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret")
    reader = _FakeStreamReader(_envelope_line())
    writer = _FakeStreamWriter()
    _run(daemon.handle_connection(reader, writer))
    reply = json.loads(writer.written.decode())
    assert reply["status"] == "accepted"
    assert writer.closed


def test_mismatched_hostname_is_dropped_before_hmac_verification(monkeypatch):
    """2026-08-01 DEPLOYMENT CONSTRAINT fix: a cross-host envelope must be
    rejected by the hostname check BEFORE hmac verification runs, so the
    daemon can report a clear "different host" diagnostic instead of a
    confusing "bad hmac" that looks like tampering. Proven by making
    verify() itself blow up if it's ever reached -- the hostname check must
    short-circuit before that point."""
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret")
    env = Envelope(
        from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1",
        kind="task_handoff", sender_id="sender-tower", sender_hostname="some-other-host",
    ).signed("s3cret")

    def _verify_must_not_be_called(*a, **k):
        raise AssertionError("verify() must not run when sender_hostname mismatches")

    monkeypatch.setattr("herdr_bridge.signal.daemon.verify", _verify_must_not_be_called)
    reader = _FakeStreamReader((env.to_json() + "\n").encode())
    writer = _FakeStreamWriter()
    _run(daemon.handle_connection(reader, writer))
    assert writer.written == b""  # no Accepted ack for a rejected envelope
    assert writer.closed


def test_bad_hmac_gets_no_reply(tmp_path):
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret")
    reader = _FakeStreamReader(_envelope_line(secret="wrong-secret"))
    writer = _FakeStreamWriter()
    _run(daemon.handle_connection(reader, writer))
    assert writer.written == b""
    assert writer.closed


def test_replayed_nonce_is_dropped_on_second_delivery(tmp_path):
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret")
    env = Envelope(
        from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1",
        kind="task_handoff", sender_id="sender-tower",
    ).signed("s3cret")
    line = (env.to_json() + "\n").encode()

    writer1 = _FakeStreamWriter()
    _run(daemon.handle_connection(_FakeStreamReader(line), writer1))
    assert json.loads(writer1.written.decode())["status"] == "accepted"

    writer2 = _FakeStreamWriter()
    _run(daemon.handle_connection(_FakeStreamReader(line), writer2))
    assert writer2.written == b""  # same nonce, second delivery dropped silently


def test_merge_window_batches_and_marks_each_message_injected(monkeypatch, tmp_path):
    """§3.6: two wakes within the merge window -> one notify-pane call, but
    each message_id independently reaches `injected` in the ACK store."""
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret", merge_window_seconds=0.05)

    inject_calls = []

    async def _fake_inject(message: str) -> bool:
        inject_calls.append(message)
        return True

    monkeypatch.setattr(daemon, "_notify_pane_self_inject", _fake_inject)

    async def _body():
        env1 = Envelope(
            from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1",
            kind="task_handoff", sender_id="sender-tower",
        ).signed("s3cret")
        env2 = Envelope(
            from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-2",
            kind="task_handoff", sender_id="sender-tower",
        ).signed("s3cret")

        memory_mod.mark_accepted(
            "herdr-bridge", env1.message_id, from_project="herdr-bridge",
            to_project="remagraph", inbox_ref="task-1",
        )
        memory_mod.mark_accepted(
            "herdr-bridge", env2.message_id, from_project="herdr-bridge",
            to_project="remagraph", inbox_ref="task-2",
        )

        for env in (env1, env2):
            w = _FakeStreamWriter()
            await daemon.handle_connection(
                _FakeStreamReader((env.to_json() + "\n").encode()), w
            )
        await asyncio.sleep(0.2)  # let the merge-window batch task fire
        return env1, env2

    env1, env2 = _run(_body())

    assert len(inject_calls) == 1  # merged into a single notify-pane call
    # 2026-08-01: daemon.py now advances injected -> completed itself right
    # away (see orchestration/memory.py's SIGNAL_STATE_TRANSITIONS docstring
    # for why nothing else ever did) -- "completed" here still only means
    # "the daemon confirmed injection", not "the receiving agent acted on it".
    assert get_signal_state("herdr-bridge", env1.message_id)["state"] == "completed"
    assert get_signal_state("herdr-bridge", env2.message_id)["state"] == "completed"


def test_notify_pane_failure_does_not_mark_injected(monkeypatch, tmp_path):
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret", merge_window_seconds=0.05)

    async def _failing_inject(message: str) -> bool:
        return False

    monkeypatch.setattr(daemon, "_notify_pane_self_inject", _failing_inject)

    async def _body():
        env = Envelope(
            from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1",
            kind="task_handoff", sender_id="sender-tower",
        ).signed("s3cret")
        memory_mod.mark_accepted(
            "herdr-bridge", env.message_id, from_project="herdr-bridge",
            to_project="remagraph", inbox_ref="task-1",
        )
        w = _FakeStreamWriter()
        await daemon.handle_connection(_FakeStreamReader((env.to_json() + "\n").encode()), w)
        await asyncio.sleep(0.2)
        return env

    env = _run(_body())
    # notify-pane failed -> mark_injected was never called -> state stays "accepted"
    assert get_signal_state("herdr-bridge", env.message_id)["state"] == "accepted"


# -- real Unix socket end-to-end (design doc §3.2 full round trip) ----------

def test_end_to_end_over_a_real_unix_socket(monkeypatch, short_socket_dir):
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret", merge_window_seconds=0.05)

    async def _fake_inject(message: str) -> bool:
        return True

    monkeypatch.setattr(daemon, "_notify_pane_self_inject", _fake_inject)

    async def _body():
        socket_path = short_socket_dir / "d.sock"
        server = await daemon.serve(socket_path)
        try:
            env = Envelope(
                from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1",
                kind="task_handoff", sender_id="sender-tower",
            ).signed("s3cret")
            memory_mod.mark_accepted(
                "herdr-bridge", env.message_id, from_project="herdr-bridge",
                to_project="remagraph", inbox_ref="task-1",
            )
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write((env.to_json() + "\n").encode())
            await writer.drain()
            reply_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            reply = json.loads(reply_line.decode())
            writer.close()
            await asyncio.sleep(0.2)
            return reply, env
        finally:
            server.close()
            await server.wait_closed()

    reply, env = _run(_body())
    assert reply["status"] == "accepted"
    assert get_signal_state("herdr-bridge", env.message_id)["state"] == "completed"


def test_socket_file_permissions_are_0600(short_socket_dir):
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret")

    async def _body():
        socket_path = short_socket_dir / "d.sock"
        server = await daemon.serve(socket_path)
        try:
            mode = socket_path.stat().st_mode & 0o777
            return mode
        finally:
            server.close()
            await server.wait_closed()

    assert _run(_body()) == 0o600


# -- §3.8 acceptance tests not already covered above -------------------------

def test_expired_envelope_gets_no_reply(tmp_path):
    """§3.8 test 2: an expired (stale timestamp) envelope is rejected -- no ACK."""
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret")
    env = Envelope(
        from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1",
        kind="task_handoff", sender_id="sender-tower", timestamp=time.time() - 120,
    ).signed("s3cret")
    writer = _FakeStreamWriter()
    _run(daemon.handle_connection(_FakeStreamReader((env.to_json() + "\n").encode()), writer))
    assert writer.written == b""
    assert writer.closed


def test_notify_pane_called_with_allow_busy(monkeypatch, tmp_path):
    """§3.8 test 4: Signal must not refuse a busy target the way ordinary
    dispatch does -- the daemon always passes --allow-busy to notify-pane."""
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret", merge_window_seconds=0.05)
    captured_args = []

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            pass

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured_args.append(args)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    async def _body():
        env = Envelope(
            from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-1",
            kind="task_handoff", sender_id="sender-tower",
        ).signed("s3cret")
        memory_mod.mark_accepted(
            "herdr-bridge", env.message_id, from_project="herdr-bridge",
            to_project="remagraph", inbox_ref="task-1",
        )
        w = _FakeStreamWriter()
        await daemon.handle_connection(_FakeStreamReader((env.to_json() + "\n").encode()), w)
        await asyncio.sleep(0.2)

    _run(_body())
    assert len(captured_args) == 1
    assert "--allow-busy" in captured_args[0]


def test_idempotent_resend_does_not_trigger_a_second_injection(monkeypatch, tmp_path):
    """§3.8 test 5: two envelopes sharing (to_project, inbox_ref) -- i.e. the
    same idempotency_key -- must not cause the target to be processed twice,
    even with different message_ids (two independent send attempts)."""
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret", merge_window_seconds=0.05)
    inject_calls = []

    async def _fake_inject(message: str) -> bool:
        inject_calls.append(message)
        return True

    monkeypatch.setattr(daemon, "_notify_pane_self_inject", _fake_inject)

    async def _body():
        env1 = Envelope(
            from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-shared",
            kind="task_handoff", sender_id="sender-tower",
        ).signed("s3cret")
        memory_mod.mark_accepted(
            "herdr-bridge", env1.message_id, from_project="herdr-bridge",
            to_project="remagraph", inbox_ref="task-shared",
        )
        w1 = _FakeStreamWriter()
        await daemon.handle_connection(_FakeStreamReader((env1.to_json() + "\n").encode()), w1)

        # a second, independent send attempt for the SAME (to_project, inbox_ref)
        # before the first has completed -- same idempotency target, different message_id
        env2 = Envelope(
            from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-shared",
            kind="task_handoff", sender_id="sender-tower",
        ).signed("s3cret")
        w2 = _FakeStreamWriter()
        await daemon.handle_connection(_FakeStreamReader((env2.to_json() + "\n").encode()), w2)

        assert json.loads(w1.written.decode())["status"] == "accepted"
        assert json.loads(w2.written.decode())["status"] == "accepted"  # both get Accepted...

        await asyncio.sleep(0.2)
        return env1, env2

    env1, env2 = _run(_body())
    assert len(inject_calls) == 1  # ...but only one actual injection happened
    assert get_signal_state("herdr-bridge", env1.message_id)["state"] == "completed"
    assert get_signal_state("herdr-bridge", env2.message_id) is None  # never enqueued


def test_a_second_reminder_after_the_first_completes_is_not_permanently_swallowed(monkeypatch):
    """Regression test for the F1+F2 production bug (2026-08-01 adversarial
    review): before daemon.py advanced injected -> completed itself, a signal
    never left "injected", so find_active_by_target() treated it as in-flight
    forever and every later send for the same (to_project, inbox_ref) was
    silently dropped -- exactly the reminder/retry/escalation scenario Signal
    exists for. Once the first wake has actually completed (not merely been
    injected), a second, later wake for the same target must go through and
    trigger its own real injection."""
    daemon = SignalDaemon("herdr-bridge", "w1:p1", "s3cret", merge_window_seconds=0.05)
    inject_calls = []

    async def _fake_inject(message: str) -> bool:
        inject_calls.append(message)
        return True

    monkeypatch.setattr(daemon, "_notify_pane_self_inject", _fake_inject)

    async def _body():
        env1 = Envelope(
            from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-reminder",
            kind="task_handoff", sender_id="sender-tower",
        ).signed("s3cret")
        w1 = _FakeStreamWriter()
        await daemon.handle_connection(_FakeStreamReader((env1.to_json() + "\n").encode()), w1)
        await asyncio.sleep(0.2)  # let the first wake fully complete

        # A second, later reminder for the same (to_project, inbox_ref) --
        # this must NOT be treated as still in-flight, since the first one
        # already reached "completed".
        env2 = Envelope(
            from_project="herdr-bridge", to_project="remagraph", inbox_ref="task-reminder",
            kind="task_handoff", sender_id="sender-tower",
        ).signed("s3cret")
        w2 = _FakeStreamWriter()
        await daemon.handle_connection(_FakeStreamReader((env2.to_json() + "\n").encode()), w2)
        await asyncio.sleep(0.2)
        return env1, env2

    env1, env2 = _run(_body())
    assert len(inject_calls) == 2  # both reminders actually got injected
    assert get_signal_state("herdr-bridge", env1.message_id)["state"] == "completed"
    assert get_signal_state("herdr-bridge", env2.message_id)["state"] == "completed"
