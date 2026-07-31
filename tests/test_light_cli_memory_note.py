# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `herdr-commander memory note` -- the CLI escape hatch for logging a
Herdr Bridge Memory note by hand, without needing to know the underlying
`remagraph` CLI's own invocation.
"""

from __future__ import annotations

from herdr_bridge.errors import HerdrMemoryError
from herdr_bridge.light import cli
from herdr_bridge.light.cli import build_parser, main


class _FakeMemory:
    def __init__(self, *, enabled=True, store_result=None, store_raises=None):
        self._enabled = enabled
        self._store_result = store_result if store_result is not None else {"status": "stored"}
        self._store_raises = store_raises
        self.store_calls = []

    def is_remagraph_enabled(self):
        return self._enabled

    def store_memory(self, task_id, agent_id, *, kind, summary, project_id, tags, learnings):
        self.store_calls.append(
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "kind": kind,
                "summary": summary,
                "project_id": project_id,
                "tags": tags,
                "learnings": learnings,
            }
        )
        if self._store_raises is not None:
            raise self._store_raises
        return self._store_result


def test_parser_memory_note_basic():
    p = build_parser()
    args = p.parse_args(
        ["memory", "note", "hello world", "--task-id", "t1", "--agent-id", "a1"]
    )
    assert args.command == "memory"
    assert args.memory_action == "note"
    assert args.message == "hello world"
    assert args.task_id == "t1"
    assert args.agent_id == "a1"
    assert args.project is None


def test_verbose_before_note_is_rejected_not_silently_dropped():
    """Regression test: -v used to be accepted (and silently clobbered back to
    False) when placed between "memory" and "note", because both the
    intermediate "memory" subparser and the leaf "note" subparser each
    defined their own -v/--verbose -- reproducing, one level deeper, the
    exact argparse subparser-clobbering hazard _verbose_parent()'s own
    docstring warns about. Now only "note" defines it, so this placement is
    a clean argparse error instead of a silent, placement-dependent feature
    failure.
    """
    import pytest

    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["memory", "-v", "note", "hello", "--task-id", "t1", "--agent-id", "a1"])


def test_verbose_after_note_still_works():
    p = build_parser()
    args = p.parse_args(
        ["memory", "note", "-v", "hello", "--task-id", "t1", "--agent-id", "a1"]
    )
    assert args.verbose is True


def test_memory_note_requires_task_id_and_agent_id():
    p = build_parser()
    import pytest

    with pytest.raises(SystemExit):
        p.parse_args(["memory", "note", "hello world"])


def test_memory_note_success_passes_non_empty_learnings(monkeypatch, capsys):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "note", "just finished this step", "--task-id", "t1", "--agent-id", "a1"])

    assert rc == 0
    assert len(fake.store_calls) == 1
    call = fake.store_calls[0]
    assert call["task_id"] == "t1"
    assert call["agent_id"] == "a1"
    assert call["summary"] == "just finished this step"
    # Regression: store_memory() requires a non-empty `learnings` list (RemaGraph
    # arbitration rejects an empty one with "learnings_empty") -- this call must
    # always pass at least one entry.
    assert call["learnings"] and call["learnings"][0]
    out = capsys.readouterr().out
    assert "memory note stored" in out


def test_memory_note_uses_resolved_project(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)

    main(["memory", "note", "msg", "--task-id", "t1", "--agent-id", "a1", "--project", "my-project"])

    assert fake.store_calls[0]["project_id"] == "my-project"


def test_memory_note_reports_backend_disabled(monkeypatch, capsys):
    fake = _FakeMemory(enabled=False)
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "note", "msg", "--task-id", "t1", "--agent-id", "a1"])

    assert rc == 1
    assert fake.store_calls == []
    err = capsys.readouterr().err
    assert "not available" in err.lower()


def test_memory_note_reports_rejection_without_raising(monkeypatch, capsys):
    # Regression test: store_memory() can return a rejection dict (not raise)
    # whose "detail"/"reason" text comes straight from the embedded backend
    # (e.g. a raw subprocess stderr or a TimeoutExpired's str(), which
    # deterministically embeds the literal word "remagraph"). This must not
    # be printed verbatim in default mode.
    fake = _FakeMemory(
        store_result={
            "status": "rejected",
            "reason": "summary_too_short",
            "detail": "Command '['remagraph', 'store', ...]' timed out after 10 seconds",
        }
    )
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "note", "msg", "--task-id", "t1", "--agent-id", "a1"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "failed to store memory note" in err
    assert "remagraph" not in err.lower()
    assert "run with -v" in err


def test_memory_note_verbose_shows_rejection_detail(monkeypatch, capsys):
    fake = _FakeMemory(store_result={"status": "rejected", "reason": "summary_too_short"})
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "note", "msg", "--task-id", "t1", "--agent-id", "a1", "-v"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "summary_too_short" in err


def test_memory_note_default_mode_hides_backend_error_detail(monkeypatch, capsys):
    fake = _FakeMemory(store_raises=HerdrMemoryError("clean failure message"))
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "note", "msg", "--task-id", "t1", "--agent-id", "a1"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "clean failure message" in err
    assert "remagraph" not in err.lower()


def test_memory_note_verbose_reraises(monkeypatch):
    import pytest

    fake = _FakeMemory(store_raises=HerdrMemoryError("clean failure message"))
    monkeypatch.setattr(cli, "_rg", fake)

    with pytest.raises(HerdrMemoryError):
        main(["memory", "note", "msg", "--task-id", "t1", "--agent-id", "a1", "-v"])
