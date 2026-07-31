# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""RemaGraph memory integration (embedded governance layer).

This module is the memory-layer adapter for the herdr Bridge command
tower. During development, it directly imports RemaGraph via a uv editable
install (changes take effect immediately). At runtime, regular end users
fall back to the CLI.

All governance-layer dispatching should go through this module rather
than calling the remagraph CLI directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from herdr_bridge.errors import DeliveryStateWriteFailed, HerdrMemoryError
from herdr_bridge.orchestration import delivery_state_store as _fsm_store
from herdr_bridge.orchestration._state_paths import (
    project_state_dir as _project_state_dir,
)
from herdr_bridge.orchestration._state_paths import (
    slugify_project as _slugify_project,
)

logger = logging.getLogger("herdr_bridge.orchestration.memory")

# ---------------------------------------------------------------------------
# Mode control
# HERDR_MEMORY_MODE: auto | on | off
# (HERDR_REMAGRAPH_MODE is a deprecated alias, kept working for backward
# compatibility -- see _DEPRECATED_ENV_ALIASES.)
# ---------------------------------------------------------------------------

# Deprecated env var name -> its replacement. Reading the deprecated name still
# works (with a DeprecationWarning); the new name always takes priority when
# both are set.
_DEPRECATED_ENV_ALIASES = {
    "HERDR_REMAGRAPH_MODE": "HERDR_MEMORY_MODE",
}


def _read_deprecated_or(new_name: str, default: str) -> str:
    val = os.environ.get(new_name)
    if val is not None:
        return val
    for old_name, replacement in _DEPRECATED_ENV_ALIASES.items():
        if replacement == new_name:
            old_val = os.environ.get(old_name)
            if old_val is not None:
                warnings.warn(
                    f"{old_name} is deprecated; use {new_name} instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                return old_val
    return default


_REMAGRAPH_MODE = _read_deprecated_or("HERDR_MEMORY_MODE", "auto").lower()

# ---------------------------------------------------------------------------
# Direct-import detection (embedded development mode)
# ---------------------------------------------------------------------------

# RemaGraph is embedded (herdr-bridge's decision): force direct import, no
# graceful fallback. RemaGraph as the core memory engine must be embedded;
# the CLI fallback is only for non-herdr contexts.
try:
    from remagraph.db import connect as _rg_connect
    from remagraph.models import SearchRequest as _RgSearchRequest
    from remagraph.models import StoreRequest as _RgStoreRequest
    from remagraph.search import search_memories as _rg_search_memories
    from remagraph.store import process_store as _rg_process_store
    _DIRECT_IMPORT = True
except ImportError as _e:
    _DIRECT_IMPORT = False
    raise HerdrMemoryError(
        "Herdr Bridge Memory failed to initialize: its embedded memory backend "
        "could not be imported. Try reinstalling herdr-bridge "
        "(`pip install --force-reinstall herdr-bridge`, or `uv sync` in a source checkout)."
    ) from _e


def _is_remagraph_available() -> bool:
    if _REMAGRAPH_MODE == "off":
        return False
    if _REMAGRAPH_MODE == "on":
        return True
    if _DIRECT_IMPORT:
        return True
    return shutil.which("remagraph") is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_remagraph_enabled() -> bool:
    """Whether memory functionality is enabled."""
    if _REMAGRAPH_MODE == "off":
        return False
    if _REMAGRAPH_MODE == "on":
        return True
    return _is_remagraph_available()


def uses_direct_import() -> bool:
    """Whether direct import (embedded mode) is currently in use."""
    return _DIRECT_IMPORT


def get_remagraph_mode() -> str:
    return _REMAGRAPH_MODE


def _ensure_remagraph_project(project_id: str = "herdr", *, force_reinit: bool = False) -> None:
    """Stabilization helper: make sure the RemaGraph project is initialized, to avoid stale-DB-schema issues.
    Calls remagraph init --project <project>, and forcibly sets REMAGRAPH_STATE_DIR / REMAGRAPH_PROJECT for
    cross-project consistency. Supports force_reinit to reset/clear stale data (used for tests / cross-project
    demonstrations). Always forces the env vars so direct import and the CLI consistently use the correct project DB.
    """
    try:
        expected_dir = _project_state_dir(project_id)
        # Only reuse a leftover REMAGRAPH_STATE_DIR if it maps exactly to
        # this project_id (e.g. a test deliberately re-enters this function
        # with an already-prepared environment). Any leftover value that
        # isn't an exact match — whether it's a completely unrelated
        # directory, or something like remagraph-herdr-bridge-<task-id-suffix>
        # that "happens to contain" the project prefix in its name but is
        # actually a different directory — must be treated as untrusted;
        # fall back to this project_id's own dedicated path instead of
        # silently reusing a value that points to the wrong place (2026-07-25
        # postmortem: it was exactly this old branch's unconditional reuse
        # that scattered memory across 7 different DBs).
        env_state_dir = os.environ.get("REMAGRAPH_STATE_DIR")
        if env_state_dir and Path(env_state_dir).resolve() == expected_dir.resolve():
            state_dir = Path(env_state_dir)
        else:
            state_dir = expected_dir
        db_path = state_dir / "remagraph.db"
        if force_reinit and db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                logger.debug("force_reinit: failed to unlink stale db %s", db_path, exc_info=True)
        # Only init (to ensure schema) when there's no DB yet, or force is set.
        need_init = force_reinit or not db_path.exists()
        if need_init:
            # Explicitly pass the freshly computed state_dir to the child
            # process via env — if the child process doesn't receive
            # REMAGRAPH_STATE_DIR, it will re-derive remagraph-<project_id>
            # on its own using the standard rule (exactly the path we're
            # trying to deviate from), bypassing the self-protection above
            # and landing the init result right back where the external
            # serve process can recognize it.
            subprocess.run(
                ["remagraph", "init", "--project", project_id],
                capture_output=True,
                timeout=15,
                env={**os.environ, "REMAGRAPH_STATE_DIR": str(state_dir)},
                text=True,
                check=False,
            )
        # Force-set env vars for the direct connection and all callers (critical for cross-project consistency).
        os.environ["REMAGRAPH_STATE_DIR"] = str(state_dir)
        os.environ["REMAGRAPH_PROJECT"] = project_id
        # If env.sh exists, parse it too as a supplement (without overriding the forced values above).
        env_sh = state_dir / "env.sh"
        if env_sh.exists():
            try:
                content = env_sh.read_text()
                for line in content.splitlines():
                    if line.startswith("export REMAGRAPH_STATE_DIR=") and "REMAGRAPH_STATE_DIR" not in os.environ:
                        val = line.split("=", 1)[1].strip('"')
                        os.environ["REMAGRAPH_STATE_DIR"] = val
                    elif line.startswith("export REMAGRAPH_PROJECT=") and "REMAGRAPH_PROJECT" not in os.environ:
                        val = line.split("=", 1)[1].strip('"')
                        os.environ["REMAGRAPH_PROJECT"] = val
            except OSError:
                logger.debug("failed to read/parse env.sh at %s", env_sh, exc_info=True)
    except Exception:  # noqa: BLE001  # last-resort init guard: whatever went wrong above, this function must never raise -- only ensure the fallback env vars below get set so downstream calls don't crash.
        # Still force-set the basic env vars, to avoid failing downstream.
        try:
            os.environ.setdefault("REMAGRAPH_PROJECT", project_id)
            if "REMAGRAPH_STATE_DIR" not in os.environ:
                os.environ["REMAGRAPH_STATE_DIR"] = str(_project_state_dir(project_id))
        except Exception:  # deepest fallback safety net; any failure here must not propagate, this is the last line of defense for setting minimal env vars.
            logger.debug("failed to force-set fallback env vars for project %s", project_id, exc_info=True)


def _enforce_remagraph_safety_valve(project_id: str) -> None:
    """Safety valve: block non-compliant RemaGraph operation inputs.
    Per RemaGraph's guidance: every herdr-bridge dispatch/operation must first call ensure, and
    REMAGRAPH_STATE_DIR must be set correctly. project_id must not use the default "herdr" and
    contaminate the default DB. Non-compliant calls are rejected outright (raise), never silently.
    """
    if not is_remagraph_enabled():
        return
    _ensure_remagraph_project(project_id)
    state_dir = os.environ.get("REMAGRAPH_STATE_DIR")
    if not state_dir:
        raise HerdrMemoryError(
            f"Herdr Bridge Memory is not initialized for project '{project_id}' "
            f"(internal setup-ordering issue: the memory backend must be initialized "
            f"before any store/search)."
        ) from RuntimeError(
            f"REMAGRAPH_STATE_DIR not set. Must call _ensure_remagraph_project('{project_id}') "
            f"first before any store/search."
        )
    # Guard against using the default "herdr" as a herdr-bridge context (unless this is clearly a test).
    if project_id in ("herdr", "default") and "herdr-bridge" in str(Path.cwd()).lower():
        # Allowed but suspicious; could be turned into a hard raise in the future.
        pass  # Just logged for now; can be hardened later.
    # Verify state_dir is project-specific. Must compare resolved paths exactly, not via substring
    # matching — a suffixed directory name like remagraph-herdr-bridge-20260722-task-xxx-8edeb3 does
    # "contain" remagraph-herdr-bridge as a substring, but is actually a completely different
    # directory; substring matching would wrongly treat it as legitimate and let it through, making
    # the safety valve useless (2026-07-25 postmortem).
    expected_dir = _project_state_dir(project_id)
    if Path(state_dir).resolve() != expected_dir.resolve():
        raise HerdrMemoryError(
            f"Herdr Bridge Memory safety check failed for project '{project_id}': "
            f"the memory storage location doesn't match what's expected for this project. "
            f"This usually means setup didn't complete correctly — see docs/memory-advanced.md."
        ) from RuntimeError(
            f"Safety valve: REMAGRAPH_STATE_DIR '{state_dir}' does not match project '{project_id}' "
            f"(expected '{expected_dir}'). Must use dedicated project DB. "
            f"Call _ensure_remagraph_project('{project_id}') first."
        )


def generate_task_id(
    project: str = "herdr",
    description: str = "task",
    *,
    date_str: str | None = None,
    short: str | None = None,
) -> str:
    d = date_str or datetime.now(UTC).date().strftime("%Y%m%d")
    safe_desc = re.sub(r"[^a-z0-9-]", "-", description.lower()).strip("-")[:30] or "task"
    if short is None:
        seed = f"{project}-{d}-{description}".encode()
        short = hashlib.md5(seed).hexdigest()[:6]
    return f"{project}-{d}-{safe_desc}-{short}"


def ensure_task_ids(
    base_task_id: str,
    agent_id: str,
    *,
    project: str = "herdr",
) -> tuple[str, str]:
    task_id = os.environ.get("TASK_ID") or generate_task_id(project=project, description=base_task_id)
    agent_id = os.environ.get("AGENT_ID") or agent_id or "commander:light"
    return task_id, agent_id


def recall_memories(
    task_id: str, agent_id: str, *, timeout_sec: int = 8, top_k: int = 5, project_id: str
) -> list[dict[str, Any]]:
    if not is_remagraph_enabled():
        return []

    if not project_id or project_id in ("herdr", "default"):
        raise HerdrMemoryError("project_id is a required field; the herdr-bridge context must not use 'default'.")

    # Safety valve (strict RemaGraph compliance).
    _enforce_remagraph_safety_valve(project_id)

    if _DIRECT_IMPORT:
        try:
            conn = _rg_connect()
            req = _RgSearchRequest(query="", top_k=top_k, task_id=task_id)
            resp = _rg_search_memories(conn, req)
            try:
                conn.close()
            except Exception:  # best-effort cleanup of an external (remagraph-owned) connection object; failing to close must not fail the recall itself.
                logger.debug("failed to close remagraph connection after recall", exc_info=True)
            return getattr(resp, "results", []) or resp.get("results", [])
        except Exception:  # noqa: BLE001  # recall is a best-effort read: any backend/connection failure degrades to "no prior memories found" rather than surfacing to the caller.
            return []

    # CLI fallback.
    try:
        cmd = ["remagraph", "auto", "--recall-only", "--task-id", task_id, "--agent-id", agent_id, "--quiet"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, env={**os.environ}, check=False)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout or "{}")
        return data.get("memories", []) or []
    except Exception:  # noqa: BLE001  # same best-effort recall contract as the direct-import branch above (CLI failure, timeout, or malformed JSON all degrade to "no prior memories found").
        return []


def format_memory_summary(memories: list[dict[str, Any]], max_items: int = 5) -> str:
    if not memories:
        return ""
    lines = ["[Herdr Bridge Memory: Prior Summary for This Task]"]
    for m in memories[:max_items]:
        summary = (m.get("summary") or m.get("handoff_note") or "")[:120]
        kind = m.get("kind", "")
        ts = str(m.get("timestamp", ""))[:10]
        if summary:
            lines.append(f"- [{kind}] {ts} {summary}")
    lines.append("Please continue from these memories, and log progress after each key step.")
    lines.append("")
    return "\n".join(lines)


def augment_prompt_with_memory(original_prompt: str, memories: list[dict[str, Any]]) -> str:
    summary = format_memory_summary(memories)
    if not summary:
        return original_prompt
    return f"{summary}\n{original_prompt}"


def get_usage_instruction(task_id: str, agent_id: str, *, assume_remagraph: bool = True) -> str:
    if assume_remagraph:
        return (
            "\n\n[MANDATORY - Herdr Bridge Memory Logging]\n"
            "Before you finish or hit a key milestone, you must use an available tool (shell / terminal) "
            "to run the following command to log an ack:\n"
            f'  herdr-commander memory note "Acknowledged direct ACP communication, work completed/progress summary: ..." --task-id "{task_id}" --agent-id "{agent_id}"\n\n'
            "This is the only reliable way to confirm the other side actually engaged. Make sure you run it — don't just reply with text."
        )
    return (
        "\n\n【Please log progress whenever possible (Herdr Bridge Memory optional)】\n"
        "If the memory backend isn't available, please output:\n"
        "  [[MEMORY_NOTE: brief summary]]\n"
        "The command tower will pick it up automatically."
    )


def extract_remagraph_notes(text: str) -> list[str]:
    if not text:
        return []
    pattern = r"\[\[(?:MEMORY_NOTE|REMAGRAPH_NOTE):\s*(.+?)\s*\]\]"
    matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
    return [m.strip() for m in matches if m.strip()]


def prepare_dispatch_text(
    base_text: str,
    *,
    base_task_id: str,
    agent_id: str,
    project: str = "herdr",
    max_memories: int = 5,
) -> tuple[str, str, str]:
    if not is_remagraph_enabled():
        return base_text, base_task_id, agent_id

    try:
        task_id, aid = ensure_task_ids(base_task_id, agent_id, project=project)
        memories = recall_memories(task_id, aid, top_k=max_memories, project_id=project)
        text = augment_prompt_with_memory(base_text, memories)
        assume = uses_direct_import()
        text += get_usage_instruction(task_id, aid, assume_remagraph=assume)
        return text, task_id, aid
    except Exception:  # noqa: BLE001  # best-effort prompt augmentation: any failure preparing memory context must fall back to the original, un-augmented prompt rather than blocking dispatch.
        return base_text, base_task_id, agent_id


# ---------------------------------------------------------------------------
# Store (new: automatically write memory when reporting)
# ---------------------------------------------------------------------------

def store_memory(
    task_id: str,
    agent_id: str,
    *,
    kind: str = "status_update",
    summary: str,
    handoff_note: str = "",
    tags: list[str] | None = None,
    project_id: str,
    learnings: list[str] | None = None,
) -> dict[str, Any]:
    """Write a memory record.

    Prefers direct import, falls back to the CLI.
    On failure, returns an error dict gracefully instead of raising.
    project_id is a **required field** (per PPLX: no falling back to a default).
    """
    if not is_remagraph_enabled():
        return {"status": "disabled"}

    if not project_id or project_id in ("herdr", "default"):
        raise HerdrMemoryError("project_id is a required field; the herdr-bridge context must not use 'default'.")

    tags = tags or ["governance", "bridge"]

    # Safety valve (strict RemaGraph compliance): blocks non-compliant input.
    _enforce_remagraph_safety_valve(project_id)

    # Stabilize + force-set env vars.
    _ensure_remagraph_project(project_id)
    state_dir = str(_project_state_dir(project_id))
    os.environ["REMAGRAPH_STATE_DIR"] = state_dir
    os.environ["REMAGRAPH_PROJECT"] = project_id

    if _DIRECT_IMPORT:
        for attempt in (0, 1):
            try:
                conn = _rg_connect()
                req = _RgStoreRequest(
                    project_id=project_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    kind=kind,
                    summary=summary,
                    handoff_note=handoff_note,
                    tags=tags,
                    learnings=learnings or [],
                )
                resp = _rg_process_store(req, conn)
                try:
                    conn.close()
                except Exception:  # best-effort cleanup of an external (remagraph-owned) connection object; failing to close must not fail the store itself.
                    logger.debug("failed to close remagraph connection after store", exc_info=True)
                return {
                    "status": resp.status,
                    "id": getattr(resp, "id", None),
                    "superseded": getattr(resp, "superseded", False),
                    "reason": getattr(resp, "reason", None),
                    "detail": getattr(resp, "detail", None),
                }
            except Exception as e:  # noqa: BLE001  # remagraph's direct-import store call can raise any of its own internal exception types; the error text is inspected below to decide retry-after-reinit vs. falling back to the CLI, so the catch must stay broad here.
                estr = str(e).lower()
                if attempt == 0 and ("project_id" in estr or "no such column" in estr or "i/o" in estr or "disk" in estr):
                    # schema or I/O issue, force reinit and retry
                    _ensure_remagraph_project(project_id, force_reinit=True)
                    continue
                # Fall back to CLI when direct import fails.
                break
        # fall to CLI below

    # CLI fallback (or when direct import failed).
    try:
        cmd = [
            "remagraph", "store",
            "--project", project_id,
            "--task-id", task_id,
            "--agent-id", agent_id,
            "--kind", kind,
            "--summary", summary,
            "--handoff-note", handoff_note or "",
            "--tags", json.dumps(tags),
        ]
        if learnings:
            cmd += ["--learnings", json.dumps(learnings)]
        env = {**os.environ}
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env, check=False)
        if result.returncode == 0:
            return json.loads(result.stdout or "{}")  # type: ignore[no-any-return]
        return {"status": "error", "detail": result.stderr}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"status": "error", "detail": str(exc)}


def record_fleet_member(
    task_id: str,
    agent_id: str,
    *,
    pane_id: str,
    name: str,
    project_id: str,
    learnings: list[str] | None = None,
) -> dict[str, Any]:
    """Record when the command tower dispatches a fleet member (hardened HR bookkeeping).
    Requires report_sock (the side-channel's sole report path).
    Uses task_handoff + tags to stay compatible with the RemaGraph CLI.
    This is the basis for the tower owning recycling: only a fleet member the tower itself
    has recorded can be recycled by the tower itself.
    project_id is required (per PPLX).
    """
    if not project_id or project_id in ("herdr", "default"):
        raise HerdrMemoryError("project_id is a required field; the herdr-bridge context must not use 'default'.")

    base_learnings = [f"pane_id={pane_id}", f"name={name}"]
    if learnings:
        base_learnings.extend(learnings)
    return store_memory(
        task_id,
        agent_id,
        kind="task_handoff",
        summary=f"dispatched fleet member {name} on {pane_id}",
        handoff_note=f"Tower dispatched fleet member {name} (pane={pane_id}); the command tower owns its lifecycle",
        learnings=base_learnings,
        project_id=project_id,
        tags=["fleet", "dispatch", "tower-owned", "fleet_member"],
    )


def recall_fleet_members(
    task_id: str | None = None,
    agent_id: str | None = None,
    project_id: str = "herdr",
    top_k: int = 50,
) -> list[dict[str, Any]]:
    """Recall the fleet-member records dispatched by this tower.
    Filters by tags + kind, used as the sole basis for recycling decisions.
    """
    memories = recall_memories(
        task_id or "", agent_id or "", top_k=top_k, project_id=project_id
    )
    return [
        m for m in memories
        if "fleet_member" in (m.get("tags") or [])
        and (not task_id or m.get("task_id") == task_id)
        and (not agent_id or m.get("agent_id") == agent_id)
    ]


def record_fleet_recycle(
    task_id: str,
    agent_id: str,
    *,
    pane_id: str,
    reason: str = "task_completed",
    project_id: str,
) -> dict[str, Any]:
    """Record the tower's own recycle action."""
    if not project_id or project_id in ("herdr", "default"):
        raise HerdrMemoryError("project_id is a required field; the herdr-bridge context must not use 'default'.")

    return store_memory(
        task_id,
        agent_id,
        kind="status_update",
        summary=f"recycled {pane_id} ({reason})",
        learnings=[f"pane_id={pane_id}", f"reason={reason}"],
        project_id=project_id,
        tags=["fleet", "recycle", "tower-owned", "fleet_member"],
    )


# Backward-compatible names (older code may still import remagraph).
__all__ = [
    "augment_prompt_with_memory",
    "ensure_task_ids",
    "extract_remagraph_notes",
    "format_memory_summary",
    "generate_task_id",
    "get_remagraph_mode",
    "get_usage_instruction",
    "is_remagraph_enabled",
    "migrate_herdr_bridge_memories",
    "prepare_dispatch_text",
    "recall_fleet_members",
    "recall_memories",
    "record_fleet_member",
    "record_fleet_recycle",
    "store_memory",
    "uses_direct_import",
]


def migrate_herdr_bridge_memories(
    old_project: str = "herdr",
    new_project: str = "herdr-bridge",
    filter_keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Migrate memories from the old project DB into the herdr-bridge-dedicated DB, and insert an INVALIDATED marker into the old DB.
    Fix: reads old data via direct sqlite access (avoiding the issue where recall's empty
    query often returns 0 results); also uses a direct marker for the legacy "herdr" project
    (avoiding the safety valve's ban on "herdr" as a project_id).
    Only uses store_memory (compliant) for new_project.
    """
    if filter_keywords is None:
        filter_keywords = ["herdr-bridge", "herdr-acp", "tower", "bridge"]

    migrated = 0
    invalidated = 0
    try:
        # Compute the old DB path (independent of env + recall). Deliberately reuses the
        # standard remagraph-<project_id> naming instead of _project_state_dir's hb-live
        # self-protection prefix — what's being read here is the "old source" project's
        # existing data, which was always on the standard path to begin with; it isn't
        # what we're trying to protect.
        safe_old = _slugify_project(old_project)
        old_state_dir = Path.home() / ".local" / "state" / f"remagraph-{safe_old}"
        old_db_path = old_state_dir / "remagraph.db"
        if not old_db_path.exists():
            _ensure_remagraph_project(new_project)
            return {"status": "ok", "migrated": 0, "invalidated": 0, "new_project": new_project, "note": f"no old db for {old_project}"}

        # Read the entire old DB via direct sqlite (reliable, doesn't depend on recall or an empty FTS query).
        conn = sqlite3.connect(str(old_db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, project_id, task_id, agent_id, kind, summary, handoff_note, tags, learnings, status, timestamp, created_at, updated_at FROM memories"
        ).fetchall()
        conn.close()

        old_mems: list[dict[str, Any]] = []
        for r in rows:
            mem = dict(r)
            try:
                mem["tags"] = json.loads(mem.get("tags") or "[]")
            except (json.JSONDecodeError, TypeError):
                mem["tags"] = []
            try:
                mem["learnings"] = json.loads(mem.get("learnings") or "[]")
            except (json.JSONDecodeError, TypeError):
                mem["learnings"] = []
            old_mems.append(mem)

        for mem in old_mems:
            txt = str(mem.get("summary", "")) + " " + str(mem.get("handoff_note", "")) + str(mem.get("tags", ""))
            if any(kw in txt.lower() for kw in filter_keywords):
                # Move it to the new project (store is only used here, where project_id is valid).
                try:
                    store_memory(
                        mem.get("task_id", "migrated"),
                        mem.get("agent_id", "migrated"),
                        kind=mem.get("kind", "status_update"),
                        summary=mem.get("summary", ""),
                        handoff_note=mem.get("handoff_note", ""),
                        tags=(mem.get("tags", []) or []) + [f"migrated-from-{old_project}"],
                        project_id=new_project,
                        learnings=mem.get("learnings", []),
                    )
                    migrated += 1
                except Exception:
                    # Per-row best-effort: any failure (not just HerdrBridgeError) must be
                    # swallowed so the batch loop keeps migrating the remaining rows.
                    logger.debug(
                        "skipped migrating memory task_id=%s (store_memory rejected it)",
                        mem.get("task_id", "migrated"), exc_info=True,
                    )

                # Insert the INVALIDATED marker directly into the old DB (not via store_memory, to avoid the "herdr" safety raise).
                try:
                    conn = sqlite3.connect(str(old_db_path))
                    now = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
                    mid = f"mem-migrated-{old_project}-{int(time.time() * 1000)}"
                    marker_tags = json.dumps(["invalidated", "migrated-to-herdr-bridge", f"from-{old_project}"])
                    marker_learn = json.dumps([])
                    conn.execute(
                        """
                        INSERT INTO memories (id, project_id, kind, task_id, agent_id, timestamp, summary, learnings, handoff_note, tags, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            mid,
                            old_project,
                            "status_update",
                            mem.get("task_id", "migrated"),
                            mem.get("agent_id", "migrated"),
                            now,
                            "INVALIDATED - migrated to dedicated DB",
                            marker_learn,
                            str(mem)[:200],
                            marker_tags,
                            "invalidated",
                            now,
                            now,
                        ),
                    )
                    conn.commit()
                    conn.close()
                    invalidated += 1
                except sqlite3.Error:
                    logger.debug(
                        "failed to insert INVALIDATED marker into old db %s", old_db_path, exc_info=True,
                    )

        # Switch back to the new project.
        _ensure_remagraph_project(new_project)
        return {"status": "ok", "migrated": migrated, "invalidated": invalidated, "new_project": new_project, "old_project": old_project}
    except Exception as e:  # noqa: BLE001  # one-off admin migration spanning sqlite I/O, JSON, and store_memory/env setup; reports failure as a status dict rather than crashing the caller, by design.
        return {"status": "error", "detail": str(e)}


# ---------------------------------------------------------------------------
# Retention / SLA + monitoring metrics (per PPLX)
# ---------------------------------------------------------------------------

RETENTION_POLICY = {
    "COMPLETED": 30,   # days
    "DEGRADED": 14,
    "PONG_RECEIVED": 7,
    "FAILED": 90,      # for audit
    "default": 7,
}

SLA_TARGETS = {
    "pong_ack_max_sec": 30,      # PONG response SLA
    "delivery_success_rate": 0.95,
    "max_retries": 3,
}

def get_delivery_metrics(project_id: str, top_k: int = 100) -> dict[str, Any]:
    """Monitoring metrics: state distribution, latency, load.
    Per PPLX: expose DB-size-related figures, write frequency, success rate, state distribution.
    """
    try:
        mems = recall_memories("", "", top_k=top_k, project_id=project_id) or []
        state_counts: dict[str, int] = {}
        recent_pongs = 0
        for m in mems:
            if "delivery-state" in (m.get("tags") or []):
                st = m.get("summary", "").split("delivery_state=")[-1].split()[0] if "delivery_state=" in m.get("summary", "") else "unknown"
                state_counts[st] = state_counts.get(st, 0) + 1
            if m.get("kind") == "status_update" and "pong" in str(m).lower():
                recent_pongs += 1
        total = len(mems)
        success = state_counts.get("COMPLETED", 0) + state_counts.get("PONG_RECEIVED", 0)
        metrics = {
            "state_distribution": state_counts,
            "total_memories_sampled": total,
            "success_count": success,
            "success_rate": success / total if total > 0 else 0,
            "recent_pong_count": recent_pongs,
            "sla_pong_ack_max_sec": SLA_TARGETS["pong_ack_max_sec"],
            "retention_days": RETENTION_POLICY,
        }
        return metrics
    except Exception as e:  # noqa: BLE001  # best-effort monitoring endpoint; any failure computing metrics degrades to an error dict rather than crashing the caller.
        return {"error": str(e)}

def apply_retention(project_id: str, dry_run: bool = True) -> dict[str, Any]:
    """Retention policy: mark stale tasks as archived based on SLA.
    Per PPLX: ties retention to SLA, following a hot/cold tiering concept.
    """
    archived = 0
    try:
        mems = recall_memories("", "", top_k=500, project_id=project_id) or []
        now = time.time()
        for m in mems:
            if "delivery-state" not in (m.get("tags") or []):
                continue
            summary = m.get("summary", "")
            state = summary.split("delivery_state=")[1].split()[0] if "delivery_state=" in summary else "default"
            days = RETENTION_POLICY.get(state, RETENTION_POLICY["default"])
            # Simplified: judged by timestamp (should really be parsed properly).
            ts = m.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")[:19])
                    if (now - dt.timestamp()) > days * 86400:
                        if not dry_run:
                            store_memory(
                                m.get("task_id", ""), m.get("agent_id", ""),
                                kind="status_update",
                                summary=f"archived due to retention (>{days}d)",
                                project_id=project_id,
                                tags=["retention", "archived", state],
                            )
                        archived += 1
                except (ValueError, AttributeError):
                    logger.debug("failed to parse timestamp %r for retention check", ts, exc_info=True)
                if not dry_run:
                    store_memory(
                        m.get("task_id", ""), m.get("agent_id", ""),
                        kind="status_update",
                        summary=f"archived due to retention (>{days}d)",
                        project_id=project_id,
                        tags=["retention", "archived", state],
                    )
                archived += 1
        return {"archived": archived, "dry_run": dry_run, "policy": RETENTION_POLICY}
    except Exception as e:  # noqa: BLE001  # best-effort monitoring/retention sweep; any failure degrades to an error dict rather than crashing the caller.
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Delivery State Machine & 3-Layer Reliable Backup (per §9 + PPLX)
# ---------------------------------------------------------------------------

DELIVERY_STATES = [
    "INIT",
    "DISPATCH_PENDING",
    "DISPATCH_FAILED",
    "AWAIT_PONG",
    "PONG_RECEIVED",
    "SIDE_REPORT_RECEIVED",
    "TIMEOUT",
    "FAILED",
]

# Full state transition table (per PPLX + §9)
STATE_TRANSITIONS = {
    "INIT": ["DISPATCH_PENDING", "DISPATCH_FAILED"],
    "DISPATCH_PENDING": ["AWAIT_PONG", "DISPATCH_FAILED", "TIMEOUT"],
    "DISPATCH_FAILED": ["FAILED", "INIT"],  # retryable
    "AWAIT_PONG": ["PONG_RECEIVED", "SIDE_REPORT_RECEIVED", "TIMEOUT", "FAILED"],
    "PONG_RECEIVED": ["COMPLETED"],  # terminal success (added per PPLX's recommendation)
    "SIDE_REPORT_RECEIVED": ["COMPLETED", "DEGRADED"],  # fallback success
    "TIMEOUT": ["FAILED", "INIT"],
    "FAILED": ["INIT"],  # retry
    "COMPLETED": [],  # terminal state
    "DEGRADED": [],  # terminal state
}

def _validate_transition(current_state: str | None, new_state: str) -> None:
    """Validate that a state transition is legal, to avoid an invalid FSM."""
    if new_state not in DELIVERY_STATES + ["COMPLETED", "DEGRADED"]:
        raise ValueError(f"Invalid state: {new_state}")
    if current_state is None:
        if new_state != "INIT":
            # There are two possible causes for current_state=None, and the error
            # message must let both be diagnosed — it can't just hint at "called out
            # of order" (2026-07-25 #71 postmortem: an error message that only talked
            # about call order once sent debugging down the wrong path for a whole
            # round; the actual root cause was that the previous
            # update_delivery_state('INIT', ...) write had been rejected by a RemaGraph
            # arbitration rule without raising an exception, letting the caller believe
            # it had succeeded):
            #   (a) this really is the first call for this task_id, and INIT should
            #       have been passed first; or
            #   (b) INIT was called before, but that update_delivery_state() write
            #       actually failed (e.g. rejected by a RemaGraph arbitration rule) and
            #       the return value wasn't checked, so it was silently treated as a
            #       success — check the return value of that previous INIT call, or
            #       switch to the path that raises DeliveryStateWriteFailed on write
            #       failure (the self-verification built into update_delivery_state).
            raise ValueError(
                f"First state must be INIT, got {new_state} (current_state is None). "
                f"This could mean this is genuinely the first call for this task_id "
                f"(INIT should come first), or it could mean a previous INIT call's "
                f"write actually failed without being checked — please verify the "
                f"return status of the previous update_delivery_state('INIT', ...) call."
            )
        return
    allowed = STATE_TRANSITIONS.get(current_state, [])
    if new_state not in allowed:
        raise ValueError(f"Invalid transition: {current_state} -> {new_state}. Allowed: {allowed}")

# FSM terminal states — only these three trigger Dual-Write on Terminal State (writing
# an extra summary into the memory layer). DISPATCH_FAILED can technically still be
# retried in STATE_TRANSITIONS (-> INIT), so it isn't an absolute terminal state in the
# sense _validate_transition means by "allowed transitions is empty" — but here it
# refers to the outcome of "this particular dispatch attempt": even if it's genuinely
# retried later, that retry would be a brand-new INIT, and this attempt's failure is
# still worth a summary in long-term memory (for debugging/statistics).
_TERMINAL_STATES_FOR_MEMORY_SUMMARY = frozenset({"COMPLETED", "DEGRADED", "DISPATCH_FAILED"})


def _build_terminal_state_summary(
    task_id: str,
    agent_id: str,
    new_state: str,
    *,
    context: dict[str, Any] | None,
    correlation: str | None,
    duration_sec: float | None,
) -> str:
    """Terminal-state dual-write summary — needs substantive content (to avoid tripping
    RemaGraph arbitration rule #1's 30-character threshold again), not just padded to
    length: includes task identification info, the elapsed time from INIT to terminal
    state (computed from the FSM store's created_at/updated_at — substantive info the
    memory layer itself doesn't have and only this can provide), and the
    context/correlation passed in by the caller.
    """
    parts = [
        f"delivery_state={new_state} (task terminal state)",
        f"task_id={task_id} agent_id={agent_id}",
    ]
    if duration_sec is not None:
        parts.append(f"elapsed from INIT to {new_state}: approx. {duration_sec:.1f}s")
    if context:
        parts.append(f"context={context}")
    if correlation:
        parts.append(f"correlation={correlation}")
    parts.append("full transition history is in the FSM's dedicated store (orchestration.delivery_state_store)")
    return ", ".join(parts)


def update_delivery_state(
    task_id: str,
    agent_id: str,
    new_state: str,
    *,
    project_id: str,
    context: dict[str, Any] | None = None,
    correlation: str | None = None,
) -> dict[str, Any]:
    """Unified delivery state tracking (#72, PPLX review consensus: Dual-Write on Terminal State).

    All state changes go through this single API. Per PPLX: an explicit FSM + a single
    entry point + transition-table validation.

    ## Architecture (2026-07-25 #72, replacing the old "write to RemaGraph on every transition" design)

    The delivery-state FSM tracks what multi-agent coordination terminology calls
    **State** (lifecycle measured in minutes, needs exact lookup), not the **Memory**
    the memory layer is designed for (long-term knowledge, weeks to months, searched by
    semantic similarity) — the PPLX review consensus was explicit that this is an
    architectural layer mismatch (see the #71 postmortem: consecutive-transition summary
    similarity landed at 0.92-0.96, tripping RemaGraph rule #4's semantic-dedup threshold
    of 0.90, so nothing after INIT could ever get written). PPLX also explicitly ruled
    out two shortcuts: carefully engineering summary wording differences ("a plan that
    sets up the next maintainer to fall into the same trap") and hunting for a
    memory-layer exception parameter ("the standard way technical debt gets created").

    The fix:
    - **Every state transition** -> only written to `orchestration.delivery_state_store`
      (the FSM's dedicated lightweight SQLite store — overwritable, has a TTL, see that
      module's docstring).
    - **Only terminal states** (`_TERMINAL_STATES_FOR_MEMORY_SUMMARY`: COMPLETED/
      DEGRADED/DISPATCH_FAILED) -> get an extra summary with substantive content
      written into the memory layer.

    Reads back immediately after writing to self-verify (2026-07-25 #71 postmortem
    lesson retained — switching storage layers doesn't mean giving up this safeguard):
    if the FSM's dedicated store can't read back the state it just wrote,
    `DeliveryStateWriteFailed` is always raised — never a silent success. If writing the
    terminal summary into the memory layer fails, that doesn't affect the fact that the
    FSM state itself has already correctly landed in the dedicated store (no raise), but
    the return value explicitly flags `memory_summary_failed=True` so the caller doesn't
    mistakenly believe the summary was actually written.
    """
    current = get_delivery_state(task_id, agent_id, project_id=project_id)
    current_state = current["state"] if current else None

    # This check must run before _validate_transition(), and the condition must only be
    # "has genuinely reached a true terminal state" (2026-07-25 #71 postmortem, a second,
    # independent bug): PONG_RECEIVED / SIDE_REPORT_RECEIVED are intermediate
    # confirmation states in STATE_TRANSITIONS that "still have a legal next step"
    # (PONG_RECEIVED -> COMPLETED is the one legal transition) — they are not terminal
    # states. Counting them as "already done, anything after is a duplicate" would cause
    # this one legal, normal transition to be misjudged as a duplicate delivery and
    # blocked, meaning COMPLETED could never be recorded correctly.
    #
    # #72: the dedup block no longer writes any memory-layer record at all (intermediate
    # transitions were never written anyway; this is just a rejected duplicate attempt,
    # and there's nothing to touch even in the FSM's dedicated store — current_state is
    # already the correct answer) — it directly returns an explicitly flagged result
    # dict, without raising or polluting any storage layer.
    if current_state in ("COMPLETED", "DEGRADED") and new_state in ("PONG_RECEIVED", "COMPLETED", "SIDE_REPORT_RECEIVED"):
        return {
            "status": "duplicate_blocked",
            "state": current_state,
            "attempted_state": new_state,
            "task_id": task_id,
            "agent_id": agent_id,
        }

    _validate_transition(current_state, new_state)

    if new_state not in DELIVERY_STATES + ["COMPLETED", "DEGRADED"]:
        raise ValueError(f"Invalid state: {new_state}. Must be one of {DELIVERY_STATES}")

    state_dir = _project_state_dir(project_id)
    _fsm_store.write_state(
        state_dir, project_id, task_id, agent_id, new_state,
        context=context, correlation=correlation,
    )

    # Self-verify: read back immediately to confirm the just-written state is really queryable (#71 postmortem lesson retained).
    verify_row = _fsm_store.read_state(state_dir, project_id, task_id, agent_id)
    if not verify_row or verify_row.get("state") != new_state:
        raise DeliveryStateWriteFailed(
            f"update_delivery_state('{new_state}', task_id='{task_id}') failed "
            f"read-back verification immediately after write: the FSM's dedicated store "
            f"returned {verify_row}, expected state='{new_state}'. "
            f"The FSM state has not actually landed and should not be treated as transitioned."
        )

    result: dict[str, Any] = {
        "status": "stored",
        "state": new_state,
        "task_id": task_id,
        "agent_id": agent_id,
    }

    # Dual-Write on Terminal State: only terminal states get an extra summary written into the memory layer.
    if new_state in _TERMINAL_STATES_FOR_MEMORY_SUMMARY:
        duration_sec = None
        if verify_row.get("created_at") is not None and verify_row.get("updated_at") is not None:
            duration_sec = verify_row["updated_at"] - verify_row["created_at"]
        summary = _build_terminal_state_summary(
            task_id, agent_id, new_state,
            context=context, correlation=correlation, duration_sec=duration_sec,
        )
        tags = ["delivery-state", "fsm", "terminal", new_state.lower(), "tower-bookkeeping"]
        if correlation:
            tags.append("correlation")
        mem_result = store_memory(
            task_id,
            agent_id,
            kind="status_update",
            summary=summary,
            handoff_note=f"FSM terminal state {new_state} (full transition history is in the FSM's dedicated store)",
            project_id=project_id,
            tags=tags,
            learnings=[f"state={new_state}", f"correlation={correlation or ''}"],
        )
        result["memory_summary"] = mem_result
        if mem_result.get("status") != "stored":
            # A failed terminal-summary write shouldn't be a silent success, but it also
            # shouldn't undo the state transition that's already correctly landed in the
            # FSM's dedicated store — flag it explicitly and let the caller decide
            # whether to retry/alert.
            result["memory_summary_failed"] = True

    return result


def get_delivery_state(
    task_id: str, agent_id: str, *, project_id: str, top_k: int = 5
) -> dict[str, Any] | None:
    """Get the latest delivery state (#72: reads the FSM's dedicated store, no longer the memory layer).

    The `top_k` parameter is kept but no longer used — the old version relied on
    `recall_memories(top_k=...)`'s semantic search to approximate several records and
    then pick the latest one; the FSM's dedicated store is an exact lookup on a single
    `(project_id, task_id, agent_id)` key, so there's no "pick from several" concept
    needed. The parameter is only kept so existing callers' call signatures don't break.
    """
    state_dir = _project_state_dir(project_id)
    row = _fsm_store.read_state(state_dir, project_id, task_id, agent_id)
    if row is None:
        return None
    return {"state": row["state"], "context": row.get("context"), "correlation": row.get("correlation")}
