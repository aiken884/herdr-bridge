import os


def test_isolate_remagraph_state_dir_also_isolates_registry_home(tmp_path):
    """Regression test (2026-08-15, found via `remagraph doctor --project herdr-bridge`
    surfacing a real polluted registry entry on Aiken's machine, pointing at a
    long-deleted pytest tmp_path from a prior test run).

    conftest.py's autouse `_isolate_remagraph_state_dir` fixture isolates HOME so
    that state-dir resolution (based on `Path.home()`) stays inside tmp_path, but
    RemaGraph's shared cross-project registry (`project_registry.json`) resolves
    its location via a *separate* env var, `REMAGRAPH_HOME`, independent of HOME
    (see RemaGraph CHANGELOG 0.3.0-alpha's `REMAGRAPH_HOME` entry) -- so a test
    calling `store_memory()`/`search_memories()` without REMAGRAPH_HOME also
    isolated silently registers its own tmp_path into the real, shared,
    developer-machine-global registry file, which then outlives the test (the
    registry entry references a path that no longer exists once tmp_path is
    cleaned up).

    This test only asserts the isolation itself (both env vars point inside
    tmp_path) -- it does not need to exercise RemaGraph's actual registry-write
    behavior to catch a regression of the isolation gap itself.
    """
    assert os.environ.get("HOME") == str(tmp_path)
    assert os.environ.get("REMAGRAPH_HOME") == str(tmp_path)
