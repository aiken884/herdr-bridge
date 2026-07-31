# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Herdr Bridge Memory branding tests.

Covers the "no RemaGraph name leak in default (non-`-v`/`--verbose`) user-visible
output" guarantee adopted from the PPLX design review, plus the env-var
deprecated-alias mechanism and the intentional REMAGRAPH_PROJECT bypass path.

Known, accepted exceptions to the leakage check (documented in
docs/memory-advanced.md, not tested here): `-v`/`--verbose` output, and
package metadata (`pip show` / wheel `Requires-Dist`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import herdr_bridge.orchestration.memory as rg
from herdr_bridge.errors import HerdrMemoryError
from herdr_bridge.light import cli
from herdr_bridge.light.cli import _resolve_memory_project, main
from tests.test_light_cli_doctor import _FakeResult, _install_healthy_doubles


def _clean(text: str) -> bool:
    return "remagraph" not in text.lower()


# ---------------------------------------------------------------------------
# doctor: every path's default output must stay clean
# ---------------------------------------------------------------------------


def test_doctor_healthy_path_output_is_clean(monkeypatch, tmp_path, capsys):
    _install_healthy_doubles(monkeypatch, tmp_path)
    main(["doctor"])
    captured = capsys.readouterr()
    assert _clean(captured.out)
    assert _clean(captured.err)


def test_doctor_missing_backend_output_is_clean(monkeypatch, tmp_path, capsys):
    _install_healthy_doubles(monkeypatch, tmp_path, which_map={"remagraph": None})
    main(["doctor"])
    captured = capsys.readouterr()
    assert _clean(captured.out)
    assert _clean(captured.err)


def test_doctor_search_failure_output_is_clean(monkeypatch, tmp_path, capsys):
    _install_healthy_doubles(
        monkeypatch, tmp_path,
        search_result=_FakeResult(returncode=1, stderr="connection refused"),
    )
    main(["doctor"])
    captured = capsys.readouterr()
    assert _clean(captured.out)
    assert _clean(captured.err)


def test_doctor_project_json_missing_output_is_clean(monkeypatch, tmp_path, capsys):
    """project.json not existing yet used to print the raw state_dir path,
    which contains the "remagraph-hb-live-..." directory prefix -- a real
    leak this test caught during implementation (fixed in cli.py)."""
    _install_healthy_doubles(monkeypatch, tmp_path)
    main(["doctor"])
    captured = capsys.readouterr()
    assert _clean(captured.out)
    assert _clean(captured.err)


def test_doctor_project_json_mismatch_output_is_clean(monkeypatch, tmp_path, capsys):
    state_dir = _install_healthy_doubles(monkeypatch, tmp_path)
    (state_dir / "project.json").write_text(
        json.dumps({"project_id": "some-other-project"}), encoding="utf-8"
    )
    main(["doctor", "--project", "herdr-bridge"])
    captured = capsys.readouterr()
    assert _clean(captured.out)
    assert _clean(captured.err)


def test_doctor_runaway_maintenance_loop_output_is_clean(monkeypatch, tmp_path, capsys):
    state_dir = _install_healthy_doubles(monkeypatch, tmp_path)
    audit_path = state_dir / f"audit-{time.strftime('%Y%m')}.jsonl"
    now = time.time()
    lines = [
        json.dumps({"action": "maintenance_completed", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - i))})
        for i in range(50)
    ]
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    main(["doctor"])
    captured = capsys.readouterr()
    assert _clean(captured.out)
    assert _clean(captured.err)


# ---------------------------------------------------------------------------
# main(): default mode hides the chain; -v/--verbose shows it (exempt)
# ---------------------------------------------------------------------------


def test_main_default_mode_prints_only_the_clean_message(monkeypatch, capsys):
    def _boom(_args):
        raise HerdrMemoryError("clean, user-facing failure message") from RuntimeError(
            "raw internal detail naming remagraph"
        )

    monkeypatch.setattr(cli, "cmd_doctor", _boom)
    rc = main(["doctor"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "clean, user-facing failure message" in err
    assert _clean(err)


def test_main_verbose_reraises_full_chain(monkeypatch):
    def _boom(_args):
        raise HerdrMemoryError("clean message") from RuntimeError("raw remagraph detail")

    monkeypatch.setattr(cli, "cmd_doctor", _boom)
    with pytest.raises(HerdrMemoryError):
        main(["doctor", "-v"])


# ---------------------------------------------------------------------------
# Module-import failure: end-to-end, in an isolated subprocess (a broken
# install makes `import herdr_bridge` itself fail, at package-import time --
# before any CLI argument parsing, so this can't be exercised in-process
# without reloading every module that already holds a reference to
# orchestration.memory; a subprocess with the backend genuinely blocked from
# importing is the safe way to trigger the real failure path end-to-end).
# ---------------------------------------------------------------------------


def test_import_failure_is_clean_end_to_end_when_backend_is_missing():
    repo_root = Path(__file__).resolve().parents[1]
    script = (
        "import sys, importlib.abc\n"
        "class _Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name == 'remagraph' or name.startswith('remagraph.'):\n"
        "            raise ImportError(f'simulated missing package: {name}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import herdr_bridge\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0, "import should fail when the backend package is missing"
    assert "HerdrMemoryError" in result.stderr
    assert _clean(result.stderr), (
        f"a broken install must not leak the backend's name in the crash output:\n{result.stderr}"
    )
    # No chained "During handling of the above exception..." / "direct cause"
    # text either -- from None must actually suppress the chain, not just
    # keep the message clean while still printing two tracebacks.
    assert "another exception occurred" not in result.stderr
    assert "direct cause" not in result.stderr


def test_import_failure_message_does_not_name_the_backend():
    """Supplementary static check on the raise site's own message text."""
    src_path = Path(__file__).resolve().parents[1] / "src" / "herdr_bridge" / "orchestration" / "memory.py"
    src = src_path.read_text(encoding="utf-8")
    match = re.search(
        r"except ImportError as _e:.*?raise HerdrMemoryError\(\s*(.*?)\)\s*from _e",
        src,
        re.DOTALL,
    )
    assert match, "could not locate the ImportError-handling raise site in orchestration/memory.py"
    assert _clean(match.group(1))


# ---------------------------------------------------------------------------
# Deprecated env var alias: HERDR_REMAGRAPH_MODE -> HERDR_MEMORY_MODE
# ---------------------------------------------------------------------------


def test_new_mode_env_var_takes_priority(monkeypatch):
    monkeypatch.setenv("HERDR_MEMORY_MODE", "on")
    monkeypatch.setenv("HERDR_REMAGRAPH_MODE", "off")
    assert rg._read_deprecated_or("HERDR_MEMORY_MODE", "auto") == "on"


def test_deprecated_mode_env_var_still_works_with_warning(monkeypatch):
    monkeypatch.delenv("HERDR_MEMORY_MODE", raising=False)
    monkeypatch.setenv("HERDR_REMAGRAPH_MODE", "off")
    with pytest.warns(DeprecationWarning, match="HERDR_REMAGRAPH_MODE"):
        result = rg._read_deprecated_or("HERDR_MEMORY_MODE", "auto")
    assert result == "off"


def test_mode_env_var_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("HERDR_MEMORY_MODE", raising=False)
    monkeypatch.delenv("HERDR_REMAGRAPH_MODE", raising=False)
    assert rg._read_deprecated_or("HERDR_MEMORY_MODE", "auto") == "auto"


# ---------------------------------------------------------------------------
# _resolve_memory_project: --project > HERDR_MEMORY_PROJECT > REMAGRAPH_PROJECT
# (direct REMAGRAPH_PROJECT is an intentional advanced-user bypass, not an
# error -- see docs/memory-advanced.md)
# ---------------------------------------------------------------------------


def test_resolve_memory_project_flag_wins(monkeypatch):
    monkeypatch.setenv("HERDR_MEMORY_PROJECT", "from-new-env")
    monkeypatch.setenv("REMAGRAPH_PROJECT", "from-old-env")
    assert _resolve_memory_project("from-flag") == "from-flag"


def test_resolve_memory_project_new_env_wins_over_old_env(monkeypatch):
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.setenv("HERDR_MEMORY_PROJECT", "from-new-env")
    monkeypatch.setenv("REMAGRAPH_PROJECT", "from-old-env")
    assert _resolve_memory_project(None) == "from-new-env"


def test_resolve_memory_project_old_env_bypass_still_works(monkeypatch):
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.setenv("REMAGRAPH_PROJECT", "from-old-env")
    assert _resolve_memory_project(None) == "from-old-env"


def test_resolve_memory_project_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)
    assert _resolve_memory_project(None) == "herdr-bridge"
