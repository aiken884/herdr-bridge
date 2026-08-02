# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""signal.lock: single-instance advisory lock (design doc §3.4)."""

from __future__ import annotations

import multiprocessing
import os

import pytest

from herdr_bridge.signal.lock import DaemonAlreadyRunning, SingleInstanceLock


def test_acquire_and_release_round_trip(tmp_path):
    lock = SingleInstanceLock(tmp_path / "daemon.lock")
    lock.acquire()
    lock.release()
    # released, so a second acquire in the same process must succeed
    lock2 = SingleInstanceLock(tmp_path / "daemon.lock")
    lock2.acquire()
    lock2.release()


def test_context_manager_releases_on_exit(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    with SingleInstanceLock(lock_path):
        pass
    with SingleInstanceLock(lock_path):
        pass  # must not raise DaemonAlreadyRunning


def test_second_holder_in_same_process_is_rejected(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    first = SingleInstanceLock(lock_path)
    first.acquire()
    try:
        second = SingleInstanceLock(lock_path)
        with pytest.raises(DaemonAlreadyRunning):
            second.acquire()
    finally:
        first.release()


def _hold_lock_and_signal(lock_path: str, ready: multiprocessing.synchronize.Event, release: multiprocessing.synchronize.Event) -> None:
    lock = SingleInstanceLock(__import__("pathlib").Path(lock_path))
    lock.acquire()
    ready.set()
    release.wait(timeout=10)
    lock.release()


def test_lock_is_released_when_holding_process_dies(tmp_path):
    """flock is kernel-released on process death — no stale-lock detection needed."""
    lock_path = tmp_path / "daemon.lock"
    ctx = multiprocessing.get_context("spawn" if os.name != "posix" else "fork")
    ready = ctx.Event()
    release = ctx.Event()
    proc = ctx.Process(target=_hold_lock_and_signal, args=(str(lock_path), ready, release))
    proc.start()
    assert ready.wait(timeout=10)

    lock = SingleInstanceLock(lock_path)
    with pytest.raises(DaemonAlreadyRunning):
        lock.acquire()

    proc.kill()
    proc.join(timeout=10)

    # kernel released the flock when the process was killed; must succeed now
    lock2 = SingleInstanceLock(lock_path)
    lock2.acquire()
    lock2.release()
