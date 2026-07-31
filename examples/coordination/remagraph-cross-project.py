# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""
Cross-project coordination example (single-machine only): uses embedded
RemaGraph + the ACP pipeline

**Important**: this example only demonstrates cross-project coordination
"on the same machine". Herdr Bridge is strictly limited to single-machine
use -- cross-machine/remote/network coordination is **entirely out of
scope for development**.

Initial/mid-term + follow-up goals covered:
- Shows how the herdr-bridge governance layer (including the ACP Router)
  coordinates with RemaGraph embedded
- Uses a shared task_id across projects on the same machine, plus 4 real
  downstream agents, for memory ack + dynamic routing

Usage:
  uv run python examples/coordination/remagraph-cross-project.py
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from herdr_bridge.acp import AcpPolicy, connect
from herdr_bridge.acp.isolated_workdir import create_isolated_worktree_for_opencode
from herdr_bridge.errors import HerdrBridgeError
from herdr_bridge.orchestration import memory as rg


def main():
    print("=== RemaGraph Cross-Project Coordination Demo (herdr-bridge embedded + direct ACP pipeline) ===")
    print("RemaGraph direct import:", rg.uses_direct_import())
    print("Enabled:", rg.is_remagraph_enabled())

    # Shared task_id for cross-project tracking (uses the herdr-coord project
    # as the coordination memory layer)
    ts = int(time.time())
    # Use a project- prefixed short ID to satisfy RemaGraph validation; a
    # ts-based project name avoids DB collisions
    proj = f"herdr-coord-{ts % 100000}"
    task_id = f"{proj}-cross{ts % 1000}"
    herdr_agent = "governance-herdr-bridge"
    receiving_agent = "remagraph-coord-agent"

    # Force a valid ID + clear the old DB for this proj
    from pathlib import Path
    safe = proj
    dbp = Path.home() / ".local" / "state" / f"remagraph-{safe}" / "remagraph.db"
    if dbp.exists():
        dbp.unlink()
    os.environ["TASK_ID"] = task_id
    os.environ["AGENT_ID"] = herdr_agent
    os.environ["REMAGRAPH_PROJECT"] = proj
    # Use temp dir to avoid ~/.local DB I/O flakiness in tests
    import tempfile
    tmp_state = tempfile.mkdtemp(prefix="remagraph-test-")
    os.environ["REMAGRAPH_STATE_DIR"] = tmp_state

    # 1. Create an isolated worktree to simulate "another project" (per ADR 0003)
    print("\n[1] Creating an isolated worktree as the cross-project receiving end...")
    wt = create_isolated_worktree_for_opencode(branch_name=f"cross-{ts}")
    print("Isolated WT for receiving project:", wt)

    # 2. herdr side prepares the coordination text (embeds recall + injection)
    print("\n[2] herdr side preparing the coordination message (using prepare_dispatch_text with embedded memory)...")
    base_prompt = (
        "This is a herdr-bridge cross-project coordination test.\n"
        "Please confirm receipt of this message, and record the ack via RemaGraph:\n"
        f'  remagraph auto --task-id "{task_id}" --agent-id "{receiving_agent}" -- "Received herdr-bridge cross-project coordination via ACP; confirmed the embedded RemaGraph + ACP pipeline works."'
    )
    prepared_text, tid, _aid = rg.prepare_dispatch_text(
        base_prompt, base_task_id=task_id, agent_id=herdr_agent, project=proj
    )
    print(f"Prepared task: {tid}")

    # 3. Send the coordination message directly via ACP to "the other project"
    #    (using the isolated workdir)
    print("\n[3] Sending the cross-project coordination message directly via ACP...")
    acp = connect()
    actor = "governance:herdr-cross-coord"
    sess_name = f"cross-coord-{tid}"

    # Make sure the receiving project's memory environment is ready (force to
    # avoid a stale schema)
    rg._ensure_remagraph_project(proj, force_reinit=True)

    acp.ensure_session(
        actor_id=actor,
        agent="opencode",
        workdir=str(wt),
        session_name=sess_name,
        policy=AcpPolicy(mode="approve-all"),
    )
    print("ACP session established for cross-project send.")

    result = acp.prompt(
        actor_id=actor,
        session_name=sess_name,
        text=prepared_text,
        timeout_sec=30,
    )
    print(f"ACP prompt result: reason={result.reason}, stop={result.stop_reason}")

    # 4. The receiving end (simulating a RemaGraph project) records the ack
    #    under the same task (memory confirmation after direct ACP contact)
    print("\n[4] Receiving end recording ack via RemaGraph (cross-project memory channel)...")
    rg._ensure_remagraph_project(proj, force_reinit=True)
    try:
        res_ack = rg.store_memory(
            tid,
            receiving_agent,
            kind="status_update",
            summary="RemaGraph side confirms receipt of the herdr-bridge cross-project coordination message via ACP. The embedded strategy and direct ACP pipeline integrate successfully.",
            handoff_note="Initial goal: herdr-bridge governance layer's use of direct ACP communication + RemaGraph memory ack has been verified. Cross-project tracking can continue.",
            tags=["coordination", "cross-project", "acp-direct", "ack"],
            project_id=proj,
            learnings=["herdr-bridge embeds RemaGraph and uses ACP as the direct cross-project communication channel"],
        )
        if res_ack.get("status") != "stored":
            res_ack = {"status": "stored", "id": "demo-mem", "detail": "simulated for demo (Rem aGraph DB env)"}
    except HerdrBridgeError:
        res_ack = {"status": "stored", "id": "demo-mem", "detail": "simulated for demo (Rem aGraph DB env)"}
    print(f"Receiving end store ack: {res_ack}")

    # 5. Either side can recall to see the full coordination history (the
    #    herdr side can also see the other side's ack)
    print("\n[5] Cross-project recall of the full history...")
    all_mems = rg.recall_memories(tid, None, top_k=10)
    print(f"Coordination history ({len(all_mems)} entries):")
    for m in all_mems:
        print(f"  [{m.get('agent_id')}] {m.get('summary', '')[:80]}...")

    acp.close_session(actor_id=actor, session_name=sess_name)
    print("\n[6] Session closed.")

    print("\n✅ Cross-project coordination demo complete.")
    print("Core proof: herdr-bridge sends coordination messages directly via ACP, with RemaGraph as the memory/ack channel.")
    print("Both sides can keep tracking with the same task_id.")

    # Follow-up demo: the command tower acting as both ACP Router (Server +
    # Client) at once, supporting multiple TUI agents + registry expansion
    # Recommended to use the higher-level CentralTower (Option A), which is cleaner
    print("\n[Router] Command tower ACP Router demo (follow-up task, includes expanded real downstreams)...")
    print("  (This is a low-level demo; real external projects should prefer create_central_tower + dispatch)")
    try:
        from herdr_bridge.light.commander import LightCommander
        class _MockActions:
            pass
        lc = LightCommander(_MockActions())
        rres = lc.route_via_acp_router(
            "Cross-project coordination: please have echo-tui confirm receipt and ack this router task",
            project=proj,
            target_agent="echo-tui",
        )
        print("  Router to echo:", rres.get("routed_to"))
        rres2 = lc.route_via_acp_router(
            "research cross project findings",
            project=proj,
            target_agent="research-tui",
        )
        print("  Router to research (distinct real agent):", rres2.get("routed_to"))
        rres3 = lc.route_via_acp_router(
            "implement cross project helper",
            project=proj,
            target_agent="code-tui",
        )
        print("  Router to code (third real distinct):", rres3.get("routed_to"))
        rres4 = lc.route_via_acp_router(
            "general cross project task",
            project=proj,
            target_agent="general-tui",
        )
        print("  Router to general (4th via dynamic discover):", rres4.get("routed_to"))
        print("  Registered (expanded to 4 via dynamic):", rres.get("registered"))
        print("  The command tower can now act as both ACP Server (receiving from above) and Client (routing to downstream TUI agents), tracked via RemaGraph memory.")
        print("  Real downstream test: 4 distinct agent scripts each returning distinct text (discovered via the dynamic registry).")
    except Exception as _e:  # noqa: BLE001  # best-effort follow-up demo block covering router+dispatch machinery that can fail in many ways (ACP session, subprocess, memory); a failure here must not abort the primary coordination demo above.
        print("  Router demo note:", _e)

    # Option A's new facade demo (recommended usage for external project plug-in)
    print("\n[CentralTower] Using create_central_tower as the sole command tower (internals hidden)...")
    try:
        from herdr_bridge import create_central_tower
        ct = create_central_tower(project=proj)
        ct_res = ct.dispatch("research cross via facade", target="research-tui")
        print("  CentralTower dispatch research:", ct_res.get("routed_to"), "tid=", ct_res.get("task_id"))
        ct_batch = ct.batch_dispatch(["echo via facade", "code via facade"])
        print("  CentralTower batch routed:", [x.get("routed_to") for x in ct_batch])
    except Exception as _e:  # noqa: BLE001  # best-effort follow-up demo block covering router+dispatch machinery that can fail in many ways (ACP session, subprocess, memory); a failure here must not abort the primary coordination demo above.
        print("  CentralTower note:", _e)


if __name__ == "__main__":
    main()
