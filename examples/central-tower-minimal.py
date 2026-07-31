# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""
Minimal central tower example (20-30 lines) -- the Option A goal.

Shows how to plug in herdr-bridge as the "sole command tower" with the least
amount of code:
- create_central_tower hides all the internals
- dispatch / batch_dispatch automatically do RemaGraph prepare + store
- target=None auto-routes

Run:
  uv run python examples/central-tower-minimal.py
"""

from herdr_bridge import create_central_tower
from herdr_bridge.orchestration import memory as rg

# For a custom project, the entry point also needs to ensure it exists
# (create_central_tower does this internally too).
rg._ensure_remagraph_project("minimal-tower-demo")

def main():
    print("=== Central Tower Minimal Example ===")
    tower = create_central_tower(project="minimal-tower-demo")
    print("Tower created, agents:", tower.list_agents())

    # Single dispatch, target=None -> auto-route
    r1 = tower.dispatch("echo test message from central tower")
    print("dispatch echo:", r1.get("routed_to"), "ok=", r1.get("ok"), "tid=", r1.get("task_id"))

    # Explicit target
    r2 = tower.dispatch("research cross-project coordination best practices", target="research-tui")
    print("dispatch research:", r2.get("routed_to"))

    # Batch (mixed prompt types, router assigns based on capabilities)
    batch = tower.batch_dispatch([
        "implement a small helper",
        "general question about agents",
    ])
    print("batch results:", [ (b.get("routed_to"), b.get("ok")) for b in batch ])

    print("✅ Central tower can now be plugged directly into external projects.")
    print("Every dispatch path enforces a RemaGraph memory ack.")

if __name__ == "__main__":
    main()
