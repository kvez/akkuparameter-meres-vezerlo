"""
Kapcsolat-ellenőrző script — minden műszer IDN + safe_off teszt.
Akkumulátor nélkül futtatható; PSU OUTPUT soha nem kapcsol ON-ra.

Futtatás:
    python Prog/tools/connection_test.py
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml


# ------------------------------------------------------------------ #
# Config betöltés                                                      #
# ------------------------------------------------------------------ #

_ROOT = Path(__file__).parent.parent.parent

# Közvetlen script futtatáshoz: győződj meg róla, hogy a projekt gyökér a path-ban van
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))



def load_config(
    local_path: Optional[Path] = None,
    default_path: Optional[Path] = None,
) -> dict:
    """Betölti a local_config.yaml-t ha létezik, különben default_config.yaml-t."""
    if local_path is None:
        local_path = _ROOT / "Prog" / "config" / "local_config.yaml"
    if default_path is None:
        default_path = _ROOT / "Prog" / "config" / "default_config.yaml"
    path = local_path if local_path.exists() else default_path
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def is_placeholder(resource: str) -> bool:
    """Igaz, ha a resource string még nincs kitöltve (üres vagy PLACEHOLDER)."""
    return not resource or "PLACEHOLDER" in resource


# ------------------------------------------------------------------ #
# Eredmény nyilvántartás                                              #
# ------------------------------------------------------------------ #

_RESULTS: list[tuple[str, str, str]] = []


def _ok(name: str, msg: str) -> None:
    _RESULTS.append(("✅", name, msg))
    print(f"  ✅ {name:<8s}: {msg}")


def _skip(name: str, msg: str) -> None:
    _RESULTS.append(("⚠️", name, msg))
    print(f"  ⚠️ SKIP {name:<8s}: {msg}")


def _fail(name: str, msg: str) -> None:
    _RESULTS.append(("❌", name, msg))
    print(f"  ❌ FAIL {name:<8s}: {msg}")


# ------------------------------------------------------------------ #
# Instrument ellenőrzések                                             #
# ------------------------------------------------------------------ #

def check_psu(cfg: dict) -> None:
    from Prog.drivers.device_psu import Keithley2220PSU
    resource = cfg["instruments"]["psu"]["resource"]
    if is_placeholder(resource):
        _skip("PSU", f"resource nincs kitöltve: {resource}")
        return
    psu = Keithley2220PSU()
    try:
        psu.connect(resource)
        idn = psu.idn()
        _ok("PSU", idn.strip())
    except Exception as exc:
        _fail("PSU", str(exc))
    finally:
        psu.safe_off()
        psu.disconnect()


def check_load(cfg: dict) -> None:
    from Prog.drivers.device_load import Keithley2380Load
    resource = cfg["instruments"]["load"]["resource"]
    if is_placeholder(resource):
        _skip("Load", f"resource nincs kitöltve: {resource}")
        return
    load = Keithley2380Load()
    try:
        load.connect(resource)
        idn = load.idn()
        _ok("Load", idn.strip())
    except Exception as exc:
        _fail("Load", str(exc))
    finally:
        load.safe_off()
        load.disconnect()


def check_dmm_voltage(cfg: dict) -> None:
    from Prog.drivers.device_dmm import Keysight34465ADMM
    resource = cfg["instruments"]["dmm_voltage"]["resource"]
    if is_placeholder(resource):
        _skip("DMM_V", f"resource nincs kitöltve: {resource}")
        return
    dmm = Keysight34465ADMM()
    try:
        dmm.connect(resource)
        idn = dmm.idn()
        dmm.configure_dcv(range_V=100, nplc=1.0)
        v = dmm.read_voltage()
        _ok("DMM_V", f"{idn.strip()} | Mért: {v:.4f} V")
    except Exception as exc:
        _fail("DMM_V", str(exc))
    finally:
        dmm.disconnect()


def check_dmm_temperature(cfg: dict) -> None:
    from Prog.drivers.device_dmm import Keysight34465ADMM
    resource = cfg["instruments"]["dmm_temperature"]["resource"]
    if is_placeholder(resource):
        _skip("DMM_T", f"resource nincs kitöltve: {resource}")
        return
    dmm = Keysight34465ADMM()
    try:
        dmm.connect(resource)
        idn = dmm.idn()
        dmm.configure_temp_4wire_pt100(nplc=1.0)
        t = dmm.read_temperature()
        _ok("DMM_T", f"{idn.strip()} | Mért: {t:.2f} °C")
    except Exception as exc:
        _fail("DMM_T", str(exc))
    finally:
        dmm.disconnect()


# ------------------------------------------------------------------ #
# Resource discovery                                                  #
# ------------------------------------------------------------------ #

def discover_resources() -> None:
    print("\n=== VISA Resource Discovery ===")
    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
        resources = rm.list_resources()
        if resources:
            for i, r in enumerate(resources):
                print(f"  [{i}] {r}")
        else:
            print("  (nem található VISA eszköz)")
    except Exception as exc:
        print(f"  ⚠️ pyvisa nem elérhető: {exc}")


# ------------------------------------------------------------------ #
# Összefoglaló + main                                                 #
# ------------------------------------------------------------------ #

def print_summary() -> int:
    """Kiírja az összefoglalót, visszaadja az exit code-ot (0=OK/SKIP, 1=FAIL)."""
    print("\n=== Összefoglaló ===")
    fail_count = sum(1 for r in _RESULTS if r[0] == "❌")
    skip_count = sum(1 for r in _RESULTS if r[0] == "⚠️")
    ok_count = sum(1 for r in _RESULTS if r[0] == "✅")
    for status, name, msg in _RESULTS:
        print(f"  {status} {name:<8s}: {msg}")
    print(f"\n  OK: {ok_count}  SKIP: {skip_count}  FAIL: {fail_count}")
    return 1 if fail_count > 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Akkuteszter — Connection Test: műszer IDN + safe_off ellenőrzés"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="local_config.yaml elérési útja (alapértelmezett: Prog/config/local_config.yaml)",
    )
    args = parser.parse_args()

    _RESULTS.clear()
    print("=== Akkuteszter — Connection Test ===")
    discover_resources()
    try:
        cfg = load_config(local_path=args.config)
    except Exception as exc:
        print(f"\n❌ Config betöltési hiba: {exc}")
        return 1
    print("\n=== Instrument Checks ===")
    check_psu(cfg)
    check_load(cfg)
    check_dmm_voltage(cfg)
    check_dmm_temperature(cfg)
    return print_summary()


if __name__ == "__main__":
    # Windows konzolon UTF-8 kimenet biztosítása (csak közvetlen futtatáskor)
    if sys.platform == "win32":
        import io as _io
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
