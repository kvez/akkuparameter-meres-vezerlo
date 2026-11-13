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


def test_bundle_dir_exe_mode_uses_meipass(monkeypatch, tmp_path):
    fake_meipass = tmp_path / "_internal"
    fake_meipass.mkdir()
    fake_exe = tmp_path / "akkuteszter.exe"
    fake_exe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_meipass), raising=False)
    from Prog import app_paths
    importlib.reload(app_paths)
    assert app_paths.bundle_dir() == fake_meipass


def test_local_config_template_path_dev_mode(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    from Prog import app_paths
    importlib.reload(app_paths)
    result = app_paths.local_config_template_path()
    assert result.name == "local_config.template.yaml"
    assert "Prog" in str(result) and "config" in str(result)


def test_ensure_local_config_copies_template(monkeypatch, tmp_path):
    config_path = tmp_path / "local_config.yaml"
    template_path = tmp_path / "local_config.template.yaml"
    template_path.write_text("instruments:\n  psu:\n    resource: PLACEHOLDER\n")

    import Prog.app_paths as ap
    monkeypatch.setattr(ap, "local_config_path", lambda: config_path)
    monkeypatch.setattr(ap, "local_config_template_path", lambda: template_path)

    ap.ensure_local_config()

    assert config_path.exists()
    assert "PLACEHOLDER" in config_path.read_text()


def test_ensure_local_config_does_not_overwrite_existing(monkeypatch, tmp_path):
    config_path = tmp_path / "local_config.yaml"
    config_path.write_text("my_custom_config: true\n")
    template_path = tmp_path / "local_config.template.yaml"
    template_path.write_text("instruments:\n  psu:\n    resource: PLACEHOLDER\n")

    import Prog.app_paths as ap
    monkeypatch.setattr(ap, "local_config_path", lambda: config_path)
    monkeypatch.setattr(ap, "local_config_template_path", lambda: template_path)

    ap.ensure_local_config()

    assert "my_custom_config" in config_path.read_text()


def test_real_template_has_no_unread_timeout_ms_keys():
    """[Kör 21 F17, ARC21-4(a), DFAM-026] a 4 `instruments.*.timeout_ms`
    kulcsnak SOHA nem volt YAML-olvasó fogyasztója -- mind a 4 driver
    (device_psu.py/device_load.py/device_dmm.py×2) a saját, kódba égetett
    alapértékét használja. Törölve a valós sablonból, NEM bekötve (az
    archívum döntése: vagy-vagy, nem mindkettő -- a bekötés ÚJ GUI/
    SessionConfig-funkciót igényelne, CLAUDE.md §10). Ez a teszt a VALÓS
    (nem szintetikus) sablonfájlt olvassa, hogy egy jövőbeli szerkesztés
    ne vezesse vissza csendben a holt kulcsokat."""
    import yaml

    from Prog import app_paths

    template = yaml.safe_load(
        app_paths.local_config_template_path().read_text(encoding="utf-8")
    )
    for instrument_name, instrument_cfg in template["instruments"].items():
        assert "timeout_ms" not in instrument_cfg, (
            f"instruments.{instrument_name}.timeout_ms visszakerult a "
            f"sablonba, de meg nincs YAML-olvaso fogyasztoja (l. ARC21-4(a))"
        )
