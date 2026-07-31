# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import json
import threading

from herdr_bridge.audit import AuditLogger


def test_record_writes_jsonl(tmp_path):
    log = AuditLogger(tmp_path / "audit.jsonl")
    log.record("actor-1", "send_to_agent", agent_id="term_1", priority=2)
    log.record("actor-2", "list_agents")
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["actor_id"] == "actor-1"
    assert first["action"] == "send_to_agent"
    assert first["priority"] == 2
    assert first["ts"].endswith("+00:00")


def test_concurrent_records_are_line_atomic(tmp_path):
    log = AuditLogger(tmp_path / "audit.jsonl")

    def worker(n: int) -> None:
        for i in range(50):
            log.record(f"actor-{n}", "ping", i=i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 200
    for line in lines:
        json.loads(line)  # every line is complete JSON


def test_audit_file_permissions_0600(tmp_path):
    log = AuditLogger(tmp_path / "sub" / "audit.jsonl")
    log.record("actor-1", "ping")
    assert (log.path.stat().st_mode & 0o777) == 0o600
    assert (log.path.parent.stat().st_mode & 0o777) == 0o700


def test_chmod_failure_is_swallowed_and_logged(tmp_path, monkeypatch, caplog):
    """A chmod failure (e.g. a read-only filesystem) must not fail the entire
    AuditLogger initialization -- it should only log a warning."""
    from pathlib import Path as _Path

    def fake_chmod(self, mode):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(_Path, "chmod", fake_chmod)

    with caplog.at_level("WARNING"):
        log = AuditLogger(tmp_path / "sub" / "audit.jsonl")

    assert log.path is not None
    assert any("cannot chmod" in r.message for r in caplog.records)


def test_actor_id_status_classification(tmp_path):
    """Governance memo v1.0 spec 1: actor_id_status classification."""
    log = AuditLogger(tmp_path / "audit.jsonl")
    log.record("human:aiken", "ping")            # valid -> field omitted
    log.record("", "ping")                        # empty
    log.record("Human:Aiken", "ping")             # malformed (uppercase)
    log.record("no-colon", "ping")                # malformed (no colon)
    log.record("system:intruder", "ping")         # reserved_violation
    log.record("system:bridge", "ping")           # valid (bridge itself)
    entries = [json.loads(line) for line in
               log.path.read_text().strip().splitlines()]
    assert "actor_id_status" not in entries[0]
    assert entries[1]["actor_id_status"] == "empty"
    assert entries[2]["actor_id_status"] == "malformed"
    assert entries[3]["actor_id_status"] == "malformed"
    assert entries[4]["actor_id_status"] == "reserved_violation"
    assert "actor_id_status" not in entries[5]


def test_write_failure_warns_but_does_not_raise(tmp_path, caplog):
    import logging
    log = AuditLogger(tmp_path / "audit.jsonl")
    log.record("actor-1", "ping")
    log.path.unlink()
    log.path.parent.chmod(0o500)  # read-only directory -> append fails
    try:
        with caplog.at_level(logging.WARNING, logger="herdr_bridge.audit"):
            log.record("actor-1", "ping")  # must not raise
        assert any("audit write failed" in r.message for r in caplog.records)
    finally:
        log.path.parent.chmod(0o700)


def test_trailing_newline_actor_id_is_malformed(tmp_path):
    """M1 gate CC9 regression: a trailing newline must not pass the actor_id
    format (\\Z anchoring)."""
    import json
    log = AuditLogger(tmp_path / "audit.jsonl")
    log.record("human:aiken\n", "ping")
    entry = json.loads(log.path.read_text().strip().splitlines()[-1])
    assert entry["actor_id_status"] == "malformed"


def test_get_audit_log_path_public_api(monkeypatch, tmp_path):
    # 0.1.1 Fix C: a public read-only API for consumers to get the audit path
    # -- no more reaching into the private AuditLogger().path coupling
    from pathlib import Path

    from herdr_bridge import get_audit_log_path
    from herdr_bridge.audit import AuditLogger

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    p = get_audit_log_path()
    assert isinstance(p, Path)
    assert p == AuditLogger().path      # matches the existing default path
