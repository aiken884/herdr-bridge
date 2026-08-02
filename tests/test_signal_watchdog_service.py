# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `herdr_bridge.signal.watchdog_service` — the launchd installer for
the Signal daemon's own healthcheck watchdog: a herdr-bridge-owned feature,
not something generic cross-project tower-governance tooling should own."""

from __future__ import annotations

import pytest

from herdr_bridge.signal import watchdog_service

# -- plist_label / plist_path ---------------------------------------------------

def test_plist_label_uses_herdr_prefix_and_project():
    assert watchdog_service.plist_label("herdr-bridge") == "herdr.signal-healthcheck.herdr-bridge"


def test_plist_label_slugifies_unsafe_project_characters():
    assert watchdog_service.plist_label("my project!") == "herdr.signal-healthcheck.my-project-"


def test_plist_path_joins_launch_agents_dir_and_label(tmp_path):
    result = watchdog_service.plist_path("herdr-bridge", tmp_path)
    assert result == tmp_path / "herdr.signal-healthcheck.herdr-bridge.plist"


# -- build_plist_dict -----------------------------------------------------------

def test_build_plist_dict_has_expected_program_arguments(tmp_path):
    data = watchdog_service.build_plist_dict(
        "herdr-bridge",
        herdr_commander_path="/Users/x/.local/bin/herdr-commander",
        out_path=tmp_path / "out.log",
        err_path=tmp_path / "err.log",
        path_env="/usr/bin:/bin",
        home_env="/Users/x",
    )
    assert data["ProgramArguments"] == [
        "/Users/x/.local/bin/herdr-commander",
        "signal", "status", "--project", "herdr-bridge", "--notify-on-problem",
    ]


def test_build_plist_dict_uses_label_and_default_interval(tmp_path):
    data = watchdog_service.build_plist_dict(
        "herdr-bridge",
        herdr_commander_path="/bin/herdr-commander",
        out_path=tmp_path / "out.log",
        err_path=tmp_path / "err.log",
        path_env="/usr/bin:/bin",
        home_env="/Users/x",
    )
    assert data["Label"] == "herdr.signal-healthcheck.herdr-bridge"
    assert data["StartInterval"] == watchdog_service.DEFAULT_INTERVAL_SEC
    assert data["RunAtLoad"] is False


def test_build_plist_dict_honors_custom_interval(tmp_path):
    data = watchdog_service.build_plist_dict(
        "herdr-bridge",
        herdr_commander_path="/bin/herdr-commander",
        out_path=tmp_path / "out.log",
        err_path=tmp_path / "err.log",
        interval_sec=60,
        path_env="/usr/bin:/bin",
        home_env="/Users/x",
    )
    assert data["StartInterval"] == 60


def test_build_plist_dict_omits_socket_path_when_not_given(tmp_path):
    data = watchdog_service.build_plist_dict(
        "herdr-bridge",
        herdr_commander_path="/bin/herdr-commander",
        out_path=tmp_path / "out.log",
        err_path=tmp_path / "err.log",
        path_env="/usr/bin:/bin",
        home_env="/Users/x",
    )
    assert "HERDR_SOCKET_PATH" not in data["EnvironmentVariables"]


def test_build_plist_dict_includes_socket_path_when_given(tmp_path):
    data = watchdog_service.build_plist_dict(
        "herdr-bridge",
        herdr_commander_path="/bin/herdr-commander",
        out_path=tmp_path / "out.log",
        err_path=tmp_path / "err.log",
        path_env="/usr/bin:/bin",
        home_env="/Users/x",
        socket_path="/Users/x/.config/herdr/herdr.sock",
    )
    assert data["EnvironmentVariables"]["HERDR_SOCKET_PATH"] == "/Users/x/.config/herdr/herdr.sock"


def test_build_plist_dict_stringifies_log_paths(tmp_path):
    out = tmp_path / "out.log"
    err = tmp_path / "err.log"
    data = watchdog_service.build_plist_dict(
        "herdr-bridge",
        herdr_commander_path="/bin/herdr-commander",
        out_path=out,
        err_path=err,
        path_env="/usr/bin:/bin",
        home_env="/Users/x",
    )
    assert data["StandardOutPath"] == str(out)
    assert data["StandardErrorPath"] == str(err)


# -- install_watchdog -------------------------------------------------------------

def _fake_runner(calls):
    def _run(cmd, **kwargs):
        calls.append(cmd)
        class _Result:
            returncode = 0
        return _Result()
    return _run


def test_install_watchdog_raises_on_non_macos_platform(tmp_path):
    with pytest.raises(watchdog_service.WatchdogInstallError, match="macOS"):
        watchdog_service.install_watchdog(
            "herdr-bridge",
            launch_agents_dir=tmp_path / "LaunchAgents",
            log_dir=tmp_path / "log",
            herdr_commander_path="/bin/herdr-commander",
            platform_name="Linux",
            runner=_fake_runner([]),
            path_env="", home_env="",
        )


def test_install_watchdog_raises_when_herdr_commander_not_found(tmp_path):
    with pytest.raises(watchdog_service.WatchdogInstallError, match="herdr-commander"):
        watchdog_service.install_watchdog(
            "herdr-bridge",
            launch_agents_dir=tmp_path / "LaunchAgents",
            log_dir=tmp_path / "log",
            herdr_commander_path=None,
            which=lambda name: None,
            platform_name="Darwin",
            runner=_fake_runner([]),
            path_env="", home_env="",
        )


def test_install_watchdog_writes_plist_file(tmp_path):
    launch_agents_dir = tmp_path / "LaunchAgents"
    dest = watchdog_service.install_watchdog(
        "herdr-bridge",
        launch_agents_dir=launch_agents_dir,
        log_dir=tmp_path / "log",
        herdr_commander_path="/bin/herdr-commander",
        platform_name="Darwin",
        runner=_fake_runner([]),
        path_env="/usr/bin:/bin", home_env="/Users/x",
        uid=501,
    )
    assert dest == launch_agents_dir / "herdr.signal-healthcheck.herdr-bridge.plist"
    assert dest.exists()


def test_install_watchdog_written_plist_is_valid_and_matches_build_plist_dict(tmp_path):
    import plistlib

    launch_agents_dir = tmp_path / "LaunchAgents"
    dest = watchdog_service.install_watchdog(
        "herdr-bridge",
        launch_agents_dir=launch_agents_dir,
        log_dir=tmp_path / "log",
        herdr_commander_path="/bin/herdr-commander",
        platform_name="Darwin",
        runner=_fake_runner([]),
        path_env="/usr/bin:/bin", home_env="/Users/x",
        uid=501,
    )
    with dest.open("rb") as f:
        loaded = plistlib.load(f)
    assert loaded["Label"] == "herdr.signal-healthcheck.herdr-bridge"
    assert loaded["ProgramArguments"][0] == "/bin/herdr-commander"


def test_install_watchdog_creates_log_dir_if_missing(tmp_path):
    log_dir = tmp_path / "nested" / "log"
    assert not log_dir.exists()
    watchdog_service.install_watchdog(
        "herdr-bridge",
        launch_agents_dir=tmp_path / "LaunchAgents",
        log_dir=log_dir,
        herdr_commander_path="/bin/herdr-commander",
        platform_name="Darwin",
        runner=_fake_runner([]),
        path_env="", home_env="",
        uid=501,
    )
    assert log_dir.exists()


def test_install_watchdog_calls_launchctl_bootout_then_bootstrap(tmp_path):
    calls = []
    launch_agents_dir = tmp_path / "LaunchAgents"
    dest = watchdog_service.install_watchdog(
        "herdr-bridge",
        launch_agents_dir=launch_agents_dir,
        log_dir=tmp_path / "log",
        herdr_commander_path="/bin/herdr-commander",
        platform_name="Darwin",
        runner=_fake_runner(calls),
        path_env="", home_env="",
        uid=501,
    )
    assert calls[0][:2] == ["launchctl", "bootout"]
    assert "gui/501/herdr.signal-healthcheck.herdr-bridge" in calls[0]
    assert calls[1][:2] == ["launchctl", "bootstrap"]
    assert calls[1][2] == "gui/501"
    assert calls[1][3] == str(dest)


def test_install_watchdog_resolves_herdr_commander_via_which_when_path_omitted(tmp_path):
    seen = {}

    def _which(name):
        seen["name"] = name
        return "/opt/homebrew/bin/herdr-commander"

    watchdog_service.install_watchdog(
        "herdr-bridge",
        launch_agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "log",
        herdr_commander_path=None,
        which=_which,
        platform_name="Darwin",
        runner=_fake_runner([]),
        path_env="", home_env="",
        uid=501,
    )
    assert seen["name"] == "herdr-commander"


# -- uninstall_watchdog -----------------------------------------------------------

def test_uninstall_watchdog_raises_on_non_macos_platform(tmp_path):
    with pytest.raises(watchdog_service.WatchdogInstallError, match="macOS"):
        watchdog_service.uninstall_watchdog(
            "herdr-bridge",
            launch_agents_dir=tmp_path / "LaunchAgents",
            platform_name="Linux",
            runner=_fake_runner([]),
        )


def test_uninstall_watchdog_removes_existing_plist_and_returns_true(tmp_path):
    launch_agents_dir = tmp_path / "LaunchAgents"
    watchdog_service.install_watchdog(
        "herdr-bridge",
        launch_agents_dir=launch_agents_dir,
        log_dir=tmp_path / "log",
        herdr_commander_path="/bin/herdr-commander",
        platform_name="Darwin",
        runner=_fake_runner([]),
        path_env="", home_env="",
        uid=501,
    )
    dest = watchdog_service.plist_path("herdr-bridge", launch_agents_dir)
    assert dest.exists()

    removed = watchdog_service.uninstall_watchdog(
        "herdr-bridge",
        launch_agents_dir=launch_agents_dir,
        platform_name="Darwin",
        runner=_fake_runner([]),
        uid=501,
    )
    assert removed is True
    assert not dest.exists()


def test_uninstall_watchdog_returns_false_when_nothing_installed(tmp_path):
    removed = watchdog_service.uninstall_watchdog(
        "herdr-bridge",
        launch_agents_dir=tmp_path / "LaunchAgents",
        platform_name="Darwin",
        runner=_fake_runner([]),
        uid=501,
    )
    assert removed is False


def test_uninstall_watchdog_calls_launchctl_bootout(tmp_path):
    calls = []
    watchdog_service.uninstall_watchdog(
        "herdr-bridge",
        launch_agents_dir=tmp_path / "LaunchAgents",
        platform_name="Darwin",
        runner=_fake_runner(calls),
        uid=501,
    )
    assert calls[0][:2] == ["launchctl", "bootout"]
    assert "gui/501/herdr.signal-healthcheck.herdr-bridge" in calls[0]


# -- adversarial-review follow-ups (2026-08-02) ------------------------------

def _fake_runner_bootstrap_fails(stderr=b"launchctl: Could not bootstrap"):
    def _run(cmd, **kwargs):
        class _Result:
            def __init__(self, returncode, stderr):
                self.returncode = returncode
                self.stderr = stderr
                self.stdout = b""
        if cmd[1] == "bootstrap":
            return _Result(1, stderr)
        return _Result(0, b"")
    return _run


def test_install_watchdog_wraps_bootstrap_failure_as_watchdog_install_error(tmp_path):
    with pytest.raises(watchdog_service.WatchdogInstallError, match="Could not bootstrap"):
        watchdog_service.install_watchdog(
            "herdr-bridge",
            launch_agents_dir=tmp_path / "LaunchAgents",
            log_dir=tmp_path / "log",
            herdr_commander_path="/bin/herdr-commander",
            platform_name="Darwin",
            runner=_fake_runner_bootstrap_fails(),
            path_env="", home_env="",
            uid=501,
        )


def test_install_watchdog_bootstrap_failure_does_not_raise_calledprocesserror(tmp_path):
    """The caller (cli.py) only catches WatchdogInstallError -- a raw
    subprocess.CalledProcessError leaking through would print an unhandled
    traceback instead of a clean error message."""
    with pytest.raises(watchdog_service.WatchdogInstallError):
        watchdog_service.install_watchdog(
            "herdr-bridge",
            launch_agents_dir=tmp_path / "LaunchAgents",
            log_dir=tmp_path / "log",
            herdr_commander_path="/bin/herdr-commander",
            platform_name="Darwin",
            runner=_fake_runner_bootstrap_fails(),
            path_env="", home_env="",
            uid=501,
        )
    # (pytest.raises above already fails the test if a different exception type escapes)


def test_install_watchdog_wraps_mkdir_oserror_as_watchdog_install_error(tmp_path):
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.write_text("not a directory")  # pre-occupy the path with a file

    with pytest.raises(watchdog_service.WatchdogInstallError, match="plist"):
        watchdog_service.install_watchdog(
            "herdr-bridge",
            launch_agents_dir=launch_agents_dir,
            log_dir=tmp_path / "log",
            herdr_commander_path="/bin/herdr-commander",
            platform_name="Darwin",
            runner=_fake_runner([]),
            path_env="", home_env="",
            uid=501,
        )


def test_install_watchdog_raises_when_herdr_commander_path_is_relative(tmp_path):
    with pytest.raises(watchdog_service.WatchdogInstallError, match="absolute"):
        watchdog_service.install_watchdog(
            "herdr-bridge",
            launch_agents_dir=tmp_path / "LaunchAgents",
            log_dir=tmp_path / "log",
            herdr_commander_path="herdr-commander",  # bare command, not absolute
            platform_name="Darwin",
            runner=_fake_runner([]),
            path_env="", home_env="",
            uid=501,
        )


def test_install_watchdog_relative_path_rejection_happens_before_any_write(tmp_path):
    launch_agents_dir = tmp_path / "LaunchAgents"
    with pytest.raises(watchdog_service.WatchdogInstallError, match="absolute"):
        watchdog_service.install_watchdog(
            "herdr-bridge",
            launch_agents_dir=launch_agents_dir,
            log_dir=tmp_path / "log",
            herdr_commander_path="./herdr-commander",
            platform_name="Darwin",
            runner=_fake_runner([]),
            path_env="", home_env="",
            uid=501,
        )
    assert not launch_agents_dir.exists()


def test_install_watchdog_rejects_non_positive_interval_sec(tmp_path):
    with pytest.raises(watchdog_service.WatchdogInstallError, match="interval_sec"):
        watchdog_service.install_watchdog(
            "herdr-bridge",
            launch_agents_dir=tmp_path / "LaunchAgents",
            log_dir=tmp_path / "log",
            herdr_commander_path="/bin/herdr-commander",
            interval_sec=0,
            platform_name="Darwin",
            runner=_fake_runner([]),
            path_env="", home_env="",
            uid=501,
        )


def test_install_watchdog_sets_plist_file_permissions_to_owner_only(tmp_path):
    dest = watchdog_service.install_watchdog(
        "herdr-bridge",
        launch_agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "log",
        herdr_commander_path="/bin/herdr-commander",
        platform_name="Darwin",
        runner=_fake_runner([]),
        path_env="", home_env="",
        uid=501,
    )
    mode = dest.stat().st_mode & 0o777
    assert mode == 0o600


def test_uninstall_watchdog_wraps_unlink_oserror_as_watchdog_install_error(tmp_path):
    launch_agents_dir = tmp_path / "LaunchAgents"
    watchdog_service.install_watchdog(
        "herdr-bridge",
        launch_agents_dir=launch_agents_dir,
        log_dir=tmp_path / "log",
        herdr_commander_path="/bin/herdr-commander",
        platform_name="Darwin",
        runner=_fake_runner([]),
        path_env="", home_env="",
        uid=501,
    )
    launch_agents_dir.chmod(0o500)  # remove write permission on the containing dir
    try:
        with pytest.raises(watchdog_service.WatchdogInstallError, match="plist"):
            watchdog_service.uninstall_watchdog(
                "herdr-bridge",
                launch_agents_dir=launch_agents_dir,
                platform_name="Darwin",
                runner=_fake_runner([]),
                uid=501,
            )
    finally:
        launch_agents_dir.chmod(0o700)  # restore so pytest's tmp_path cleanup can remove it
