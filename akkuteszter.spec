# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 6.20.0 — onedir build
import sys
sys.path.insert(0, '.')

from PyInstaller.utils.hooks import collect_all

pyqtgraph_datas, pyqtgraph_binaries, pyqtgraph_hiddenimports = collect_all("pyqtgraph")

a = Analysis(
    ["Prog/main.py"],
    pathex=["."],
    binaries=pyqtgraph_binaries,
    datas=[
        ("Prog/config/default_config.yaml",              "Prog/config"),
        ("Prog/config/local_config.template.yaml",       "Prog/config"),
        ("Prog/config/battery_profiles/FIAMM_12V.yaml",  "Prog/config/battery_profiles"),
        ("Prog/config/battery_profiles/FIAMM_24V.yaml",  "Prog/config/battery_profiles"),
        ("Prog/resources/psnd.png",                      "Prog/resources"),
        # Pico-firmware: a "Pico előkészítése BQ34Z110-hez" menüpont ezeket a
        # MicroPython fájlokat tölti fel a Pico-ra (l. Prog/app_paths.py::
        # pico_firmware_dir(), Prog/src/pico_provisioning_service.py). ADAT,
        # nem importálható modul — a `machine` modul csak a Picon létezik.
        ("Prog/pico_firmware/pico_bq_service.py",        "Prog/pico_firmware"),
        ("Prog/pico_firmware/pico_bq_rpc.py",            "Prog/pico_firmware"),
        ("Prog/pico_firmware/main.py",                   "Prog/pico_firmware"),
        ("INSTALL.md",                                   "."),
    ] + pyqtgraph_datas,
    hiddenimports=[
        "pyvisa",
        "pyvisa.resources",
        "pyvisa.resources.messagebased",
        "yaml",
    ] + pyqtgraph_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pip", "setuptools"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="akkuteszter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="akkuteszter",
)
