"""Minimal hypothetical governance rule (M1 interface completeness validation, spec §6).

Rule: wait for FAILED/ERROR to appear in the tester agent's output, capture a
summary of the tail of that output, and forward it to the reviewer agent.
Uses only the public herdr_bridge API throughout.

Usage:
    uv run python examples/failed_forwarder.py --tester <agent_id> --reviewer <agent_id>
"""

from __future__ import annotations

import argparse
import re

from herdr_bridge import connect

FAIL_PATTERN = re.compile(r"FAILED|ERROR")
ACTOR = "rule:failed-forwarder"  # format specified by Governance Memo v1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tester", required=True)
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    actions = connect()
    agents = {a.agent_id: a for a in actions.list_agents(ACTOR)}
    for role, agent_id in (("tester", args.tester), ("reviewer", args.reviewer)):
        if agent_id not in agents:
            raise SystemExit(f"{role} agent {agent_id!r} not found; "
                             f"known: {sorted(agents)}")

    result = actions.wait_until(
        ACTOR, args.tester,
        predicate=lambda out: bool(FAIL_PATTERN.search(out.text)),
        timeout_sec=args.timeout, poll_interval_sec=2,
    )
    if not result.success:
        print(f"no failure observed within {args.timeout}s ({result.reason})")
        return

    context = actions.read_agent(ACTOR, args.tester)
    summary = context.text[-2000:]
    actions.send_to_agent(
        ACTOR, args.reviewer,
        f"[herdr-bridge] tester {args.tester} reported failure; "
        f"recent output:\n{summary}",
        priority=1,
    )
    print(f"forwarded failure context to reviewer {args.reviewer}")


if __name__ == "__main__":
    main()
