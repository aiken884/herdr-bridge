# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from herdr_bridge.testing import FakeHerdrServer

# Snapshot the real user HOME at conftest.py module load time (before any
# monkeypatch is applied), for use by the HF_HOME exception below — it can't
# be read lazily inside the fixture, since by then HOME may already have been
# swapped out by this file's own fixture.
_REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def _isolate_remagraph_state_dir(tmp_path, monkeypatch):
    """Prevent any test from writing to or deleting the real
    ~/.local/state/remagraph-* production database.

    Background (2026-07-25 postmortem): several tests called
    _ensure_remagraph_project(..., force_reinit=True) directly with a
    production project_id (e.g. "herdr-bridge"). That parameter unlinks the
    existing DB file outright — this once wiped and rebuilt the real
    ~/.local/state/remagraph-herdr-bridge/remagraph.db, leaving "the DB
    exists but the memories table has 0 rows". Separately, tests had left
    behind remagraph-* directories under ~/.local/state (127 of them found
    in practice, including things like remagraph-herdr-coord-<PID>) that
    vastly outnumbered real production data, and overran RemaGraph's
    cross-project query fan-out cap (20), causing a project that actually
    had data to get crowded out and come back empty.

    RemaGraph's state dir resolution (both herdr-bridge's own
    _ensure_remagraph_project and RemaGraph's own resolve_project_state_dir
    fallback branch) ultimately bases everything on Path.home(), so pointing
    HOME at each test's own tmp_path makes every dynamically computed state
    dir land inside that test's temp directory. It gets cleaned up along
    with tmp_path when the test ends, so individual test files don't each
    need to remember to isolate themselves manually.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "REMAGRAPH_STATE_DIR",
        "REMAGRAPH_PROJECT",
        "TASK_ID",
        "AGENT_ID",
        "HERDR_MEMORY_MODE",
        "HERDR_REMAGRAPH_MODE",
        "HERDR_MEMORY_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)
    # TASK_ID/AGENT_ID postmortem (2026-07-31): the use_router branch in
    # src/herdr_bridge/light/commander.py sets `os.environ["TASK_ID"] = ...`
    # directly (that's intentional production behavior, so that
    # route_via_acp_router reuses the same tid internally — not a bug), but
    # that write completely bypasses monkeypatch. If a test goes down that
    # branch and then fails an assertion before it gets a chance to clean up
    # (test_generate_and_ensure_task_ids once did exactly this), those two
    # env vars leak into every subsequent test in the same pytest process, so
    # any test calling ensure_task_ids() ends up reading the stale leaked
    # value instead of the new id it expected — and this only reproduces when
    # the full suite runs in a particular order; running that one test file
    # alone looks completely fine. Clearing these here, once, up front for
    # every test closes that gap, instead of relying on each test to
    # remember to del them at the end.

    # HuggingFace model cache exception (2026-07-25, #62 postmortem):
    # RemaGraph's semantic dedup loads minishlab/potion-multilingual-128M via
    # model2vec, whose default cache path is $HF_HOME (falling back to
    # ~/.cache/huggingface when unset). If HOME gets swapped for tmp_path
    # without special-casing this, that cache path gets swapped along with
    # it, turning every test into a cold start that has to make dozens of API
    # requests against huggingface.co to download/verify model files — which
    # can take tens of seconds and once tripped the post-commit hook test's
    # deliberate 10-second timeout guard, killing an otherwise-fine but
    # cold-starting call. Pinning HF_HOME to the real user cache directory
    # here lets tests reuse the model already downloaded on this machine —
    # that cache holds only public model weights, no user data, so it's
    # nothing like the ~/.local/state (real memory content) this fixture is
    # otherwise isolating against, and carries no contamination risk.
    monkeypatch.setenv(
        "HF_HOME", str(_REAL_HOME / ".cache" / "huggingface")
    )
    yield


@pytest.fixture()
def fake_herdr():
    with FakeHerdrServer() as srv:
        yield srv


def wait_until_true(cond, timeout=5.0):
    """Shared polling helper for tests (avoids every file rolling its own _wait_until)."""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False
