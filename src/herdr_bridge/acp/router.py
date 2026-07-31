# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""
AcpRouter — the command tower acting as an ACP Router (both Server and Client roles).

Goals:
- Receive tasks from an upstream ACP client (acting as Server).
- Route tasks by capability to different downstream TUI/ACP agents (acting as Client).
- Use an embedded RemaGraph for task-state tracking, handoff, and cross-agent memory acks.
- Support multiple ACP-capable TUI agents (opencode, claude, gemini, codex, etc.).

References:
- Agent Client Protocol (https://agentclientprotocol.dev)
- python-sdk quickstart: subclass acp.Agent as server, spawn_agent_process as client.
- Combined with herdr-bridge's existing custom ACP + embedded RemaGraph.

Minimal implementation: the Router itself is an Agent, can register downstream agents,
and on prompt() routes + aggregates + records memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import select
import shlex
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from typing import Any

try:
    from acp import (
        PROTOCOL_VERSION,
        Agent,
        PromptResponse,
        spawn_agent_process,
        text_block,
    )
    from acp.interfaces import Client
    ACP_SDK_AVAILABLE = True
except Exception:  # noqa: BLE001
    # 2026-07-25: acp-sdk has been promoted to a primary dependency (pyproject.toml
    # `dependencies`), so this except branch should normally never be hit; kept as a
    # defensive fallback (e.g. a corrupted install or a stale lockfile) — we don't assume
    # it's "guaranteed to be installable". mypy sees the real types from the try branch
    # when acp-sdk is installed, so the reassignment below needs an explicit type: ignore
    # (the opposite of mypy's judgment call from when acp-sdk was an optional extra —
    # this isn't drift, the dependency's status genuinely changed).
    ACP_SDK_AVAILABLE = False
    Agent = object  # type: ignore[assignment]
    PromptResponse = dict  # type: ignore[assignment]
    Client = object  # type: ignore[assignment]
    def text_block(s: str) -> str:
        return s

# Embedded RemaGraph governance memory
try:
    from herdr_bridge.orchestration import memory as _rg
except Exception:  # noqa: BLE001
    _rg = None  # type: ignore


class SimpleRouterClient(Client):
    """Minimal client implementation used to call downstream ACP agents and collect updates."""

    def __init__(self) -> None:
        self.updates: list[Any] = []
        self.final_text = ""

    async def request_permission(self, session_id: str, tool_call: Any, options: Any, **kwargs: Any) -> dict[str, Any]:
        # Approve by default (the router layer can add stronger policy later)
        return {"outcome": {"outcome": "approved"}}

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update)
        # Expanded collection: support content, text, dict, and other real ACP downstream update shapes
        txt = ""
        if hasattr(update, "content"):
            c = update.content
            if isinstance(c, (list, tuple)):
                for part in c:
                    if hasattr(part, "text"):
                        txt += getattr(part, "text", "")
                    else:
                        txt += str(part)
            else:
                txt = str(c)
        elif isinstance(update, dict):
            if "content" in update:
                txt = str(update["content"])
            elif "text" in update:
                txt = update["text"]
            else:
                txt = str(update)
        else:
            txt = str(update)
        if txt:
            self.final_text += txt + "\n"


def is_last_pane_in_first_tab(pane_id: str) -> bool:
    """Determine whether pane_id is the last pane in its workspace's first tab.

    Why this protection exists: if the last pane in a Space's first tab (especially the
    root tab the Tower helped open) gets recycled, the whole project disappears from the
    Space and the user has to manually reopen it — this protection must apply regardless
    of whether the recycle was triggered by the command tower's manual
    recycle_fleet_member call or by the event-driven listener's automatic
    _recycle_on_complete; missing it on either path causes the project to vanish.

    On query failure, fail conservatively and return False (no protection, letting the
    caller proceed with the recycle attempt) — consistent with the existing
    light/commander.py behavior.
    """
    try:
        layout = subprocess.run(
            ["herdr", "pane", "layout", "--pane", pane_id],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if layout.returncode != 0:
            return False
        data = json.loads(layout.stdout)
        tab_id = data["result"]["layout"]["tab_id"]
        ws = data["result"]["layout"]["workspace_id"]
        tabs = subprocess.run(
            ["herdr", "tab", "list", "--workspace", ws],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if tabs.returncode != 0:
            return False
        tab_list = json.loads(tabs.stdout)["result"]["tabs"]
        if not tab_list or tab_list[0]["tab_id"] != tab_id:
            return False
        panes = subprocess.run(
            ["herdr", "pane", "list", "--workspace", ws],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if panes.returncode != 0:
            return False
        panes_in_tab = [p for p in json.loads(panes.stdout)["result"]["panes"] if p["tab_id"] == tab_id]
        return len(panes_in_tab) <= 1
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, IndexError, TypeError):
        return False


def _get_peer_uid(conn: socket.socket) -> int | None:
    """Get the uid of the process on the other end of a connected Unix domain socket.

    Linux uses SO_PEERCRED (`struct ucred { pid_t pid; uid_t uid; gid_t gid; }`);
    macOS/BSD have no SO_PEERCRED, so we fall back to LOCAL_PEERCRED
    (`struct xucred { u_int cr_version; uid_t cr_uid; ... }` — the first 8 bytes,
    cr_version + cr_uid, are all we need). If neither is available, return None; the
    caller should treat that as "unable to verify" (this module is fail-closed, see
    `_peer_uid_allowed`).
    """
    try:
        if hasattr(socket, "SO_PEERCRED"):
            size = struct.calcsize("3i")
            data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
            _pid, uid, _gid = struct.unpack("3i", data)
            return uid
        if hasattr(socket, "LOCAL_PEERCRED"):
            data = conn.getsockopt(0, socket.LOCAL_PEERCRED, 128)
            _version, uid = struct.unpack_from("II", data, 0)
            return uid
    except OSError:
        return None
    return None


def _peer_uid_allowed(peer_uid: int | None) -> bool:
    """Only allow when peer_uid explicitly equals the current user's uid; None (unverifiable) is always rejected (fail-closed)."""
    return peer_uid is not None and peer_uid == os.getuid()


def _safe_unlink_socket(path: str) -> None:
    """Safely remove an existing socket file.

    Uses `os.lstat` (does not follow symlinks) to confirm the target itself is a socket,
    not a symlink or other regular file, to prevent an attacker from pre-placing a symlink
    at this path that points at an arbitrary file, tricking this process into deleting an
    unrelated target when it calls `os.unlink`.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError(
            f"Refusing to delete a non-socket file (possible symlink attack): {path}"
        )
    os.unlink(path)


def _moshi_socket_path() -> str:
    return os.environ.get("MOSHI_SOCKET_PATH") or os.path.expanduser(
        "~/Library/Application Support/Moshi/moshi-hook.sock"
    )


def _send_moshi_envelope(
    envelope: dict[str, Any],
    *,
    max_retries: int = 3,
    base_delay: float = 0.1,
    log_prefix: str = "[moshi]",
) -> bool:
    """Send an envelope to the Moshi hook socket, returning whether it was delivered
    (including a full handshake with the daemon).

    Under the Moshi local socket protocol (moshi-hook API reference §1), even a
    fire-and-forget `session.update` gets an `ack` frame back from the daemon once
    received; if the caller only sends and closes the connection without reading, the
    daemon hits a broken pipe while writing the ack — this is one of the causes behind
    the large volume of "socket write error: broken pipe" entries in Moshi's hook.log.
    So we always try to read one response here to complete the handshake; failing to
    read it (timeout/EOF) is treated as best-effort and doesn't affect whether this send
    is considered successful.

    Connection-layer failures (broken pipe / connection reset / refused / timeout — all
    `OSError` subclasses) are retried with exponential backoff; only once retries are
    exhausted do we print an explicit warning — no longer silently swallowed with
    `except Exception: pass` like the old version, which made fleet notifications
    disappear without the user knowing.
    """
    moshi_path = _moshi_socket_path()
    payload = (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")
    last_err: Exception | None = None

    for attempt in range(max_retries):
        if not os.path.exists(moshi_path):
            return False  # Moshi not installed/not running — not transient, retrying won't help
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(moshi_path)
                s.sendall(payload)
                try:
                    s.recv(4096)  # read ack/error to complete the handshake, avoiding a broken pipe when the daemon writes back
                except OSError:
                    pass
            return True
        except OSError as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
                continue

    print(
        f"{log_prefix} Failed to send Moshi envelope (retried {max_retries} times, "
        f"type={envelope.get('type')} eventName={envelope.get('eventName')}): {last_err}",
        flush=True,
    )
    return False


# Marker string appended when prompt() falls into echo fallback (nothing was actually
# sent downstream). Broken out as a constant so dispatch_with_memory_confirm() can
# reliably detect this state without each caller re-hardcoding the same text (#60: echo
# fallback used to be buried only in free-form text, so a caller looking only at the `ok`
# field would be misled into thinking the task was "successfully dispatched" — see
# docs/decisions/acp-layer-status-20260725.md).
_ECHO_FALLBACK_MARKER = "[no downstream or sdk unavailable; echo back]"


def _clean_downstream_env(base_env: dict[str, str]) -> dict[str, str]:
    """Build the environment variables used to spawn a real downstream ACP agent
    process, filtering out traces of the local Claude Code session.

    2026-07-25 hard-won lesson: `env = dict(os.environ)` passed the tower's entire
    process environment (including PATH entries like `~/.claude/plugins/cache/...` /
    `~/.claude/skills/...`, plus `CLAUDECODE`/`CLAUDE_CODE_*`/`AI_AGENT` env vars)
    straight through to the downstream agent process, unmodified. Tested with opencode
    as the downstream: it picked up on these traces and assumed it, too, was running
    under Claude Code, so the ACP `available_commands_update` event returned all 151
    local Claude Code plugins/skills as "available commands" (completely unrelated to
    the actual task), burning 57,859 tokens on a single trivial round trip. After
    stripping `CLAUDE*`/`ANTHROPIC*` env vars and any PATH segment containing `.claude`,
    a retest showed `available_commands_update` returning 0 entries — problem gone.

    This is the same class of issue as the known Grok token risk in BOUNDARIES.md WP9
    (a downstream unintentionally inheriting traces of the local tooling ecosystem), but
    the root cause differs: Grok actively scans via its own plugin-discovery mechanism,
    whereas here it's the tower itself leaking its environment to the downstream — this
    is the caller's responsibility and should be fixed here, not a downstream agent bug.
    """
    clean = {
        k: v
        for k, v in base_env.items()
        if not k.startswith("CLAUDE") and not k.startswith("ANTHROPIC") and k != "AI_AGENT"
    }
    path = clean.get("PATH", "")
    if path:
        clean["PATH"] = ":".join(p for p in path.split(":") if ".claude" not in p)
    return clean


def _build_report_instruction(sock: str, task_id: str, agent_id: str) -> str:
    """Build the side-channel completion-report instruction template appended to the end
    of the agent prompt.

    Security note: sock (which may come from the `TOWER_REPORT_SOCK` env var) and
    task_id/agent_id (task_id is generated by `_make_valid_task_id()`, but it only
    sanitizes the `base` argument, not `self.project` — `--project` can contain
    arbitrary characters) may all be user-controlled input, so they must not be
    string-interpolated directly into Python source — a value containing quotes could
    break out of the string and inject arbitrary code. Instead we pass them via shell
    environment variables (escaped with `shlex.quote`); the `python3 -c` source itself is
    entirely a static string with no interpolation of any external value (mirrors the
    S-5 fix in commander.py, #51).

    Also fixes the hardcoded `python` (macOS typically has no `python` shim, only
    `python3`, which would cause `bash: python: command not found` when the agent runs
    it, so the report would never get sent — same class of issue as #51).
    """
    env_assignments = (
        f"TOWER_REPORT_SOCK={shlex.quote(sock)} "
        f"TOWER_TASK_ID={shlex.quote(task_id)} "
        f"TOWER_AGENT_ID={shlex.quote(agent_id)}"
    )
    python_script = (
        "import socket, json, os\n"
        "sock_path = os.environ[\"TOWER_REPORT_SOCK\"]\n"
        "report = {\n"
        '    "type": "task_report",\n'
        '    "task_id": os.environ["TOWER_TASK_ID"],\n'
        '    "agent_id": os.environ["TOWER_AGENT_ID"],\n'
        '    "status": "completed",\n'
        '    "result": {"summary": "Task complete, see output above for details", "artifacts": []},\n'
        '    "version": 1\n'
        "}\n"
        "try:\n"
        "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:\n"
        "        s.settimeout(3)\n"
        "        s.connect(sock_path)\n"
        "        s.sendall((json.dumps(report, ensure_ascii=False) + \"\\n\").encode())\n"
        "except Exception as e: print(\"side-channel report err:\", e)\n"
    )
    return f"""

[PPLX's sole recommendation: send a structured report via the side-channel when done]
Herdr only handles pane.agent_status_changed / exited events.
You **must** send JSON directly to the following socket as the last step of the task
(never rely solely on a "DONE" text marker):

{env_assignments} python3 -c '
{python_script}'
"""


class AcpRouter(Agent if ACP_SDK_AVAILABLE else object):
    """
    The command tower's ACP Router.

    Simultaneously:
    - Acts as an ACP Server (called by an upstream client).
    - Acts as an ACP Client (calls specialized downstream agents).
    - Has built-in RemaGraph memory tracking (cross-agent handoff).
    """

    # Side-channel orphaned-report cleanup thresholds (see _sweep_side_reports).
    # - MAX_ENTRIES: the upper bound on the number of side reports tracked at once in a
    #   long-lived tower session. 200 is a conservative value that's "far larger than
    #   normal concurrent dispatch volume, while still bounding memory usage".
    # - MAX_AGE_SEC: dispatch_with_memory_confirm's own wait window is about 16 seconds;
    #   if a task_id's side report was created over an hour ago and still hasn't been
    #   read or cleared, whoever was waiting for it has long since timed out and given
    #   up — the record itself is an orphan and not worth keeping indefinitely.
    _SIDE_REPORT_MAX_ENTRIES = 200
    _SIDE_REPORT_MAX_AGE_SEC = 3600.0

    def __init__(self, *, project: str = "herdr-router", start_fleet_listener: bool = True) -> None:
        self.project = project
        self.registered_agents: dict[str, dict[str, Any]] = {}
        self.registry: dict[str, dict[str, Any]] = {}  # expanded registry for discovery
        self._side_events: dict[str, threading.Event] = {}
        self._side_reports: dict[str, dict[str, Any]] = {}  # task_id -> the latest side report (for reliable detection, no longer relying only on recall)
        self._side_report_times: dict[str, float] = {}  # task_id -> creation timestamp, used for timeout/upper-bound cleanup (prevents orphaned-report leaks)
        if _rg and _rg.is_remagraph_enabled():
            _rg._ensure_remagraph_project(project)  # strengthens cross-project consistency; ensure() already handles this gracefully internally

        # Only start the Herdr event listener for the herdr-bridge project (replaces
        # polling). start_fleet_listener lets a caller (tests, one-off operations)
        # explicitly opt out of this side effect, which spins up a background daemon
        # thread that connects to the real Herdr event socket (2026-07-25 #61: reading
        # the code confirms the recycle decision already has double filtering — the
        # _watched_panes set plus a recall_fleet_members record match — so it won't
        # react to an arbitrary pane, only to a pane this router itself has dispatched
        # to; but "constructing a router implicitly starts a background thread" is
        # still a side effect that shouldn't happen for tests/one-off operations).
        # Defaults to True so existing callers' behavior is unchanged.
        if self.project == "herdr-bridge" and start_fleet_listener:
            self._start_fleet_event_listener()
            self._start_report_side_channel()  # PPLX recommendation: side-channel for structured reports, Herdr for lifecycle events
        # Strict RemaGraph compliance: force ensure + safety valve at the entry point
        if _rg and _rg.is_remagraph_enabled():
            _rg._ensure_remagraph_project(self.project)
            try:
                _rg._enforce_remagraph_safety_valve(self.project)
            except Exception as e:  # noqa: BLE001  # RemaGraph internals; constructor must not fail if the safety valve check errors
                print(f"[acp-router {self.project}] safety valve check failed (ignored): {e}", flush=True)

    def _start_fleet_event_listener(self) -> None:
        """Event-driven fleet monitoring (Herdr's native subscription_event).

        Replaces the old polling approach:
        - Subscribes to pane.agent_status_changed
        - status == "idle" / "done" -> recycle the pane immediately
        - status == "blocked" -> auto-unblock immediately
        - Dynamically watches new panes on dispatch
        - Can also push to the Moshi socket (task_complete / permission)
        This is the tower using Herdr events directly, with zero polling overhead.
        """
        if self.project != "herdr-bridge":
            return

        self._watched_panes: set[str] = set()
        self._event_sock: socket.socket | None = None
        log_prefix = f"[fleet-event {self.project}]"

        def _extract_pane_from_member(m: dict[str, Any]) -> str | None:
            for learning in (m.get("learnings") or []):
                if isinstance(learning, str) and learning.startswith("pane_id="):
                    return learning.split("=", 1)[1]
            return None

        def _auto_unblock(pane_id: str, name: str, task_id: str, agent_ref: str) -> None:
            try:
                print(f"{log_prefix} BLOCKED {name} ({pane_id}), auto-unblocking via event", flush=True)
                # Event-driven: send the unblock key just once, letting the subsequent
                # pane.agent_status_changed event trigger _handle to confirm recycle
                # Removed the loop sleep + read (PPLX priority 1/4)
                subprocess.run(["herdr", "agent", "send-keys", agent_ref, "tab", "enter"], timeout=5, capture_output=True, check=False)
                subprocess.run(["herdr", "agent", "send-keys", agent_ref, "enter"], timeout=5, capture_output=True, check=False)
                # Assume the event will confirm once sent; the actual state is updated by the listener event's pane_state
                _rg.store_memory(task_id, agent_ref, kind="status_update",
                    summary=f"Tower auto-unblocked {name}'s stuck permission (pane={pane_id}, via herdr-event)",
                    project_id=self.project, tags=["fleet-event", "permission", "auto-unblock"])
                _send_moshi_envelope({
                    "type": "approval.request",
                    "source": "herdr-fleet",
                    "sessionId": f"herdr-{pane_id}",
                    "actionId": f"perm-{pane_id}-{int(time.time())}",
                    "eventName": "herdr.permission.asked",
                    "phase": "waitingForApproval",
                    "herdrPane": pane_id,
                    "title": "Herdr Fleet Permission",
                    "subtitle": f"{name} blocked",
                    "message": "Permission required - Tower auto-unblocked",
                    "requestedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                }, log_prefix=log_prefix)
                print(f"{log_prefix} unblock sent {name}", flush=True)
            except Exception as e:  # noqa: BLE001  # background event-handler thread: must never crash the listener; already logged below
                print(f"{log_prefix} unblock err: {e}", flush=True)

        def _recycle_on_complete(pane_id: str, name: str, task_id: str, agent_ref: str) -> None:
            try:
                if is_last_pane_in_first_tab(pane_id):
                    print(f"{log_prefix} SKIP recycle {name} ({pane_id}): last pane in the Space's first tab, recycling would make the project disappear", flush=True)
                    return
                print(f"{log_prefix} COMPLETE {name} ({pane_id}), closing pane immediately", flush=True)
                res = subprocess.run(["herdr", "pane", "close", pane_id], capture_output=True, text=True, timeout=10, check=False)
                if res.returncode == 0:
                    _rg.record_fleet_recycle(task_id, agent_ref, pane_id=pane_id, reason="herdr_event_idle_or_done", project_id=self.project)
                    _rg.store_memory(task_id, agent_ref, kind="status_update",
                        summary=f"Tower immediately recycled completed fleet member {name} (pane={pane_id}) via herdr-event",
                        project_id=self.project, tags=["fleet-event", "recycle", "auto"])
                    # PPLX architecture: send a side-channel report when Herdr signals done (the agent should send this proactively; this is a cross-check / backup send)
                    self._send_task_report(task_id, agent_ref, {"summary": "completed via Herdr event", "pane_id": pane_id})
                    # Push Moshi task_complete
                    _send_moshi_envelope({
                        "type": "session.update",
                        "source": "herdr-fleet",
                        "sessionId": f"herdr-fleet-{pane_id}",
                        "eventName": "fleet.task_complete",
                        "category": "task_complete",
                        "herdrPane": pane_id,
                        "title": f"Fleet {name} complete",
                        "message": "Task finished, the command tower has closed the pane",
                        "requestedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    }, log_prefix=log_prefix)
                    print(f"{log_prefix} recycled {name}", flush=True)
            except Exception as e:  # noqa: BLE001  # background event-handler thread: must never crash the listener; already logged below
                print(f"{log_prefix} recycle err: {e}", flush=True)

        def _handle_event(msg: dict[str, Any]) -> None:
            # Support both direct and wrapped shapes
            pane = msg.get("pane_id") or (msg.get("data") or {}).get("pane_id")
            if not pane or pane not in self._watched_panes:
                return
            # agent status changed
            status = msg.get("agent_status") or (msg.get("data") or {}).get("agent_status")
            if status:
                members = _rg.recall_fleet_members(project_id=self.project) or [] if _rg else []
                for m in members:
                    if _extract_pane_from_member(m) != pane:
                        continue
                    name = m.get("name") or m.get("agent_id", pane)
                    tid = m.get("task_id", "unknown")
                    aid = m.get("agent_id", pane)
                    # PPLX #3 idempotent: prefer listener.pane_state (stateful class), otherwise skip the duplicate check
                    lst = {}
                    if hasattr(self, "_fleet_listener") and self._fleet_listener:
                        lst = self._fleet_listener.pane_state
                    last = lst.get(pane, {}).get("last_status")
                    if status in ("idle", "done") and last != status:
                        _recycle_on_complete(pane, name, tid, aid)
                        lst.setdefault(pane, {})["last_status"] = status
                    elif status == "blocked" and last != "blocked":
                        _auto_unblock(pane, name, tid, aid)
                        lst.setdefault(pane, {})["last_status"] = status
                    break
                return
            # output matched (for permission text) - PPLX enhancement: regex + variants + normalized + combined with blocked condition + false-positive guard
            matched = msg.get("matched_line") or (msg.get("data") or {}).get("matched_line", "") or ""
            normalized = str(matched).strip().lower()
            if matched:
                import re
                perm_pattern = re.compile(
                    r"\b(permission required|permission is required|access denied|authorization required|"
                    r"elevated privileges required|permission.*required|denied.*permission)\b",
                    re.IGNORECASE
                )
                # Only trigger when permission is matched and (status is blocked or context shows it's needed), to avoid false positives
                status = msg.get("agent_status") or (msg.get("data") or {}).get("agent_status", "")
                if perm_pattern.search(normalized) and (status == "blocked" or "blocked" in normalized or "permission" in normalized):
                    members = _rg.recall_fleet_members(project_id=self.project) or [] if _rg else []
                    for m in members:
                        if _extract_pane_from_member(m) != pane:
                            continue
                        name = m.get("name") or m.get("agent_id", pane)
                        tid = m.get("task_id", "unknown")
                        aid = m.get("agent_id", pane)
                        _auto_unblock(pane, name, tid, aid)
                        break

        def _subscribe_pane(s: socket.socket, pane_id: str) -> None:
            """Keeps a single-pane interface for backward compatibility with older
            callers, but internally always goes through the batch version, to avoid
            calling events.subscribe more than once on the same connection (see
            _subscribe_panes docstring)."""
            _subscribe_panes(s, [pane_id])

        def _subscribe_panes(s: socket.socket, pane_ids: list[str]) -> None:
            """Subscribe to multiple panes in a single merged call.

            Root cause (confirmed by diagnosis on 2026-07-24): herdr's events.subscribe
            has "whole-connection subscription set" semantics, not "additive" semantics
            — calling events.subscribe more than once on the same connection (e.g. once
            per pane) causes the connection to be judged abnormal and reset, and the more
            panes there are, the more frequent the reconnects. Verified experimentally:
            sending one merged subscriptions call (covering multiple panes) on the same
            connection stays perfectly stable for 30 seconds with just a single ack;
            calling events.subscribe once per pane instead causes repeated reconnects
            within a few seconds. So all pane subscriptions must be merged into a
            **single** events.subscribe call.
            """
            if not pane_ids:
                return
            try:
                subs: list[dict[str, Any]] = []
                for pane_id in pane_ids:
                    subs.append({"type": "pane.agent_status_changed", "pane_id": pane_id})
                    # PPLX enhancement: subscribe to output_matched broadly, and let the
                    # client-side regex filter multiple variants (permission/access/denied
                    # etc.) to avoid missing variants from a hardcoded server-side filter
                    subs.append({"type": "pane.output_matched", "pane_id": pane_id, "source": "recent", "match": {"type": "regex", "value": "(?i)(permission required|permission is required|access denied|authorization required|elevated privileges required|permission.*required|denied.*permission|blocked)"}})
                req = {
                    "id": f"sub-batch-{int(time.time()*1000)}",
                    "method": "events.subscribe",
                    "params": {"subscriptions": subs}
                }
                s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            except OSError as e:
                print(f"{log_prefix} subscribe send failed (will retry on reconnect): {e}", flush=True)

        class _FleetEventListener:
            """Stateful background fleet event listener (PPLX priority-3 refactor).

            - pane_state: dict map (per pane last_seq / last_status) for idempotency + dedupe
            - exponential backoff + jitter on reconnect
            - auto resubscribe watched panes on (re)connect
            - dynamic watch() for new panes from dispatch
            - zero polling, purely event-driven
            - cleans up the socket on close
            """
            def __init__(self, outer: AcpRouter, rg: Any, log_prefix: str) -> None:
                self.outer = outer
                self._rg = rg
                self.log_prefix = log_prefix
                self.pane_state: dict[str, dict[str, Any]] = {}
                self.backoff = 0.5
                self._event_sock: socket.socket | None = None
                self.buffer = b""
                self._stop = threading.Event()
                self._pending_subs: set[str] = set()
                # Protects the atomicity between `_event_sock`'s "not yet connected /
                # connected" state transition and `_pending_subs` (2026-07-25 hard-won
                # lesson: #61/#72 were both misdiagnosed as the culprit; the real cause
                # was a pre-existing race in the abefe39 batch-subscribe fix that hadn't
                # been fully closed — see the watch()/_run() comments).
                self._sub_lock = threading.Lock()
                self._sub_pipe_r, self._sub_pipe_w = os.pipe()

            def watch(self, pane_id: str) -> None:
                """Dynamically add a pane and subscribe to it immediately (supports output_matched + status)."""
                if not pane_id:
                    return
                self.outer._watched_panes.add(pane_id)
                self.pane_state.setdefault(pane_id, {"last_status": None, "last_seq": None})
                # Race root cause: unless the read of `_event_sock` and the subsequent
                # `_pending_subs.add()` are protected by the same critical section, they
                # can interleave with the "snapshot _watched_panes + send the first
                # subscribe" moment in `_run()` at connection setup — some of several
                # back-to-back `watch()` calls get counted into the first subscribe,
                # while others get queued for the immediately following
                # `_drain_pending_subs()` to send as a second subscribe. herdr treats
                # receiving two events.subscribe calls on the same connection as
                # abnormal and resets it (see the _subscribe_panes docstring). Wrapping
                # this check + write in the same lock lets `_run()` fold "any watch()
                # calls queued right before the connection was established" into the
                # first subscribe within the same critical section, closing the time
                # window between the two.
                with self._sub_lock:
                    if self._event_sock:
                        self._pending_subs.add(pane_id)
                        need_wake = True
                    else:
                        need_wake = False
                if need_wake:
                    try:
                        os.write(self._sub_pipe_w, b"x")
                    except OSError as e:
                        print(f"{self.log_prefix} wake-pipe write failed: {e}", flush=True)
                    # do not _subscribe_pane here: all socket ops must be from listener thread to avoid concurrent send/recv from main thread causing server to drop connection

            # After receiving a wake-up signal that "a new pane needs subscribing", wait
            # this long before actually draining + sending events.subscribe, so multiple
            # back-to-back watch() calls arriving within a short window (e.g. batch
            # registration of a whole group of panes) have time to accumulate into the
            # same `_pending_subs` batch and get sent in one merged call, instead of
            # sending once per wake-up (see the note in _run() and the _subscribe_panes
            # docstring: receiving multiple events.subscribe calls on the same connection
            # is treated as abnormal and causes a reset).
            _SUB_DEBOUNCE_SECONDS = 0.15

            def _drain_wake_pipe(self) -> None:
                """Non-blocking drain of whatever bytes currently exist on the wake-up pipe (does not consume bytes not yet written)."""
                while True:
                    rlist, _, _ = select.select([self._sub_pipe_r], [], [], 0)
                    if not rlist:
                        return
                    try:
                        if not os.read(self._sub_pipe_r, 1024):
                            return
                    except OSError:
                        return

            def _drain_pending_subs(self, s: socket.socket) -> None:
                """Send pending subs from the listener thread only (a single merged
                call — the multiple panes collected in one drain batch are sent as one
                events.subscribe call). Only handles watch() calls that were genuinely
                added after the connection was already stable — the batch that was
                queued at the instant the connection was established has already been
                merged with the first subscribe in `_run()` and never reaches here."""
                with self._sub_lock:
                    to_sub_all = list(self._pending_subs)
                    self._pending_subs.clear()
                to_sub = []
                for p in to_sub_all:
                    if p in self.outer._watched_panes:
                        to_sub.append(p)
                        self.pane_state.setdefault(p, {"last_status": None, "last_seq": None})
                if to_sub:
                    _subscribe_panes(s, to_sub)
                    print(f"{self.log_prefix} now watching new panes {to_sub}", flush=True)

            def _run(self) -> None:
                sock_path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
                while not self._stop.is_set():
                    try:
                        # recall before connect to avoid any delay between sub send and entering recv loop
                        additional_panes = set()
                        if self._rg and self._rg.is_remagraph_enabled():
                            members = self._rg.recall_fleet_members(project_id=self.outer.project) or []
                            for m in members:
                                p = _extract_pane_from_member(m)
                                if p and p not in self.outer._watched_panes:
                                    additional_panes.add(p)

                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.settimeout(30)
                        s.connect(sock_path)

                        # Put "switching to the connected state" and "pulling out any
                        # watch() calls that were queued right before the connection was
                        # established" into the same critical section — this is the key
                        # to fixing this race: before this lock is released, any
                        # `_event_sock` seen by watch() is always None (not yet
                        # connected), so it only lands in `self.outer._watched_panes`
                        # (already covered by the all_panes snapshot below) and never
                        # mistakenly ends up in `_pending_subs`; a watch() call made only
                        # after the lock is released is then correctly treated as "added
                        # after the connection was already stable" and goes into
                        # `_pending_subs`, handled by the subsequent select() loop's
                        # `_drain_pending_subs()` — closing the gap where some panes
                        # would get counted into the first subscribe and others sent by
                        # an immediately following second call (herdr treats receiving
                        # two events.subscribe calls on the same connection as abnormal
                        # and resets it, see the _subscribe_panes docstring).
                        with self._sub_lock:
                            self._event_sock = s
                            self.outer._event_sock = s
                            extra_pending = set(self._pending_subs)
                            self._pending_subs.clear()
                        print(f"{self.log_prefix} connected to Herdr event socket", flush=True)

                        # subscribe to everything (watched + additional from recall + any
                        # pending subs queued right before the connection was
                        # established) — one merged call, not one events.subscribe call
                        # per pane (see the _subscribe_panes docstring). Deliberately not
                        # calling _drain_pending_subs() right after this — the critical
                        # section above has already merged every pane known at this point
                        # into this one call, so there's nothing left over.
                        all_panes = list(self.outer._watched_panes | additional_panes | extra_pending)
                        _subscribe_panes(s, all_panes)
                        for p in all_panes:
                            self.pane_state.setdefault(p, {"last_status": None, "last_seq": None})
                        print(f"{self.log_prefix} watching fleet panes: {len(all_panes)}", flush=True)

                        received_data = False
                        while not self._stop.is_set():
                            try:
                                # select to allow waking for new pending subs without concurrent send from other threads
                                # mypy's select.select() overload infers too strictly for a mixed
                                # socket + fd list; this is entirely legal at runtime (select
                                # treats both the same way), so we give an explicit type here
                                # rather than changing the logic
                                watch_list: list[Any] = [s, self._sub_pipe_r]
                                rlist, _, _ = select.select(watch_list, [], [], 30)
                                if self._sub_pipe_r in rlist:
                                    self._drain_wake_pipe()
                                    time.sleep(self._SUB_DEBOUNCE_SECONDS)
                                    self._drain_wake_pipe()
                                    self._drain_pending_subs(s)
                                if s in rlist:
                                    chunk = s.recv(8192)
                                    if not chunk:
                                        break
                                    self.buffer += chunk
                                    lines = self.buffer.split(b"\n")
                                    self.buffer = lines[-1]
                                    for line in lines[:-1]:
                                        if not line.strip():
                                            continue
                                        try:
                                            msg = json.loads(line.decode("utf-8", errors="ignore"))
                                            if msg.get("result", {}).get("type") == "subscription_started":
                                                continue
                                            print(f"RECEIVED NON-ACK: {msg.get('type') or msg.get('result', {}).get('type') or str(msg)[:100]}", flush=True)
                                            pane = msg.get("pane_id") or (msg.get("data") or {}).get("pane_id")
                                            seq = msg.get("state_change_seq") or (msg.get("data") or {}).get("state_change_seq")
                                            if pane and seq and self.pane_state.get(pane, {}).get("last_seq") == seq:
                                                continue  # dedupe / idempotent
                                            if pane and seq:
                                                self.pane_state.setdefault(pane, {})["last_seq"] = seq
                                            _handle_event(msg)
                                            st = msg.get("agent_status") or (msg.get("data") or {}).get("agent_status")
                                            if pane and st:
                                                self.pane_state.setdefault(pane, {})["last_status"] = st
                                            received_data = True
                                        except Exception as e:  # noqa: BLE001  # malformed/unexpected event line must not kill the listener thread
                                            print(f"{self.log_prefix} skipped malformed event line: {e}", flush=True)
                            except OSError:
                                # timeout or transient, continue waiting on same connection
                                continue
                            except Exception as ie:  # noqa: BLE001  # background event-listener thread: must never crash; already logged below
                                print(f"INNER except break: {ie}", flush=True)
                                import traceback
                                traceback.print_exc()
                                break
                        if received_data:
                            self.backoff = 0.5
                            print(f"{self.log_prefix} session had data, reset backoff", flush=True)
                        else:
                            print(f"{self.log_prefix} no data session, backoff sleep {self.backoff}", flush=True)
                            time.sleep(self.backoff + random.random() * 0.25)
                            self.backoff = min(self.backoff * 2, 30)
                    except Exception as e:  # noqa: BLE001  # background event-listener thread: must never crash; already logged below
                        import traceback
                        print(f"{self.log_prefix} listener err (reconnect in {self.backoff}s): {e}", flush=True)
                        traceback.print_exc()
                        self._event_sock = None
                        self.outer._event_sock = None
                        time.sleep(self.backoff + random.random() * 0.25)
                        self.backoff = min(self.backoff * 2, 30)
                    finally:
                        try:
                            if self._event_sock:
                                self._event_sock.close()
                        except OSError as e:
                            print(f"{self.log_prefix} error closing event socket (ignored): {e}", flush=True)
                        self._event_sock = None
                        self.outer._event_sock = None

            def start(self) -> None:
                t = threading.Thread(target=self._run, daemon=True, name=f"tower-fleet-event-{self.outer.project}")
                t.start()

            def stop(self) -> None:
                self._stop.set()
                if self._event_sock:
                    try:
                        self._event_sock.close()
                    except OSError as e:
                        print(f"{self.log_prefix} error closing event socket on stop (ignored): {e}", flush=True)
                self._event_sock = None
                self.outer._event_sock = None
                try:
                    os.close(self._sub_pipe_r)
                    os.close(self._sub_pipe_w)
                except OSError as e:
                    print(f"{self.log_prefix} error closing wake-pipe fds on stop (ignored): {e}", flush=True)

        # Use the stateful class instead of the old closure-based _listener
        listener = _FleetEventListener(self, _rg, log_prefix)
        self._fleet_listener = listener
        listener.start()

    def _watch_fleet_pane(self, pane_id: str) -> None:
        """Dynamically subscribe to status events for a new fleet pane (called after dispatch).

        PPLX priority 4 + 3: subscribes to both status and output_matched (broadly, filtered
        client-side) and updates pane_state for dedupe.
        """
        if not pane_id:
            return
        if not hasattr(self, "_watched_panes"):
            self._watched_panes = set()
        self._watched_panes.add(pane_id)
        if hasattr(self, "_fleet_listener") and self._fleet_listener:
            self._fleet_listener.watch(pane_id)
            return
        if self._event_sock:
            try:
                subs = [
                    {"type": "pane.agent_status_changed", "pane_id": pane_id},
                ]
                req = {
                    "id": f"sub-dyn-{pane_id}",
                    "method": "events.subscribe",
                    "params": {"subscriptions": subs}
                }
                self._event_sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
                print(f"[fleet-event] now watching new pane {pane_id} (status+output)", flush=True)
            except OSError as e:
                print(f"[fleet-event] failed to send dynamic subscribe for {pane_id} (ignored): {e}", flush=True)

    def _start_report_side_channel(self) -> None:
        """PPLX's top recommendation: Herdr only handles lifecycle events (status/exit);
        structured reports go over an independent side-channel Unix socket. Agents send
        JSON directly to this socket when done, and the Tower receives it, stores it in
        RemaGraph, and processes it. This is the only mechanism — no scraping marker text.

        Security: the socket lives in a freshly created private directory on each start
        (`tempfile.mkdtemp`, mode 0o700), not a fixed `/tmp/tower-reports.sock` (avoiding
        another user on the same machine squatting the path or tricking us into deleting
        via a symlink); the socket file itself is separately chmod'd to 0o600; after
        accepting a connection we verify the peer uid (`_peer_uid_allowed`) equals the
        current user before processing it, and reject everything else.
        """
        if self.project != "herdr-bridge":
            return
        sock_dir = tempfile.mkdtemp(prefix="tower-")  # mkdtemp's default mode is already 0o700
        self._report_sock_path = os.path.join(sock_dir, "reports.sock")
        os.environ["TOWER_REPORT_SOCK"] = self._report_sock_path  # automatically makes this visible to all dispatch calls / subprocesses
        try:
            _safe_unlink_socket(self._report_sock_path)
        except RuntimeError as e:
            print(f"[tower-report {self.project}] {e}", flush=True)
            raise
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(self._report_sock_path)
        os.chmod(self._report_sock_path, 0o600)
        server_sock.listen(10)
        log_prefix = f"[tower-report {self.project}]"
        print(f"{log_prefix} side-channel listening at {self._report_sock_path}", flush=True)

        def _report_server():
            while True:
                try:
                    conn, _ = server_sock.accept()
                    peer_uid = _get_peer_uid(conn)
                    if not _peer_uid_allowed(peer_uid):
                        print(
                            f"{log_prefix} rejected connection from uid={peer_uid!r} "
                            f"(expected {os.getuid()})",
                            flush=True,
                        )
                        conn.close()
                        continue
                    data = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    if data:
                        try:
                            report = json.loads(data.decode("utf-8").strip())
                            self._handle_structured_report(report)
                        except Exception as e:  # noqa: BLE001  # untrusted side-channel input must not crash the report server; already logged
                            print(f"{log_prefix} bad report: {e}", flush=True)
                    conn.close()
                except Exception as e:  # noqa: BLE001  # background report-server accept loop must never crash; already logged
                    print(f"{log_prefix} accept err: {e}", flush=True)
                    time.sleep(1)

        t = threading.Thread(target=_report_server, daemon=True, name="tower-report-server")
        t.start()
        self._report_server_thread = t

    def _handle_structured_report(self, report: dict[str, Any]) -> None:
        """Handle a structured report received via the side-channel. Stores it in RemaGraph and triggers follow-up actions (e.g. recycle if done)."""
        rtype = report.get("type")
        task_id = report.get("task_id") or report.get("id")
        agent_id = report.get("agent_id") or report.get("source")
        if not task_id or not agent_id:
            return

        # Update in-memory state for reliable side detection (event + direct lookup,
        # reducing reliance on recall). This must run unconditionally, regardless of
        # whether RemaGraph is enabled — the side-channel is a "last line of insurance"
        # layer independent of RemaGraph; standalone herdr-bridge users (without the
        # governance layer) must also be able to reliably detect completion through it,
        # otherwise wait_for_side_report() would always time out.
        #
        # Opportunistically sweep before inserting: this avoids the case where a
        # downstream agent's report arrives after dispatch_with_memory_confirm's own wait
        # window (~16s) has ended and _side_reports/_side_events have already been
        # popped — without the sweep, we'd re-create an "orphaned" entry here that
        # nobody will ever read or clear, growing unbounded over the life of a
        # long-lived tower session.
        self._sweep_side_reports(reserve=1)
        self._side_reports[task_id] = report
        self._side_report_times[task_id] = time.time()
        evt = self._side_events.get(task_id)
        if evt:
            evt.set()

        if not _rg or not _rg.is_remagraph_enabled():
            return
        if rtype == "task_report":
            _rg.store_memory(
                task_id,
                agent_id,
                kind="status_update",  # must use a supported kind (task_report is the payload's type)
                summary=report.get("summary", str(report.get("result", ""))[:200]),
                learnings=[f"report:{json.dumps(report.get('result', {}))}"],
                project_id=self.project,
                tags=["side-channel", "report", rtype],
            )
            print(f"[tower-report] received task_report for {task_id} from {agent_id}", flush=True)
            # Optional: trigger recycle if status is already done
            # Only stored here; the fleet listener handles recycle on the status event
        elif rtype in ("task_complete", "completion"):
            _rg.store_memory(
                task_id,
                agent_id,
                kind="status_update",
                summary=f"Task complete via side-channel: {report.get('summary', '')}",
                project_id=self.project,
                tags=["side-channel", "complete"],
            )

    def _has_side_report(self, task_id: str) -> bool:
        """Directly check (in-memory first) whether a side report has been received — an underlying fix, no longer relying only on recall."""
        return task_id in self._side_reports

    def _sweep_side_reports(self, *, reserve: int = 0) -> None:
        """Opportunistic cleanup (access-triggered, not a background thread / not a new polling loop):

        Bounds the unbounded growth of _side_reports/_side_events. The normal path (the
        pop at the end of dispatch_with_memory_confirm) only covers the case where the
        report arrives within the wait window; a downstream agent's actual completion
        time may be much later than that window (for long-running tasks), in which case
        by the time the report arrives task_id has already been popped, and
        _handle_structured_report would re-create an orphaned entry that nobody ever
        reads or clears again.

        Two mechanisms (both "lazy" — only triggered on existing call paths, no new
        threads or timers added):
        1) Timeout: any entry created more than _SIDE_REPORT_MAX_AGE_SEC ago is treated
           as an abnormal orphan and cleared.
        2) Cap: even if nothing has expired yet, once the count exceeds
           _SIDE_REPORT_MAX_ENTRIES, evict oldest-to-newest by creation time until back
           within the cap.

        `reserve`: the number of entries the caller is about to insert (currently only
        _handle_structured_report passes 1 before inserting a new report). This reserves
        that many slots when evicting for the cap, ensuring the total stays within the
        cap after the insert, rather than exceeding the cap by one entry that only gets
        caught by the next cleanup.
        """
        now = time.time()

        expired = [
            tid for tid, ts in self._side_report_times.items()
            if now - ts > self._SIDE_REPORT_MAX_AGE_SEC
        ]
        for tid in expired:
            self._side_reports.pop(tid, None)
            self._side_events.pop(tid, None)
            self._side_report_times.pop(tid, None)

        overflow = len(self._side_report_times) + reserve - self._SIDE_REPORT_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(self._side_report_times.items(), key=lambda kv: kv[1])[:overflow]
            for tid, _ts in oldest:
                self._side_reports.pop(tid, None)
                self._side_events.pop(tid, None)
                self._side_report_times.pop(tid, None)

    def wait_for_side_report(self, task_id: str, timeout_sec: float = 30.0) -> bool:
        """Event-driven wait for a side report (makes side confirmation reliable, reducing reliance on recall polling).

        Creates (or gets the existing) Event first, then checks whether a report has
        already arrived — this avoids a race window from "check, then create Event": if a
        report happens to arrive between those two steps, `_handle_structured_report`
        would find no Event yet created and be unable to wake the waiter, causing it to
        wait uselessly until timeout. After a timeout we still check the in-memory report
        directly once more, as a last-resort fallback.
        """
        # Opportunistic cleanup: complements the pre-insert sweep in
        # _handle_structured_report, so even in a scenario where no new report arrives
        # for a long time (and thus the insert path is never triggered), growth is still
        # bounded on the next wait call.
        self._sweep_side_reports()
        evt = self._side_events.setdefault(task_id, threading.Event())
        if self._has_side_report(task_id):
            return True
        if evt.wait(timeout_sec):
            return True
        return self._has_side_report(task_id)

    def _send_task_report(self, task_id: str, agent_id: str, result: dict[str, Any]) -> None:
        """Send a structured report over the side-channel (PPLX recommendation: Herdr handles only lifecycle, reports go over their own socket)."""
        sock_path = str(
            os.environ.get("TOWER_REPORT_SOCK") or getattr(self, "_report_sock_path", "/tmp/tower-reports.sock")
        )
        envelope = {
            "type": "task_report",
            "task_id": task_id,
            "agent_id": agent_id,
            "status": "completed",
            "result": result or {},
            "version": 1,
            "ts": time.time(),
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(sock_path)
                s.sendall((json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8"))
            print(f"[tower-report] sent for {task_id}", flush=True)
        except Exception as e:  # noqa: BLE001  # side-channel report send is best-effort; must not raise into the caller
            print(f"[tower-report] send err: {e}", flush=True)

    def _make_valid_task_id(self, base: str = "task") -> str:
        """Generate short, valid, project-prefixed task_id for RemaGraph validation."""
        import re
        ts = int(time.time()) % 100000
        safe = re.sub(r"[^a-zA-Z0-9_-]", "-", base)[:20]
        tid = f"{self.project}-{safe}-{ts}"
        # truncate to <=63 chars, start with alnum
        if len(tid) > 63:
            tid = tid[:63]
        if not tid[0].isalnum():
            tid = "p" + tid[1:]
        return tid

    def register_agent(self, name: str, command: str, args: list[str] | None = None, **meta) -> None:
        """Register a downstream ACP agent (TUI interface). Supports meta kwargs to extend the registry."""
        spec = {"command": command, "args": args or [], **meta}
        self.registered_agents[name] = spec
        self.registry[name] = {"type": "acp-tui-agent", "name": name, "spec": spec, "registered_at": time.time()}

    def unregister_agent(self, name: str) -> bool:
        """Remove a registered agent (supports CLI unregister cleaning up the registry)."""
        removed = False
        if name in self.registered_agents:
            del self.registered_agents[name]
            removed = True
        if name in self.registry:
            del self.registry[name]
            removed = True
        # also remove from persisted json
        try:
            import json
            from pathlib import Path
            cfg = Path.home() / ".config" / "herdr" / "acp-registry.json"
            if cfg.exists():
                cur = json.loads(cfg.read_text() or "[]")
                new_cur = [e for e in cur if e.get("name") != name]
                if len(new_cur) != len(cur):
                    cfg.write_text(json.dumps(new_cur, indent=2, ensure_ascii=False))
                    removed = True
        except Exception as e:  # noqa: BLE001  # tolerate a corrupt/unreadable user registry file; in-memory removal above must still succeed
            print(f"[acp-router] unregister_agent: failed to update persisted registry (ignored): {e}", flush=True)
        return removed

    def discover_agents(self) -> list[str]:
        """Expanded registry discovery: list registered downstream agents."""
        return list(self.registered_agents.keys())

    def get_agent_spec(self, name: str) -> dict[str, Any] | None:
        """Get an agent's spec from the registry."""
        return self.registered_agents.get(name)

    def list_registry(self) -> list[dict[str, Any]]:
        """Full registry listing, for CLI and discovery use."""
        return list(self.registry.values())

    def list_registry_filtered(self, capability: str | None = None, **meta_filters) -> list[dict[str, Any]]:
        """Expanded registry discovery: filter agents by capability or meta (e.g. capability='search')."""
        items = self.list_registry()
        if capability:
            items = [it for it in items if capability in (it.get("spec", {}).get("capabilities") or [])]
        for k, v in meta_filters.items():
            items = [it for it in items if str(it.get("spec", {}).get(k, "")).lower() == str(v).lower()]
        return items

    def list_capabilities(self) -> list[str]:
        """Expanded registry discovery: list all capabilities supported by registered agents."""
        caps = set()
        for spec in self.registered_agents.values():
            for c in spec.get("capabilities", []) or []:
                caps.add(c)
        return sorted(caps)

    def get_registry_summary(self) -> dict[str, Any]:
        """Expanded registry discovery: a full summary, for CLI and higher-level use."""
        return {
            "agents": self.discover_agents(),
            "capabilities": self.list_capabilities(),
            "count": len(self.registered_agents),
            "details": self.list_registry(),
        }

    def discover_from_examples(self, examples_dir: str | None = None, additional_paths: list[str] | None = None) -> list[str]:
        """Expanded registry discovery (dynamic core): scan multiple paths for
        acp-*-agent.py and auto-register real downstream ACP agents.

        Supports:
        - The default examples/ dir (4+ real agents built in for development)
        - The HERDR_ACP_AGENT_PATHS env var (multiple dirs separated by `:` or `,`)
        - The default user config dir ~/.config/herdr/acp-agents/
        - Passing additional_paths at call time for CLI / test extension
        Adding a new script automatically extends the registry — no need to change the
        factory or hardcode anything.
        """
        from pathlib import Path
        paths: list[Path] = []
        # 1. primary examples
        if examples_dir:
            paths.append(Path(examples_dir))
        else:
            paths.append(Path(__file__).resolve().parents[3] / "examples")
        # 2. env var (real external agents support)
        env_paths = os.environ.get("HERDR_ACP_AGENT_PATHS", "")
        for p in env_paths.replace(",", ":").split(":"):
            p = p.strip()
            if p:
                paths.append(Path(p))
        # 3. user config dir (for installed / user-provided real TUI agents)
        user_cfg = Path.home() / ".config" / "herdr" / "acp-agents"
        if user_cfg.exists():
            paths.append(user_cfg)
        # 4. explicit additional (from CLI register or tests)
        if additional_paths:
            for ap in additional_paths:
                paths.append(Path(ap))

        discovered = []
        seen_dirs = set()
        for base in paths:
            if not base or not base.exists() or str(base) in seen_dirs:
                continue
            seen_dirs.add(str(base))
            # Expanded glob: the default examples dir uses the strict acp-*-agent.py pattern;
            # additional/user dirs support the broader *agent* pattern to support real external agents (py or bin)
            is_examples = str(base).endswith("/examples")
            # Expanded registry discovery: examples uses the strict acp-*-agent.py; other
            # paths (user/external) use the broader *acp*, *agent* including extensionless
            # binaries, to support real external TUIs
            patterns = ["acp-*-agent.py"] if is_examples else ["*acp*", "*agent*.py", "*agent*", "*acp*"]
            for pat in patterns:
                for pyf in sorted(base.glob(pat)):
                    if pyf.is_dir():
                        continue
                    stem = pyf.stem
                    name = stem.replace("acp-", "").replace("-agent", "-tui")
                    if name in self.registered_agents:
                        continue
                    lower = name.lower()
                    if "echo" in lower or "ack" in lower:
                        caps = ["echo", "ack"]
                        desc = "auto-discovered echo/ack downstream (real ACP)"
                    elif "research" in lower or "search" in lower or "analyze" in lower:
                        caps = ["search", "analyze", "echo"]
                        desc = "auto-discovered research downstream (real ACP)"
                    elif "code" in lower or "implement" in lower or "write" in lower:
                        caps = ["code", "implement", "write"]
                        desc = "auto-discovered code downstream (real ACP)"
                    else:
                        caps = ["general", "ack"]
                        desc = f"auto-discovered {name} downstream (real ACP)"
                    script = str(pyf)
                    # for non-py / bin in user/additional/PATH, use direct exec (supports real external agents)
                    if not script.endswith(".py"):
                        self.register_agent(name, script, [], description=desc, capabilities=caps)
                    else:
                        self.register_agent(name, "uv", ["run", "python", script], description=desc, capabilities=caps)
                    discovered.append(name)

        # Further expansion: auto-discover real external agents from PATH (supports things like real opencode if it has an acp mode)
        for p in os.environ.get("PATH", "").split(os.pathsep):
            if not p or not os.path.isdir(p):
                continue
            try:
                for f in os.listdir(p)[:50]:  # limit
                    if "acp" in f.lower() or f.lower().endswith(("-agent", "-agent.py", "acp")):
                        full = os.path.join(p, f)
                        if os.path.isfile(full):
                            stem = os.path.splitext(f)[0]
                            name = stem.replace("acp-", "").replace("-agent", "-tui")
                            if name.lower() in {"agent", "agents", "tiny-tuis"} or len(name) < 4:
                                continue
                            if name not in self.registered_agents:
                                if f.endswith(".py"):
                                    # py in PATH: use uv run python for safety
                                    self.register_agent(name, "uv", ["run", "python", full], description=f"auto-discovered from PATH: {f} (real external py)", capabilities=["general"])
                                elif os.access(full, os.X_OK):
                                    self.register_agent(name, full, [], description=f"auto-discovered from PATH: {f} (real external)", capabilities=["general"])
                                else:
                                    continue
                                discovered.append(name)
            except OSError as e:
                print(f"[acp-router] PATH scan skipped unreadable entry in {p} (ignored): {e}", flush=True)
        return discovered

    def discover(self, additional_paths: list[str] | None = None) -> list[str]:
        """Unified entry point: runs all dynamic discovery (examples + env + config + additional)."""
        found = self.discover_from_examples(additional_paths=additional_paths)
        self._load_user_registered()
        return found + list(self.registered_agents.keys())  # may overlap

    def _load_user_registered(self) -> None:
        """Load manually-registered real agents from the user config (makes CLI register persistent)."""
        import json
        from pathlib import Path
        cfg = Path.home() / ".config" / "herdr" / "acp-registry.json"
        if not cfg.exists():
            return
        try:
            data = json.loads(cfg.read_text())
            for entry in data if isinstance(data, list) else []:
                name = entry.get("name")
                if name and name not in self.registered_agents:
                    cmd = entry.get("command", "uv")
                    args = entry.get("args", [])
                    meta = {k: v for k, v in entry.items() if k not in ("name", "command", "args")}
                    self.register_agent(name, cmd, args, **meta)
        except Exception as e:  # noqa: BLE001  # tolerate bad user registry
            print(f"[acp-router] _load_user_registered: skipping corrupt user registry (ignored): {e}", flush=True)

    def save_user_registered(self, name: str, command: str, args: list[str], **meta) -> None:
        """Save this registration to the user config, so future discover() calls pick it up automatically (persists the expanded registry)."""
        import json
        from pathlib import Path
        cfg = Path.home() / ".config" / "herdr" / "acp-registry.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cur = []
        if cfg.exists():
            try:
                cur = json.loads(cfg.read_text()) or []
            except Exception:  # noqa: BLE001  # tolerate a corrupt registry file; fall back to starting fresh
                cur = []
        entry = {"name": name, "command": command, "args": args, **meta}
        # de-dup
        cur = [e for e in cur if e.get("name") != name]
        cur.append(entry)
        cfg.write_text(json.dumps(cur, indent=2, ensure_ascii=False))

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        """
        Receives a prompt as the Server.
        Internally: decides routing -> calls downstream as the Client -> aggregates the
        result -> writes RemaGraph memory -> replies.
        Supports **kwargs target= to force routing (honors CLI --target).

        As a memory-first-class citizen: the router internally forces recall-before (via
        prepare_dispatch_text) + store-after (in every success/failure/timeout case).
        Handles handoff_note and tags automatically.
        """
        # 1. Extract text from the prompt (simplified)
        original_text = ""
        for block in prompt:
            if isinstance(block, str):
                original_text += block
            elif hasattr(block, "text"):
                original_text += getattr(block, "text", str(block))

        # Always go through prepare_dispatch_text to get recall-augmented text plus a
        # consistent task_id/agent_id (supports cross-project usage)
        task_id = self._make_valid_task_id("router-task")
        agent_id = "acp-router"
        send_text = original_text
        if _rg and _rg.is_remagraph_enabled():
            try:
                prepared, tid, aid = _rg.prepare_dispatch_text(
                    original_text, base_task_id=task_id, agent_id=agent_id, project=self.project
                )
                send_text = prepared
                task_id = tid
                agent_id = aid
            except Exception as e:  # noqa: BLE001  # RemaGraph recall/prepare is best-effort; must not block routing
                # On failure, keep going with the original values; the later store still gets attempted
                print(f"[acp-router] prepare_dispatch_text failed (ignored): {e}", flush=True)

        # Automatically inject the side-channel report socket path (PPLX's sole
        # recommended approach). Herdr only handles lifecycle events; structured reports
        # go over their own Unix socket. sock/task_id/agent_id may all be user-controlled
        # input, so they must not be string-interpolated into Python source — see the
        # _build_report_instruction() docstring (same class of fix as S-5, #51).
        if hasattr(self, '_report_sock_path') and getattr(self, '_report_sock_path', None):
            send_text += _build_report_instruction(self._report_sock_path, task_id, agent_id)

        # Store to RemaGraph once the task is received (the router fills in handoff_note/tags automatically)
        if _rg and _rg.is_remagraph_enabled():
            _rg.store_memory(
                task_id,
                agent_id,
                kind="task_handoff",
                summary=f"Router received task: {original_text[:100]}",
                handoff_note="Routing decision in progress (recall-before already prepared)",
                project_id=self.project,
                learnings=["ACP Router acts as a memory-first-class citizen: unified prepare+store"],
                tags=["router", "acp", "handoff", "governance"],
            )

        # 2. Routing: **automatic routing has been removed** — target must be provided explicitly (user preference takes priority, no _choose_target)
        forced = kwargs.get("target") or kwargs.get("target_agent")
        if not forced:
            raise ValueError("target must be provided explicitly (automatic routing has been removed)")
        target = forced
        if target not in self.registered_agents:
            # Allow a real Herdr pane_id as the target (not only a registered ACP agent)
            pass
        result_text = f"[router] routed to {target}\n"


        if target and target in self.registered_agents and ACP_SDK_AVAILABLE:
            try:
                spec = self.registered_agents[target]
                client = SimpleRouterClient()
                cmd = spec["command"]

                report_sock = getattr(self, '_report_sock_path', '/tmp/tower-reports.sock')
                env = _clean_downstream_env(dict(os.environ))
                env["TOWER_REPORT_SOCK"] = report_sock

                async with spawn_agent_process(client, cmd, *spec["args"], env=env) as (conn, _proc):
                    await conn.initialize(protocol_version=PROTOCOL_VERSION)
                    sess = await conn.new_session(cwd=os.getcwd(), mcp_servers=[])
                    prompt_result = await conn.prompt(
                        session_id=sess.session_id,
                        prompt=[text_block(send_text)],
                    )
                    # Wait for some updates (simplified; ideally this should properly wait)
                    await asyncio.sleep(1)
                    downstream_resp = client.final_text or ""
                    if hasattr(prompt_result, "_meta") and prompt_result._meta:
                        mt = prompt_result._meta or {}
                        # support both legacy echo_text and expanded result_text from real downstream agents
                        downstream_resp += mt.get("echo_text", "") or mt.get("result_text", "") or ""
                    downstream_resp = downstream_resp or str(prompt_result)
                    result_text += downstream_resp or "[downstream response collected via ACP]"

                    # PPLX-recommended side-channel: send a structured report (Herdr only handles lifecycle events)
                    try:
                        self._send_task_report(task_id, target, {
                            "text": downstream_resp or result_text,
                            "routed_to": target,
                            "source": "acp-downstream"
                        })
                    except Exception as e:  # noqa: BLE001  # side-channel report send is best-effort; must not fail the downstream call
                        print(f"[acp-router] side-channel report send failed (ignored): {e}", flush=True)

                # Record the downstream result in RemaGraph (success)
                if _rg and _rg.is_remagraph_enabled():
                    _rg.store_memory(
                        task_id,
                        f"downstream:{target}",
                        kind="status_update",
                        summary=result_text[:300],
                        handoff_note="Downstream complete",
                        project_id=self.project,
                        tags=["router", "downstream", target, "ack"],
                    )
            except Exception as exc:  # noqa: BLE001  # downstream ACP call boundary: spawn/protocol errors must be recorded, not propagated
                result_text += f"\n[error calling {target}: {exc}]"
                # [Important] record the error case too
                if _rg and _rg.is_remagraph_enabled():
                    _rg.store_memory(
                        task_id,
                        f"downstream:{target}",
                        kind="status_update",
                        summary=f"Downstream call failed: {str(exc)[:120]}",
                        handoff_note=f"ACP downstream error for {target}",
                        project_id=self.project,
                        tags=["router", "downstream", target, "error"],
                        learnings=["downstream ACP prompt failed"],
                    )
        else:
            result_text += _ECHO_FALLBACK_MARKER
            if _rg and _rg.is_remagraph_enabled():
                # PPLX consensus (docs/decisions/acp-layer-status-20260725.md): an echo
                # fallback means nothing was actually sent to any downstream, so we can't
                # write a memory entry that looks like a normal completion — anyone
                # reviewing memory afterward would mistakenly think that dispatch
                # succeeded. MemoryKind only has four values (task_handoff/status_update/
                # discovered_constraint/fleet_member — no dedicated anomaly/error kind),
                # so we distinguish this with explicit tags + summary wording instead of
                # relying on kind alone.
                _rg.store_memory(
                    task_id,
                    agent_id,
                    kind="status_update",
                    summary=(
                        "⚠️ ACP echo fallback: nothing was actually sent to any downstream "
                        "(either the ACP SDK isn't installed, or target was never "
                        "register_agent()'d) -- this is not a completion, it's a degraded rejection"
                    ),
                    handoff_note=(
                        "delivery_status=not_attempted -- this task was never handled by "
                        "any downstream; do not treat it as complete"
                    ),
                    project_id=self.project,
                    tags=["router", "fallback", "degraded", "delivery-not-attempted", "not-a-success"],
                )

        # Final memory entry (recorded for success/failure/echo alike, no try/pass)
        if _rg and _rg.is_remagraph_enabled():
            # Use the precise marker to determine echo fallback, not an
            # easily-mistaken keyword substring match (previously relied on a
            # coincidental substring like "unavailable", which wasn't reliable).
            is_echo_fallback = _ECHO_FALLBACK_MARKER in result_text
            is_err = is_echo_fallback or "error" in result_text.lower() or "fail" in result_text.lower()
            tags = ["router", "acp", "final"]
            if is_echo_fallback:
                tags.extend(["degraded", "delivery-not-attempted", "not-a-success"])
                handoff_note = "delivery_status=not_attempted -- routing never actually reached any downstream (degraded, not a successful completion)"
            elif is_err:
                tags.append("error")
                handoff_note = "Routing finished but with an error"
            else:
                tags.append("success")
                handoff_note = "Result has been integrated back to the upstream caller"
            _rg.store_memory(
                task_id,
                agent_id,
                kind="status_update",
                summary=f"Router finished routing: {result_text[:150]}",
                handoff_note=handoff_note,
                project_id=self.project,
                tags=tags,
            )


        # Return to the upstream client
        if ACP_SDK_AVAILABLE:
            # Expanded return value, includes the downstream result
            return PromptResponse(stopReason="end_turn", _meta={"result_text": result_text})
        return {"stop_reason": "end_turn", "text": result_text}

    def _choose_target(self, text: str) -> str | None:
        """Removed (smart automatic routing has been retired).
        Kept as a stub for backward compatibility with old callers; new code should always specify target/pane_id explicitly.
        """
        return None  # no longer auto-selects

    def _resolve_target_to_pane(self, target: str | None, pane_id: str | None = None) -> tuple[str | None, str | None]:
        """Dynamic resolver (PPLX-enhanced): resolves a space name to an explicit pane_id + health status.

        - Determines health (Healthy / Suspect / Stale) from recall_fleet_members + recent PONG
        - TTL tied to health (healthy can be extended, suspected-stale forces a refresh)
        - Returns (target, resolved_pane) or None
        - Per §9: health-check dimensions + explicit errors
        """
        if pane_id:
            return target, pane_id
        if not target:
            return None, None

        # Already looks like pane syntax
        if ":" in target or (target.startswith(("w", "p")) and any(c in target for c in [":", "p"])):
            return target, target

        if _rg and _rg.is_remagraph_enabled():
            try:
                members = _rg.recall_fleet_members(project_id=self.project) or []
                for m in members:
                    m_name = m.get("name") or ""
                    m_aid = m.get("agent_id") or ""
                    learnings = m.get("learnings") or []
                    if target == m_name or target in m_aid or target in str(learnings):
                        for learning in learnings:
                            if isinstance(learning, str) and learning.startswith("pane_id="):
                                p = learning.split("=", 1)[1]
                                if p:
                                    # Health check: look at the most recent PONG / fleet update time
                                    health = self._get_target_health(target, self.project)
                                    if health in ("Healthy", "Suspect"):
                                        return target, p
                                    # Still return it even if Stale, but flagged; the caller decides on a fallback
                                    return target, p
            except Exception as e:  # noqa: BLE001  # fleet-member lookup is best-effort; fall through to "unresolved"
                print(f"[acp-router] _resolve_target_to_pane lookup failed for {target} (ignored): {e}", flush=True)

        return target, None

    def _get_target_health(self, target: str, project_id: str) -> str:
        """Simple health status, based on the most recent PONG time.
        Healthy: <5min, Suspect: <15min, Stale: older or none.
        """
        import datetime as _dt

        try:
            mems = _rg.recall_memories("", target, top_k=5, project_id=project_id) or []
            now = _dt.datetime.now(_dt.UTC)
            for m in mems:
                if m.get("kind") == "status_update" and ("pong" in str(m).lower() or "ack" in str(m).lower()):
                    ts_raw = m.get("timestamp")
                    if not ts_raw:
                        continue
                    ts = _dt.datetime.fromisoformat(str(ts_raw))
                    age_min = (now - ts).total_seconds() / 60
                    if age_min < 5:
                        return "Healthy"
                    if age_min < 15:
                        return "Suspect"
            return "Suspect"
        except Exception:  # noqa: BLE001  # health check must fail-safe to "Stale" on any lookup/parse error
            return "Stale"

    def wait_for_pong(self, task_id: str, agent_id: str, correlation: str | None = None, timeout_sec: float = 30.0) -> dict[str, Any]:
        """Wait for the receiver to write a PONG (a status_update containing correlation, or 'pong'/'received').

        This is the confirmation mechanism within the three-layer fallback: RemaGraph
        acts as the primary persistent ack channel. Uses short polling (pull), since
        RemaGraph isn't event-driven.
        Enhancement: also detects a side-channel report as a backup ack, reducing reliance on pong alone.
        """
        if not _rg or not _rg.is_remagraph_enabled():
            return {"ok": False, "reason": "memory backend disabled"}
        start = time.time()
        # Check the in-memory side report directly first (an underlying detection fix)
        if self._has_side_report(task_id):
            return {"ok": True, "pong": self._side_reports.get(task_id), "via": "side-channel-direct"}
        while time.time() - start < timeout_sec:
            try:
                mems = _rg.recall_memories(task_id, agent_id, top_k=10, project_id=self.project) or []
                for m in mems:
                    if m.get("kind") != "status_update":
                        continue
                    tags = m.get("tags") or []
                    # The tower's own bookkeeping records (update_delivery_state's FSM
                    # state transitions, and dispatch_with_memory_confirm's own completion
                    # write) are all tagged "tower-bookkeeping" and don't count as a real
                    # downstream reply: their summary/learnings echo back the caller's
                    # correlation string verbatim (e.g.
                    # summary=f"...correlation={correlation}"); if we don't exclude these,
                    # the correlation-substring match below would mistake the tower's own
                    # bookkeeping for a real downstream ack, producing a false-positive
                    # "confirmed success". A genuine downstream ack comes from the
                    # downstream agent (or the side-channel handler) calling store_memory
                    # directly, and never carries this tag. We also keep the old
                    # "fsm"/"delivery-state" check as defense-in-depth (covering older
                    # data/call paths that haven't picked up the new tag yet).
                    if (
                        "tower-bookkeeping" in tags
                        or "fsm" in tags
                        or "delivery-state" in tags
                    ):
                        continue
                    txt = str(m.get("summary", "")) + " " + str(m.get("handoff_note", "")) + " " + str(m.get("learnings", ""))
                    if "side-channel" in str(tags) or "complete" in str(tags):
                        return {"ok": True, "pong": m, "via": "side-channel"}
                    if correlation and correlation in txt:
                        return {"ok": True, "pong": m, "via": "correlation"}
                    low = txt.lower()
                    if "pong" in low or "收到" in low or "ack" in low:
                        return {"ok": True, "pong": m, "via": "keyword"}
            except Exception as e:  # noqa: BLE001  # PONG-polling is best-effort; a single bad poll must not abort the wait
                print(f"[acp-router] wait_for_pong poll iteration failed (ignored): {e}", flush=True)
            # Keep directly checking the event/side report during polling too
            if self._has_side_report(task_id):
                return {"ok": True, "pong": self._side_reports.get(task_id), "via": "side-channel-direct"}
            time.sleep(0.5)
        return {"ok": False, "reason": "timeout", "task_id": task_id, "agent_id": agent_id}

    def dispatch_with_memory_confirm(
        self,
        prompt: str,
        *,
        target: str | None = None,
        pane_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """AcpRouter central facade: the primary dispatch entry point of the sole command-tower abstraction.

        **Automatic routing has been removed**: target or pane_id must be specified explicitly (user preference takes priority).
        Forces every path through:
        - prepare_dispatch_text (recall + augment)
        - store_memory (start of handoff)
        - performing the routing (explicit target only)
        - record_fleet_member (HR mechanism: the tower owns the fleet lifecycle + report_sock)
        - store_memory (completion status report)
        - automatically injecting the side-channel report instruction (PPLX's sole recommendation)

        External projects use this via create_herdr_router() + this method.
        Uses RemaGraph as the single source of truth for coordination.
        """
        if not prompt or not isinstance(prompt, str):
            return {"ok": False, "error": "prompt must be a non-empty string", "routed_to": None}

        if not target and not pane_id:
            return {"ok": False, "error": "target or pane_id must be specified explicitly (automatic smart routing has been removed; control now belongs to the user/preferences)", "routed_to": None}

        # Dynamically resolve a space name -> an explicit pane (secondary layer of the three-layer fallback)
        resolved_target, resolved_pane = self._resolve_target_to_pane(target, pane_id)
        effective_pane = resolved_pane or pane_id
        effective_target = resolved_target or target

        # 1. Force prepare_dispatch_text
        base_tid = self._make_valid_task_id("tower-confirm")
        send_text = prompt
        used_tid = base_tid
        used_aid = "acp-router-confirm"
        if _rg and _rg.is_remagraph_enabled():
            try:
                send_text, used_tid, used_aid = _rg.prepare_dispatch_text(
                    prompt, base_task_id=base_tid, agent_id=used_aid, project=self.project
                )
            except Exception as e:  # noqa: BLE001  # recall/prepare is best-effort; fall back to the raw prompt + base ids
                print(f"[acp-router] dispatch_with_memory_confirm: prepare_dispatch_text failed (ignored): {e}", flush=True)

        # 2. Force store_memory at the start (handoff) + correlation for PONG (Primary: RemaGraph PING)
        correlation = f"dispatch-{used_tid}-{int(time.time())}"
        if _rg and _rg.is_remagraph_enabled():
            try:
                _rg.store_memory(
                    used_tid,
                    used_aid,
                    kind="task_handoff",
                    summary=prompt[:120],
                    handoff_note=f"AcpRouter.dispatch_with_memory_confirm target={effective_target or 'auto'} correlation={correlation}",
                    project_id=self.project,
                    tags=["acp-router", "dispatch-confirm", "central-facade", "ping"],
                    learnings=[f"correlation={correlation}"],
                )
                _rg.update_delivery_state(used_tid, used_aid, "INIT", project_id=self.project, correlation=correlation)
                _rg.update_delivery_state(used_tid, used_aid, "DISPATCH_PENDING", project_id=self.project, correlation=correlation)
                _rg.update_delivery_state(used_tid, used_aid, "AWAIT_PONG", project_id=self.project, correlation=correlation)
            except Exception as e:  # noqa: BLE001  # handoff bookkeeping is best-effort; must not block the dispatch itself
                print(f"[acp-router] dispatch_with_memory_confirm: handoff bookkeeping failed (ignored): {e}", flush=True)

        # 3. Perform the routing (explicit target only, no automatic routing) + 3-layer backup (PPLX + §9)
        # Secondary: ACP to resolved explicit pane
        # Then wait PONG (primary ack)
        # Fallback: side-channel if no PONG
        # Avoid duplicate: use correlation + attempts

        result: dict[str, Any] = {}
        try:
            async def _run() -> dict[str, Any]:
                try:
                    resp: Any = await self.prompt(
                        session_id=f"confirm-{int(time.time())}",
                        prompt=[send_text],
                        target=effective_target,
                    )
                except Exception as e:  # noqa: BLE001
                    resp = {"error": str(e)}
                routed = effective_target or effective_pane or "explicit-required"
                text_out = ""
                if isinstance(resp, dict):
                    text_out = resp.get("text") or str(resp.get("_meta", {}).get("result_text", resp))
                else:
                    if hasattr(resp, "_meta") and resp._meta:
                        text_out = resp._meta.get("result_text", "") or str(resp)
                    else:
                        text_out = str(resp)
                # #60 + PPLX consensus review (docs/decisions/acp-layer-status-20260725.md):
                # an echo fallback (regardless of whether the cause is the ACP SDK not
                # being installed, or target never having been register_agent()'d) means
                # confirmed nothing was actually sent to any downstream, so it can't be
                # reported as "success" — a caller getting ok=True with no delivery, no
                # PONG, no downstream execution, and no explicit error would have its
                # routing trust, monitoring interpretation, retry strategy, and failure
                # attribution directly broken (silent success).
                echo_fallback = _ECHO_FALLBACK_MARKER in text_out
                has_error = "error" in str(resp).lower()
                return {
                    "ok": (not has_error) and not echo_fallback,
                    "routed_to": routed,
                    "response": text_out,
                    "task_id": used_tid,
                    "agent_id": used_aid,
                    "status": "dispatched",
                    "project": self.project,
                    "echo_fallback": echo_fallback,
                    "acp_unavailable": not ACP_SDK_AVAILABLE,
                    "degraded": echo_fallback,
                    "delivery_status": "not_attempted" if echo_fallback else "attempted",
                }

            result = asyncio.run(_run())

            # Wait for PONG (Primary RemaGraph ack, short timeout for 3-layer)
            pong_result = self.wait_for_pong(used_tid, used_aid, correlation=correlation, timeout_sec=8.0)
            side_report = None
            # Prefer the event-driven side wait for reliability (no longer relying only on recall)
            if not pong_result.get("ok") and (
                self.wait_for_side_report(used_tid, timeout_sec=5.0) or self._has_side_report(used_tid)
            ):
                side_report = self._side_reports.get(used_tid)
                if _rg:
                    try:
                        _rg.update_delivery_state(used_tid, used_aid, "SIDE_REPORT_RECEIVED", project_id=self.project, correlation=correlation, context={"via": "side-channel"})
                    except Exception as e:  # noqa: BLE001  # FSM bookkeeping must not block confirmation once a side report already arrived
                        print(f"[acp-router] update_delivery_state(SIDE_REPORT_RECEIVED) failed (ignored): {e}", flush=True)
            if _rg and not side_report:
                try:
                    mems = _rg.recall_memories(used_tid, used_aid, top_k=5, project_id=self.project) or []
                    for m in mems:
                        tags = m.get("tags") or []
                        if "side-channel" in str(tags) or "complete" in str(tags):
                            side_report = m
                            _rg.update_delivery_state(used_tid, used_aid, "SIDE_REPORT_RECEIVED", project_id=self.project, correlation=correlation, context={"via": "side-channel"})
                            break
                except Exception as e:  # noqa: BLE001  # side-channel fallback lookup is best-effort; must not block result assembly
                    print(f"[acp-router] side-report recall_memories lookup failed (ignored): {e}", flush=True)

            if pong_result.get("ok"):
                if _rg:
                    try:
                        _rg.update_delivery_state(used_tid, used_aid, "PONG_RECEIVED", project_id=self.project, correlation=correlation)
                    except Exception as e:  # noqa: BLE001  # FSM bookkeeping failure must not downgrade an already-successful dispatch result (test-verified against an arbitrary exception type)
                        print(f"[acp-router] update_delivery_state(PONG_RECEIVED) failed (ignored): {e}", flush=True)
                result["pong_confirmed"] = True
                result["pong"] = pong_result.get("pong")
                result["confirmed_via"] = "pong"
            elif side_report:
                # Strengthen the side-channel as a fallback (third layer) -- reduces reliance on pong
                result["pong_confirmed"] = False
                result["side_confirmed"] = True
                result["confirmed_via"] = "side-channel"
                result["side_report"] = side_report
            else:
                # Third-layer fallback: side-channel report (_send_task_report already sent it)
                if _rg:
                    try:
                        _rg.update_delivery_state(used_tid, used_aid, "TIMEOUT", project_id=self.project, correlation=correlation, context={"fallback": "side-channel"})
                    except Exception as e:  # noqa: BLE001  # same as above: an FSM bookkeeping failure must not overwrite the actual dispatch result
                        print(f"[acp-router] update_delivery_state(TIMEOUT) failed (ignored): {e}", flush=True)
                result["pong_confirmed"] = False
                result["fallback"] = "side-channel or manual ACK"
                # #60: use a more specific value once we're sure it's an echo fallback,
                # rather than sharing the overly-overloaded "none" with the "dispatched
                # but PONG not yet received" case (so a caller can clearly distinguish
                # two states of completely different severity).
                result["confirmed_via"] = "echo-fallback" if result.get("echo_fallback") else "none"

            # PPLX's sole side-channel recommendation: send a structured report once
            # dispatch is done (Herdr only handles the done event)
            # #73: an echo fallback (nothing was ever sent to any downstream at all)
            # should not send this "task complete" report -- this is the router
            # connecting to its own side-channel listener (the one opened by
            # _start_report_side_channel), so the report sent would be picked up by the
            # same router's own _report_server and written into self._side_reports,
            # which would then get misread by the _has_side_report() check further down
            # in step 5.5 as "the downstream confirmed completion via the side-channel".
            # Since no downstream was ever called, sending this report at all is
            # misleading -- it's not merely a downstream interpretation-logic problem.
            if not result.get("echo_fallback"):
                try:
                    self._send_task_report(used_tid, used_aid, result)
                except Exception as e:  # noqa: BLE001  # side-channel report send is best-effort; must not fail the overall dispatch
                    print(f"[acp-router] dispatch_with_memory_confirm: side-channel report send failed (ignored): {e}", flush=True)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc), "routed_to": effective_target, "task_id": used_tid, "project": self.project}
            if _rg and _rg.is_remagraph_enabled():
                try:
                    _rg.store_memory(
                        used_tid,
                        used_aid,
                        kind="status_update",
                        summary=f"dispatch_with_memory_confirm failed: {str(exc)[:100]}",
                        handoff_note=str(exc)[:150],
                        project_id=self.project,
                        tags=["acp-router", "error"],
                    )
                    _rg.update_delivery_state(used_tid, used_aid, "DISPATCH_FAILED", project_id=self.project, correlation=correlation)
                except Exception as e:  # noqa: BLE001  # error-path bookkeeping is best-effort; must not mask the original exception being handled
                    print(f"[acp-router] dispatch_with_memory_confirm: error-path bookkeeping failed (ignored): {e}", flush=True)

        # 4. Force record_fleet_member (required on every dispatch path, so the tower can recycle it) + avoid duplicates via correlation
        if _rg and _rg.is_remagraph_enabled():
            p_id = effective_pane or pane_id or f"acp-virtual-{used_tid}"
            n = name or (effective_target or "fleet-member")
            try:
                _rg.record_fleet_member(
                    used_tid, used_aid, pane_id=p_id, name=n, project_id=self.project
                )
            except Exception as e:  # noqa: BLE001  # fleet-member bookkeeping is best-effort; must not block the dispatch result
                print(f"[acp-router] record_fleet_member (initial) failed (ignored): {e}", flush=True)

            # Event-driven: watch this pane immediately after dispatch (the tower reacts
            # right away when the Herdr event arrives), and provide the agent with the
            # side-channel report_sock for its completion report (PPLX's sole recommendation)
            report_learnings = []
            if hasattr(self, '_report_sock_path'):
                report_learnings = [f"report_sock={self._report_sock_path}"]
            try:
                _rg.record_fleet_member(
                    used_tid, used_aid, pane_id=p_id, name=n, project_id=self.project,
                    learnings=report_learnings
                )
            except Exception as e:  # noqa: BLE001  # fleet-member bookkeeping is best-effort; must not block the dispatch result
                print(f"[acp-router] record_fleet_member (with report_sock) failed (ignored): {e}", flush=True)
            if effective_pane:
                try:
                    self._watch_fleet_pane(effective_pane)
                except Exception as e:  # noqa: BLE001  # dynamic event-watch registration is best-effort; must not block the dispatch result
                    print(f"[acp-router] _watch_fleet_pane({effective_pane}) failed (ignored): {e}", flush=True)

        # 5. Force a store_memory completion status report (in the PONG direction)
        if _rg and _rg.is_remagraph_enabled():
            try:
                is_ok = result.get("ok", False)
                _rg.store_memory(
                    used_tid,
                    used_aid,
                    kind="status_update",
                    summary=f"dispatch_with_memory_confirm complete routed={result.get('routed_to')} ok={is_ok} correlation={correlation}",
                    handoff_note=f"record_fleet_member done, effective_pane={effective_pane}, memory coordination complete",
                    project_id=self.project,
                    # "tower-bookkeeping": this record is the tower's own bookkeeping of
                    # the dispatch result, literally containing the correlation string --
                    # it is not a real downstream ack -- wait_for_pong must skip it,
                    # otherwise the immediately following step 5.5 wait_for_pong would
                    # mistake this self-written record for a real PONG.
                    tags=["acp-router", "done", "fleet", "memory-confirm", "ping-sent", "tower-bookkeeping"],
                    learnings=[f"correlation={correlation}"],
                )
            except Exception as e:  # noqa: BLE001  # completion-status bookkeeping is best-effort; must not block returning the result
                print(f"[acp-router] dispatch_with_memory_confirm: completion-status store_memory failed (ignored): {e}", flush=True)

        # 5.5 Briefly wait for PONG (RemaGraph primary channel, pull-based confirmation)
        # #73: the _has_side_report() check here was once misjudged -- the root cause
        # wasn't this re-check logic itself, but that _send_task_report() before step 5
        # above would also send a "task complete" report to the router's own
        # side-channel listener during an echo fallback, so this check would find its own
        # self-sent report and misjudge side_confirmed=True. We've already changed
        # _send_task_report() above to not send during an echo fallback (see the comment
        # there); with the source of that self-report gone, this re-check logic itself
        # doesn't need to change -- keeping it as-is avoids breaking
        # test_real_pong_confirmation_is_not_mislabeled_as_echo_fallback's synthetic
        # scenario of "echo_fallback is True but a PONG was genuinely received" (the PONG
        # signal takes priority over the weaker echo-fallback signal).
        pong_info = None
        if _rg and _rg.is_remagraph_enabled():
            try:
                pong_info = self.wait_for_pong(used_tid, used_aid, correlation=correlation, timeout_sec=3.0)
            except Exception as e:  # noqa: BLE001  # secondary PONG re-check is best-effort; a failure here must not overwrite result state
                print(f"[acp-router] dispatch_with_memory_confirm: secondary wait_for_pong failed (ignored): {e}", flush=True)
        if pong_info:
            result["pong_confirmed"] = pong_info.get("ok", False)
            result["pong"] = pong_info.get("pong") if pong_info.get("ok") else None

        # New confirmation field: side-channel fallback confirmation (set even on a
        # different path or on timeout) -- prefer a direct check/event first
        side_report = None
        if self._has_side_report(used_tid):
            side_report = self._side_reports.get(used_tid)
        elif _rg and _rg.is_remagraph_enabled():
            try:
                mems = _rg.recall_memories(used_tid, used_aid, top_k=5, project_id=self.project) or []
                for m in mems:
                    tags = m.get("tags") or []
                    if "side-channel" in str(tags) or "complete" in str(tags):
                        side_report = m
                        _rg.update_delivery_state(used_tid, used_aid, "SIDE_REPORT_RECEIVED", project_id=self.project, correlation=correlation, context={"via": "side-channel"})
                        break
            except Exception as e:  # noqa: BLE001  # final side-report lookup is best-effort; must not block returning the result
                print(f"[acp-router] dispatch_with_memory_confirm: final side-report lookup failed (ignored): {e}", flush=True)

        if side_report:
            result["side_confirmed"] = True
            result["side_report"] = side_report
            result["confirmed_via"] = result.get("confirmed_via", "side-channel")
        else:
            result.setdefault("side_confirmed", False)
            result.setdefault("confirmed_via", "pong" if result.get("pong_confirmed") else "none")

        # Lifecycle cleanup: every read of _side_reports/_side_events for used_tid is done
        # by this point (already captured into result), so there's no need to keep it
        # around any longer -- avoids these two dicts growing unbounded (a memory leak)
        # over the number of dispatches in a long-lived tower session. Uses
        # .pop(key, None) so it's safe even if this task_id never produced a side report
        # / event.
        self._side_reports.pop(used_tid, None)
        self._side_events.pop(used_tid, None)
        self._side_report_times.pop(used_tid, None)

        # Backward compatibility for old route_via callers (tests and the commander's old path), which expect "resp", "status", "registered"
        result.setdefault("resp", result.get("response", ""))
        result.setdefault("status", "dispatched via central")
        try:
            result.setdefault("registered", list(self.registered_agents.keys()))
        except Exception:  # noqa: BLE001  # registry snapshot for backward-compat callers; must not fail the whole dispatch result
            result.setdefault("registered", [])

        return result

    # Convenience method for starting the router synchronously (as an ACP server entry point)
    @staticmethod
    def run_server(router: AcpRouter) -> None:
        """Start the router as an ACP server (uses acp run_agent, waits for an external client to connect)."""
        if not ACP_SDK_AVAILABLE:
            print("ACP SDK not available. Install with: uv pip install 'agent-client-protocol>=0.10.0,<0.12'")
            return
        from acp import run_agent
        print("AcpRouter starting as ACP server (registered:", list(router.registered_agents.keys()), ")")
        print("Waiting for ACP client (e.g. Zed, custom client) on stdio...")
        run_agent(router)  # blocks until client connects/uses


# Convenience factory
def create_herdr_router(
    *,
    project: str = "herdr-router",
    additional_paths: list[str] | None = None,
    start_fleet_listener: bool = True,
) -> AcpRouter:
    """Create an AcpRouter (the core of the central facade). External use:
    router = create_herdr_router(); router.dispatch_with_memory_confirm(...) as a simple API.

    Built in: every dispatch path is forced through prepare_dispatch_text + store_memory
    + record_fleet_member + a completion store. Uses RemaGraph for coordination.

    start_fleet_listener=False lets the caller skip the fleet event listener's background
    thread side effect (see the AcpRouter.__init__ note), suitable for tests or one-off operations.
    """
    r = AcpRouter(project=project, start_fleet_listener=start_fleet_listener)
    r.discover(additional_paths=additional_paths)
    return r


class CentralTower:
    """
    High-level "single command tower" facade.

    Goal: let external projects plug in and use herdr-bridge as the central command
    tower with minimal knowledge. Provides a clean, sync-friendly API that hides internal
    details.

    Usage:
        from herdr_bridge.acp.router import create_central_tower
        tower = create_central_tower(project="my-tower")
        result = tower.dispatch("Please research quantum computing", target="research-tui")  # target must be explicit (no automatic routing)
        results = tower.batch_dispatch(["task 1", "task 2"], targets=["code-tui", "wV:p1"])

    Features:
    - Internally delegates to AcpRouter.dispatch_with_memory_confirm as the central facade
    - **Forces every dispatch path** through: prepare_dispatch_text + store_memory + record_fleet_member
    - All external downstream projects route through a simple API, with RemaGraph as the single source of coordination
    - Automatically calls store_memory to report status on completion
    - Return values include task_id for tracking

    Recommended: use create_herdr_router() + router.dispatch_with_memory_confirm(...)
    directly, or this thin CentralTower compatibility layer.
    """

    def __init__(self, *, project: str = "herdr-bridge", additional_paths: list[str] | None = None) -> None:
        """Create the central tower; internally creates an AcpRouter and runs discover() automatically.
        Per RemaGraph compliance + §9, uses "herdr-bridge" as the default dedicated project.
        """
        self.project = project
        from herdr_bridge.orchestration import memory as _rg  # used internally
        self._rg = _rg
        self._router: AcpRouter = create_herdr_router(project=project, additional_paths=additional_paths)
        # Ensure the RemaGraph project exists + enforce the safety valve (PPLX gate)
        if self._rg and self._rg.is_remagraph_enabled():
            try:
                self._rg._ensure_remagraph_project(project)
                self._rg._enforce_remagraph_safety_valve(project)
            except Exception as e:  # noqa: BLE001  # safety-valve failure must not block tower startup (test-verified against an arbitrary exception type)
                print(f"[central-tower {project}] safety valve check failed (ignored): {e}", flush=True)

    @property
    def router(self) -> AcpRouter:
        """Advanced access (use only when necessary; the dispatch family of methods is recommended instead)."""
        return self._router

    def register_agent(self, name: str, command: str, args: list[str] | None = None, **meta) -> None:
        """Dynamically register a real external ACP agent (persisted)."""
        self._router.register_agent(name, command, args, **meta)
        try:
            self._router.save_user_registered(name, command, args or [], **meta)
        except Exception as e:  # noqa: BLE001  # registry-persist failure must not block the in-memory registration (test-verified against an arbitrary exception type)
            print(f"[central-tower {self.project}] register_agent: save_user_registered failed (ignored): {e}", flush=True)

    def dispatch(
        self,
        prompt: str,
        *,
        target: str | None = None,
        timeout_sec: float = 60,
    ) -> dict[str, Any]:
        """
        A single dispatch (sync). Delegates to AcpRouter.dispatch_with_memory_confirm as the central facade.

        Every path is already forced through prepare_dispatch_text + store_memory + record_fleet_member.
        External callers are recommended to go directly through the router or this thin facade.
        """
        return self._router.dispatch_with_memory_confirm(prompt, target=target)

    def dispatch_with_memory_confirm(
        self,
        prompt: str,
        *,
        target: str | None = None,
        pane_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """The central tower's convenience entry point, delegating to the AcpRouter implementation (forces the three steps + RemaGraph coordination)."""
        return self._router.dispatch_with_memory_confirm(
            prompt, target=target, pane_id=pane_id, name=name
        )

    def batch_dispatch(
        self,
        prompts: list[str],
        *,
        target: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Batch dispatch (fleet-level).

        Each prompt is independently prepared + dispatched + stored.
        When target=None, each is auto-routed independently (research/code prompts can be mixed).
        """
        results: list[dict[str, Any]] = []
        for i, p in enumerate(prompts or []):
            try:
                r = self.dispatch(p, target=target)
                r["idx"] = i
                results.append(r)
            except Exception as e:  # noqa: BLE001
                results.append({"idx": i, "ok": False, "error": str(e), "prompt": p[:50]})
        return results

    def list_agents(self) -> list[str]:
        """Currently known downstream agents."""
        return self._router.discover_agents()

    def get_registry_summary(self) -> dict[str, Any]:
        """Registry summary (for debugging/governance)."""
        return self._router.get_registry_summary()


def create_central_tower(*, project: str = "herdr-tower", additional_paths: list[str] | None = None) -> CentralTower:
    """
    Create a high-level central command tower (a thin compatibility layer, internally
    using AcpRouter.dispatch_with_memory_confirm as the sole central facade).

    All external use routes through this or directly through AcpRouter's simple API;
    every path is forced through prepare+store+record; RemaGraph handles coordination.

    Example:
        tower = create_central_tower(project="my-downstream-project")
        res = tower.dispatch("Analyze the latest regulations", target=None)
        # or: router = create_herdr_router(); res = router.dispatch_with_memory_confirm(...)
    """
    return CentralTower(project=project, additional_paths=additional_paths)
