# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""S-2: audit file O_NOFOLLOW protection -- writes must not follow a symlink path."""

import json
import os

from herdr_bridge.audit import AuditLogger


def test_audit_write_to_symlink_does_not_follow(tmp_path):
    """When the audit path is a symlink, the write must not pass through to the target file."""
    real_file = tmp_path / "real-audit.jsonl"
    real_file.write_text("")
    symlink_path = tmp_path / "symlink-audit.jsonl"
    os.symlink(real_file, symlink_path)

    log = AuditLogger(symlink_path)
    log.record("human:test", "ping")

    assert real_file.read_text() == "", (
        "O_NOFOLLOW should prevent writing through symlink")


def test_audit_normal_write_still_works(tmp_path):
    """A normal, non-symlink path write is unaffected."""
    log = AuditLogger(tmp_path / "audit.jsonl")
    log.record("human:test", "ping")
    lines = log.path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "ping"
