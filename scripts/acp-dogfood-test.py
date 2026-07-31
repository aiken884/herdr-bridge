#!/usr/bin/env python3
"""Live ACP dispatch dogfood test (one-off, exploratory — not a long-term-maintained script).

Must be run with `uv run python scripts/acp-dogfood-test.py` from the herdr-bridge
repo root — `connect()` uses `Path.cwd()` to find `.vendor/opencode-patched/`.

Runs five scenarios in order, each printing a clear PASS/FAIL plus what was actually observed.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from herdr_bridge.acp.actions import connect
from herdr_bridge.acp.errors import AcpSessionError
from herdr_bridge.acp.models import AcpPolicy

SANDBOX = Path("/tmp/acp-dogfood-sandbox")
PRIMARY_WORKTREE = Path("/Users/aikenlin/Projects/herdr-bridge")


def scenario_1_basic_roundtrip(acp):
    print("\n=== Scenario 1: basic session/prompt round trip ===")
    target = SANDBOX / "hello.txt"
    target.unlink(missing_ok=True)
    acp.ensure_session("dogfood", "opencode", str(SANDBOX), "s1", policy=AcpPolicy(mode="approve-all"))
    result = acp.prompt("dogfood", "s1", "Create a file named hello.txt in the current directory with the content hi. Don't ask me, just do it.")
    print(f"reason={result.reason} stop_reason={result.stop_reason}")
    print(f"file exists: {target.exists()}, content: {target.read_text() if target.exists() else None!r}")
    ok = result.reason == "stop" and target.exists()
    print("PASS" if ok else "FAIL")
    acp.close_session("dogfood", "s1")
    return ok


def scenario_2_primary_worktree_rejected(acp):
    print("\n=== Scenario 2: pointing at the primary worktree should be rejected ===")
    try:
        acp.ensure_session("dogfood", "opencode", str(PRIMARY_WORKTREE), "s2")
        print("FAIL: no exception was raised, workdir isolation is not in effect")
        return False
    except AcpSessionError as exc:
        print(f"Correctly rejected: {exc}")
        print("PASS")
        return True


def scenario_3_cancel_reason(acp):
    print("\n=== Scenario 3: real in-generation cancel ===")
    acp.ensure_session("dogfood", "opencode", str(SANDBOX), "s3", policy=AcpPolicy(mode="approve-all"))
    handle = acp.start_prompt("dogfood", "s3", "Write a long article of at least 2000 words about the weather, as detailed as possible")
    # Wait until content is actually being generated before canceling (avoids a false pre-generation boundary)
    time.sleep(3)
    acp.cancel("dogfood", handle)
    result = acp.wait_done("dogfood", handle, timeout_sec=15)
    print(f"reason={result.reason} stop_reason={result.stop_reason}")
    ok = result.reason == "canceled"
    print("PASS" if ok else "FAIL")
    acp.close_session("dogfood", "s3")
    return ok


def scenario_4_deny_all(acp):
    print("\n=== Scenario 4: deny-all permission actually blocks the action ===")
    target = SANDBOX / "should-not-exist.txt"
    target.unlink(missing_ok=True)
    acp.ensure_session("dogfood", "opencode", str(SANDBOX), "s4", policy=AcpPolicy(mode="deny-all"))
    result = acp.prompt("dogfood", "s4", "Create a file named should-not-exist.txt with any content")
    print(f"reason={result.reason} file exists: {target.exists()}")
    ok = not target.exists()
    print("PASS" if ok else "FAIL")
    acp.close_session("dogfood", "s4")
    return ok


def scenario_5_subagent_delegation(acp):
    print("\n=== Scenario 5: G1 regression — subagent delegation should not hang ===")
    acp.ensure_session(
        "dogfood", "opencode", str(SANDBOX), "s5",
        policy=AcpPolicy(mode="deny-all"),
    )
    start = time.monotonic()
    result = acp.prompt(
        "dogfood", "s5",
        "Use the task tool to delegate a subagent to create a file named delegated.txt with the content done",
        timeout_sec=45,
    )
    elapsed = time.monotonic() - start
    print(f"reason={result.reason} elapsed={elapsed:.1f}s")
    ok = result.reason != "timeout" and elapsed < 40
    print("PASS" if ok else "FAIL — looks like we hit G1 or timed out")
    acp.close_session("dogfood", "s5")
    return ok


def main():
    SANDBOX.mkdir(parents=True, exist_ok=True)
    acp = connect(strict_version=True)
    results = {}
    results["1_basic_roundtrip"] = scenario_1_basic_roundtrip(acp)
    results["2_primary_worktree_rejected"] = scenario_2_primary_worktree_rejected(acp)
    results["3_cancel_reason"] = scenario_3_cancel_reason(acp)
    results["4_deny_all"] = scenario_4_deny_all(acp)
    results["5_subagent_delegation"] = scenario_5_subagent_delegation(acp)

    print("\n\n=== Summary ===")
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
