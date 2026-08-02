# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""herdr-commander CLI — entry point for occasional users.

Usage:
  herdr-commander start          # environment startup check + status
  herdr-commander run            # run the first task
  herdr-commander run --dry-run  # preview only
  herdr-commander status         # show current assistants
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

from herdr_bridge.errors import HerdrBridgeError
from herdr_bridge.light import tui_patterns

# RemaGraph memory integration (governance layer, embedded) — used for notify-pane audit records
try:
    from herdr_bridge.orchestration import memory as _rg
except Exception:  # noqa: BLE001
    _rg = None  # type: ignore[assignment]


def _say(msg: str) -> None:
    print(f"\033[1;35m[Tower]\033[0m {msg}")


def _err(msg: str) -> None:
    print(f"\033[1;31m[Tower]\033[0m {msg}", file=sys.stderr)


def _check_herdr() -> bool:
    return shutil.which("herdr") is not None


def _resolve_memory_project(args_project: str | None, default: str = "herdr-bridge") -> str:
    """Resolve the effective memory-project id.

    Priority: --project flag > HERDR_MEMORY_PROJECT > REMAGRAPH_PROJECT > default.
    Reading REMAGRAPH_PROJECT directly (without setting HERDR_MEMORY_PROJECT) is
    an intentional advanced-user bypass of the Herdr Bridge Memory naming layer
    (documented in docs/memory-advanced.md) -- it is allowed to work, not an
    error condition.
    """
    return (
        args_project
        or os.environ.get("HERDR_MEMORY_PROJECT")
        or os.environ.get("REMAGRAPH_PROJECT")
        or default
    )


def _resolve_signal_project(args_project: str | None, default: str = "herdr-bridge") -> str:
    """Resolve the effective project id for `signal start`/`send`/`status`.

    Priority: --project flag > CT_PROJECT env > HERDR_MEMORY_PROJECT > default.

    Unlike `_resolve_memory_project` (used by memory/dispatch/notify-pane,
    where "which project's records" is an independent choice from "who am
    I"), the signal subcommands are always about *this tower's own* daemon --
    silently defaulting to "herdr-bridge" when a non-herdr-bridge tower
    forgets --project doesn't just query the wrong project, it tries to
    start/operate herdr-bridge's own daemon under someone else's identity
    (2026-08-01 field report from a downstream deployment: bare `signal start` collided
    with herdr-bridge's own daemon lock and failed). CT_PROJECT -- written by
    bootstrap-tower.sh's session.env and set on every towerops launchd plist
    -- is the actual "which tower is this" source of truth in this ecosystem,
    so it's checked before the more general-purpose HERDR_MEMORY_PROJECT.

    REMAGRAPH_PROJECT is deliberately NOT in this chain (2026-08-01 second
    field report, a real bug in the first fix, not a stylistic
    choice): `herdr_bridge/__init__.py` unconditionally sets
    `os.environ["REMAGRAPH_PROJECT"] = "herdr-bridge"` as an import-time side
    effect (`_ensure_remagraph_project`, orchestration/memory.py), on every
    single process that imports this package -- which is every
    `herdr-commander` invocation. That's a plain assignment, not
    `setdefault`, so it clobbers any value the user exported themselves
    *after* import runs, and it made the first version of this function's
    warning genuinely dead code (the REMAGRAPH_PROJECT branch always hit,
    so `resolved` was never `None`). REMAGRAPH_PROJECT is safe to read for
    `_resolve_memory_project`'s original purpose (an intentional
    advanced-user RemaGraph-layer bypass), but for "which tower is this"
    it's self-polluted and cannot be trusted -- so signal resolution skips
    it entirely rather than trying to out-guess when the value is genuine.

    CT_PROJECT is only set on processes launchd/bootstrap actually started
    with it in their environment -- an interactive shell inside an agent
    session does NOT inherit it (each tool-call shell is a fresh process from
    the profile, not a child of the bootstrap process). So a bare `signal
    status` typed by hand in a non-herdr-bridge tower's own session still
    falls through to `default`, now correctly triggering the warning below
    (2026-08-01 follow-up field report: this used to be worse
    than a plain wrong answer, because if herdr-bridge's own daemon happened
    to be alive, the tower saw "✅ running" and reasonably assumed it was
    looking at *its own* daemon). Since that fallback can't be fixed away by
    adding more env vars to check -- there's no reliable "who is asking"
    signal for a bare interactive shell -- the honest fix is to stop being
    silent about it: warn on stderr whenever no explicit, trustworthy source
    was found, every time, not just once.
    """
    resolved = (
        args_project
        or os.environ.get("CT_PROJECT")
        or os.environ.get("HERDR_MEMORY_PROJECT")
    )
    if resolved is not None:
        return resolved
    _err(
        f"⚠️  no --project (and no CT_PROJECT/HERDR_MEMORY_PROJECT env) given -- "
        f"defaulting to project={default!r}. If this isn't the tower you meant, pass "
        "--project explicitly (an interactive agent-session shell does NOT inherit "
        "CT_PROJECT from bootstrap; REMAGRAPH_PROJECT is not checked here -- it's "
        "force-set to 'herdr-bridge' as an import side effect and can't be trusted)."
    )
    return default


def _default_socket() -> str | None:
    env = os.environ.get("HERDR_SOCKET_PATH")
    if env:
        return env
    home = Path.home()
    for name in ("light-commander", "bridge-test"):
        p = home / ".config" / "herdr" / "sessions" / name / "herdr.sock"
        if p.exists():
            return str(p)
    return None


def cmd_start(args: argparse.Namespace) -> int:
    """Start up the light environment (check for herdr, prompt for a sandbox if needed)."""
    _say("Checking environment…")
    if not _check_herdr():
        _err("herdr not found.")
        print()
        print("Please install Herdr first: https://herdr.dev")
        print("Once installed, run: herdr-commander start")
        return 1

    sock = _default_socket()
    if sock:
        os.environ["HERDR_SOCKET_PATH"] = sock
        _say("Connected to the environment")
    else:
        _say("No environment currently running was found")
        print()
        print("Suggested next step (pick one):")
        print("  A) Quick sandbox: bash scripts/sandbox-up.sh")
        print("  B) Manual: herdr --session light-commander server")
        print("     Then start your AI assistant in another terminal")
        print()
        if args.auto_sandbox:
            # light/cli.py -> light -> herdr_bridge -> src -> repo root
            repo = Path(__file__).resolve().parents[3]
            script = repo / "scripts" / "sandbox-up.sh"
            if script.exists():
                _say("Starting sandbox…")
                rc = subprocess.call(["bash", str(script)])
                if rc != 0:
                    _err("Sandbox startup failed")
                    return rc
                sock = str(
                    Path.home()
                    / ".config"
                    / "herdr"
                    / "sessions"
                    / "bridge-test"
                    / "herdr.sock"
                )
                os.environ["HERDR_SOCKET_PATH"] = sock
            else:
                _err("sandbox-up.sh not found")
                return 1
        else:
            return 0

    try:
        from herdr_bridge import connect

        actions = connect(socket_path=os.environ.get("HERDR_SOCKET_PATH"))
        agents = actions.list_agents("commander:light")
    except Exception as exc:  # noqa: BLE001
        _err(f"Connection failed: please confirm Herdr is running ({type(exc).__name__})")
        return 1

    if not agents:
        _say("The environment is ready, but there's no AI assistant yet.")
        print("Please start at least one assistant in Herdr (e.g. claude), then run:")
        print("  herdr-commander run")
        return 0

    _say(f"Ready. There are currently {len(agents)} assistant(s).")
    print()
    print("Next steps:")
    print("  herdr-commander run            # run the first task (thumbnail function + tests)")
    print("  herdr-commander run --dry-run  # preview only, don't actually run it")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    sock = args.socket or _default_socket()
    if sock:
        os.environ["HERDR_SOCKET_PATH"] = sock
    if not _check_herdr() and not sock:
        _err("herdr not found, and no socket was specified.")
        return 1
    try:
        from herdr_bridge import connect

        actions = connect(socket_path=sock)
        agents = actions.list_agents("commander:light")
    except Exception as exc:  # noqa: BLE001
        _err("Unable to connect to the environment. Please run herdr-commander start first")
        if args.verbose:
            _err(str(exc))
        return 1

    if not agents:
        _say("No assistants are currently online.")
    else:
        _say(f"Assistant list ({len(agents)}):")
        for a in agents:
            brand = getattr(a, "brand", "") or ""
            print(f"  • {a.agent_id}  status={a.status}" + (f"  ({brand})" if brand else ""))

    # Append router registry info (extra CLI hook; mid-term goal: mount the router onto the main CLI)
    try:
        from herdr_bridge.acp.router import create_herdr_router
        rr = create_herdr_router()
        _say(f"ACP Router registry: {len(rr.discover_agents())} agents (dynamic discovery)")
        if getattr(args, "verbose", False):
            print("  agents:", rr.discover_agents())
    except Exception as exc:  # noqa: BLE001  # best-effort extra info only; router may not be installed/configured and must never break `status`
        if getattr(args, "verbose", False):
            _err(f"(router registry info unavailable: {exc})")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    sock = args.socket or _default_socket()
    if sock:
        os.environ["HERDR_SOCKET_PATH"] = sock

    use_router = getattr(args, "use_acp_router", False) or not sock
    target = getattr(args, "router_target", None)

    if use_router:
        # Support a standalone router demo without needing a herdr socket (added target + early/mid integration)
        try:
            from herdr_bridge import connect as _connect  # optional
            from herdr_bridge.light import LightCommander
            from herdr_bridge.light.tasks import get_task
            task = get_task(args.task)
            try:
                actions = _connect(socket_path=sock) if sock else None
            except Exception:  # noqa: BLE001  # intentional fallback: run the standalone router demo with a Mock actions object if a real socket connection isn't available
                actions = None
            commander = LightCommander(actions or type("Mock", (), {})())
            prompt = task.user_prompt or task.agent_prompt
            effective_target = target if target and target != "auto" else None
            _say(f"Routing via ACP Router (target={effective_target or 'auto'})")
            # Prefer run_task_via_acp (use_router) for full RemaGraph + router integration
            router_res = commander.run_task_via_acp(
                task,
                use_router=True,
                agent=effective_target,  # None -> router auto choose via caps
            ) if hasattr(commander, "run_task_via_acp") else commander.route_via_acp_router(prompt, target_agent=effective_target)
            print(router_res)
            return 0 if "error" not in str(router_res) else 2
        except Exception as exc:  # noqa: BLE001
            _err(f"ACP Router execution failed: {exc}")
            return 2

    try:
        from herdr_bridge import connect
        from herdr_bridge.light import LightCommander
        from herdr_bridge.light.tasks import get_task

        actions = connect(socket_path=sock)
        commander = LightCommander(actions)
        task = get_task(args.task)
    except Exception as exc:  # noqa: BLE001
        _err("Unable to connect to the environment.")
        print()
        print("Please first:")
        print("  1. Install and start Herdr (https://herdr.dev)")
        print("  2. herdr-commander start")
        print("  3. Confirm at least one AI assistant is running")
        if args.verbose:
            _err(f"Technical detail: {type(exc).__name__}: {exc}")
        return 1

    _say(f"Starting: {task.title}")
    if not args.dry_run:
        print(f'  "{task.user_prompt[:60]}…"')
        print()

    result = commander.run_task(
        args.task,
        timeout_sec=args.timeout,
        dry_run=args.dry_run,
    )
    print()
    print(result.user_text())
    print()
    if args.verbose and result.raw_reason:
        print(f"(internal: reason={result.raw_reason}, agent={result.agent_id})")
    return 0 if result.ok else 2


def cmd_router(args: argparse.Namespace) -> int:
    """ACP Router commands: start / route / discover / list"""
    try:
        from herdr_bridge.acp.router import AcpRouter, create_herdr_router
        from herdr_bridge.light.commander import LightCommander
    except ImportError as exc:
        _err(f"Unable to load router: {exc}")
        return 1

    addp = [args.path] if getattr(args, "path", None) else None
    router = create_herdr_router(project=args.project if hasattr(args, "project") else "herdr-router", additional_paths=addp)
    if args.action in ("discover", "list"):
        cap = getattr(args, "capability", None)
        if cap:
            reg = router.list_registry_filtered(capability=cap)
            agents = [item["name"] for item in reg]
        else:
            agents = router.discover_agents()
            reg = router.list_registry() if hasattr(router, "list_registry") else []
        _say(f"Registered agents (registry): {agents}")
        if reg:
            print("registry details (expanded discovery):")
            for item in reg:
                name = item.get("name")
                spec = item.get("spec", {})
                caps = spec.get("capabilities", [])
                desc = spec.get("description", "")
                print(f"  - {name}: caps={caps} desc={desc}")
            if not cap:
                # demo expanded
                print("  all capabilities:", router.list_capabilities())
                searchers = router.list_registry_filtered(capability="search")
                print("  filtered by cap=search:", [s["name"] for s in searchers])
        # show summary for discovery
        summary = router.get_registry_summary() if hasattr(router, "get_registry_summary") else {}
        if summary:
            print("  registry summary:", {"count": summary.get("count"), "caps": summary.get("capabilities")})
        return 0

    if args.action == "register":
        name = getattr(args, "name", None) or "custom-tui"
        script = getattr(args, "script", None)
        command = getattr(args, "command", None) or ("uv" if script else None)
        args_str = getattr(args, "args", None)
        if args_str:
            import json
            try:
                arg_list = json.loads(args_str) if args_str.strip().startswith("[") else [a.strip() for a in args_str.split(",") if a.strip()]
            except json.JSONDecodeError:
                arg_list = [a.strip() for a in args_str.split() if a.strip()]
        elif script:
            arg_list = ["run", "python", script]
        else:
            arg_list = []
        caps = (getattr(args, "capabilities", "general") or "general").split(",")
        caps = [c.strip() for c in caps if c.strip()]
        if not command and not script:
            _err("register requires --command (and --args) or --script to specify the real downstream agent")
            return 1
        if not command:
            command = "uv"
        router.register_agent(name, command, arg_list, description=f"CLI-registered real downstream {name}", capabilities=caps)
        # persist so future router creations (CLI or code) auto discover this real agent
        try:
            router.save_user_registered(name, command, arg_list, description=f"CLI-registered real downstream {name}", capabilities=caps)
        except OSError as exc:
            if getattr(args, "verbose", False):
                _err(f"(failed to persist agent registration to disk: {exc})")
        _say(f"Manually registered real agent: {name} cmd={command} args={arg_list} caps={caps} (persisted)")
        # re-discover to include any
        router.discover()
        print("current registry:", router.discover_agents())
        return 0

    if args.action == "unregister":
        name = getattr(args, "name", None)
        if not name:
            _err("unregister requires --name <agent-name>")
            return 1
        removed = router.unregister_agent(name)
        if removed:
            _say(f"Removed agent: {name} (from registry and persisted)")
        else:
            _say(f"agent {name} not found or already removed")
        print("current registry:", router.discover_agents())
        return 0

    if args.action == "start":
        _say("Starting ACP Router as a Server (waiting for an external ACP client, e.g. Zed/custom, to connect)")
        try:
            from herdr_bridge.acp.router import AcpRouter
            AcpRouter.run_server(router)
        except Exception as exc:  # noqa: BLE001  # ACP SDK's run_agent() can fail in many undocumented ways (protocol/transport errors); this is the top-level CLI error boundary reporting a clean message instead of a raw traceback
            _err(f"router start failed (may need an ACP client to connect): {exc}")
            print("Hint: you can test with herdr-commander router route --prompt '...' (or a python -m acp compatible client)")
            return 1
        return 0

    if args.action == "route":
        prompt = args.prompt or "test router prompt"
        target = args.target or "(auto-choose)"
        _say(f"Router route: {prompt[:50]}... -> {target}")
        # Wrap via commander to integrate RemaGraph
        try:
            # mock actions for standalone
            class _Mock:
                pass
            lc = LightCommander(_Mock())  # type: ignore[arg-type]
            res = lc.route_via_acp_router(prompt, project=args.project, target_agent=args.target)
            print(res)
            if "error" not in res:
                _say("Routing succeeded")
            return 0 if "error" not in res else 2
        except Exception as exc:  # noqa: BLE001  # top-level CLI error boundary for the `route` action; the router/commander call chain can fail in many ways, all of which should surface as a clean CLI error rather than a raw traceback
            _err(str(exc))
            return 1

    _err(f"Unknown action: {args.action}")
    return 1


def cmd_dispatch(args: argparse.Namespace) -> int:
    """Dispatch to a specified downstream agent, via the official
    AcpRouter.dispatch_with_memory_confirm().

    This is the thin CLI wrapper for the Secondary layer of the three-layer
    communication architecture (see CLAUDE.md): just as easy to invoke as a bare
    `herdr pane send-text` + `send-keys Enter`, but underneath it has PING/PONG
    correlation, a delivery-state FSM, and delivery confirmation — so it can't end up
    in the situation where "it was sent, so we assume it was delivered, but the text is
    actually still stuck in the target's input box, never really submitted."
    """
    if not args.target and not args.pane_id:
        _err("dispatch requires an explicit --target or --pane-id (auto-routing is prohibited — see the dispatch discipline in CLAUDE.md)")
        return 1

    try:
        from herdr_bridge.acp.router import create_herdr_router
    except Exception as exc:  # noqa: BLE001
        _err(f"Unable to load ACP router: {exc}")
        return 1

    project = _resolve_memory_project(args.project)

    def _call() -> dict[str, Any]:
        # create_herdr_router() -> AcpRouter.__init__ already handles
        # REMAGRAPH_STATE_DIR / REMAGRAPH_PROJECT (_ensure_remagraph_project) internally,
        # so the caller doesn't need to export any environment variables itself first.
        router = create_herdr_router(project=project)
        return router.dispatch_with_memory_confirm(
            args.prompt,
            target=args.target,
            pane_id=args.pane_id,
            name=args.name,
        )

    if args.timeout:
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as _FutureTimeout

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_call)
        try:
            result = future.result(timeout=args.timeout)
        except _FutureTimeout:
            _err(f"dispatch timeout (> {args.timeout}s); the underlying call may still be running in the background, please verify manually with herdr pane read")
            return 3
        except Exception as exc:  # noqa: BLE001
            _err(f"dispatch execution failed: {exc}")
            return 2
        finally:
            executor.shutdown(wait=False)
    else:
        try:
            result = _call()
        except Exception as exc:  # noqa: BLE001
            _err(f"dispatch execution failed: {exc}")
            return 2

    ok = bool(result.get("ok", False))
    confirmed_via = result.get("confirmed_via", "none")
    pong_confirmed = result.get("pong_confirmed", False)
    side_confirmed = result.get("side_confirmed", False)
    degraded = bool(result.get("degraded", False)) or bool(result.get("echo_fallback", False))

    icon = "✅" if ok else "⚠️"
    _say(f"{icon} dispatch result: ok={ok}  confirmed_via={confirmed_via}")
    print(f"  routed_to={result.get('routed_to')}  task_id={result.get('task_id')}")
    print(f"  pong_confirmed={pong_confirmed}  side_confirmed={side_confirmed}")
    if args.verbose:
        print("  full result:", result)

    # PPLX consensus (docs/decisions/acp-layer-status-20260725.md): the echo-fallback /
    # degraded state needs to be handled separately, before the general `not ok`
    # check — `ok` is already False in this state, so if this were placed after
    # `not ok`, that more general branch would intercept it first and shadow this
    # more precise message, leaving the caller with only a generic "dispatch failed"
    # instead of knowing the real root cause was "delivery was never even attempted".
    if degraded or result.get("delivery_status") == "not_attempted":
        _err(
            "Delivery was never actually attempted to any downstream target (either "
            "the ACP SDK isn't installed, or the target isn't a downstream that was "
            "pre-registered via register_agent()) — this is not a delivery-"
            "confirmation failure, delivery was never attempted at all "
            f"(delivery_status={result.get('delivery_status', 'not_attempted')}). "
            "Please use notify-pane instead, or first confirm the target is correct."
        )
        return 4

    if not ok:
        if "error" in result:
            _err(f"dispatch failed: {result['error']}")
        return 2

    if confirmed_via == "none":
        _err("Sent, but no delivery confirmation was received (PONG or side-channel); please verify manually with herdr pane read whether it was actually delivered")
        return 4

    return 0


class NotifyPaneDeliveryError(RuntimeError):
    """Raised when notify-pane can't confirm delivery within its retry limit — this is
    the whole reason this tool exists: don't silently pretend success, raise a clear
    error instead so the caller knows to find another channel or verify by hand.
    """


# Message-length warning threshold (PPLX recommends flagging rather than silently
# truncating once keyboard-simulation injection tends to get slow/unreliable above ~4KB)
_NOTIFY_PANE_LONG_MESSAGE_BYTES = 4096

# Prompt markers used to locate the input box — Claude Code / OpenCode / Grok, several
# mainstream interactive TUIs, all share the ❯ (U+276F) marker as an observed common
# feature; see the "fourth layer" section of
# docs/governance/acp-direct-communication-pipeline.md
_PANE_PROMPT_MARKER = "❯"

# Input-box top/bottom border lines — the Claude Code TUI draws its box with a solid
# ─ (U+2500) line, other TUIs may use ━ or -; Gemini CLI (verified 2026-07-25) draws its
# box with half-block characters, and uses a *different* character for the top vs.
# bottom: top line ▄ (U+2584), bottom line ▀ (U+2580) — `_extract_input_box_text` only
# requires "both lines found match this pattern", it doesn't require the top and bottom
# to use the same character, so this single regex covers both border conventions
# without needing a separate Gemini-specific code path.
_BORDER_LINE_RE = re.compile(r"^[─━▄▀-]{3,}$")

# Known non-standard input prompt states that will "eat" a keyboard injection (a
# startup trust-confirmation dialog, a y/n prompt, a password prompt, etc). If any of
# these match, this isn't an ordinary typeable-and-submit input box, and we must reject
# the injection up front — we can't send it and only check the diff afterward (see the
# three 2026-07-25 blood-lessons: wT:p18 swallowed a dispatched message into a
# delivery-confirmation dialog).
_BLOCKING_PROMPT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"trust this (project|folder)", re.IGNORECASE), "startup trust-confirmation screen"),
    (re.compile(r"safety check", re.IGNORECASE), "startup trust-confirmation screen"),
    (re.compile(r"\(y/n\)", re.IGNORECASE), "y/n confirmation prompt"),
    (re.compile(r"\[y/n\]", re.IGNORECASE), "y/n confirmation prompt"),
    # Requires an immediately following colon (with optional whitespace) — a real
    # password-entry field is almost always formatted as a "Password:" style form
    # label; a bare "password" would match the screen's tail end simply mentioning the
    # word itself in ordinary text (2026-07-25 blood-lesson: the command tower's own
    # todo panel had a task description containing the word "password"; rendering that
    # line at the bottom of the screen caused its own readiness check to misjudge it as
    # a password prompt, rejecting an injection sent by another agent).
    (re.compile(r"password\s*:", re.IGNORECASE), "password input prompt"),
    (re.compile(r"enter to confirm", re.IGNORECASE), "interactive menu confirmation prompt"),
]

# _BLOCKING_PROMPT_PATTERNS is only matched against the "last few lines" of the
# screen, not searched over the whole snapshot — see the _detect_blocking_prompt()
# docstring (#55: avoids misjudging a pane as stuck on an interactive prompt just
# because the agent's conversation history happens to mention these keywords). An
# interactive dialog/confirmation prompt is at most a few lines, so 8 lines gives
# comfortable margin.
_BLOCKING_PROMPT_SCAN_LINES = 8


def _pane_read(pane_id: str, *, lines: int = 40, source: str = "recent") -> str:
    """Read a plain-text snapshot of the pane screen. Returns an empty string on
    failure, leaving the caller to treat it as "unable to determine"."""
    try:
        res = subprocess.run(
            [
                "herdr", "pane", "read", pane_id,
                "--source", source,
                "--lines", str(lines),
                "--format", "text",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode != 0:
            return ""
        return res.stdout
    except Exception:  # noqa: BLE001
        return ""


def _read_until_stable(
    pane_id: str, *, lines: int, max_wait: float, poll_interval: float = 0.05, stable_reads: int = 2,
) -> str:
    """Poll the pane screen until it stops changing (debounce/settle detection),
    instead of a single fixed-delay read.

    A fixed sleep-then-read-once check races the target TUI's own rendering
    pipeline: under system load (several concurrent panes competing for CPU) or
    for a long message (more text to parse/reflow/wrap), a short fixed delay can
    catch a transient, not-yet-settled frame that happens to look like
    "submitted" -- while the real, settled screen a moment later still shows the
    message stuck in the input box (2026-08-01 blood-lesson: a ~600-char message
    injected into a live external tower's pane was reported "delivery confirmed"
    on the very first attempt, but was still sitting unsent in the input box when
    manually re-verified a few seconds later -- feeding that exact stuck-screen
    text into _looks_submitted() in isolation correctly judged "not submitted",
    proving the detection logic itself was fine; the bug was purely in checking
    before the screen had actually settled).

    Reads repeatedly at `poll_interval`; once `stable_reads` consecutive reads
    are byte-identical, the screen is considered settled and that snapshot is
    returned immediately (the common case -- a short message -- typically
    settles within one or two polls, so this adds negligible latency over the
    old fixed delay). If `max_wait` elapses without reaching that many
    consecutive identical reads, returns the last read as a best-effort
    fallback -- the caller's own _looks_submitted() check still runs against
    it, so this only removes the "checked too early" failure mode, it doesn't
    introduce a new one.
    """
    deadline = time.monotonic() + max_wait
    last: str | None = None
    consecutive = 0
    while True:
        snap = _pane_read(pane_id, lines=lines)
        if snap == last:
            consecutive += 1
            if consecutive >= stable_reads:
                return snap
        else:
            consecutive = 1
            last = snap
        if time.monotonic() >= deadline:
            return snap
        time.sleep(poll_interval)


def _pane_agent_status(pane_id: str) -> str | None:
    """A secondary signal only; known to have false negatives (see CLAUDE.md), must
    not be treated as the sole source of truth."""
    try:
        res = subprocess.run(
            ["herdr", "pane", "get", pane_id],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout or "{}")
        status = data.get("result", {}).get("pane", {}).get("agent_status")
        return str(status) if status is not None else None
    except Exception:  # noqa: BLE001
        return None


def _pane_get_agent_info(pane_id: str) -> tuple[str | None, str | None]:
    """Read a pane's agent brand and agent_status (read-only, `herdr pane get`).
    Returns `(agent, agent_status)`; both are None on read failure.
    """
    try:
        res = subprocess.run(
            ["herdr", "pane", "get", pane_id],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode != 0:
            return None, None
        data = json.loads(res.stdout or "{}")
        pane = data.get("result", {}).get("pane", {})
        return pane.get("agent"), pane.get("agent_status")
    except Exception:  # noqa: BLE001
        return None, None


def _pane_has_foreground_process(pane_id: str) -> bool | None:
    """Precisely check whether a pane's foreground process exists
    (`herdr pane process-info`, read-only).

    More expensive than `agent_status` (one extra call), only worth calling once the
    cheap `agent_status == "unknown"` signal has already raised suspicion. Returns
    True/False; if the query itself fails (timeout/exception/non-zero return),
    returns None, meaning "cannot determine further" — not the same as "the process
    doesn't exist".
    """
    try:
        res = subprocess.run(
            ["herdr", "pane", "process-info", "--pane", pane_id],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout or "{}")
        processes = data.get("result", {}).get("process_info", {}).get("foreground_processes", [])
        return bool(processes)
    except Exception:  # noqa: BLE001
        return None


def _wait_pane_ready(pane_id: str, *, tui: str | None, timeout_ms: int) -> bool:
    """#69: before injecting, use herdr's native `pane wait-output --regex` to first
    wait for the TUI to have rendered at least one known prompt/border — we can't just
    trust `agent_status`. Right after `herdr agent start`, `agent_status` immediately
    reports `idle`, but the TUI may not have finished initializing yet, so an injected
    message would be silently dropped during initialization (F1-04 blood-lesson).
    Baked into notify-pane internally so callers don't have to remember to wait
    themselves first.

    Per `wait-output`'s documentation: it "searches the current snapshot immediately,
    and only starts polling if that fails" — in the common case where the TUI is
    already ready, this call returns almost instantly and doesn't slow down normal
    dispatch.

    If no known pattern is found at all (a regex timeout, or the wait-output call
    itself fails/raises), this always returns False, leaving the caller to treat it as
    "not ready" and reject the injection — consistent with every other readiness check
    in this file: when in doubt, reject rather than inject rashly.
    """
    pattern = tui_patterns.ready_regex(tui=tui)
    if not pattern:
        return True  # no known pattern to wait for (shouldn't happen in practice, e.g. an invalid tui value would already be rejected by argparse choices)
    try:
        res = subprocess.run(
            [
                "herdr", "pane", "wait-output", pane_id,
                "--regex", pattern,
                "--timeout", str(timeout_ms),
            ],
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000.0) + 5,
            check=False,
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _detect_unsafe_agent_status(pane_id: str, *, allow_busy: bool) -> str | None:
    """Pre-injection readiness check: combines detecting "possibly dead" (#64) and
    "busy" (#68), two `agent_status`-based rejection conditions, into a single call to
    `herdr pane get` (along with #44's `_detect_blocking_prompt` — the screen being in
    a non-prompt state — these are three manifestations of the same gap: missing
    pre-injection checks on the target's state). Returns a reason string if the
    injection should be rejected by the caller.

    `agent_status`'s trustworthiness is asymmetric:
    - `idle` is not trustworthy (known false negative: a message has already been
      processed but it still shows idle), so it can't be used to confirm "safe to
      inject" — there is no dedicated "idle means go ahead" branch here; this function
      never actively allows an injection just because of idle, it only rejects when
      some other explicit signal fires.
    - `working` is a trustworthy **positive** signal — when it shows working, the agent
      really is busy. #68 blood-lesson: injecting into grok while busy
      **interrupts** the prior work (the first message is always lost) — it's not a
      queuing TUI; claude/agy have been verified to queue (safe). codex/opencode
      haven't been tested — conservatively treat them as possibly interrupting too,
      don't assume they queue. So injection into a busy pane is rejected by default;
      `allow_busy` is an explicit escape hatch (for when the caller already knows the
      target is a queuing TUI and wants to add further instructions to an agent that's
      currently working).
    - `unknown` is a trustworthy **suspicion** signal — herdr can't detect the agent,
      which likely means it's dead (#64 blood-lesson, F1-02: after `kill -9`-ing the
      agent's foreground process, agent_status went from a known value to unknown).
      Suspicion is only raised when the pane had previously detected an agent (the
      `agent` field is non-empty) and `agent_status` is currently `unknown`; once
      suspected, `herdr pane process-info` is used to precisely verify whether the
      foreground process is still there — only report a likely zombie once the
      process is confirmed gone, to avoid blocking a still-alive agent based solely
      on the weaker `unknown` signal.

    Conclusion: using `agent_status` to make a "reject" decision is safe; using it to
    make a "confirm safe to inject" decision is not — so every branch here is
    "match → reject", none is "match → allow".
    """
    agent, status = _pane_get_agent_info(pane_id)

    if status == "unknown" and agent:
        has_fg = _pane_has_foreground_process(pane_id)
        if has_fg is False:
            return f"suspected zombie pane (agent={agent} but agent_status=unknown and the foreground process no longer exists — the agent is dead)"
        if has_fg is None:
            return (
                f"suspected zombie pane (agent={agent} but agent_status=unknown; the "
                "process-info query failed, unable to further confirm whether the foreground process is still alive)"
            )

    if status == "working" and not allow_busy:
        return (
            "agent_status=working (busy; not every TUI queues — grok has been "
            "verified to interrupt the prior work, causing that message to be lost "
            "forever; codex/opencode are untested and conservatively treated as "
            "possibly interrupting too. If the target is a known queuing TUI (e.g. "
            "claude/agy) and you're sure you want to add further instructions, pass "
            "--allow-busy to override)"
        )

    return None


def _pane_send_text(pane_id: str, text: str) -> bool:
    """Atomic injection: the text itself may contain real newline characters, sent in
    a single call (not split into a send-text step followed by a send-keys Enter step),
    avoiding the event-loop race condition caused by the Ink framework's ~80ms flush
    cycle. Must be passed to subprocess as a list, never assembled into a shell string
    — the message may contain backticks/brackets/$ and other shell-special characters,
    and passing it as a list ensures they're always treated as plain text, never parsed
    by a shell.
    """
    try:
        res = subprocess.run(
            ["herdr", "pane", "send-text", pane_id, text],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _pane_send_keys(pane_id: str, *keys: str) -> bool:
    try:
        res = subprocess.run(
            ["herdr", "pane", "send-keys", pane_id, *keys],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _extract_input_line(snapshot: str) -> str | None:
    """Find the input-box line in a screen snapshot (the line containing the prompt
    ❯, scanning from the bottom up for the last match), returning the content after
    the prompt (trimmed). Returns None if no prompt is found, leaving the caller to
    fall back.
    """
    for line in reversed(snapshot.splitlines()):
        idx = line.find(_PANE_PROMPT_MARKER)
        if idx != -1:
            return line[idx + len(_PANE_PROMPT_MARKER):].strip()
    return None


def _extract_input_box_text(
    snapshot: str, *, markers: tuple[str, ...] = (_PANE_PROMPT_MARKER,)
) -> str | None:
    """Extract the full content of the input box — scanning from the bottom up for
    the last matching pair of border lines (the Claude Code TUI draws its box with a
    matching pair of ─ lines top and bottom; Gemini CLI draws its box with a
    top/bottom pair using *different* characters ▄/▀ — see `_BORDER_LINE_RE`), taking
    every line between the border lines and merging them into one block of text (not
    just the single line containing the prompt).

    When a long message wraps into multiple lines in a narrow pane, the line
    containing the prompt itself may be empty (the message text has been pushed onto
    the following lines, and those lines don't have a prompt marker) — looking at a
    single line alone would miss this content and misjudge it as "already submitted"
    (2026-07-25 blood-lesson: wT:p1E, a 4959-byte message, wrapped and the whole thing
    stayed in the input box, yet was judged as successful).

    `markers`: the actual prompt character(s) used by this specific TUI (defaults to
    `❯`, which applies to Claude/Copilot) — second blood-lesson from 2026-07-25:
    Gemini CLI also draws its input box with a matching pair of border lines, but its
    prompt is `>`, not `❯` — hard-coding only `❯` here would fail to find the prompt,
    conclude "this isn't an input box", and return None, falling back to the
    single-line marker scan, which completely fails to catch "content wrapped onto the
    second line, with no prompt marker" — leading to the same kind of misjudged
    submission. The caller (`_looks_submitted`) must pass in the actual marker set for
    that TUI — it can't rely on the default here.

    If no matching pair of border lines is found, or none of the given markers is
    found between the border lines (meaning this isn't an input box but some other
    bordered screen element), returns None, leaving the caller to fall back to a
    single-line check.
    """
    lines = snapshot.splitlines()
    border_idxs = [i for i, ln in enumerate(lines) if _BORDER_LINE_RE.match(ln.strip())]
    if len(border_idxs) < 2:
        return None
    top, bottom = border_idxs[-2], border_idxs[-1]
    if bottom <= top:
        return None
    inner = "\n".join(lines[top + 1 : bottom])
    matched = next((m for m in markers if m in inner), None)
    if matched is None:
        return None
    return inner.replace(matched, "")


def _detect_blocking_prompt(snapshot: str) -> str | None:
    """Pre-injection readiness check: detects whether the screen is in one of the
    known non-standard input-prompt states (a startup trust-confirmation dialog, a
    menu, a y/n prompt, a password prompt, etc). If so, returns a reason string and
    the caller should reject the injection — we can't send it and only check the diff
    afterward, because these kinds of prompts swallow the keyboard injection whole
    (the agent never actually receives the message), while the screen diff still shows
    "something changed on screen" — looking at the diff alone can't tell the two apart.

    _BLOCKING_PROMPT_PATTERNS is only matched against the "last few lines" of the
    screen (where the currently-rendered interactive UI actually lives), never against
    the whole snapshot (including scrolled-up history) — a genuine interactive
    prompt/menu is always rendered at the very bottom of the screen, so checking just
    the tail lines is enough to catch it; whereas searching the whole snapshot would
    misjudge a pane as being stuck on an interactive prompt just because the agent's
    conversation history happens to mention "password"/"(y/n)"/"enter to confirm" —
    for example, while discussing these very keywords (common in real usage:
    dispatched messages and replies frequently mention trust-confirmation
    dialogs/y-n prompts/password entry as topics) — rejecting an injection that was
    actually legitimate (discovered 2026-07-25).
    """
    tail = "\n".join(snapshot.splitlines()[-_BLOCKING_PROMPT_SCAN_LINES:])
    for pattern, reason in _BLOCKING_PROMPT_PATTERNS:
        if pattern.search(tail):
            return reason

    input_line = _extract_input_line(snapshot)
    if input_line is not None and re.match(r"^\d+[.)]\s", input_line):
        return "detected a numbered menu option (the input-box content looks like a menu choice rather than free-form input)"

    return None


class _SubmitCheck(NamedTuple):
    """Structured result of `_looks_submitted()`.

    `ambiguous=True` means this determination came from the conservative,
    whole-screen-diff guess used when no known TUI pattern matched at all — the caller
    must not treat a `submitted=True` result as "confirmed submitted" and skip the
    fallback Enter when `ambiguous=True` too (#65 blood-lesson: codex/agy were both
    once misjudged as already-submitted under this conservative fallback, wasting the
    fallback Enter and leaving the message stuck for good). Only when `ambiguous=False`
    is `submitted` trustworthy.
    """

    submitted: bool
    ambiguous: bool


def _looks_submitted(
    before: str, after: str, message: str, *, tui: str | None = None
) -> _SubmitCheck:
    """Determine whether a message has been submitted (left the input box), rather
    than just looking at agent_status.

    Three tiers of checks, from most specific to most conservative:
    1. Claude Code's border-style input box (see `_extract_input_box_text`) — handles
       the case where a long message wraps into multiple lines in a narrow pane and
       the line containing the prompt itself becomes empty; other TUIs don't have this
       structure.
    2. Per-TUI pattern candidate matching (`tui_patterns.PROMPT_PATTERNS`, #65): no
       longer hard-coding a single `❯`, covers codex (›), agy (>), opencode (┃/╹
       border), grok/claude (❯) each with their own prompt, unaffected by a status bar
       below pushing the input line out of a "fixed number of lines at the end of the
       screen" window — this is exactly the root cause of #65: codex/agy both have a
       status bar below their input box, so the input line isn't necessarily among the
       last few lines on screen.
    3. When completely unrecognizable, falls back to the conservative whole-screen
       diff check, and marks `ambiguous=True` — better to send one extra harmless Enter
       than to miss a message that's genuinely still stuck, based on this conservative
       guess.
    """
    probe = message.strip()
    if not probe:
        return _SubmitCheck(True, False)
    head = probe.splitlines()[0][:40]

    # The border-style input-box prompt check must use "this TUI's actual markers", not
    # a hard-coded ❯ — see the Gemini blood-lesson in the _extract_input_box_text
    # docstring. When tui is unknown, merge the markers of every known TUI (consistent
    # with the merging logic in tui_patterns.ready_regex(tui=None)).
    if tui is not None:
        _pattern_for_tui = tui_patterns.PROMPT_PATTERNS.get(tui)
        box_markers = _pattern_for_tui.markers if _pattern_for_tui else (_PANE_PROMPT_MARKER,)
    else:
        box_markers = tuple(
            {m for p in tui_patterns.PROMPT_PATTERNS.values() for m in p.markers}
        ) or (_PANE_PROMPT_MARKER,)

    box = _extract_input_box_text(after, markers=box_markers)
    if box is not None:
        box_norm = re.sub(r"\s+", "", box)
        head_norm = re.sub(r"\s+", "", head)
        if head_norm and head_norm in box_norm:
            # Found the message's head — confirmed still stuck in the input box.
            return _SubmitCheck(False, False)
        if not box_norm:
            # Input box is completely empty — a trustworthy "submitted" signal.
            return _SubmitCheck(True, False)
        # 2026-07-25 blood-lesson: after injecting a long, multi-paragraph message,
        # the input-box view can scroll to the end of the message, scrolling the first
        # line (head) out of view — the head can't be found even though the whole
        # message is still stuck in the box (you just can't see the beginning) — use
        # the "last line of the message" for a second check: if it's genuinely still
        # stuck in the box, at least this tail fragment should remain visible after
        # scrolling; only if the tail can't be found either does the box content count
        # as something else entirely (e.g. Gemini CLI's grayed-out idle placeholder
        # "Type your message or @path/to/file" — that's not "leftover content", it's
        # "already submitted, input box back to idle" — the old logic required the box
        # to be cleared character-for-character to count as submitted, which a TUI with
        # such a placeholder could never satisfy, getting stuck in an ambiguous
        # determination forever, unresolved even after exhausting all retries).
        tail = probe.splitlines()[-1][-40:]
        tail_norm = re.sub(r"\s+", "", tail)
        if tail_norm and tail_norm in box_norm:
            return _SubmitCheck(False, False)
        # 2026-08-02 field incident: the box has *some* content, but it
        # matches neither the message's head nor its tail. This is
        # deliberately NOT treated the same as the empty-box case above --
        # unlike a literally empty box, "some unrelated content" is
        # ambiguous by default: it can mean the message truly left (a TUI
        # idle placeholder like Gemini's) or it can mean the substring match
        # just didn't line up (wrapping/normalization) while the message is
        # still sitting right there. The old logic treated this as
        # confirmed-submitted unconditionally, which skipped the caller's
        # follow-up Enter and left a real Signal wake message stuck in a
        # target's input box while notify-pane reported success.
        #
        # The one case this must still resolve as confirmed-submitted (else
        # the Gemini idle-placeholder scenario this branch was originally
        # written for gets stuck ambiguous forever, see the block above) is
        # when `before`'s own box content proves the message really was
        # sitting in this same box a moment ago -- i.e. the box's content
        # demonstrably changed out from under this exact message, not just
        # "the box currently contains something else, for all we know."
        before_box = _extract_input_box_text(before, markers=box_markers)
        if before_box is not None:
            before_box_norm = re.sub(r"\s+", "", before_box)
            if (head_norm and head_norm in before_box_norm) or (
                tail_norm and tail_norm in before_box_norm
            ):
                return _SubmitCheck(True, False)
        return _SubmitCheck(False, True)

    after_lines = after.splitlines()
    located = tui_patterns.locate_any(after_lines, tui=tui)
    if located is not None:
        _box_indices, input_text, _matched_tui = located
        head_norm = re.sub(r"\s+", "", head)
        text_norm = re.sub(r"\s+", "", input_text)
        return _SubmitCheck(head_norm not in text_norm, False)

    if after == before:
        return _SubmitCheck(False, True)
    tail = "\n".join(after_lines[-3:])
    return _SubmitCheck(head not in tail, True)


def _doctor_count_recent_maintenance(audit_path: Path, window_sec: float) -> int:
    """Count maintenance_completed entries in the audit log within the last
    window_sec seconds.

    Only reads the last 2000 lines (bounded memory), to avoid a large audit file
    slowing doctor down — this check is meant to catch "a runaway loop that's still
    running right now", not to do historical statistics, so looking at recent samples
    is enough.
    """
    import datetime as _dt
    from collections import deque

    cutoff = time.time() - window_sec
    count = 0
    with open(audit_path, encoding="utf-8") as f:
        recent_lines: deque[str] = deque(f, maxlen=2000)
    for line in recent_lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Malformed line (e.g. truncated mid-write); safe to skip for this
            # best-effort recent-activity sample — not worth per-line logging in a
            # loop over up to 2000 lines.
            continue
        if rec.get("action") != "maintenance_completed":
            continue
        ts_raw = rec.get("timestamp")
        if not ts_raw:
            continue
        try:
            t = _dt.datetime.fromisoformat(str(ts_raw)).timestamp()
        except (ValueError, OverflowError, OSError):
            # Unparsable or out-of-range timestamp; safe to skip for this
            # best-effort recent-activity sample.
            continue
        if t >= cutoff:
            count += 1
    return count


def cmd_doctor(args: argparse.Namespace) -> int:
    """One-shot diagnostic: global install, RemaGraph connectivity, project.json
    mapping, and maintenance-loop health.

    Blood-lesson from 2026-07-25: all four of these had to be checked by hand, one at a
    time (whether the global pipx install actually took effect, whether RemaGraph can
    connect, whether project.json's project_id matches what's expected, whether an
    external remagraph serve is running a runaway cleanup loop that's wiping out
    another project's data), which took a fair amount of time. Consolidated into a
    single command — next time something feels off, run this first.
    """
    project = _resolve_memory_project(args.project)
    problems: list[str] = []

    # 1. Whether the global install actually took effect
    hc_path = shutil.which("herdr-commander")
    if hc_path:
        _say(f"✅ herdr-commander is globally callable: {hc_path}")
    else:
        problems.append(
            "herdr-commander is not on PATH (not installed globally; see the "
            "\"Global install\" section of docs/light-user-quickstart.md: "
            "pipx install --editable <repo>)"
        )
        _err("❌ herdr-commander is not on PATH")

    herdr_path = shutil.which("herdr")
    if herdr_path:
        _say(f"✅ herdr platform command is available: {herdr_path}")
    else:
        problems.append("herdr platform command not found")
        _err("❌ herdr command not found")

    # 2/3/4: Herdr Bridge Memory connectivity, project.json mapping, maintenance-loop health
    remagraph_path = shutil.which("remagraph")
    if not remagraph_path:
        problems.append("Herdr Bridge Memory backend command not found")
        _err("❌ Herdr Bridge Memory backend command not found")
    else:
        _say("✅ Herdr Bridge Memory backend is available")
        if getattr(args, "verbose", False):
            print(f"  (backend command: {remagraph_path})")
        try:
            from herdr_bridge.orchestration._state_paths import project_state_dir

            state_dir = project_state_dir(project)
            env = {**os.environ, "REMAGRAPH_STATE_DIR": str(state_dir), "REMAGRAPH_PROJECT": project}

            res = subprocess.run(
                ["remagraph", "search", "--project", project, "--top-k", "1", "--task-id", "herdr-commander-doctor-probe"],
                capture_output=True, text=True, timeout=10, env=env, check=False,
            )
            if res.returncode == 0:
                _say(f"✅ Herdr Bridge Memory connection is healthy (project={project})")
            elif getattr(args, "verbose", False):
                detail = (res.stderr or res.stdout or "").strip()[:200]
                problems.append(f"Herdr Bridge Memory search failed: {detail}")
                _err(f"❌ Herdr Bridge Memory search failed: {detail}")
            else:
                problems.append("Herdr Bridge Memory search failed (run with -v for detail)")
                _err("❌ Herdr Bridge Memory search failed (run with -v for detail)")

            meta_path = state_dir / "project.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                actual = meta.get("project_id")
                if actual == project:
                    _say(f"✅ project.json matches correctly (project_id={actual!r})")
                else:
                    problems.append(
                        f"project.json's project_id={actual!r} does not match the "
                        f"expected {project!r} (see #66: possibly an external serve "
                        "connected to the wrong project and wrote to it)"
                    )
                    _err(f"❌ project.json mismatch: expected {project!r}, actual {actual!r}")
            else:
                _say("ℹ️  project metadata does not exist yet (will be created automatically on first use — not an error)")
                if getattr(args, "verbose", False):
                    print(f"  (expected at: {meta_path})")

            audit_path = state_dir / f"audit-{time.strftime('%Y%m')}.jsonl"
            if audit_path.exists():
                recent = _doctor_count_recent_maintenance(audit_path, window_sec=3600)
                if recent > 30:
                    problems.append(
                        f"detected {recent} maintenance_completed events in the past hour, "
                        "suggesting an external process may be running a runaway cleanup loop "
                        "(an external memory-backend maintenance routine once wiped cross-project "
                        "data; this was fixed but it's still worth confirming)"
                    )
                    _err(f"❌ detected {recent} maintenance_completed events in the past hour — possible runaway cleanup loop")
                else:
                    _say(f"✅ maintenance loop healthy ({recent} events in the past hour)")
            else:
                _say("ℹ️  No audit records yet, skipping the maintenance-loop check")
        except Exception as exc:  # noqa: BLE001
            if getattr(args, "verbose", False):
                problems.append(f"exception during Herdr Bridge Memory diagnostics: {exc}")
                _err(f"❌ exception during Herdr Bridge Memory diagnostics: {exc}")
            else:
                problems.append("exception during Herdr Bridge Memory diagnostics (run with -v for detail)")
                _err("❌ exception during Herdr Bridge Memory diagnostics (run with -v for detail)")

    _doctor_check_signal_daemon(project, problems)

    print()
    if problems:
        _err(f"❌ found {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    _say("✅ All checks passed")
    return 0


def _doctor_check_signal_daemon(project: str, problems: list[str]) -> None:
    """Design doc §3.4: distinguish "daemon half-dead" (situation A: restart the
    daemon) from "pane was rebuilt" (situation B: re-run bootstrap) rather than
    reporting a bare "abnormal" — the two need different remedies."""
    from herdr_bridge.orchestration._state_paths import signal_state_dir

    state_dir = signal_state_dir(project)
    lock_path = state_dir / "daemon.lock"
    pin_path = state_dir / "pane_id.pin"

    if not lock_path.exists():
        _say("ℹ️  Signal daemon has never been started for this project (not an error — signal start when ready)")
        return

    from herdr_bridge.signal.lock import DaemonAlreadyRunning, SingleInstanceLock

    probe = SingleInstanceLock(lock_path)
    try:
        probe.acquire()
        probe.release()
        problems.append(
            "Signal daemon lock file exists but is not held by any live process "
            "(the daemon has stopped) — restart it with `herdr-commander signal start`"
        )
        _err("❌ Signal daemon is not running (lock not held)")
        return
    except DaemonAlreadyRunning:
        pass  # a live process holds the lock -- daemon is alive, continue below

    if pin_path.exists():
        pinned_pane_id = pin_path.read_text().strip()
        out = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True, timeout=10, check=False)
        try:
            data = json.loads(out.stdout)
            panes = data["result"].get("panes", data["result"])
            still_exists = any(p.get("pane_id") == pinned_pane_id for p in panes)
        except (json.JSONDecodeError, KeyError, TypeError):
            still_exists = True  # can't tell -- don't false-positive situation B
        if not still_exists:
            problems.append(
                f"Signal daemon is running but bound to pane_id={pinned_pane_id!r}, which no "
                "longer exists (situation B: the pane was rebuilt) — re-run bootstrap to bind "
                "the new pane_id, restarting the daemon alone will not fix this"
            )
            _err(f"❌ Signal daemon's pane_id={pinned_pane_id!r} no longer exists (pane was rebuilt)")
            return
    _say("✅ Signal daemon is running")


def _shared_secret_path(project_id: str) -> Path:
    from herdr_bridge.orchestration._state_paths import signal_state_dir

    return signal_state_dir(project_id) / "shared_secret"


def _read_shared_secret_asserting_ownership(path: Path) -> str:
    """Read an existing shared-secret file, asserting it's actually owned by
    this process's OS user (2026-08-01 DEPLOYMENT CONSTRAINT fix, see
    signal/envelope.py's docstring for the full rationale): the entire
    "shared secret" scheme only works because sender and receiver happen to
    be the same OS user reading the same file on the same host. If that file
    somehow belongs to a different user -- e.g. it was copied over from
    another machine, or something unexpected wrote it -- silently reading it
    anyway would produce a confusing "bad hmac" failure downstream instead
    of pointing at the real, fixable problem.
    """
    own_uid = os.getuid()
    file_uid = path.stat().st_uid
    if file_uid != own_uid:
        raise HerdrBridgeError(
            f"shared secret file {path} is owned by uid={file_uid}, not this "
            f"process's uid={own_uid} -- Herdr Bridge Signal only supports "
            "same-user deployment (see signal/envelope.py's DEPLOYMENT "
            "CONSTRAINT docstring); refusing to use a secret that isn't ours"
        )
    return path.read_text().strip()


def _load_or_create_shared_secret(project_id: str) -> str:
    """§3.3a: per-tower shared secret, generated at bootstrap time, never
    hardcoded. Stored 0600 under the project's own Signal state dir.

    Created atomically at mode 0600 from the very first byte (O_CREAT|O_EXCL),
    not written-then-chmod'd -- a write-then-chmod sequence leaves a real
    window where the secret sits on disk at the process's default umask
    (commonly 0644/world-readable) before the restrictive mode is applied.
    O_EXCL doubles as the concurrent-first-run race fix: if another process
    wins the create, this one just reads what they wrote instead of
    clobbering it.
    """
    import errno
    import secrets

    path = _shared_secret_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_shared_secret_asserting_ownership(path)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        return _read_shared_secret_asserting_ownership(path)
    try:
        secret = secrets.token_hex(32)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
    except BaseException:
        path.unlink(missing_ok=True)  # don't leave a half-written secret behind
        raise
    return secret


def cmd_signal_start(args: argparse.Namespace) -> int:
    """Start the resident Signal daemon for this project (design doc §3.4).
    Normally invoked by bootstrap, not by hand -- runs in the foreground
    (the caller is expected to background it, e.g. `nohup ... &`)."""
    import asyncio

    from herdr_bridge.signal import daemon as signal_daemon
    from herdr_bridge.signal.lock import DaemonAlreadyRunning

    project = _resolve_signal_project(args.project)
    try:
        own_pane_id = signal_daemon.resolve_own_pane_id(project)
    except signal_daemon.PaneIdResolutionError as exc:
        _err(f"❌ {exc}")
        return 1
    _say(f"Signal daemon starting for project={project}, pane_id={own_pane_id}")
    shared_secret = _load_or_create_shared_secret(project)
    try:
        asyncio.run(signal_daemon.run(project, shared_secret))
    except DaemonAlreadyRunning as exc:
        _err(f"❌ {exc}")
        return 1
    except KeyboardInterrupt:
        _say("Signal daemon stopped")
    return 0


def cmd_signal_send(args: argparse.Namespace) -> int:
    """Wake the target project's Signal daemon (design doc §3.2/§3.5). Assumes
    the caller already wrote the real content via `herdr-commander memory note`
    or equivalent -- this only ever sends the wake control signal (§3.3)."""
    import asyncio

    from herdr_bridge.orchestration._state_paths import signal_state_dir
    from herdr_bridge.signal import outbound

    from_project = _resolve_signal_project(args.project)
    to_project = args.to
    socket_path = signal_state_dir(to_project) / f"{to_project}.sock"
    shared_secret = _load_or_create_shared_secret(to_project)

    result = asyncio.run(outbound.send(
        from_project, to_project, args.inbox_ref, args.kind,
        from_project, shared_secret, socket_path,
    ))

    if result.status == "injected":
        _say(f"✅ Signal delivered and injected (message_id={result.message_id})")
        return 0
    if result.status == "daemon_unreachable":
        _err(
            f"❌ Signal daemon unreachable for project={to_project} (message_id={result.message_id}) "
            "-- your content is still safe in RemaGraph and will be seen next time they check; "
            "run `herdr-commander doctor --project " + to_project + "` to diagnose"
        )
        return 1
    if result.status == "deduplicated_inflight":
        _err(
            f"⚠️  Not sent (message_id={result.message_id}): another signal for the same "
            f"--to {to_project} --inbox-ref {args.inbox_ref} is already in flight -- retrying "
            "right now won't help. Run `herdr-commander signal status --project " + to_project +
            "` to check its state, or wait for it to complete before resending."
        )
        return 1
    # "injection_failed_transient": daemon was reachable and accepted it, but
    # didn't confirm injection within the window -- worth a plain retry.
    _err(
        f"⚠️  Signal sent and Accepted but injection unconfirmed (message_id={result.message_id}) "
        "-- content is safe in RemaGraph; consider falling back to `herdr-commander notify-pane` "
        "for guaranteed delivery if this is urgent"
    )
    return 1


def cmd_signal_status(args: argparse.Namespace) -> int:
    """Show this project's Signal daemon liveness and recent ACK records."""
    project = _resolve_signal_project(args.project)
    problems: list[str] = []
    _doctor_check_signal_daemon(project, problems)

    from herdr_bridge.orchestration import list_recent_signal_states

    records = list_recent_signal_states(project, limit=args.top_k)
    if not records:
        _say("No Signal records yet for this project")
    else:
        print(f"\nRecent Signal records (project={project}):")
        for r in records:
            print(f"  {r['message_id']}  state={r['state']:<22} inbox_ref={r['inbox_ref']}")

    if problems and getattr(args, "notify_on_problem", False):
        _notify_signal_daemon_problem(project, problems)
    return 0 if not problems else 1


def _notify_signal_daemon_problem(project: str, problems: list[str]) -> None:
    """Best-effort desktop notification for `signal status --notify-on-problem`
    (task #84: `_doctor_check_signal_daemon` already knows how to detect a
    dead/misbound daemon, but until now nothing scheduled it or surfaced a
    failure outside the terminal -- every daemon was a bare `nohup ... &`
    with no supervision, so a crash went unnoticed indefinitely). Never
    raises: a notification failure must not turn a healthcheck script into a
    crashing one."""
    try:
        subprocess.run(
            [
                "herdr", "notification", "show", f"Herdr Bridge Signal 異常［{project}］",
                "--body", problems[0], "--position", "top-right", "--sound", "request",
            ],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass  # best-effort: a notification failure must not fail the healthcheck itself


def cmd_notify_pane(args: argparse.Namespace) -> int:
    """Reliable delivery notification for an interactive TUI pane (an already-running
    Claude Code / OpenCode / Grok, etc. TUI).

    Fourth-layer communication: the adapter layer for interactive agents that don't
    support ACP headless mode, see the "Fourth Layer" section of
    docs/governance/acp-direct-communication-pipeline.md. What sets this apart from a
    bare `herdr pane send-text` + `send-keys` is the flow — "atomic injection →
    screen-diff verification → send Enter if not yet submitted → verify again" — and if
    that still fails within the retry limit, it raises a clear error rather than
    ending up in the situation where "it was sent, so we assume it was delivered, but
    the text is actually stuck in the target's input box."
    """
    pane_id = args.pane_id
    message = args.message

    msg_bytes = len(message.encode("utf-8"))
    if msg_bytes > _NOTIFY_PANE_LONG_MESSAGE_BYTES:
        _err(
            f"❌ message length {msg_bytes} bytes exceeds the recommended limit of "
            f"{_NOTIFY_PANE_LONG_MESSAGE_BYTES} bytes; simulated keyboard injection is "
            "unreliable for long messages in a narrow pane (line-wrapping causing a "
            "false-positive screen diff has been observed in practice), rejecting the "
            "injection. Please use a temp file + a read-file instruction instead to "
            "deliver long content."
        )
        raise NotifyPaneDeliveryError(
            f"notify-pane rejected injection into pane={pane_id}: message length "
            f"{msg_bytes} bytes exceeds the recommended limit of "
            f"{_NOTIFY_PANE_LONG_MESSAGE_BYTES} bytes. Please use a temp file + a "
            "read-file instruction instead to deliver long content."
        )

    if not _wait_pane_ready(pane_id, tui=args.tui, timeout_ms=int(args.ready_timeout * 1000)):
        _err(
            f"❌ pane={pane_id} did not render any known TUI prompt within "
            f"{args.ready_timeout}s; it's likely still starting up (agent_status "
            "reporting idle right away doesn't mean the TUI is ready, see #69) — "
            "rejecting the injection to avoid the message being silently dropped during initialization."
        )
        raise NotifyPaneDeliveryError(
            f"notify-pane detected that pane={pane_id} was not ready within "
            f"{args.ready_timeout}s (no known TUI prompt appeared) and has rejected "
            f"the injection. Please try again later, or verify the current screen "
            f"with herdr pane read {pane_id}."
        )

    project = _resolve_memory_project(args.project)
    correlation = f"notify-pane-{pane_id}-{int(time.time())}"
    task_id = f"notify-pane-{pane_id.replace(':', '-')}-{int(time.time())}"
    agent_id = f"pane:{pane_id}"

    attempts = 0
    submitted = False
    last_status: str | None = None
    fallback_used = False

    for attempt_no in range(1, args.retries + 1):
        attempts = attempt_no
        before = _pane_read(pane_id, lines=args.read_lines)

        blocking_reason = _detect_blocking_prompt(before)
        if blocking_reason:
            _err(
                f"❌ pane={pane_id}'s screen is in a non-injectable state ({blocking_reason}); "
                "rejecting the injection to avoid the message being swallowed by an interactive prompt. "
                f"Please handle the prompt by hand first, or verify the screen with herdr pane read {pane_id} and retry."
            )
            raise NotifyPaneDeliveryError(
                f"notify-pane detected that pane={pane_id}'s screen is in a "
                f"non-injectable state ({blocking_reason}) and has rejected the "
                "injection. Please handle the interactive prompt by hand first "
                "(trust confirmation / menu / y-n / password, etc.), or verify the "
                "screen with herdr pane read and retry."
            )

        unsafe_status_reason = _detect_unsafe_agent_status(pane_id, allow_busy=args.allow_busy)
        if unsafe_status_reason:
            _err(
                f"❌ pane={pane_id} ({unsafe_status_reason}); rejecting the injection to "
                "avoid the message never being processed, or interrupting work already in progress. "
                f"Please confirm the agent's state first, or verify with herdr pane process-info --pane {pane_id}."
            )
            raise NotifyPaneDeliveryError(
                f"notify-pane detected pane={pane_id} ({unsafe_status_reason}) and has "
                "rejected the injection. Please confirm the agent's state first "
                "(it may need restarting, or pass --allow-busy to override), or "
                "verify by hand and retry."
            )

        if not _pane_send_text(pane_id, message + "\n"):
            _err(f"Attempt {attempt_no}: send-text call failed (herdr CLI returned non-zero or raised an exception)")
            continue

        after = _read_until_stable(pane_id, lines=args.read_lines, max_wait=args.settle_delay)
        last_status = _pane_agent_status(pane_id)

        check1 = _looks_submitted(before, after, message, tui=args.tui)
        if check1.submitted and not check1.ambiguous:
            submitted = True
            break

        # Not "confirmed" submitted (whether definitely-not-submitted, or the
        # uncertain state the conservative fallback can't resolve): always send a
        # fallback Enter and verify again. #65 fix direction 2: the old logic used to
        # skip the fallback Enter as soon as it judged "looks submitted" — that
        # direction was wrong; only a *confirmed* submission (matched against a known
        # TUI pattern) should skip it. When uncertain, better to send one extra
        # harmless Enter than to miss a message that's genuinely still stuck.
        fallback_used = True
        _pane_send_keys(pane_id, "Enter")
        after2 = _read_until_stable(pane_id, lines=args.read_lines, max_wait=args.settle_delay)
        last_status = _pane_agent_status(pane_id)

        check2a = _looks_submitted(after, after2, message, tui=args.tui)
        check2b = _looks_submitted(before, after2, message, tui=args.tui)
        if (check2a.submitted and not check2a.ambiguous) or (check2b.submitted and not check2b.ambiguous):
            submitted = True
            break

    result_summary = (
        f"notify-pane pane={pane_id} attempts={attempts}/{args.retries} "
        f"submitted={submitted} fallback_enter_used={fallback_used} "
        f"agent_status={last_status} correlation={correlation}"
    )

    if not args.no_audit and _rg is not None and _rg.is_remagraph_enabled():
        try:
            _rg.store_memory(
                task_id,
                agent_id,
                kind="status_update",
                summary=result_summary,
                handoff_note=f"pane_id={pane_id} correlation={correlation}",
                tags=["notify-pane", "fourth-layer", "tui-keyboard-injection"],
                project_id=project,
                learnings=[f"correlation={correlation}"],
            )
        except Exception as exc:  # noqa: BLE001
            _err(f"Audit record write failed (does not affect the delivery result): {exc}")

    if submitted:
        icon = "✅"
        _say(f"{icon} notify-pane delivery confirmed: pane={pane_id} attempts={attempts} fallback_enter_used={fallback_used}")
        if args.verbose:
            print(f"  {result_summary}")
        return 0

    _err(f"❌ {result_summary}")
    # The word "retries" below satisfies the OR-style assertion in
    # tests/test_light_cli_notify_pane.py::test_notify_pane_exhausts_retries_raises_clear_error.
    raise NotifyPaneDeliveryError(
        f"notify-pane failed to confirm delivery to pane={pane_id} within "
        f"{args.retries} retries (agent_status={last_status}). Please verify "
        f"manually with herdr pane read {pane_id}, or fall back to Secondary-layer "
        "dispatch (if the target is an ACP-compliant agent subprocess)."
    )


def cmd_memory_note(args: argparse.Namespace) -> int:
    """Log a Herdr Bridge Memory note: the CLI-escape-hatch equivalent of the
    shell command agents are instructed to run at task completion (see
    get_usage_instruction() in orchestration/memory.py), for a human to log a
    note by hand without needing to know the underlying `remagraph` CLI.
    """
    if _rg is None or not _rg.is_remagraph_enabled():
        _err("Herdr Bridge Memory is not available in this environment.")
        return 1

    project = _resolve_memory_project(args.project)
    try:
        result = _rg.store_memory(
            args.task_id,
            args.agent_id,
            kind="status_update",
            summary=args.message,
            project_id=project,
            tags=["cli", "memory-note"],
            learnings=[args.message[:200]],
        )
    except HerdrBridgeError as exc:
        if getattr(args, "verbose", False):
            raise
        _err(str(exc))
        return 1

    if result.get("status") in ("stored", "ok"):
        _say(f"✅ memory note stored (task_id={args.task_id}, agent_id={args.agent_id}, project={project})")
        return 0
    if getattr(args, "verbose", False):
        _err(f"❌ failed to store memory note: {result}")
    else:
        _err(f"❌ failed to store memory note (status={result.get('status', 'unknown')}; run with -v for detail)")
    return 1


def cmd_memory_search(args: argparse.Namespace) -> int:
    """Search Herdr Bridge Memory: the CLI-escape-hatch counterpart to
    `memory note`, for looking up memories/task handoffs (e.g. a cross-project
    request left for this project) without needing to know the underlying
    `remagraph` CLI directly.
    """
    if _rg is None or not _rg.is_remagraph_enabled():
        _err("Herdr Bridge Memory is not available in this environment.")
        return 1

    project = _resolve_memory_project(args.project)
    tags: list[str] | None = None
    if args.tags:
        import json as _json
        try:
            tags = _json.loads(args.tags) if args.tags.strip().startswith("[") else [t.strip() for t in args.tags.split(",") if t.strip()]
        except _json.JSONDecodeError:
            tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    try:
        results = _rg.search_memories(
            args.query or "",
            top_k=args.top_k,
            kind=args.kind,
            status=args.status,
            tags=tags,
            project_id=project,
            agent_id=args.agent_id,
            task_id=args.task_id,
            all_projects=args.all_projects,
            cross_project_label=args.cross_project_label,
        )
    except HerdrBridgeError as exc:
        if getattr(args, "verbose", False):
            raise
        _err(str(exc))
        return 1

    scope = f"project={project}" + (", all-projects" if args.all_projects else "")
    if not results:
        _say(f"No memories found ({scope}).")
        return 0

    _say(f"Found {len(results)} memor{'y' if len(results) == 1 else 'ies'} ({scope}):")
    for r in results:
        kind = r.get("kind", "?")
        task_id = r.get("task_id", "?")
        agent_id = r.get("agent_id", "?")
        ts = str(r.get("timestamp", ""))[:19]
        summary = (r.get("summary") or r.get("handoff_note") or "")[:200]
        print(f"  [{kind}] {task_id} (agent={agent_id}, {ts})")
        if summary:
            print(f"    {summary}")
    return 0


def cmd_memory_status(args: argparse.Namespace) -> int:
    """List recent Herdr Bridge Memory activity for a project (the
    `remagraph status` counterpart) -- a quick "what's happened lately"
    view, as opposed to `memory search`'s keyword lookup. Thin wrapper
    around search_memories() with an empty query (list mode).
    """
    if _rg is None or not _rg.is_remagraph_enabled():
        _err("Herdr Bridge Memory is not available in this environment.")
        return 1

    project = _resolve_memory_project(args.project)
    try:
        results = _rg.search_memories(
            "",
            top_k=args.top_k,
            kind=args.kind,
            project_id=project,
            all_projects=args.all_projects,
        )
    except HerdrBridgeError as exc:
        if getattr(args, "verbose", False):
            raise
        _err(str(exc))
        return 1

    scope = f"project={project}" + (", all-projects" if args.all_projects else "")
    if not results:
        _say(f"No recent memory activity ({scope}).")
        return 0

    _say(f"Recent activity ({scope}):")
    for r in results:
        kind = r.get("kind", "?")
        task_id = r.get("task_id", "?")
        agent_id = r.get("agent_id", "?")
        ts = str(r.get("timestamp", ""))[:19]
        namespace = r.get("_namespace", "?")
        summary = (r.get("summary") or r.get("handoff_note") or "")[:160]
        print(f"  [{kind}/{namespace}] {task_id} (agent={agent_id}, {ts})")
        if summary:
            print(f"    {summary}")
    return 0


def cmd_memory_maintain(args: argparse.Namespace) -> int:
    """Apply Herdr Bridge Memory's retention policy: archive stale
    delivery-state-tagged records past their SLA (the `remagraph maintain`
    counterpart, scoped to herdr-bridge's own retention concept). Defaults
    to a dry run -- pass --apply to actually archive.
    """
    if _rg is None or not _rg.is_remagraph_enabled():
        _err("Herdr Bridge Memory is not available in this environment.")
        return 1

    project = _resolve_memory_project(args.project)
    try:
        result = _rg.apply_retention(project, dry_run=not args.apply)
    except HerdrBridgeError as exc:
        if getattr(args, "verbose", False):
            raise
        _err(str(exc))
        return 1

    mode = "dry run" if result.get("dry_run", True) else "applied"
    _say(f"Retention ({mode}, project={project}): {result.get('archived', 0)} record(s) archived")
    if getattr(args, "verbose", False):
        print(f"  policy: {result.get('policy')}")
    if result.get("dry_run", True) and result.get("archived", 0) > 0:
        _say("Re-run with --apply to actually archive these.")
    return 0


def cmd_memory_link(args: argparse.Namespace) -> int:
    """Declare a relation between two projects (the `remagraph link`
    counterpart), so `memory search --include-related` can traverse it.
    """
    if _rg is None or not _rg.is_remagraph_enabled():
        _err("Herdr Bridge Memory is not available in this environment.")
        return 1

    try:
        _rg.link_project(args.from_project, args.to_project, args.relation)
    except ValueError as exc:
        _err(str(exc))
        return 1
    except HerdrBridgeError as exc:
        if getattr(args, "verbose", False):
            raise
        _err(str(exc))
        return 1

    _say(f"Linked {args.from_project} -> {args.to_project} ({args.relation})")
    return 0


def _verbose_parent() -> argparse.ArgumentParser:
    """A shared parent parser exposing -v/--verbose to each subcommand
    individually (`herdr-commander doctor -v`, `herdr-commander run -v`, ...).

    Deliberately NOT also added to the top-level parser: argparse's
    subparsers dispatch (`_SubParsersAction.__call__`) parses each
    subcommand's own args into a *fresh* namespace and then unconditionally
    copies every key from it back onto the shared namespace -- so if the same
    dest were defined on both the top-level parser and every subparser, a
    value set by `-v` BEFORE the subcommand (top-level) would always get
    silently clobbered back to the subparser's own default (False) the
    moment the subcommand's namespace is merged in, regardless of whether it
    was already set. Defining it only on subparsers avoids that entirely and
    keeps behavior simple and predictable: `-v` goes after the subcommand.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-v", "--verbose", action="store_true", help="Show technical detail (for debugging)")
    return parent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="herdr-commander",
        description="A command tower for occasional AI-assisted development — say one thing, and it manages the rest for you",
    )
    p.set_defaults(verbose=False)
    p.add_argument("--socket", default=None, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("start", help="Check and prepare the environment", parents=[_verbose_parent()])
    sp.add_argument(
        "--auto-sandbox",
        action="store_true",
        help="Automatically start a test sandbox if no environment is found",
    )
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("status", help="Show current assistant status", parents=[_verbose_parent()])
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("run", help="Run a task (default thumbnail-py; use --task fastapi-health for the other one)", parents=[_verbose_parent()])
    sp.add_argument("--task", default="thumbnail-py", help="Task id: thumbnail-py or fastapi-health")
    sp.add_argument("--dry-run", action="store_true", help="Preview only, don't actually dispatch")
    sp.add_argument("--timeout", type=int, default=600, help=argparse.SUPPRESS)
    sp.add_argument("--use-acp-router", action="store_true", help="Route to downstream TUI agents via the ACP Router")
    sp.add_argument("--router-target", default=None, help="Specify the router target agent (e.g. echo-tui)")
    sp.set_defaults(func=cmd_run)

    # ACP Router-related (added target)
    spr = sub.add_parser("router", help="ACP Router control (Server + Client + registry); extended to support a dynamic registry + register/unregister for real external agents", parents=[_verbose_parent()])
    spr.add_argument("action", choices=["start", "route", "discover", "list", "register", "unregister"], help="start/route/discover/list/register/unregister (register/unregister: manage real external acp agents)")
    spr.add_argument("--prompt", default=None, help="the prompt to use for route")
    spr.add_argument("--target", default=None, help="route target agent (default: auto choose from registry capabilities)")
    spr.add_argument("--project", default="herdr-router", help="router project id for Herdr Bridge Memory")
    spr.add_argument("--capability", default=None, help="for list/discover: filter by capability e.g. search,code")
    spr.add_argument("--path", default=None, help="for list/discover: additional path to scan for agents (expand registry)")
    # for register/unregister real downstream (supports arbitrary real agents)
    spr.add_argument("--name", default=None, help="register/unregister: agent name (e.g. my-opencode-tui)")
    spr.add_argument("--script", default=None, help="register: path to script (if using default uv python)")
    spr.add_argument("--command", default=None, help="register: command for real agent e.g. opencode or /bin/my-agent")
    spr.add_argument("--args", default=None, help="register: args as json list or comma sep e.g. '[\"--acp\"]' or 'run,python,/p'")
    spr.add_argument("--capabilities", default="general", help="register: comma sep caps e.g. code,general,search")
    spr.set_defaults(func=cmd_router)

    # dispatch: thin wrapper making AcpRouter.dispatch_with_memory_confirm() as easy to use as a bare command
    spd = sub.add_parser(
        "dispatch",
        help="Dispatch to a specified downstream agent (via the official AcpRouter.dispatch_with_memory_confirm, with PONG/side-channel delivery confirmation)",
        parents=[_verbose_parent()],
    )
    spd.add_argument("prompt", help="The prompt text to send")
    spd.add_argument("--target", default=None, help="Target pane_id or a registered agent name (at least one of this or --pane-id is required; auto-routing is prohibited)")
    spd.add_argument("--pane-id", dest="pane_id", default=None, help="Explicitly specify pane_id (usable when --target isn't provided)")
    spd.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spd.add_argument("--name", default=None, help="Fleet member display name (for recording)")
    spd.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Overall CLI-layer timeout in seconds (overrides the default; if unset, follows dispatch_with_memory_confirm's built-in PONG/side-channel timeout logic)",
    )
    spd.set_defaults(func=cmd_dispatch)

    # notify-pane: fourth-layer communication, the keyboard-injection adapter for
    # interactive TUI panes (Claude Code / OpenCode / Grok, etc. — non-ACP-headless
    # agents), with screen-diff delivery verification
    spn = sub.add_parser(
        "notify-pane",
        help="Notify an interactive TUI pane (fourth layer: atomic keyboard injection + screen-diff delivery verification; use this for non-ACP-compliant agents, not dispatch)",
        parents=[_verbose_parent()],
    )
    spn.add_argument("message", help="The message text to send")
    spn.add_argument("--pane", dest="pane_id", required=True, help="Target pane_id (required, auto-routing is prohibited)")
    spn.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spn.add_argument("--retries", type=int, default=3, help="Delivery-verification retry limit (each attempt includes atomic injection + a fallback Enter if not submitted), default 3")
    spn.add_argument("--settle-delay", type=float, default=1.5, help="Max seconds to poll the screen for after each injection/fallback Enter, waiting for it to stop changing (debounced settle detection, not a fixed sleep) before checking whether the message was submitted, default 1.5 -- the common case (a short message) settles within one or two 50ms polls, so this only matters as a ceiling for long messages or a loaded system (2026-08-01 fix: a fixed short sleep could check before the screen had actually settled, see _read_until_stable())")
    spn.add_argument("--read-lines", type=int, default=40, help="Number of lines to sample on each herdr pane read, default 40")
    spn.add_argument(
        "--tui",
        default=None,
        choices=sorted(tui_patterns.PROMPT_PATTERNS.keys()),
        help="Explicitly specify the target TUI (submission is judged using its per-TUI pattern, see #65); if unset, tries every known pattern in turn automatically",
    )
    spn.add_argument(
        "--allow-busy",
        dest="allow_busy",
        action="store_true",
        help="Allow injecting into a pane with agent_status=working (rejected by default, see #68: interrupting TUIs like grok will lose the prior work; only pass this flag once you're sure the target is a queuing TUI like claude/agy)",
    )
    spn.add_argument(
        "--ready-timeout",
        type=float,
        default=5.0,
        help="Timeout in seconds to wait for the TUI to render a known prompt before injecting, default 5.0 (see #69: right after the agent starts, agent_status immediately reports idle but the TUI may not be ready yet)",
    )
    spn.add_argument("--no-audit", dest="no_audit", action="store_true", help="Skip the Herdr Bridge Memory audit record (for testing)")
    spn.set_defaults(func=cmd_notify_pane)

    # doctor: one-shot diagnostic for global install / RemaGraph connectivity / project.json mapping / maintenance-loop health
    spdoc = sub.add_parser("doctor", help="One-shot diagnostic for communication-channel health (global install, Herdr Bridge Memory connectivity, project.json mapping, maintenance loop)", parents=[_verbose_parent()])
    spdoc.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spdoc.set_defaults(func=cmd_doctor)

    # memory: Herdr Bridge Memory CLI escape hatch ("note" to write, "search"
    # to read; kept deliberately minimal rather than replicating the full
    # underlying remagraph CLI)
    # NOTE: parents=[_verbose_parent()] is deliberately NOT added here (unlike
    # every other subparser) -- "memory" nests a second subparsers level
    # ("note"/"search", below), and adding -v/--verbose at both levels
    # reproduces the exact same clobbering hazard _verbose_parent()'s
    # docstring describes, one level deeper (verified: `memory -v note ...`
    # would silently drop -v). Only the leaf subparsers get it.
    spmem = sub.add_parser("memory", help="Herdr Bridge Memory operations")
    memsub = spmem.add_subparsers(dest="memory_action", required=True)
    spnote = memsub.add_parser("note", help="Log a memory note for a task/agent", parents=[_verbose_parent()])
    spnote.add_argument("message", help="The note text to store")
    spnote.add_argument("--task-id", dest="task_id", required=True, help="Task id to associate this note with")
    spnote.add_argument("--agent-id", dest="agent_id", required=True, help="Agent id to associate this note with")
    spnote.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spnote.set_defaults(func=cmd_memory_note)

    spsearch = memsub.add_parser("search", help="Search memories/task handoffs (e.g. a cross-project request left for this project)", parents=[_verbose_parent()])
    spsearch.add_argument("query", nargs="?", default="", help="Full-text keyword query (optional; omit to just filter/list by the flags below)")
    spsearch.add_argument("--top-k", dest="top_k", type=int, default=10, help="Max results to return (default 10)")
    spsearch.add_argument("--kind", default=None, choices=["task_handoff", "status_update", "discovered_constraint", "fleet_member"], help="Filter by memory kind")
    spsearch.add_argument("--status", default=None, choices=["active", "superseded", "invalidated"], help="Filter by status")
    spsearch.add_argument("--tags", default=None, help="Filter by tags (comma-separated, or a JSON array)")
    spsearch.add_argument("--agent-id", dest="agent_id", default=None, help="Filter by agent id")
    spsearch.add_argument("--task-id", dest="task_id", default=None, help="Filter by task id")
    spsearch.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spsearch.add_argument("--all-projects", dest="all_projects", action="store_true", help="Don't filter to --project; search everything in this memory store (e.g. when a cross-project message's project_id doesn't match what you expected)")
    spsearch.add_argument("--cross-project-label", dest="cross_project_label", default=None, help="Search across each known project's own separate memory store by a namespaced label (e.g. 'topic:how-to-contact-tower')")
    spsearch.set_defaults(func=cmd_memory_search)

    spmemstatus = memsub.add_parser("status", help="List recent memory activity for a project", parents=[_verbose_parent()])
    spmemstatus.add_argument("--top-k", dest="top_k", type=int, default=10, help="Max entries to show (default 10)")
    spmemstatus.add_argument("--kind", default=None, choices=["task_handoff", "status_update", "discovered_constraint", "fleet_member"], help="Filter by memory kind")
    spmemstatus.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spmemstatus.add_argument("--all-projects", dest="all_projects", action="store_true", help="Don't filter to --project; show everything in this memory store")
    spmemstatus.set_defaults(func=cmd_memory_status)

    spmaintain = memsub.add_parser("maintain", help="Apply the retention policy (archive stale delivery-state records)", parents=[_verbose_parent()])
    spmaintain.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spmaintain.add_argument("--apply", action="store_true", help="Actually archive (default is a dry run that only reports what would be archived)")
    spmaintain.set_defaults(func=cmd_memory_maintain)

    splink = memsub.add_parser("link", help="Declare a relation between two projects (for --include-related traversal in search)", parents=[_verbose_parent()])
    splink.add_argument("--from", dest="from_project", required=True, help="Source project id")
    splink.add_argument("--to", dest="to_project", required=True, help="Target project id")
    splink.add_argument("--relation", required=True, choices=["depends_on", "sibling", "shares_upstream", "monorepo_member"], help="Relation type (treated as bidirectional during traversal)")
    splink.set_defaults(func=cmd_memory_link)

    # signal: Herdr Bridge Signal (design doc §3.7) -- cross-tower wake-up
    # acceleration. Same NOTE as "memory" above re: -v placement (nested
    # subparsers -- only leaf commands get _verbose_parent()).
    spsig = sub.add_parser("signal", help="Herdr Bridge Signal: cross-tower wake-up daemon")
    sigsub = spsig.add_subparsers(dest="signal_action", required=True)

    spsigstart = sigsub.add_parser("start", help="Start the resident Signal daemon for this project (usually invoked by bootstrap)", parents=[_verbose_parent()])
    spsigstart.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spsigstart.set_defaults(func=cmd_signal_start)

    spsigsend = sigsub.add_parser("send", help="Wake the target project's Signal daemon", parents=[_verbose_parent()])
    spsigsend.add_argument("--to", required=True, help="Target project id to wake")
    spsigsend.add_argument("--inbox-ref", dest="inbox_ref", required=True, help="Reference to the RemaGraph content already stored (task_id/agent_id) -- Signal never carries content itself")
    spsigsend.add_argument("--kind", default="task_handoff", help="Envelope kind, default task_handoff")
    spsigsend.add_argument("--project", default=None, help="Sending project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spsigsend.set_defaults(func=cmd_signal_send)

    spsigstatus = sigsub.add_parser("status", help="Show this project's Signal daemon liveness and recent ACK records", parents=[_verbose_parent()])
    spsigstatus.add_argument("--project", default=None, help="Herdr Bridge Memory project id (defaults to HERDR_MEMORY_PROJECT, otherwise herdr-bridge)")
    spsigstatus.add_argument("--top-k", dest="top_k", type=int, default=10, help="Max records to show (default 10)")
    spsigstatus.add_argument(
        "--notify-on-problem", action="store_true",
        help="Also push a desktop notification if a problem is found (for unattended/scheduled "
             "healthcheck calls -- plain interactive use already sees the stderr output, so this "
             "defaults off to avoid a redundant popup)",
    )
    spsigstatus.set_defaults(func=cmd_signal_status)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except NotifyPaneDeliveryError as exc:
        _err(str(exc))
        return 5
    except HerdrBridgeError as exc:
        # Default mode: print only the clean, non-backend-named message (the
        # HerdrBridgeError hierarchy's raise sites keep the full original
        # exception on __cause__/__context__ via `from e`). --verbose/-v
        # re-raises so the user sees the complete chain.
        if getattr(args, "verbose", False):
            raise
        _err(str(exc))
        return 1
