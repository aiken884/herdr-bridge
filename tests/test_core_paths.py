
from herdr_bridge.core.paths import resolve_project_root


def test_env_var_wins_over_everything(tmp_path, monkeypatch):
    target = tmp_path / "explicit-root"
    target.mkdir()
    monkeypatch.setenv("HERDR_PROJECT_ROOT", str(target))
    resolve_project_root.cache_clear()
    assert resolve_project_root() == target.resolve()


def test_config_toml_used_when_env_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PROJECT_ROOT", raising=False)
    root = tmp_path / "proj"
    (root / ".herdr").mkdir(parents=True)
    (root / ".herdr" / "config.toml").write_text('project_root = "."\n')
    monkeypatch.chdir(root)
    resolve_project_root.cache_clear()
    assert resolve_project_root() == root.resolve()


def test_walk_up_for_herdr_dir_when_no_config_key(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PROJECT_ROOT", raising=False)
    root = tmp_path / "proj"
    (root / ".herdr").mkdir(parents=True)
    sub = root / "src" / "pkg"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    resolve_project_root.cache_clear()
    assert resolve_project_root() == root.resolve()


def test_fallback_to_cwd_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("HERDR_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    resolve_project_root.cache_clear()
    with caplog.at_level("WARNING"):
        result = resolve_project_root()
    assert result == tmp_path.resolve()
    assert "project_root" in caplog.text
