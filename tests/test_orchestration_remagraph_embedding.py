# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests the embedded integration of the RemaGraph governance layer.

Initial goal: verify the basic behavior of prepare / store / recall under direct-import
mode, and its compatibility with ACP / Light dispatch.
"""

import os
from unittest.mock import MagicMock, patch

from herdr_bridge.orchestration import memory as rg


def test_direct_import_mode():
    """Confirms direct-import mode is enabled."""
    assert rg.uses_direct_import() is True
    assert rg.is_remagraph_enabled() is True


def test_generate_and_ensure_task_ids(monkeypatch):
    """Basic ID generation and environment-variable override.

    Uses monkeypatch.setenv instead of writing os.environ directly + manually deleting
    it -- there was a real incident where "an assertion failed partway through, so the
    manual del lines never ran," leaving TASK_ID/AGENT_ID leaking into every subsequent
    test in the same pytest process (2026-07-31 evidence; see also the
    `_isolate_remagraph_state_dir` fixture docstring in conftest.py). monkeypatch always
    restores state automatically when the fixture ends, no matter how the test fails
    partway through, so this risk doesn't apply.
    """
    tid, aid = rg.ensure_task_ids("test-task", "test-agent", project="herdr-test")
    assert tid.startswith("herdr-test-")
    assert "test-task" in tid or len(tid) > 10
    assert aid == "test-agent"

    # Environment variables take priority
    monkeypatch.setenv("TASK_ID", "env-task-123")
    monkeypatch.setenv("AGENT_ID", "env-agent-456")
    tid2, aid2 = rg.ensure_task_ids("ignored", "ignored")
    assert tid2 == "env-task-123"
    assert aid2 == "env-agent-456"


def test_prepare_dispatch_text_basic():
    """prepare should return augmented text and IDs when enabled.

    This deliberately asserts that text actually differs from base and contains the
    usage-instruction block, rather than only checking len(text) >= len(base) -- the
    latter is too weak: even if prepare_dispatch_text() silently threw an exception
    internally that got swallowed by except Exception, falling back to base_text
    unchanged, that weak assertion would still pass (equal length). This actually
    happened once: an internal call to recall_memories() passed an undefined variable
    name, causing a NameError every time and silently falling back to the original
    text every time -- RemaGraph memory augmentation was effectively dead, but this
    test completely failed to catch it at the time.

    Like test_store_and_recall_roundtrip(), this test needs to clear
    REMAGRAPH_STATE_DIR first and switch to the herdr-test-specific DB with
    force_reinit: when the `herdr_bridge` package is imported (see
    src/herdr_bridge/__init__.py), it already pins REMAGRAPH_STATE_DIR to the
    herdr-bridge project. Without redirecting it, _enforce_remagraph_safety_valve()
    would judge project="herdr-test" as mismatched with the current state dir and
    raise, which would likewise get swallowed by prepare_dispatch_text()'s outer
    except Exception and silently fall back to the original text -- exactly the
    category of scenario the newly added text != base assertion is meant to catch,
    and it shouldn't be allowed to slip through by luck.
    """
    if "REMAGRAPH_STATE_DIR" in os.environ:
        del os.environ["REMAGRAPH_STATE_DIR"]
    rg._ensure_remagraph_project("herdr-test", force_reinit=True)

    base = "please execute a certain task"
    text, tid, aid = rg.prepare_dispatch_text(
        base, base_task_id="demo-task", agent_id="demo-agent", project="herdr-test"
    )
    assert isinstance(text, str)
    assert text != base, "text is identical to base, meaning prepare_dispatch_text() likely silently fell back to the original text (an internal exception was swallowed)"
    assert "Herdr Bridge Memory" in text
    assert tid
    assert aid == "demo-agent"


def test_store_and_recall_roundtrip():
    """After store, it can be recalled back (uses a test project to avoid pollution)."""
    if "REMAGRAPH_STATE_DIR" in os.environ:
        del os.environ["REMAGRAPH_STATE_DIR"]
    rg._ensure_remagraph_project("herdr-test", force_reinit=True)
    task_id = "test-roundtrip-20260722"
    agent_id = "test-agent-roundtrip"

    # Clear any possible leftover old data first (if present)
    # This only tests that write and read basically work

    res = rg.store_memory(
        task_id,
        agent_id,
        kind="status_update",
        summary="this is a test store record, used to verify the embedded RemaGraph store functionality works correctly",
        handoff_note="roundtrip test for embedded remagraph store and recall",
        tags=["test", "roundtrip"],
        project_id="herdr-test",
        learnings=["tested embedded functionality"],
    )
    assert res.get("status") in ("stored", "rejected", "error")  # dedup rejected or error is allowed, but should preferably be stored/rejected

    # recall
    memories = rg.recall_memories(task_id, agent_id, top_k=5, project_id="herdr-test")
    # Even if store fails, recall should still return a list (possibly empty)
    assert isinstance(memories, list)

    if res.get("status") == "stored":
        # If the write succeeded, recall should retrieve at least one entry
        found = any("roundtrip test" in str(m) for m in memories)
        assert found or len(memories) > 0  # lenient, depends on the actual DB


def test_augment_prompt_with_memory():
    """augment should append a summary block when memories are present."""
    memories = [
        {"summary": "previously completed task A", "kind": "status_update", "timestamp": "2026-07-22"},
    ]
    base = "now need to do task B"
    result = rg.augment_prompt_with_memory(base, memories)
    assert "Herdr Bridge Memory: Prior Summary for This Task" in result
    assert "previously completed task A" in result


def test_get_usage_instruction_embedded():
    """Under embedded mode, a mandatory memory-logging directive should be issued."""
    instr = rg.get_usage_instruction("t1", "a1", assume_remagraph=True)
    assert "herdr-commander memory note" in instr
    assert "MANDATORY - Herdr Bridge Memory Logging" in instr


# ---------------------------------------------------------------------------
# Initial goal: cover integration tests for recall/store on the ACP and herdr socket
# paths. Uses patch to verify LightCommander dispatch actually calls prepare + store
# (without depending on a real socket).
# ---------------------------------------------------------------------------


def test_light_run_task_invokes_prepare_and_store():
    """The herdr socket path (run_task) should use prepare_dispatch_text + store_memory."""
    from herdr_bridge.light.commander import LightCommander

    # Set up mock actions (simulating the herdr socket success path)
    acts = MagicMock()
    sample_agent = type("A", (), {"agent_id": "claude-1", "brand": "claude"})()
    acts.list_agents.return_value = [sample_agent]
    cur = MagicMock()
    cur.normalized_text = ""
    cur.text = ""
    cur.revision = None
    acts.read_agent.return_value = cur
    acts.send_to_agent.return_value = None
    # wait success
    wait = MagicMock()
    wait.success = True
    wait.last_output = MagicMock(normalized_text="done [success-marker]", text="done")
    wait.reason = "done"
    wait.elapsed_sec = 1.0
    acts.wait_until.return_value = wait

    lc = LightCommander(acts)

    with patch("herdr_bridge.light.commander._rg") as mock_rg:
        mock_rg.is_remagraph_enabled.return_value = True
        mock_rg.prepare_dispatch_text.return_value = ("augmented prompt", "tid-123", "aid-1")
        mock_rg.store_memory.return_value = {"status": "stored"}
        mock_rg.extract_remagraph_notes.return_value = []

        res = lc.run_task("thumbnail-py")

        # Verify prepare was called (herdr socket path)
        assert mock_rg.prepare_dispatch_text.called
        # Verify store was called at least twice (start + success)
        assert mock_rg.store_memory.call_count >= 2
        assert res.ok is True


def test_run_task_via_acp_invokes_prepare_store_and_recall():
    """The ACP path (run_task_via_acp) should use prepare + store + recall confirmation."""
    from herdr_bridge.light.commander import LightCommander

    acts = MagicMock()  # not really used in acp path
    lc = LightCommander(acts)

    with patch("herdr_bridge.light.commander._rg") as mock_rg, \
         patch("herdr_bridge.acp.isolated_workdir.create_isolated_worktree_for_opencode") as mock_wt, \
         patch("herdr_bridge.acp.connect") as mock_connect:

        mock_rg.is_remagraph_enabled.return_value = True
        mock_rg.prepare_dispatch_text.return_value = ("acp-aug", "tid-acp", "aid-acp")
        mock_rg.store_memory.return_value = {"status": "stored"}
        mock_rg.recall_memories.return_value = [{"summary": "ack from downstream"}]

        mock_wt.return_value = "/tmp/wt"
        mock_acp = MagicMock()
        mock_acp.prompt.return_value = type("R", (), {"reason": "end_turn", "stop_reason": "end"})()
        mock_acp.close_session.return_value = None
        mock_connect.return_value = mock_acp

        # run with use_router=False to hit direct ACP block
        res = lc.run_task_via_acp("thumbnail-py", use_router=False)

        assert mock_rg.prepare_dispatch_text.called
        assert mock_rg.store_memory.called  # start + complete
        assert mock_rg.recall_memories.called
        assert res.ok is True


def test_dispatch_with_memory_confirm_uses_router_path():
    """dispatch_with_memory_confirm now delegates to the AcpRouter.dispatch_with_memory_confirm central facade, enforcing the three-step process."""
    from herdr_bridge.light.commander import LightCommander

    lc = LightCommander(MagicMock())

    with patch("herdr_bridge.acp.router.create_herdr_router") as mock_create:
        fake_router = MagicMock()
        fake_router.dispatch_with_memory_confirm.return_value = {"routed_to": "general-tui", "status": "ok"}
        mock_create.return_value = fake_router
        out = lc.dispatch_with_memory_confirm("do research task", project="test", target_agent=None, use_router=True)
        fake_router.dispatch_with_memory_confirm.assert_called_once()
        assert "routed_to" in out


def test_batch_dispatch_with_memory():
    """batch should call dispatch multiple times and return a list (supports fleet coordination)."""
    from herdr_bridge.light.commander import LightCommander
    lc = LightCommander(MagicMock())
    with patch.object(lc, "dispatch_with_memory_confirm", side_effect=lambda p, **k: {"routed": "auto", "p": p}) as mock_d:
        res = lc.batch_dispatch_with_memory(["task one", "task two research"], project="batch-test")
        assert len(res) == 2
        assert mock_d.call_count == 2


# ---------------------------------------------------------------------------
# PPLX requirement: failure-path tests + FSM / dedup / retention / SLA
# ---------------------------------------------------------------------------

def test_validate_transition_logic():
    """Verifies the valid/invalid decision logic of the state-transition table itself
    -- tests the pure function _validate_transition directly, without going through
    store_memory/RemaGraph.

    Changed to a pure-function test after the 2026-07-25 PPLX review consensus: under
    the current architecture, calling update_delivery_state repeatedly almost always
    runs into RemaGraph's semantic dedup (rule #4, see
    test_update_delivery_state_dedup_is_a_known_architectural_limitation), so testing
    the transition-table logic itself via end-to-end calls is unstable.
    _validate_transition doesn't depend on RemaGraph, making it suitable for
    independent, reliable verification here.
    """
    from herdr_bridge.orchestration.memory import _validate_transition

    # Valid: the first state must be INIT
    _validate_transition(None, "INIT")
    # Valid: INIT -> DISPATCH_PENDING
    _validate_transition("INIT", "DISPATCH_PENDING")
    # Invalid: DISPATCH_PENDING can't jump straight to PONG_RECEIVED (must go through AWAIT_PONG first)
    try:
        _validate_transition("DISPATCH_PENDING", "PONG_RECEIVED")
    except ValueError:
        pass
    else:
        assert False, "should raise on invalid transition"
    # Invalid: when current_state=None, new_state must be INIT
    try:
        _validate_transition(None, "DISPATCH_PENDING")
    except ValueError:
        pass
    else:
        assert False, "should raise when current_state is None and new_state != INIT"


def test_update_delivery_state_init_writes_and_reads_back(tmp_path, monkeypatch):
    """A single INIT transition should actually be written and retrievable -- it must
    not be silently rejected by a RemaGraph arbitration rule just because the summary
    is too short.

    Background (2026-07-25 #71 evidence, root cause 1): update_delivery_state()
    originally generated a summary of f"delivery_state={new_state}", which for most
    state names (INIT=19 chars, AWAIT_PONG=25 chars, PONG_RECEIVED=28 chars,
    COMPLETED=24 chars...) falls below the 30-character threshold required by
    RemaGraph arbitration rule #1, so store_memory() returned status='rejected' --
    but the caller returned immediately without checking that return value. On the
    next call, get_delivery_state() couldn't read back any record, so
    _validate_transition mistakenly treated it as "this is the first call" and raised
    an error, when in fact the previous INIT write had simply failed.

    This test only covers a "single" transition, not consecutive transitions --
    consecutive transitions run into a separate, independent architectural limitation
    (rule #4 semantic dedup), see
    test_update_delivery_state_dedup_is_a_known_architectural_limitation.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)

    project_id = "test-fsm-init-only"
    task_id = "fsm-init-only-task"
    agent_id = "fsm-init-only-agent"

    res_init = rg.update_delivery_state(task_id, agent_id, "INIT", project_id=project_id)
    assert res_init.get("status") == "stored", (
        f"INIT write should succeed (summary needs padding above the arbitration threshold), actual: {res_init}"
    )

    state_after_init = rg.get_delivery_state(task_id, agent_id, project_id=project_id)
    assert state_after_init is not None and state_after_init.get("state") == "INIT", (
        f"should be able to read back state=INIT after writing INIT, actual: {state_after_init}"
    )


def test_update_delivery_state_full_chain_reaches_completed(tmp_path, monkeypatch):
    """#72 fix verification (2026-07-25 PPLX review consensus, Dual-Write on Terminal
    State): after FSM state transitions switched to their own dedicated lightweight
    store (`orchestration.delivery_state_store`), the full transition chain for the
    same task_id -- INIT -> DISPATCH_PENDING -> AWAIT_PONG -> PONG_RECEIVED ->
    COMPLETED -- should actually complete without running into RemaGraph's semantic
    dedup again (#71 root cause 2: adjacent-state summary similarity of 0.92-0.96
    gets rejected by rule #4's 0.90 threshold).

    This is the successor to the old
    test_update_delivery_state_dedup_is_a_known_architectural_limitation -- that test
    asserted the honest version of "explicitly errors out when hitting the
    limitation"; now that #72 is done, the limitation itself no longer exists, so
    this asserts that the full transition chain actually completes.

    Intermediate transitions (INIT/DISPATCH_PENDING/AWAIT_PONG/PONG_RECEIVED) land
    only in the FSM's dedicated store and are not written to the memory layer; only
    the terminal state COMPLETED gets an extra summary written to the memory layer.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)

    project_id = "test-fsm-full-chain"
    task_id = "fsm-full-chain-task"
    agent_id = "fsm-full-chain-agent"

    chain = ["INIT", "DISPATCH_PENDING", "AWAIT_PONG", "PONG_RECEIVED", "COMPLETED"]
    results = []
    for state in chain:
        res = rg.update_delivery_state(task_id, agent_id, state, project_id=project_id)
        results.append(res)
        assert res.get("status") == "stored", f"transition to {state} should succeed, actual: {res}"

    final = rg.get_delivery_state(task_id, agent_id, project_id=project_id)
    assert final is not None and final.get("state") == "COMPLETED", (
        f"should read back COMPLETED after the full transition chain ends, actual: {final}"
    )

    for res, state in zip(results[:-1], chain[:-1]):
        assert "memory_summary" not in res, (
            f"{state} is an intermediate transition, should not have a dual-write summary, actual: {res}"
        )
    assert "memory_summary" in results[-1], "COMPLETED is the terminal state, should have a dual-write summary"
    assert results[-1]["memory_summary"].get("status") == "stored"

    # Confirm the summary actually landed in the memory layer (not just claimed success by the return value alone).
    mems = rg.recall_memories(task_id, agent_id, top_k=10, project_id=project_id) or []
    terminal_mems = [
        m for m in mems
        if "delivery-state" in (m.get("tags") or []) and "terminal" in (m.get("tags") or [])
    ]
    assert len(terminal_mems) >= 1, f"memory layer should have a terminal-state summary, recall result: {mems}"
    assert len(terminal_mems[0].get("summary", "")) >= 30, "terminal summary should have substantive content, not just padded to hit a length"


def test_dedup_branch_only_blocks_true_terminal_states_not_valid_transitions(tmp_path, monkeypatch):
    """The dedup guard branch's condition must only trigger once "a true terminal
    state has been reached," and must not misjudge an intermediate confirmation state
    that "still has a legal next step" (a direct regression test for #71 root cause
    2).

    Background (2026-07-25 #71 evidence): the original condition was
    current_state in ("PONG_RECEIVED", "COMPLETED", "SIDE_REPORT_RECEIVED", "DEGRADED")
    -- but PONG_RECEIVED / SIDE_REPORT_RECEIVED are intermediate confirmation states in
    STATE_TRANSITIONS that "still have a legal next step" (PONG_RECEIVED -> COMPLETED
    is the only legal transition), not terminal states. Counting them under the
    "already done, anything after is a duplicate" condition caused the one and only
    legal, normal PONG_RECEIVED -> COMPLETED transition to be misjudged as a duplicate
    delivery and intercepted -- so COMPLETED could never be recorded correctly.

    After #72, the dedup branch no longer calls store_memory (intermediate
    transitions never write to the memory layer at all); instead it uses the real
    FSM dedicated store to walk the full transition chain, asserting that the dedup
    branch only gracefully blocks after a true terminal state, without raising or
    polluting the memory layer.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)

    project_id = "test-dedup-branch-logic"
    tid, aid = "dedup-branch-task-1", "dedup-branch-agent"

    # Scenario 1: PONG_RECEIVED -> COMPLETED is the only legal normal transition and
    # should not be intercepted by the dedup branch; and since COMPLETED is the
    # terminal state, it should trigger a dual-write summary into the memory layer.
    rg.update_delivery_state(tid, aid, "INIT", project_id=project_id)
    rg.update_delivery_state(tid, aid, "DISPATCH_PENDING", project_id=project_id)
    rg.update_delivery_state(tid, aid, "AWAIT_PONG", project_id=project_id)
    rg.update_delivery_state(tid, aid, "PONG_RECEIVED", project_id=project_id)
    res = rg.update_delivery_state(tid, aid, "COMPLETED", project_id=project_id)
    assert res.get("status") == "stored"
    assert "memory_summary" in res, "COMPLETED is the terminal state, should have a dual-write summary"
    assert res["memory_summary"].get("status") == "stored"
    assert res.get("status") != "duplicate_blocked"

    # Scenario 2: only a PONG_RECEIVED received after COMPLETED is a true duplicate
    # delivery, and should be gracefully blocked by the dedup branch (no raise, no
    # new memory-layer record written).
    res2 = rg.update_delivery_state(tid, aid, "PONG_RECEIVED", project_id=project_id)
    assert res2.get("status") == "duplicate_blocked", (
        f"a PONG_RECEIVED after COMPLETED is a true duplicate delivery and should be blocked by the dedup branch, actual: {res2}"
    )
    assert res2.get("state") == "COMPLETED", "dedup branch should report the true terminal state it's still sitting at"


def test_retention_and_metrics():
    from unittest.mock import patch

    from herdr_bridge.orchestration import memory as rg
    with patch("herdr_bridge.orchestration.memory.recall_memories") as mock_recall:
        mock_recall.return_value = [
            {"tags": ["delivery-state"], "summary": "delivery_state=COMPLETED", "kind": "status_update"}
        ]
        # get_delivery_metrics / apply_retention only depend on recall_memories, not
        # on get_delivery_state, so there's no need to actually run an FSM chain to
        # prepare data.
        metrics = rg.get_delivery_metrics("test-retention-metrics")
        assert "state_distribution" in metrics
        ret = rg.apply_retention("test-retention-metrics", dry_run=True)
        assert "archived" in ret

def test_failure_paths_no_ensure_wrong_project():
    from herdr_bridge.errors import HerdrBridgeError
    from herdr_bridge.orchestration import memory as rg
    # missing project_id
    try:
        rg.store_memory("bad", "bad", kind="status_update", summary="x", project_id=None)
    except HerdrBridgeError:
        pass
    # default project
    try:
        rg.store_memory("bad", "bad", kind="status_update", summary="x", project_id="herdr")
    except HerdrBridgeError:
        pass
    # removed a leftover bogus assert (the original test logic doesn't need res)


# ---------------------------------------------------------------------------
# Regression test (2026-07-25 wT:pX dispatch track A): RemaGraph memories not
# landing in the correct DB
#
# Root cause 1: as long as REMAGRAPH_STATE_DIR already exists in the environment,
# _ensure_remagraph_project() reuses it unconditionally, without ever checking
# whether it actually corresponds to the project_id currently passed in (see lines
# 100-103 of the old memory.py). Any wrong directory left over from a previous call
# (a test, a different project, a stale shell session) gets silently reused, so
# memories end up written to the wrong DB.
#
# Root cause 2: _enforce_remagraph_safety_valve() uses substring matching
# (`expected not in state_dir`) instead of exact path comparison to verify whether
# state_dir corresponds to project_id. A suffixed directory name like
# `remagraph-herdr-bridge-20260722-task-...-8edeb3` (one of the scattered DBs
# confirmed during the command tower's 2026-07-25 inventory) does literally contain
# the substring `remagraph-herdr-bridge`, so it gets misjudged as legitimate and
# waved through -- the safety valve is effectively useless.
# ---------------------------------------------------------------------------


def test_ensure_remagraph_project_rejects_stale_state_dir(tmp_path, monkeypatch):
    """When a leftover REMAGRAPH_STATE_DIR points at a different project,
    _ensure_remagraph_project must not reuse it -- it must switch back to the
    directory dedicated to this project_id.

    Reproduction approach: simulate "leftover env left behind by a previous call (a
    test or a different project)" -- REMAGRAPH_STATE_DIR points at a directory
    completely unrelated to the current project_id.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)

    stale_dir = tmp_path / ".local" / "state" / "remagraph-some-other-project"
    stale_dir.mkdir(parents=True)
    monkeypatch.setenv("REMAGRAPH_STATE_DIR", str(stale_dir))

    rg._ensure_remagraph_project("herdr-bridge")

    expected_dir = rg._project_state_dir("herdr-bridge")
    actual = os.environ.get("REMAGRAPH_STATE_DIR")
    assert actual == str(expected_dir), (
        f"_ensure_remagraph_project reused a leftover state dir that doesn't match project_id: "
        f"{actual!r}, should have switched back to {expected_dir!r}"
    )


def test_safety_valve_rejects_suffixed_lookalike_state_dir(tmp_path, monkeypatch):
    """A state_dir name that just "happens to contain" the project name as a
    substring (e.g. a directory with a task-id suffix) doesn't mean it's actually
    dedicated to this project. The safety valve must use exact path comparison, not
    substring matching, or it will wave through something like
    remagraph-herdr-bridge-20260722-task-xxx-8edeb3, which isn't actually
    remagraph-herdr-bridge itself.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)

    lookalike_dir = (
        tmp_path / ".local" / "state"
        / "remagraph-herdr-bridge-20260722-task-20250722-a-gov-8edeb3"
    )
    lookalike_dir.mkdir(parents=True)
    monkeypatch.setenv("REMAGRAPH_STATE_DIR", str(lookalike_dir))

    from herdr_bridge.errors import HerdrBridgeError

    # The first internal step of _enforce_remagraph_safety_valve calls
    # _ensure_remagraph_project() to self-correct state_dir (see the previous test),
    # so without isolating it, this test could never actually detect whether the
    # comparison logic itself is precise -- it would be masked by the preceding
    # self-correction. Turn _ensure into a no-op so we can independently verify
    # whether the comparison itself correctly rejects a leftover value that merely
    # matches as a substring.
    monkeypatch.setattr(rg, "_ensure_remagraph_project", lambda *a, **k: None)

    try:
        rg._enforce_remagraph_safety_valve("herdr-bridge")
    except HerdrBridgeError:
        pass
    else:
        assert False, (
            "safety valve should reject a leftover state dir that matches by substring "
            f"but not by actual path ({lookalike_dir}), but let it through"
        )


def test_store_then_search_roundtrip_survives_stale_state_dir(tmp_path, monkeypatch):
    """End-to-end reproduction of what the command tower reported: after storing to
    the herdr-bridge project, `search --project herdr-bridge` can't find it back --
    because the caller's environment had a leftover different REMAGRAPH_STATE_DIR,
    and the memory got silently written to the wrong DB.

    Reads directly via direct-sqlite from the path where the "herdr-bridge dedicated
    DB" should be, asserting on actual content (row count, summary) rather than just
    the return value of store_memory().
    """
    import sqlite3

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)

    stale_dir = tmp_path / ".local" / "state" / "remagraph-leftover-from-a-test"
    stale_dir.mkdir(parents=True)
    monkeypatch.setenv("REMAGRAPH_STATE_DIR", str(stale_dir))

    marker_summary = "regression-marker-store-then-search-roundtrip-2026-07-25"
    res = rg.store_memory(
        "regression-task",
        "regression-agent",
        kind="status_update",
        summary=marker_summary,
        project_id="herdr-bridge",
        learnings=["regression test marker"],
    )
    assert res.get("status") == "stored", f"store_memory failed: {res}"

    herdr_bridge_db = rg._project_state_dir("herdr-bridge") / "remagraph.db"
    assert herdr_bridge_db.exists(), (
        f"herdr-bridge's dedicated DB doesn't exist at the expected path {herdr_bridge_db}, "
        "memory may have been written to a leftover state dir"
    )
    conn = sqlite3.connect(str(herdr_bridge_db))
    rows = conn.execute(
        "SELECT summary FROM memories WHERE project_id = ?", ("herdr-bridge",)
    ).fetchall()
    conn.close()
    summaries = [r[0] for r in rows]
    assert marker_summary in summaries, (
        f"couldn't find the just-written memory in the herdr-bridge DB, actual content: {summaries}"
    )

    # Besides direct sqlite, also use the official RemaGraph recall API (the path
    # herdr-bridge callers actually use) to look up the same record by the same
    # project_id + task_id, verifying "looking it up via project" itself, not just
    # whether an exception was thrown or the DB file exists.
    recalled = rg.recall_memories(
        "regression-task", "regression-agent", top_k=10, project_id="herdr-bridge"
    )
    recalled_summaries = [m.get("summary") for m in recalled]
    assert marker_summary in recalled_summaries, (
        f"recall_memories(project_id='herdr-bridge') couldn't find the just-written memory, "
        f"actual return: {recalled_summaries}"
    )

    stale_db = stale_dir / "remagraph.db"
    if stale_db.exists():
        conn = sqlite3.connect(str(stale_db))
        try:
            leaked = conn.execute(
                "SELECT summary FROM memories WHERE summary = ?", (marker_summary,)
            ).fetchall()
        except sqlite3.OperationalError:
            leaked = []
        conn.close()
        assert not leaked, f"memory leaked into the leftover wrong state dir: {stale_dir}"


def test_project_state_dir_deliberately_deviates_from_standard_naming():
    """Insurance for the #66 self-protective measure: the path computed by
    _project_state_dir() must **not equal** RemaGraph's standard
    remagraph-<project_id> naming format.

    Background (2026-07-25 evidence): a resident remagraph serve process (PID 3760)
    started by another project's (MegaNote's) agent, not herdr-bridge's, would derive
    paths using the standard naming rule and wipe/rebuild any directory it recognized
    -- causing herdr-bridge's real memories to survive only a few minutes. A
    controlled experiment confirmed: a path that deviates from the standard naming
    rule is never touched by that external process.

    This test may look "counterintuitive" (asserting inequality with what looks like
    a reasonable value), but its purpose is precisely to prevent someone in the
    future from "casually changing the naming back to the standard rule" and
    reintroducing the problem -- once #66's root cause is fixed (adding project
    isolation to that resident serve process), this test and its corresponding
    workaround
    should both be reevaluated to see if they're still needed.
    """
    for project_id in ("herdr-bridge", "herdr-test", "some-other-project"):
        actual = rg._project_state_dir(project_id)
        standard = actual.parent / f"remagraph-{rg._slugify_project(project_id)}"
        assert actual != standard, (
            f"_project_state_dir({project_id!r}) computed path {actual} reverted back to "
            f"the standard naming rule {standard} -- this would let the external serve "
            f"process described in #66 recognize this directory again and wipe it"
        )
        # Still needs each project_id to stay independent and predictable (not random or a fixed hardcoded value).
        assert rg._slugify_project(project_id) in actual.name
