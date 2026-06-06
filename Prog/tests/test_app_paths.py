"""app_paths unit tesztek — dev mód és exe mód szimulációval."""
import sys
import importlib


def test_exe_dir_dev_mode_is_project_root(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    from Prog import app_paths
    importlib.reload(app_paths)
    result = app_paths.exe_dir()
    assert (result / "Prog").is_dir(), f"Várt projekt gyökér, kapott: {result}"


def test_exe_dir_exe_mode_returns_exe_parent(monkeypatch, tmp_path):
    fake_exe = tmp_path / "akkuteszter.exe"
    fake_exe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    from Prog import app_paths
    importlib.reload(app_paths)
    assert app_paths.exe_dir() == fake_exe.parent


def test_local_config_path_dev_mode_under_prog_config(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    from Prog import app_paths
    importlib.reload(app_paths)
    result = app_paths.local_config_path()
    assert result.name == "local_config.yaml"
    assert "Prog" in str(result) and "config" in str(result)


def test_local_config_path_exe_mode_next_to_exe(monkeypatch, tmp_path):
    fake_exe = tmp_path / "akkuteszter.exe"
    fake_exe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    from Prog import app_paths
    importlib.reload(app_paths)
    assert app_paths.local_config_path() == tmp_path / "local_config.yaml"


def test_default_config_path_exists_in_dev_mode(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    from Prog import app_paths
    importlib.reload(app_paths)
    assert app_paths.default_config_path().exists()
