import pytest

from herdr_bridge.tower.charter import resolve_charter_path


def test_neither_env_set_returns_none(monkeypatch):
    monkeypatch.delenv("HERDR_TOWER_PROMPT", raising=False)
    monkeypatch.delenv("HERDR_ORG_ROOT", raising=False)
    assert resolve_charter_path() is None


def test_explicit_path_set_but_missing_raises(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist.md"
    monkeypatch.setenv("HERDR_TOWER_PROMPT", str(missing))
    monkeypatch.delenv("HERDR_ORG_ROOT", raising=False)
    with pytest.raises(RuntimeError):
        resolve_charter_path()


def test_explicit_path_set_and_exists_returns_path(monkeypatch, tmp_path):
    charter = tmp_path / "tower-prompt.md"
    charter.write_text("# charter content")
    monkeypatch.setenv("HERDR_TOWER_PROMPT", str(charter))
    assert resolve_charter_path() == charter


def test_org_root_set_but_derived_path_missing_returns_none_not_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_TOWER_PROMPT", raising=False)
    monkeypatch.setenv("HERDR_ORG_ROOT", str(tmp_path))
    assert resolve_charter_path() is None


def test_org_root_set_and_derived_path_exists_returns_path(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_TOWER_PROMPT", raising=False)
    monkeypatch.setenv("HERDR_ORG_ROOT", str(tmp_path))
    charter_dir = tmp_path / "agents" / "leader"
    charter_dir.mkdir(parents=True)
    (charter_dir / "tower-prompt.md").write_text("# charter content")
    assert resolve_charter_path() == charter_dir / "tower-prompt.md"
