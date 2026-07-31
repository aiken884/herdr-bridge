# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""actor_id audit log (mandatory per spec §4.3). JSONL, for incremental reads by a
future governance layer.

Security conventions (PPLX review R-08):
- File mode 0600, directory mode 0700 (may contain an operational trail, readable
  only by the owner).
- Callers pass only summary fields; full payload text is never persisted.
- On a write failure, log a warning and continue: the audit trail is a supporting
  record, and must never block the main operation.

actor_id format tiers (governance principles memo v1.0, rule 1):
- A valid format is `<category>:<name>` (all lowercase, exactly one colon, <=64
  chars) — no extra field is added.
- Anything else gets an actor_id_status field (empty/malformed/reserved_violation),
  but is still recorded and never raises — the tool layer doesn't do permission
  checks, it just flags entries for the governance layer to audit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("herdr_bridge.audit")

_ACTOR_ID_RE = re.compile(r"^[a-z][a-z0-9]*:[a-z0-9][a-z0-9\-]{0,61}\Z")  # \Z instead of $: rejects a trailing newline (review CC9)
_BRIDGE_ACTOR = "system:bridge"


def _actor_id_status(actor_id: str) -> str | None:
    """None = valid (field omitted); otherwise returns the tier string."""
    if actor_id == "":
        return "empty"
    if len(actor_id) > 64 or not _ACTOR_ID_RE.match(actor_id):
        return "malformed"
    if actor_id.startswith("system:") and actor_id != _BRIDGE_ACTOR:
        return "reserved_violation"
    return None


def _default_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME", "~/.local/state")
    return Path(state_home).expanduser() / "herdr-bridge" / "audit.jsonl"


def get_audit_log_path() -> Path:
    """The default audit log path (public read-only API, 0.1.1 Fix C).

    Consumers (e.g. downstream audit-viewing tools) should use this function to get
    the path, rather than reaching into AuditLogger().path — the internal layout is
    private and not guaranteed stable across a 0.2 refactor.
    Pure query, no side effects (doesn't create the directory or change permissions).
    """
    return _default_path()


class AuditLogger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else _default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            logger.warning("cannot chmod audit dir %s", self.path.parent)
        self._lock = threading.Lock()

    def record(self, actor_id: str, action: str, **fields: Any) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "actor_id": actor_id,
            "action": action,
            **fields,
        }
        status = _actor_id_status(actor_id)
        if status is not None:
            entry["actor_id_status"] = status
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                created = not self.path.exists()
                fd = os.open(
                    str(self.path),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
                if created:
                    self.path.chmod(0o600)
            except OSError as exc:
                logger.warning("audit write failed (%s); continuing without audit", exc)
