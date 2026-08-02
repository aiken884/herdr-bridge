# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""launchd installer for the Signal daemon's own healthcheck watchdog.

This watchdog monitors herdr-bridge's own Signal daemon specifically -- a
herdr-bridge concern, not generic cross-project tower governance -- so it's
a first-class herdr-bridge feature rather than something an external
governance-layer project's tower-management tooling should own (see
`CONTRIBUTING.md`'s "Governance-layer boundary" section for that split).
Lets herdr-bridge install/remove its own healthcheck service with no
external dependency required to set it up.

Every side-effecting function here takes its filesystem/subprocess/lookup
dependencies as explicit parameters (`runner`, `which`, `uid`,
`platform_name`) rather than reaching for globals — this keeps the whole
module testable without touching the real filesystem or launchd.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from herdr_bridge.errors import HerdrBridgeError
from herdr_bridge.orchestration._state_paths import slugify_project

DEFAULT_INTERVAL_SEC = 300


class WatchdogInstallError(HerdrBridgeError):
    """install_watchdog()/uninstall_watchdog() failed: unsupported platform,
    herdr-commander could not be located (or the given path isn't absolute),
    an invalid interval was given, writing/removing the plist hit a
    filesystem error, or `launchctl bootstrap` itself failed (a real, fairly
    common failure mode when there's no live GUI login session -- e.g.
    running over SSH). Every one of these is caught and re-raised as this
    type specifically so `cmd_signal_install_watchdog`/
    `cmd_signal_uninstall_watchdog` can print a clean `_err(...)` message
    instead of a raw traceback (2026-08-02 adversarial-review finding: the
    original version only translated the first two cases)."""


def plist_label(project: str) -> str:
    """The launchd Label for this project's Signal healthcheck watchdog.

    Uses the plain `herdr` prefix (no `com.`/vendor/company segment, no
    project name baked into the prefix) -- deliberately not a vendor-branded
    reverse-DNS-style identifier."""
    return f"herdr.signal-healthcheck.{slugify_project(project)}"


def plist_path(project: str, launch_agents_dir: Path) -> Path:
    return launch_agents_dir / f"{plist_label(project)}.plist"


def build_plist_dict(
    project: str,
    *,
    herdr_commander_path: str,
    out_path: Path,
    err_path: Path,
    path_env: str,
    home_env: str,
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    socket_path: str | None = None,
) -> dict[str, Any]:
    """Pure plist-content builder -- no I/O, fully testable."""
    env = {"HOME": home_env, "PATH": path_env}
    if socket_path:
        env["HERDR_SOCKET_PATH"] = socket_path
    return {
        "Label": plist_label(project),
        "ProgramArguments": [
            herdr_commander_path,
            "signal", "status", "--project", project, "--notify-on-problem",
        ],
        "EnvironmentVariables": env,
        "RunAtLoad": False,
        "StartInterval": interval_sec,
        "StandardOutPath": str(out_path),
        "StandardErrorPath": str(err_path),
    }


def _require_macos(platform_name: str) -> None:
    if platform_name != "Darwin":
        raise WatchdogInstallError(
            f"signal watchdog install/uninstall only supports macOS (launchd); "
            f"this host reports platform={platform_name!r}"
        )


def install_watchdog(
    project: str,
    *,
    launch_agents_dir: Path,
    log_dir: Path,
    path_env: str,
    home_env: str,
    herdr_commander_path: str | None = None,
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    socket_path: str | None = None,
    platform_name: str | None = None,
    uid: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """Write the plist and (re)load it via launchctl. Returns the plist path.

    Idempotent: bootout is attempted (and ignored if nothing was loaded)
    before bootstrap, so calling this again after an edit cleanly reloads.
    """
    _require_macos(platform_name if platform_name is not None else platform.system())

    if interval_sec <= 0:
        raise WatchdogInstallError(f"interval_sec must be positive, got {interval_sec}")

    if herdr_commander_path is not None:
        # launchd's ProgramArguments does no PATH resolution -- a relative path or
        # bare command name would let `launchctl bootstrap` "succeed" but every
        # scheduled run would then silently fail to spawn (2026-08-02
        # adversarial-review finding).
        if not Path(herdr_commander_path).is_absolute():
            raise WatchdogInstallError(
                f"--herdr-commander-path must be an absolute path (launchd does not do "
                f"PATH resolution for ProgramArguments), got {herdr_commander_path!r}"
            )
        resolved_bin = herdr_commander_path
    else:
        which_result = which("herdr-commander")
        if not which_result:
            raise WatchdogInstallError(
                "herdr-commander not found on PATH; pass --herdr-commander-path explicitly"
            )
        resolved_bin = which_result

    try:
        launch_agents_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        out_path = log_dir / "signal-healthcheck.out"
        err_path = log_dir / "signal-healthcheck.err"

        data = build_plist_dict(
            project,
            herdr_commander_path=resolved_bin,
            out_path=out_path,
            err_path=err_path,
            path_env=path_env,
            home_env=home_env,
            interval_sec=interval_sec,
            socket_path=socket_path,
        )
        dest = plist_path(project, launch_agents_dir)
        with dest.open("wb") as f:
            plistlib.dump(data, f)
        dest.chmod(0o600)
    except OSError as exc:
        raise WatchdogInstallError(f"failed to write watchdog plist: {exc}") from exc

    resolved_uid = uid if uid is not None else os.getuid()
    label = plist_label(project)
    runner(["launchctl", "bootout", f"gui/{resolved_uid}/{label}"], capture_output=True, check=False)
    result = runner(
        ["launchctl", "bootstrap", f"gui/{resolved_uid}", str(dest)], capture_output=True, check=False
    )
    returncode = getattr(result, "returncode", 0)
    if returncode != 0:
        stderr = getattr(result, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise WatchdogInstallError(
            f"launchctl bootstrap failed (exit {returncode}): {stderr.strip() or '(no stderr)'}"
        )
    return dest


def uninstall_watchdog(
    project: str,
    *,
    launch_agents_dir: Path,
    platform_name: str | None = None,
    uid: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> bool:
    """Unload and remove the plist. Returns True if a plist file was present
    and removed, False if there was nothing installed."""
    _require_macos(platform_name if platform_name is not None else platform.system())

    resolved_uid = uid if uid is not None else os.getuid()
    label = plist_label(project)
    runner(["launchctl", "bootout", f"gui/{resolved_uid}/{label}"], capture_output=True, check=False)

    dest = plist_path(project, launch_agents_dir)
    try:
        if dest.exists():
            dest.unlink()
            return True
        return False
    except OSError as exc:
        raise WatchdogInstallError(f"failed to remove watchdog plist: {exc}") from exc
