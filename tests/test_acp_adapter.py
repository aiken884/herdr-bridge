# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.adapter: unit tests for AcpxAdapter's three pure functions (TDD RED,
docs/acpx-adapter-implementation-plan.md §2.1)."""

from __future__ import annotations

import json
import os
import shlex
import stat
import time
from pathlib import Path

import pytest

from herdr_bridge.acp.adapter import (
    build_acpx_argv_and_env,
    build_acpx_policy_flags,
    build_opencode_permission_config,
    detect_target_triple,
    resolve_claude_binary,
    resolve_patched_opencode_binary,
    write_session_config,
)
from herdr_bridge.acp.errors import AcpAdapterError
from herdr_bridge.acp.models import AcpPolicy


class TestBuildOpencodePermissionConfig:
    def test_approve_all_maps_to_wildcard_allow(self):
        config = build_opencode_permission_config(AcpPolicy(mode="approve-all"))
        assert config == {"*": "allow"}

    def test_approve_reads_uses_wildcard_base_not_only_edit(self):
        config = build_opencode_permission_config(AcpPolicy(mode="approve-reads"))
        assert "*" in config
        assert config["*"] == "ask"
        assert config["read"] == "allow"
        assert config["glob"] == "allow"
        assert config["grep"] == "allow"
        assert config["list"] == "allow"

    def test_deny_all_uses_wildcard_base_not_only_edit(self):
        config = build_opencode_permission_config(AcpPolicy(mode="deny-all"))
        assert config["*"] == "deny"

    def test_deny_all_excludes_task_from_wildcard_deny(self):
        """PPLX B3: `*`:"deny" would also block `task` itself, preventing
        subagents from ever spawning."""
        config = build_opencode_permission_config(AcpPolicy(mode="deny-all"))
        assert config["task"] != "deny"

    def test_unknown_mode_fails_closed(self):
        with pytest.raises(AcpAdapterError):
            build_opencode_permission_config(AcpPolicy(mode="bogus-mode"))


class TestWriteSessionConfig:
    def test_writes_valid_json_file_under_session_dir(self, tmp_path: Path):
        policy = AcpPolicy(mode="approve-all")
        result = write_session_config(policy, session_dir=tmp_path)

        assert result.exists()
        assert result.parent == tmp_path
        content = json.loads(result.read_text())
        assert content == {"permission": {"*": "allow"}}

    def test_file_permissions_are_owner_only(self, tmp_path: Path):
        policy = AcpPolicy(mode="approve-all")
        result = write_session_config(policy, session_dir=tmp_path)

        mode = stat.S_IMODE(result.stat().st_mode)
        assert mode == 0o600

    def test_stale_orphan_files_are_cleaned_up_before_writing(self, tmp_path: Path):
        """PPLX B4: on startup, scan and clean up leftover config files from a
        previous run in the same session_dir."""
        orphan = tmp_path / "opencode-permission-stale123.json"
        orphan.write_text("{}")
        assert orphan.exists()

        write_session_config(AcpPolicy(mode="approve-all"), session_dir=tmp_path)

        assert not orphan.exists()

    def test_unrelated_files_in_session_dir_are_left_alone(self, tmp_path: Path):
        unrelated = tmp_path / "not-ours.json"
        unrelated.write_text("{}")

        write_session_config(AcpPolicy(mode="approve-all"), session_dir=tmp_path)

        assert unrelated.exists()

    def test_does_not_delete_a_file_with_mtime_at_or_after_call_start(self, tmp_path: Path):
        """Defense in depth: the primary line of defense against genuine
        concurrent calls is `AcpActions.ensure_session()`'s per-session_name
        lock (see actions.py), but this additionally verifies that orphan
        cleanup itself doesn't delete a file whose mtime falls "at or after
        this call started" — simulating a valid config that another
        concurrent call just wrote and acpx hasn't read yet (using a future
        timestamp to represent "not earlier than this call's start", so the
        test itself isn't flaky due to filesystem mtime resolution)."""
        fresh = tmp_path / "opencode-permission-fresh456.json"
        fresh.write_text("{}")
        future = time.time() + 5
        os.utime(fresh, (future, future))

        write_session_config(AcpPolicy(mode="approve-all"), session_dir=tmp_path)

        assert fresh.exists()

    def test_boundary_mtime_equal_to_call_start_is_not_treated_as_stale(self, tmp_path: Path, monkeypatch):
        """Boundary case: a file whose mtime is exactly equal to this call's
        start time doesn't count as "earlier than" and shouldn't be swept up
        as an orphan — this locks in the use of strict `<` rather than `<=`
        (`<=` would misclassify a valid file, written by a concurrent call
        landing on the exact same timestamp, as an orphan too)."""
        fixed_time = 1_700_000_000.0
        monkeypatch.setattr(time, "time", lambda: fixed_time)

        boundary = tmp_path / "opencode-permission-boundary999.json"
        boundary.write_text("{}")
        os.utime(boundary, (fixed_time, fixed_time))

        write_session_config(AcpPolicy(mode="approve-all"), session_dir=tmp_path)

        assert boundary.exists()

    def test_stale_file_removed_by_concurrent_call_does_not_raise(self, tmp_path: Path, monkeypatch):
        """Another concurrent call already deleted the same orphan file after
        our glob but before our unlink — FileNotFoundError should be
        swallowed, and must not make this call fail as collateral damage."""
        stale = tmp_path / "opencode-permission-stale789.json"
        stale.write_text("{}")
        old = time.time() - 10
        os.utime(stale, (old, old))

        original_unlink = Path.unlink

        def racy_unlink(self, *args, **kwargs):
            if self == stale:
                raise FileNotFoundError()
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", racy_unlink)

        result = write_session_config(AcpPolicy(mode="approve-all"), session_dir=tmp_path)

        assert result.exists()

    def test_cleans_up_temp_file_if_write_fails(self, tmp_path: Path, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(json, "dump", boom)

        with pytest.raises(RuntimeError):
            write_session_config(AcpPolicy(mode="approve-all"), session_dir=tmp_path)

        assert list(tmp_path.glob("opencode-permission-*.json")) == []


class TestBuildAcpxArgvAndEnv:
    def test_argv_uses_agent_escape_hatch(self, tmp_path: Path):
        agent_binary = tmp_path / "opencode"
        config_path = tmp_path / "config.json"
        argv, _env = build_acpx_argv_and_env(
            agent="opencode", agent_binary=agent_binary, config_path=config_path, cwd=tmp_path
        )

        assert argv[0] == "acpx"
        assert "--agent" in argv

    def test_argv_agent_command_is_shlex_quoted_for_paths_with_spaces(self, tmp_path: Path):
        """PPLX B2: paths containing spaces must go through shlex.quote(), or
        the acpx tokenizer would split/error on them.

        The test that actually proves acpx can correctly round-trip this
        escape sequence is left to the §2.2 integration tests (which run the
        full pipeline against a shared fixture with a space in its path,
        using real acpx) — this test only locks in the fact that the Python
        side's output uses shlex.quote(), without self-verifying via Python's
        own shlex.split() (the B2 mirror-image trap)."""
        agent_binary = tmp_path / "some dir" / "opencode"
        config_path = tmp_path / "config.json"
        argv, _env = build_acpx_argv_and_env(
            agent="opencode", agent_binary=agent_binary, config_path=config_path, cwd=tmp_path
        )

        agent_index = argv.index("--agent")
        agent_command = argv[agent_index + 1]
        expected_quoted = shlex.quote(str(agent_binary))
        assert agent_command == f"{expected_quoted} acp"

    def test_env_sets_opencode_config_to_config_path(self, tmp_path: Path):
        agent_binary = tmp_path / "opencode"
        config_path = tmp_path / "config.json"
        _argv, env = build_acpx_argv_and_env(
            agent="opencode", agent_binary=agent_binary, config_path=config_path, cwd=tmp_path
        )

        assert env["OPENCODE_CONFIG"] == str(config_path)

    def test_extra_env_cannot_override_opencode_config(self, tmp_path: Path):
        """extra_env overriding OPENCODE_CONFIG is a real permission-bypass
        path (PPLX add-on item)."""
        agent_binary = tmp_path / "opencode"
        config_path = tmp_path / "config.json"
        _argv, env = build_acpx_argv_and_env(
            agent="opencode",
            agent_binary=agent_binary,
            config_path=config_path,
            cwd=tmp_path,
            extra_env={"OPENCODE_CONFIG": "/tmp/evil.json", "FOO": "bar"},
        )

        assert env["OPENCODE_CONFIG"] == str(config_path)
        assert env["FOO"] == "bar"

    def test_does_not_mutate_real_os_environ(self, tmp_path: Path):
        agent_binary = tmp_path / "opencode"
        config_path = tmp_path / "config.json"
        marker = str(config_path)
        assert os.environ.get("OPENCODE_CONFIG") != marker

        _argv, env = build_acpx_argv_and_env(
            agent="opencode", agent_binary=agent_binary, config_path=config_path, cwd=tmp_path
        )

        assert env is not os.environ

    def test_disables_project_level_config_discovery(self, tmp_path: Path):
        """Discovered during adversarial review: opencode's config merge order
        continues past OPENCODE_CONFIG to also walk up to a project-level
        opencode.json/.opencode/opencode.json (searching parent directories),
        with mergeDeep letting the latter win — meaning a project's own
        config file could silently override the permission policy set here.
        Must be explicitly disabled.
        """
        agent_binary = tmp_path / "opencode"
        config_path = tmp_path / "config.json"
        _argv, env = build_acpx_argv_and_env(
            agent="opencode", agent_binary=agent_binary, config_path=config_path, cwd=tmp_path
        )

        assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"

    def test_strips_ambient_opencode_config_content(self, tmp_path: Path, monkeypatch):
        """OPENCODE_CONFIG_CONTENT is the last source to take effect in
        opencode's config merge chain (after OPENCODE_CONFIG), so if it were
        inherited from the caller's process it would directly overwrite the
        entire permission config. It must be actively stripped from the
        inherited environment — relying on the hope that no one ever sets it
        isn't good enough.
        """
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"permission":{"*":"allow"}}')
        agent_binary = tmp_path / "opencode"
        config_path = tmp_path / "config.json"
        _argv, env = build_acpx_argv_and_env(
            agent="opencode", agent_binary=agent_binary, config_path=config_path, cwd=tmp_path
        )

        assert "OPENCODE_CONFIG_CONTENT" not in env

    def test_extra_env_cannot_smuggle_in_opencode_config_content(self, tmp_path: Path):
        """extra_env also can't be used to bypass the previous rule — same
        class of bypass, must be blocked even if the caller passes it in
        deliberately."""
        agent_binary = tmp_path / "opencode"
        config_path = tmp_path / "config.json"
        _argv, env = build_acpx_argv_and_env(
            agent="opencode",
            agent_binary=agent_binary,
            config_path=config_path,
            cwd=tmp_path,
            extra_env={"OPENCODE_CONFIG_CONTENT": '{"permission":{"*":"allow"}}'},
        )

        assert "OPENCODE_CONFIG_CONTENT" not in env

    def test_unsupported_agent_raises_clear_error(self, tmp_path: Path):
        with pytest.raises(AcpAdapterError, match="unsupported agent 'gemini'"):
            build_acpx_argv_and_env(
                agent="gemini",
                agent_binary=tmp_path / "gemini",
                config_path=None,
                cwd=tmp_path,
            )


class TestDetectTargetTriple:
    def test_maps_darwin_arm64(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        assert detect_target_triple() == "darwin-arm64"

    def test_maps_linux_x86_64_to_x64(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        assert detect_target_triple() == "linux-x64"

    def test_maps_windows_amd64_to_x64(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr("platform.machine", lambda: "AMD64")
        assert detect_target_triple() == "windows-x64"


class TestResolvePatchedOpencodeBinary:
    def _manifest(self, tmp_path: Path, **overrides) -> Path:
        vendor_dir = tmp_path / "vendor"
        target_dir = vendor_dir / "darwin-arm64"
        target_dir.mkdir(parents=True)
        (target_dir / "opencode").write_text("#!/bin/sh\necho fake\n")
        manifest = {
            "target_triple": "darwin-arm64",
            "base_upstream_version": "1.18.3",
            "compatible_upstream_range": {"min_inclusive": "1.18.0", "max_inclusive": "1.18.99"},
        }
        manifest.update(overrides)
        (vendor_dir / "MANIFEST.json").write_text(json.dumps(manifest))
        return vendor_dir

    def test_resolves_binary_path_for_current_platform(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        vendor_dir = self._manifest(tmp_path)

        path, warnings = resolve_patched_opencode_binary(vendor_dir)

        assert path == vendor_dir / "darwin-arm64" / "opencode"
        assert warnings == []

    def test_missing_manifest_fails_closed(self, tmp_path: Path):
        vendor_dir = tmp_path / "empty-vendor"
        vendor_dir.mkdir()
        with pytest.raises(AcpAdapterError):
            resolve_patched_opencode_binary(vendor_dir)

    def test_missing_binary_for_platform_raises_clear_error(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        vendor_dir = self._manifest(tmp_path)  # only darwin-arm64 was built

        with pytest.raises(AcpAdapterError, match="linux-x64"):
            resolve_patched_opencode_binary(vendor_dir)

    def test_warns_when_base_version_outside_compatible_range(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        vendor_dir = self._manifest(tmp_path, base_upstream_version="1.25.0")

        _path, warnings = resolve_patched_opencode_binary(vendor_dir)

        assert len(warnings) == 1
        assert "1.25.0" in warnings[0]


class TestBuildAcpxPolicyFlags:
    """N4: acpx's own defense-in-depth decision — regardless of
    AcpPolicy.mode, always add `--non-interactive-permissions deny` first
    (not relying on whether the calling machine's own ~/.acpx/config.json
    happens to be set correctly; acpx's parser only accepts the two safe
    values deny/fail, but we pin it explicitly in our own argv rather than
    assume anything about the environment). approve-all/deny-all additionally
    stack the corresponding acpx flag (approve-all is "same permissions as
    today, zero regression"; deny-all is "not even task should be allowed to
    ask" — both are consistent with their mode's existing semantics, so
    stacking carries no surprise risk). approve-reads deliberately does not
    stack acpx's own --approve-reads — acpx's own heuristic for telling
    "is this a read" apart hasn't been verified, and stacking it recklessly
    could accidentally auto-allow a write that opencode's local config was
    supposed to block, which is riskier than relying on the already-verified
    non-interactive-deny default.
    """

    def test_always_includes_non_interactive_deny(self):
        for mode in ("approve-all", "approve-reads", "deny-all"):
            flags = build_acpx_policy_flags(AcpPolicy(mode=mode))
            assert "--non-interactive-permissions" in flags
            idx = flags.index("--non-interactive-permissions")
            assert flags[idx + 1] == "deny"

    def test_approve_all_adds_acpx_approve_all(self):
        flags = build_acpx_policy_flags(AcpPolicy(mode="approve-all"))
        assert "--approve-all" in flags

    def test_deny_all_adds_acpx_deny_all(self):
        flags = build_acpx_policy_flags(AcpPolicy(mode="deny-all"))
        assert "--deny-all" in flags

    def test_approve_reads_does_not_add_acpx_approve_reads(self):
        """Don't trust acpx's own --approve-reads read/write classification —
        leave it to opencode's local config's "*":"ask" plus the
        non-interactive default to handle, which has already been verified
        with real integration tests."""
        flags = build_acpx_policy_flags(AcpPolicy(mode="approve-reads"))
        assert "--approve-reads" not in flags

    def test_unknown_mode_fails_closed(self):
        with pytest.raises(AcpAdapterError):
            build_acpx_policy_flags(AcpPolicy(mode="bogus-mode"))


class TestResolveClaudeBinary:
    def test_uses_env_var_when_set(self, tmp_path: Path, monkeypatch):
        fake_claude = tmp_path / "fake-claude"
        fake_claude.write_text("#!/bin/sh\necho claude\n")
        fake_claude.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(fake_claude))

        path = resolve_claude_binary()

        assert path == fake_claude

    def test_env_var_points_to_nonexistent_path_raises(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_BIN", "/nonexistent/claude/binary")

        with pytest.raises(AcpAdapterError, match="does not exist"):
            resolve_claude_binary()

    def test_uses_which_when_env_var_not_set(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _name: "/fake/bin/claude")
        path = resolve_claude_binary()
        assert path is not None

    def test_not_found_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _name: None)

        with pytest.raises(AcpAdapterError, match="claude CLI not found"):
            resolve_claude_binary()


class TestBuildAcpxArgvAndEnvClaude:
    """Tests for build_acpx_argv_and_env's behavior with agent="claude": uses
    acpx's named subcommand form, and sets none of the opencode-specific env
    vars."""

    def test_argv_uses_cwd_flag_for_claude(self, tmp_path: Path):
        agent_binary = tmp_path / "claude"
        argv, _env = build_acpx_argv_and_env(
            agent="claude", agent_binary=agent_binary, config_path=None, cwd=tmp_path
        )

        assert argv == ["acpx", "--cwd", str(tmp_path)]
        assert "--agent" not in argv

    def test_no_opencode_env_vars_for_claude_agent(self, tmp_path: Path):
        agent_binary = tmp_path / "claude"
        _argv, env = build_acpx_argv_and_env(
            agent="claude", agent_binary=agent_binary, config_path=None, cwd=tmp_path
        )

        assert "OPENCODE_CONFIG" not in env
        assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in env
        assert "OPENCODE_CONFIG_CONTENT" not in env

    def test_extra_env_passed_through_for_claude(self, tmp_path: Path):
        agent_binary = tmp_path / "claude"
        _argv, env = build_acpx_argv_and_env(
            agent="claude",
            agent_binary=agent_binary,
            config_path=None,
            cwd=tmp_path,
            extra_env={"MY_CUSTOM_VAR": "hello"},
        )

        assert env.get("MY_CUSTOM_VAR") == "hello"

    def test_claude_argv_has_no_agent_flag(self, tmp_path: Path):
        agent_binary = tmp_path / "claude"
        argv, env = build_acpx_argv_and_env(
            agent="claude", agent_binary=agent_binary, config_path=None, cwd=tmp_path
        )

        assert "--agent" not in argv
        assert "--cwd" in argv
        assert "OPENCODE_CONFIG" not in env
