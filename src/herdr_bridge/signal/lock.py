# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Single-instance lock for the Signal daemon (design doc §3.4): prevents a
duplicate bootstrap run from starting two daemons that both bind the same
pane_id and fight each other.

Uses a POSIX advisory lock (`fcntl.flock`) on a dedicated lock file rather than
a PID file: `flock` is automatically released by the kernel when the holding
process exits or dies (crash, SIGKILL, machine sleep/wake) — no stale-PID
detection logic needed, which a PID-file scheme would otherwise require.
"""

from __future__ import annotations

import errno
import fcntl
from pathlib import Path
from types import TracebackType
from typing import Self


class DaemonAlreadyRunning(Exception):
    """Another process already holds the single-instance lock for this project."""


class SingleInstanceLock:
    """Context manager: `with SingleInstanceLock(lock_path): ...` — raises
    DaemonAlreadyRunning immediately (non-blocking) if another holder is live.
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fh: object | None = None

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately not a `with open(...)` block (ruff SIM115): the file
        # handle must outlive this method — it's held as instance state across
        # the acquire()/release() call boundary, not scoped to a local block.
        fh = open(self._lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DaemonAlreadyRunning(
                    f"another process already holds the Signal daemon lock at {self._lock_path}"
                ) from exc
            raise
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            self._fh.close()  # type: ignore[attr-defined]
            self._fh = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
