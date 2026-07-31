# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""
Simple ACP echo agent for testing router / cross agent comms.
Uses agent-client-protocol SDK.

Run as: uv run python examples/acp-echo-agent.py
Then can be registered as downstream TUI-like agent in router.
"""

import asyncio
import sys
from pathlib import Path

# ensure import
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from acp import Agent, PromptResponse, run_agent, text_block  # noqa: F401
except ImportError:
    print("Need agent-client-protocol. uv pip install 'agent-client-protocol>=0.10.0,<0.12'")
    sys.exit(1)


class EchoAgent(Agent):
    """Minimal echo agent that returns the prompt as response, plus RemaGraph note if possible.
    Implements required protocol methods for ACP compatibility.
    """

    async def initialize(self, protocol_version: int, **kwargs):
        return {"protocol_version": protocol_version}

    async def new_session(self, cwd: str, **kwargs):
        return {"session_id": "echo-sess"}

    async def prompt(self, session_id: str, prompt: list, **kwargs) -> PromptResponse:
        # collect user text
        user_text = ""
        for p in prompt:
            if isinstance(p, str):
                user_text += p
            elif hasattr(p, "text"):
                user_text += getattr(p, "text", "")
            else:
                user_text += str(p)

        # simulate "work" and ack
        response_text = f"Echo from ACP downstream agent: {user_text[:200]} [ack via ACP]"

        # PPLX-recommended: send a side-channel report directly when the
        # agent finishes (no marker).
        import json
        import os
        import socket
        import time
        # This /tmp/tower-reports.sock is only a fallback guess for "running
        # standalone with TOWER_REPORT_SOCK unset" -- when spawned normally
        # by the router, it's always overridden with the actual path for
        # that run (see docs/governance/acp-direct-communication-pipeline.md §11).
        sock = os.environ.get("TOWER_REPORT_SOCK", "/tmp/tower-reports.sock")
        try:
            rpt = {
                "type": "task_report",
                "task_id": os.environ.get("TASK_ID", "unknown"),
                "agent_id": "echo-tui",
                "status": "completed",
                "result": {"echo": response_text[:300]},
                "version": 1
            }
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(sock)
                s.sendall((json.dumps(rpt) + "\n").encode())
        except OSError as _e:
            print("side report err", _e, file=sys.stderr)

        # optional: if herdr memory available, but for standalone demo just echo
        print(f"[echo-agent] received: {user_text[:100]}...", file=sys.stderr)

        # PPLX-recommended structured side-channel report (Herdr only manages
        # lifecycle; the report itself goes over an independent socket, no marker).
        try:
            import json
            import os
            import re
            import socket
            import time
            # This /tmp/tower-reports.sock is only a fallback guess for "running
            # standalone with TOWER_REPORT_SOCK unset" -- when spawned normally
            # by the router, it's always overridden with the actual path for
            # that run (see docs/governance/acp-direct-communication-pipeline.md §11).
            sock_path = os.environ.get("TOWER_REPORT_SOCK", "/tmp/tower-reports.sock")
            tid = "unknown"
            for p in prompt:
                txt = str(p) if not isinstance(p, str) else p
                m = re.search(r'task-[a-zA-Z0-9_-]+', txt)
                if m:
                    tid = m.group(0)
                    break
            aid = "echo-tui"
            report = {
                "type": "task_report",
                "task_id": tid,
                "agent_id": aid,
                "status": "completed",
                "result": {"text": response_text[:1000], "summary": response_text[:200]},
                "version": 1,
                "ts": time.time(),
            }
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(sock_path)
                s.sendall((json.dumps(report, ensure_ascii=False) + "\n").encode("utf-8"))
            print(f"[echo-agent] sent side-channel report for {tid}", file=sys.stderr)
        except OSError as e:
            print(f"[echo-agent] report send err (non-fatal): {e}", file=sys.stderr)

        # return proper with text in meta for collection in router
        from acp.schema import PromptResponse
        return PromptResponse(stopReason="end_turn", _meta={"echo_text": response_text})

    async def close_session(self, session_id: str, **kwargs):
        return None

    # other methods stub
    async def load_session(self, **kwargs): return None
    async def list_sessions(self, **kwargs): return {"sessions": []}
    async def cancel(self, **kwargs): pass
    async def ext_method(self, method: str, params: dict): return {}
    async def ext_notification(self, method: str, params: dict): pass
    def on_connect(self, conn): pass


async def main():
    agent = EchoAgent()
    print("Starting ACP Echo Agent (for herdr-bridge router demo)...", file=sys.stderr)
    # run_agent will handle stdio for ACP protocol
    await run_agent(agent)


if __name__ == "__main__":
    asyncio.run(main())
