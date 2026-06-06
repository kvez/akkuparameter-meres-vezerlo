"""Path resolver — fejlesztői mód és PyInstaller exe mód egységes kezelése."""
from __future__ import annotations
import sys
from pathlib import Path


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def exe_dir() -> Path:
    """Exe mappa (onedir) — felhasználó által írható fájlokhoz (local_config, Mérések/)."""
    if _is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).parent.parent  # <project>/


def bundle_dir() -> Path:
    """Beágyazott (read-only) fájlok gyökere — sys._MEIPASS exe-ben, projekt gyökér devben."""
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else exe_dir()
    return Path(__file__).parent.parent  # <project>/


def local_config_path() -> Path:
    """local_config.yaml — exe mellett írható; dev módban Prog/config/ alatt."""
    if _is_frozen():
        return exe_dir() / "local_config.yaml"
    return Path(__file__).parent / "config" / "local_config.yaml"


def local_config_template_path() -> Path:
    """Beágyazott template — másolás forrása első futásnál."""
    return bundle_dir() / "Prog" / "config" / "local_config.template.yaml"


def default_config_path() -> Path:
    """Beágyazott default_config.yaml."""
    return bundle_dir() / "Prog" / "config" / "default_config.yaml"


def resources_dir() -> Path:
    """Beágyazott resources/ mappa (képek, ikonok)."""
    return bundle_dir() / "Prog" / "resources"
