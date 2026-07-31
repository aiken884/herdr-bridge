# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for herdr-commander doctor -- a one-shot diagnostic for the global
install, the RemaGraph connection, project.json alignment, and maintenance
loop health. 2026-07-25 hard-won lesson: these four things previously had to
be tracked down one by one by hand; this consolidates them into a single
command.
"""

from __future__ import annotations

import json
import time

from herdr_bridge.light.cli import main


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_healthy_doubles(monkeypatch, tmp_path, *, which_map=None, search_result=None, search_raises=None):
    """Replace the three external dependencies doctor needs (shutil.which,
    subprocess.run, project_state_dir) with controlled fakes, healthy by
    default. Returns state_dir so individual tests can customize
    project.json/audit files.
    """
    state_dir = tmp_path / "remagraph-hb-live-herdr-bridge"
    state_dir.mkdir()

    default_which = {
        "herdr-commander": "/fake/bin/herdr-commander",
        "herdr": "/fake/bin/herdr",
        "remagraph": "/fake/bin/remagraph",
    }
    which_map = {**default_which, **(which_map or {})}

    def _fake_which(cmd):
        return which_map.get(cmd)

    def _fake_run(argv, **kwargs):
        if argv[:2] == ["remagraph", "search"]:
            if search_raises is not None:
                raise search_raises
            return search_result or _FakeResult(returncode=0, stdout='{"results": []}')
        raise AssertionError(f"unexpected subprocess.run call: {argv}")

    monkeypatch.setattr("herdr_bridge.light.cli.shutil.which", _fake_which)
    monkeypatch.setattr("herdr_bridge.light.cli.subprocess.run", _fake_run)
    monkeypatch.setattr("herdr_bridge.orchestration._state_paths.project_state_dir", lambda project_id: state_dir)
    return state_dir


def test_doctor_all_checks_pass_reports_healthy(monkeypatch, tmp_path, capsys):
    _install_healthy_doubles(monkeypatch, tmp_path)

    rc = main(["doctor"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "All checks passed" in out


def test_doctor_reports_missing_global_install(monkeypatch, tmp_path, capsys):
    _install_healthy_doubles(monkeypatch, tmp_path, which_map={"herdr-commander": None})

    rc = main(["doctor"])

    assert rc == 1, "when herdr-commander is not on PATH, it must report the problem with a nonzero exit code"
    err = capsys.readouterr().err
    assert "herdr-commander is not on PATH" in err


def test_doctor_reports_missing_remagraph_cli(monkeypatch, tmp_path, capsys):
    _install_healthy_doubles(monkeypatch, tmp_path, which_map={"remagraph": None})

    rc = main(["doctor"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Herdr Bridge Memory backend command not found" in err


def test_doctor_reports_remagraph_search_failure(monkeypatch, tmp_path, capsys):
    _install_healthy_doubles(
        monkeypatch, tmp_path,
        search_result=_FakeResult(returncode=1, stderr="connection refused"),
    )

    rc = main(["doctor"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Herdr Bridge Memory search failed" in err
    # Default mode: the raw backend stderr is not shown (may itself name the
    # backend) -- only -v/--verbose reveals it.
    assert "connection refused" not in err
    assert "run with -v" in err


def test_doctor_reports_remagraph_search_failure_verbose_shows_detail(monkeypatch, tmp_path, capsys):
    _install_healthy_doubles(
        monkeypatch, tmp_path,
        search_result=_FakeResult(returncode=1, stderr="connection refused"),
    )

    rc = main(["doctor", "-v"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Herdr Bridge Memory search failed" in err
    assert "connection refused" in err


def test_doctor_search_timeout_output_is_clean_by_default(monkeypatch, tmp_path, capsys):
    """Regression test: subprocess.run(..., timeout=10) raising
    subprocess.TimeoutExpired on the `remagraph search` probe used to be caught
    by cmd_doctor's blanket `except Exception` and printed unguarded -- and
    TimeoutExpired's default str() embeds the literal argv, including the word
    "remagraph" (e.g. "Command '['remagraph', 'search', ...]' timed out after
    10 seconds"). Default mode must not show this; -v/--verbose must."""
    import subprocess

    _install_healthy_doubles(
        monkeypatch, tmp_path,
        search_raises=subprocess.TimeoutExpired(cmd=["remagraph", "search"], timeout=10),
    )

    rc = main(["doctor"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "exception during Herdr Bridge Memory diagnostics" in err
    assert "remagraph" not in err.lower()
    assert "run with -v" in err


def test_doctor_search_timeout_verbose_shows_detail(monkeypatch, tmp_path, capsys):
    import subprocess

    _install_healthy_doubles(
        monkeypatch, tmp_path,
        search_raises=subprocess.TimeoutExpired(cmd=["remagraph", "search"], timeout=10),
    )

    rc = main(["doctor", "-v"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "exception during Herdr Bridge Memory diagnostics" in err
    assert "timed out" in err.lower()


def test_doctor_verbose_shows_backend_command_and_project_json_path(monkeypatch, tmp_path, capsys):
    """The intentionally-permitted -v/--verbose detail (backend command path,
    expected project.json path) must actually appear when asked for -- not
    just stay hidden in default mode."""
    _install_healthy_doubles(monkeypatch, tmp_path)

    rc = main(["doctor", "-v"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "backend command:" in out
    assert "expected at:" in out


def test_doctor_reports_project_json_mismatch(monkeypatch, tmp_path, capsys):
    """#66's specific check target: state_dir belongs to herdr-bridge, but
    project.json has a different project_id -- exactly the signal of an
    external serve process connecting and writing to the wrong place."""
    state_dir = _install_healthy_doubles(monkeypatch, tmp_path)
    (state_dir / "project.json").write_text(
        json.dumps({"project_id": "some-other-project"}), encoding="utf-8"
    )

    rc = main(["doctor", "--project", "herdr-bridge"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "project.json mismatch" in err
    assert "some-other-project" in err


def test_doctor_reports_healthy_when_project_json_matches(monkeypatch, tmp_path, capsys):
    state_dir = _install_healthy_doubles(monkeypatch, tmp_path)
    (state_dir / "project.json").write_text(
        json.dumps({"project_id": "herdr-bridge"}), encoding="utf-8"
    )

    rc = main(["doctor", "--project", "herdr-bridge"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "project.json matches correctly" in out


def test_doctor_reports_runaway_maintenance_loop(monkeypatch, tmp_path, capsys):
    """#66 hard-won lesson: an external remagraph serve's maintenance routine
    once ran wild in a short window and wiped cross-project data. doctor
    needs to catch this pattern from the audit log while it's still
    happening."""
    state_dir = _install_healthy_doubles(monkeypatch, tmp_path)
    audit_path = state_dir / f"audit-{time.strftime('%Y%m')}.jsonl"
    now = time.time()
    lines = []
    for i in range(50):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - i))
        lines.append(json.dumps({"action": "maintenance_completed", "timestamp": ts}))
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = main(["doctor"])

    assert rc == 1, "50 maintenance_completed entries in the past hour should be judged a runaway loop"
    err = capsys.readouterr().err
    assert "runaway cleanup loop" in err


def test_doctor_ignores_old_maintenance_entries_outside_window(monkeypatch, tmp_path, capsys):
    """A burst of maintenance_completed entries from two hours ago in the
    audit log should not count toward the "currently still running"
    judgment -- that's finished historical activity, not a current
    problem."""
    state_dir = _install_healthy_doubles(monkeypatch, tmp_path)
    audit_path = state_dir / f"audit-{time.strftime('%Y%m')}.jsonl"
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
    lines = [json.dumps({"action": "maintenance_completed", "timestamp": old_ts}) for _ in range(50)]
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = main(["doctor"])

    assert rc == 0, "old activity from two hours ago should not be misjudged as a currently running runaway loop"
    out = capsys.readouterr().out
    assert "maintenance loop healthy" in out
