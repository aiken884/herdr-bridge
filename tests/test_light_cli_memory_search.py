# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `herdr-commander memory search/status/maintain/link` -- the CLI
escape hatches added 2026-08-01 alongside `memory note`, so users can do
everything through `herdr-commander` without knowing the underlying
`remagraph` CLI (Aiken's product decision, see CLAUDE.md).
"""

from __future__ import annotations

from herdr_bridge.errors import HerdrMemoryError
from herdr_bridge.light import cli
from herdr_bridge.light.cli import build_parser, main


class _FakeMemory:
    def __init__(self, *, enabled=True, search_results=None, search_raises=None,
                 retention_result=None, retention_raises=None, link_raises=None):
        self._enabled = enabled
        self._search_results = search_results if search_results is not None else []
        self._search_raises = search_raises
        self._retention_result = retention_result if retention_result is not None else {"archived": 0, "dry_run": True, "policy": {}}
        self._retention_raises = retention_raises
        self._link_raises = link_raises
        self.search_calls = []
        self.retention_calls = []
        self.link_calls = []

    def is_remagraph_enabled(self):
        return self._enabled

    def search_memories(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        if self._search_raises is not None:
            raise self._search_raises
        return self._search_results

    def apply_retention(self, project_id, *, dry_run):
        self.retention_calls.append({"project_id": project_id, "dry_run": dry_run})
        if self._retention_raises is not None:
            raise self._retention_raises
        return self._retention_result

    def link_project(self, from_project, to_project, relation):
        self.link_calls.append((from_project, to_project, relation))
        if self._link_raises is not None:
            raise self._link_raises


# ---------------------------------------------------------------------------
# memory search
# ---------------------------------------------------------------------------


def test_parser_memory_search_basic():
    p = build_parser()
    args = p.parse_args(["memory", "search", "hello", "--top-k", "5", "--all-projects"])
    assert args.command == "memory"
    assert args.memory_action == "search"
    assert args.query == "hello"
    assert args.top_k == 5
    assert args.all_projects is True


def test_parser_memory_search_query_is_optional():
    p = build_parser()
    args = p.parse_args(["memory", "search"])
    assert args.query == ""


def test_memory_search_passes_filters_through(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)
    monkeypatch.delenv("HERDR_MEMORY_PROJECT", raising=False)
    monkeypatch.delenv("REMAGRAPH_PROJECT", raising=False)

    main([
        "memory", "search", "readme scorecard", "--kind", "task_handoff",
        "--task-id", "t1", "--agent-id", "a1", "--project", "my-project", "--all-projects",
    ])

    assert len(fake.search_calls) == 1
    call = fake.search_calls[0]
    assert call["query"] == "readme scorecard"
    assert call["kind"] == "task_handoff"
    assert call["task_id"] == "t1"
    assert call["agent_id"] == "a1"
    assert call["project_id"] == "my-project"
    assert call["all_projects"] is True


def test_memory_search_finds_and_prints_a_cross_project_record(monkeypatch, capsys):
    """The scenario this feature exists for: surfacing a record written by
    another tower via the standard namespace, tagged accordingly."""
    fake = _FakeMemory(search_results=[
        {"kind": "task_handoff", "task_id": "readme-request", "agent_id": "remagraph-command-tower",
         "timestamp": "2026-08-01T01:21:24Z", "summary": "please apply the readme change",
         "_namespace": "standard"},
    ])
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "search", "readme"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "readme-request" in out
    assert "remagraph-command-tower" in out
    assert "please apply the readme change" in out


def test_memory_search_no_results_reports_clearly(monkeypatch, capsys):
    fake = _FakeMemory(search_results=[])
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "search", "nonexistent"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no memories found" in out.lower()


def test_memory_search_tags_json_array_parsed(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)

    main(["memory", "search", "x", "--tags", '["a", "b"]'])

    assert fake.search_calls[0]["tags"] == ["a", "b"]


def test_memory_search_tags_comma_separated_parsed(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)

    main(["memory", "search", "x", "--tags", "a, b, c"])

    assert fake.search_calls[0]["tags"] == ["a", "b", "c"]


def test_memory_search_backend_disabled(monkeypatch, capsys):
    fake = _FakeMemory(enabled=False)
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "search", "x"])

    assert rc == 1
    assert fake.search_calls == []
    assert "not available" in capsys.readouterr().err.lower()


def test_memory_search_default_mode_hides_backend_error_detail(monkeypatch, capsys):
    fake = _FakeMemory(search_raises=HerdrMemoryError("clean failure message"))
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "search", "x"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "clean failure message" in err
    assert "remagraph" not in err.lower()


# ---------------------------------------------------------------------------
# memory status
# ---------------------------------------------------------------------------


def test_parser_memory_status_basic():
    p = build_parser()
    args = p.parse_args(["memory", "status", "--top-k", "3", "--kind", "status_update"])
    assert args.memory_action == "status"
    assert args.top_k == 3
    assert args.kind == "status_update"


def test_memory_status_calls_search_with_empty_query(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)

    main(["memory", "status", "--top-k", "7"])

    assert len(fake.search_calls) == 1
    assert fake.search_calls[0]["query"] == ""
    assert fake.search_calls[0]["top_k"] == 7


def test_memory_status_shows_namespace_per_entry(monkeypatch, capsys):
    fake = _FakeMemory(search_results=[
        {"kind": "task_handoff", "task_id": "t1", "agent_id": "a1", "timestamp": "2026-08-01T00:00:00Z",
         "summary": "isolated entry", "_namespace": "isolated"},
        {"kind": "task_handoff", "task_id": "t2", "agent_id": "a2", "timestamp": "2026-08-01T00:00:01Z",
         "summary": "standard entry", "_namespace": "standard"},
    ])
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "status"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "isolated" in out
    assert "standard" in out


# ---------------------------------------------------------------------------
# memory maintain
# ---------------------------------------------------------------------------


def test_parser_memory_maintain_defaults_to_dry_run():
    p = build_parser()
    args = p.parse_args(["memory", "maintain"])
    assert args.apply is False


def test_memory_maintain_dry_run_by_default(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)

    main(["memory", "maintain"])

    assert fake.retention_calls == [{"project_id": "herdr-bridge", "dry_run": True}]


def test_memory_maintain_apply_flag_disables_dry_run(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)

    main(["memory", "maintain", "--apply"])

    assert fake.retention_calls[0]["dry_run"] is False


def test_memory_maintain_reports_archived_count(monkeypatch, capsys):
    fake = _FakeMemory(retention_result={"archived": 3, "dry_run": True, "policy": {}})
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "maintain"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "3" in out
    assert "dry run" in out.lower()


def test_memory_maintain_backend_disabled(monkeypatch, capsys):
    fake = _FakeMemory(enabled=False)
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "maintain"])

    assert rc == 1
    assert fake.retention_calls == []


# ---------------------------------------------------------------------------
# memory link
# ---------------------------------------------------------------------------


def test_parser_memory_link_requires_all_three_args():
    import pytest

    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["memory", "link", "--from", "a"])


def test_parser_memory_link_rejects_unknown_relation():
    import pytest

    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["memory", "link", "--from", "a", "--to", "b", "--relation", "not-a-real-relation"])


def test_memory_link_success(monkeypatch, capsys):
    fake = _FakeMemory()
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "link", "--from", "herdr-bridge", "--to", "remagraph", "--relation", "depends_on"])

    assert rc == 0
    assert fake.link_calls == [("herdr-bridge", "remagraph", "depends_on")]
    out = capsys.readouterr().out
    assert "herdr-bridge" in out
    assert "remagraph" in out


def test_memory_link_invalid_relation_from_backend_reported_cleanly(monkeypatch, capsys):
    fake = _FakeMemory(link_raises=ValueError("invalid relation 'bogus'"))
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "link", "--from", "a", "--to", "b", "--relation", "sibling"])

    assert rc == 1
    assert "invalid relation" in capsys.readouterr().err


def test_memory_link_backend_disabled(monkeypatch, capsys):
    fake = _FakeMemory(enabled=False)
    monkeypatch.setattr(cli, "_rg", fake)

    rc = main(["memory", "link", "--from", "a", "--to", "b", "--relation", "sibling"])

    assert rc == 1
    assert fake.link_calls == []
