# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""AcpxAdapter: makes permission policy for opencode-family tiers actually
take effect under acpx.

See docs/acpx-adapter-implementation-plan.md for scope and background -- this
module only delivers three pure functions (config mapping, file writing,
argv/env assembly); it does not include the actual spawning (left to the
future `ensure_session()`/`prompt()`), nor the full M1 six-function facade.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from herdr_bridge.acp.errors import AcpAdapterError
from herdr_bridge.acp.models import AcpPolicy

_ORPHAN_PREFIX = "opencode-permission-"
_ORPHAN_SUFFIX = ".json"

_MACHINE_TO_ARCH = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86_64": "x64",
    "amd64": "x64",
}


def build_opencode_permission_config(policy: AcpPolicy) -> dict[str, object]:
    """Maps `AcpPolicy.mode` to opencode's `permission` config dict.

    Always uses `"*"` as the base rule (can't just set `edit` -- tools
    provided by MCP use the tool's own name as the permission key, so a
    narrow `edit` rule has no effect on them).

    The `list` key under `mode="approve-reads"` doesn't correspond to any
    built-in tool; it's kept for consistency with opencode's own established
    convention for its `explore` read-only agent (a commonly used
    compatibility key for MCP filesystem tool names). Known limitation (not a
    security defect): `read`/`glob`/`grep`/`lsp` on paths outside the
    worktree additionally trigger a separate `external_directory` gate, which
    this mapping doesn't add to the allowlist, so cross-worktree reads under
    `approve-reads` still fall through to `"*":"ask"` (effectively deny in
    non-interactive mode).

    `mode="deny-all"` explicitly carves `task` out of the `"*":"deny"`
    blanket rule -- the `task` tool is itself gated via
    `ctx.ask({permission:"task",...})`, and if it were also blocked by `"*"`,
    subagents could never be spawned at all.
    """
    if policy.mode == "approve-all":
        return {"*": "allow"}
    if policy.mode == "approve-reads":
        return {
            "*": "ask",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
        }
    if policy.mode == "deny-all":
        return {"*": "deny", "task": "ask"}
    raise AcpAdapterError(f"unknown AcpPolicy.mode: {policy.mode!r}")


def write_session_config(policy: AcpPolicy, *, session_dir: Path) -> Path:
    """Writes the permission config as a JSON file into `session_dir` and
    returns its absolute path.

    Before writing the new file, clears out any config files left behind by a
    previous call in the same `session_dir` (naming convention
    `opencode-permission-*.json`) -- so even if a previous process crashed
    abnormally (crash/SIGKILL) without reaching `close_session()`, orphaned
    files won't accumulate indefinitely. Only clears files "older than the
    start time of this call" (judged by mtime) -- it doesn't clear files that
    were just written and might still be in use by another concurrent call
    (defense in depth, see (a) below; the primary line of defense is the
    per-session_name lock in the caller, `AcpActions.ensure_session()` -- see
    that function's docstring for details).

    Known limitations (found during adversarial verification, recorded in
    docs/acpx-adapter-implementation-plan.md §5 N5, out of scope for this
    stage's fixes):
    (a) The cleanup here only guarantees it won't delete files that
    "appeared after this call started" -- it doesn't distinguish whether an
    older file in the same directory is currently in use by another session.
    Currently the only caller (this module itself) guarantees each
    session_dir serves a single session, but that guarantee isn't enforced by
    this function itself. If a future caller ever lets multiple sessions
    share the same session_dir with configs that outlive the span of a
    single call, this could still delete a config someone else is using.
    (b) When opencode can't read the file `OPENCODE_CONFIG` points to
    (missing, or empty content), it's fail-OPEN, not fail-closed --
    `readFileStringSafe` returns `undefined` on NotFound, and `loadFile`
    returns `{}` directly for empty content (`packages/opencode/src/
    config/config.ts`), which quietly falls back to opencode's factory
    default `"*":"allow"` rather than failing loudly. If the race in (a)
    above ever happens, this is the consequence, not an error.
    """
    call_started_at = time.time()
    for stale in session_dir.glob(f"{_ORPHAN_PREFIX}*{_ORPHAN_SUFFIX}"):
        try:
            if stale.stat().st_mtime < call_started_at:
                stale.unlink()
        except FileNotFoundError:
            pass  # already removed by another concurrent cleanup pass

    config = {"permission": build_opencode_permission_config(policy)}

    fd, raw_path = tempfile.mkstemp(
        prefix=_ORPHAN_PREFIX, suffix=_ORPHAN_SUFFIX, dir=session_dir
    )
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def build_acpx_argv_and_env(
    *,
    agent: str,
    agent_binary: Path,
    config_path: Path | None,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Builds acpx's argv and env; this function does not actually call
    `subprocess.Popen`.

    agent="opencode": uses the `--agent` escape hatch pointing at the
    locally-patched opencode binary, with the path `shlex.quote()`'d first --
    acpx's `splitCommandLine()` is a proper tokenizer that handles
    quoting/escaping, and naive string concatenation would get split apart or
    raise an `unterminated quote` error when the path contains spaces. Sets
    `OPENCODE_CONFIG` and `OPENCODE_DISABLE_PROJECT_CONFIG` in env, and clears
    `OPENCODE_CONFIG_CONTENT`.

    agent="claude": likewise uses the `--agent` escape hatch, pointing at the
    claude CLI binary (`claude acp`). Doesn't set any opencode-specific env
    vars (`OPENCODE_CONFIG`, etc.), but shares the acpx global flags
    (`--cwd`/`--ttl`/`--non-interactive-permissions`, etc.). `config_path` is
    None (claude doesn't need an opencode permission config file).

    `OPENCODE_CONFIG` in env must be placed last in the merge expression and
    must not be overridable by `extra_env` -- if `extra_env` were merged in
    last, a caller could quietly override `OPENCODE_CONFIG` through it,
    bypassing the entire permission config, which is a real privilege
    escalation path.

    Found during adversarial verification: `OPENCODE_CONFIG` is not the
    final authoritative source in opencode's config merge chain
    (`packages/opencode/src/config/config.ts`) -- the merge order is
    global -> `OPENCODE_CONFIG` -> project-level
    `opencode.json`/`.opencode/opencode.json` (searched upward through
    parent directories) -> `OPENCODE_CONFIG_CONTENT` (last), all merged via
    `mergeDeep`, with the later source winning. Because of that we also
    need: (a) setting `OPENCODE_DISABLE_PROJECT_CONFIG=true` to disable
    project-level config file discovery -- otherwise the governed working
    directory's own `opencode.json` could quietly override the permissions
    set here; (b) actively clearing `OPENCODE_CONFIG_CONTENT` (whether
    inherited from `os.environ` or passed in via `extra_env`) -- this is the
    last-applied source in the merge chain, and leaving it in place would
    leave a complete bypass backdoor.
    """
    env = {**os.environ, **(extra_env or {})}

    # Governance-layer memory integration: if the host has
    # REMAGRAPH_STATE_DIR set, pass it through explicitly to the agent's
    # environment (even if the agent itself may not have the remagraph
    # binary, this lets it point at the right DB when writing NOTEs)
    rg_state = os.environ.get("REMAGRAPH_STATE_DIR")
    if rg_state:
        env["REMAGRAPH_STATE_DIR"] = rg_state

    if agent == "opencode":
        agent_command = f"{shlex.quote(str(agent_binary))} acp"
        argv = ["acpx", "--agent", agent_command, "--cwd", str(cwd)]
        env.pop("OPENCODE_CONFIG_CONTENT", None)
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
        assert config_path is not None, "opencode requires a permission config_path"
        env["OPENCODE_CONFIG"] = str(config_path)
        return argv, env

    if agent == "claude":
        argv = ["acpx", "--cwd", str(cwd)]
        return argv, env

    raise AcpAdapterError(f"unsupported agent {agent!r} — only 'opencode' and 'claude' are wired")


def build_acpx_policy_flags(policy: AcpPolicy) -> list[str]:
    """Builds the acpx-level flags corresponding to `AcpPolicy.mode` (N4:
    defense in depth).

    Always prepends `--non-interactive-permissions deny` -- doesn't rely on
    the caller's machine having `~/.acpx/config.json` set correctly on its
    own. Even though acpx's parser only accepts the two safe values
    `deny`/`fail` (it can never quietly turn into allow), writing it
    explicitly into our own argv means the safety guarantee isn't left
    resting on a machine-level config file outside this module's control.

    `approve-all`/`deny-all` additionally layer on the corresponding acpx
    flag -- both are consistent with each mode's existing semantics
    (approve-all = today's zero-regression equivalent; deny-all = shouldn't
    even ask about task), so layering them carries no surprise risk.
    `approve-reads` deliberately does **not** layer on acpx's own
    `--approve-reads`: acpx's own heuristic for telling "is this a read" has
    not been verified, and carelessly layering it on could accidentally
    auto-allow a write that should have been blocked by opencode's local
    config -- riskier than trusting the non-interactive-deny default, which
    has already been verified with a real integration test
    (`TestApproveReadsAllowsReadBlocksWrite`).
    """
    flags = ["--non-interactive-permissions", "deny"]
    if policy.mode == "approve-all":
        flags.append("--approve-all")
    elif policy.mode == "approve-reads":
        pass
    elif policy.mode == "deny-all":
        flags.append("--deny-all")
    else:
        raise AcpAdapterError(f"unknown AcpPolicy.mode: {policy.mode!r}")
    return flags


def detect_target_triple() -> str:
    """Returns the current machine's `{platform}-{arch}` target triple (N2
    multi-platform path)."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = _MACHINE_TO_ARCH.get(machine, machine)
    return f"{system}-{arch}"


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def resolve_patched_opencode_binary(vendor_dir: Path) -> tuple[Path, list[str]]:
    """Resolves the patched opencode binary path for the current platform
    (N1/N2).

    Path structure is `{vendor_dir}/{target_triple}/opencode`, with
    `target_triple` detected from the machine currently running this code
    (not trusting whatever the manifest itself records, to avoid
    cross-platform misuse). Fails closed if no binary exists for the
    matching platform (raises explicitly rather than guessing and falling
    back to another platform's binary).

    Also checks whether `MANIFEST.json`'s `base_upstream_version` falls
    within the manually-verified `compatible_upstream_range` -- being
    outside that range is not an error (the binary still works), it just
    returns a warning string, and the caller must decide for itself whether
    to display it or block on it (N1: no silent use).
    """
    manifest_path = vendor_dir / "MANIFEST.json"
    if not manifest_path.exists():
        raise AcpAdapterError(f"missing MANIFEST.json under {vendor_dir}")
    manifest = json.loads(manifest_path.read_text())

    target_triple = detect_target_triple()
    binary_path = vendor_dir / target_triple / "opencode"
    if not binary_path.exists():
        raise AcpAdapterError(
            f"no patched opencode binary for {target_triple!r} under {vendor_dir} "
            f"— rebuild it with scripts/rebuild-patched-opencode.sh"
        )

    warnings: list[str] = []
    base_version = manifest.get("base_upstream_version")
    version_range = manifest.get("compatible_upstream_range")
    if base_version and version_range:
        base = _version_tuple(base_version)
        low = _version_tuple(version_range["min_inclusive"])
        high = _version_tuple(version_range["max_inclusive"])
        if not (low <= base <= high):
            warnings.append(
                f"base_upstream_version {base_version!r} is outside the "
                f"manually-verified compatible_upstream_range {version_range!r} "
                f"— the permission-mapping assumptions in "
                f"build_opencode_permission_config may no longer hold; "
                f"re-verify before trusting this build"
            )

    return binary_path, warnings


def resolve_claude_binary() -> Path:
    """Resolves the claude CLI binary path.

    Checks the `CLAUDE_BIN` env var first, then falls back to
    `shutil.which("claude")` -- equivalent to `command -v claude`. Raises
    `AcpAdapterError` if not found (fail-closed: doesn't assume claude is
    installed, doesn't guess at a path).
    """
    from_env = os.environ.get("CLAUDE_BIN")
    if from_env:
        path = Path(from_env)
        if not path.exists():
            raise AcpAdapterError(
                f"CLAUDE_BIN={from_env!r} points to a path that does not exist"
            )
        return path

    which = shutil.which("claude")
    if which is None:
        raise AcpAdapterError(
            "claude CLI not found on PATH — install the Claude CLI "
            "(https://docs.anthropic.com/en/docs/claude-code) or set CLAUDE_BIN"
        )
    return Path(which)
