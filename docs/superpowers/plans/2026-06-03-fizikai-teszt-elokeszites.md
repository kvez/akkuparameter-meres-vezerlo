# Fizikai tesztelés előkészítése — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Három artifact létrehozása az első fizikai hardveres futtatás előkészítéséhez: `.gitignore` + config sablon + pre-flight checklist + connection test script.

**Architecture:** B megközelítés — elkülönített három artifact. A `connection_test.py` önálló szkript a `Prog/tools/` csomagban; a config sablon és checklist statikus fájlok. A szkript a meglévő drivereket (`Keithley2220PSU`, `Keithley2380Load`, `Keysight34465ADMM`) használja közvetlenül, GUI és TestRunner nélkül.

**Tech Stack:** Python 3.x, pyvisa, PyYAML, pytest

---

## Fájl struktúra

| Fájl | Változás |
|------|----------|
| `.gitignore` | Módosítás — `Prog/config/local_config.yaml` hozzáadása |
| `Prog/config/local_config.template.yaml` | Új — kitölthető resource string sablon |
| `Folyamatok/tervek/preflight_checklist.md` | Új — labor referencia dokumentum |
| `Prog/tools/__init__.py` | Új — package scaffold |
| `Prog/tools/connection_test.py` | Új — connection test szkript |
| `Prog/tests/tools/test_connection_test.py` | Új — pure funkciók tesztjei |

**Elvárt végeredmény:** 370 teszt zöld (365 + 5 új)

---

## Task 1: Statikus fájlok — `.gitignore`, config sablon, pre-flight checklist

**Files:**
- Modify: `.gitignore`
- Create: `Prog/config/local_config.template.yaml`
- Create: `Folyamatok/tervek/preflight_checklist.md`

Nincs unit teszt — statikus fájlok, kompileálhatóság és tartalom ellenőrzés elegendő.

- [ ] **Step 1: Bővítsd a `.gitignore`-t**

`.gitignore` végéhez add hozzá:

```
# Helyi műszer konfiguráció — gépenként eltérő, nem verziókövetett
Prog/config/local_config.yaml
```

- [ ] **Step 2: Hozd létre a `local_config.template.yaml`-t**

`Prog/config/local_config.template.yaml` tartalma:

```yaml
# ================================================================
# local_config.yaml — helyi műszer konfiguráció
# Másold át local_config.yaml névre és töltsd ki.
# Ez a fájl NINCS git-követve (lásd .gitignore).
#
# Használat:
#   1. cp Prog/config/local_config.template.yaml Prog/config/local_config.yaml
#   2. Töltsd ki a resource stringeket (connection_test.py segít megtalálni)
#   3. Állítsd be a PSU módot és erősítsd meg a fizikai bekötést
# ================================================================

instruments:
  psu:
    # USB eszköz — tipikus format: "USB0::0x05E6::0x2220::XXXXXXXX::INSTR"
    # Resource keresés: python Prog/tools/connection_test.py
    resource: "USB0::PLACEHOLDER::INSTR"
    timeout_ms: 5000
    # PSU mód: INDEPENDENT | PARALLEL | SERIES
    #   INDEPENDENT : 12V pack, CH1+CH2 külön, 30V/1.5A — nincs extra bekötés
    #   PARALLEL    : 12V pack, jumper CH1-CH2 között, 30V/3A — fizikai bekötés szükséges!
    #   SERIES      : 24V pack, 60V/1.5A — fizikai bekötés szükséges!
    combination_mode: INDEPENDENT
    # PARALLEL vagy SERIES módban kötelező True-ra állítani!
    hardware_wiring_confirmed: false

  load:
    # USB eszköz — tipikus format: "USB0::0x05E6::0x2380::XXXXXXXX::INSTR"
    resource: "USB0::PLACEHOLDER::INSTR"
    timeout_ms: 5000

  dmm_voltage:
    # LAN eszköz — format: "TCPIP0::192.168.X.X::inst0::INSTR"
    # Keysight 34465A — DCV mérés az akkukapocsokon (BY550 katód oldal)
    resource: "TCPIP0::192.168.X.X::inst0::INSTR"
    timeout_ms: 10000

  dmm_temperature:
    # LAN eszköz — format: "TCPIP0::192.168.X.X::inst0::INSTR"
    # Keysight 34465A — PT100 hőmérsékletmérés (4-wire FRTD mód)
    resource: "TCPIP0::192.168.X.X::inst0::INSTR"
    timeout_ms: 10000
```

- [ ] **Step 3: Hozd létre a `preflight_checklist.md`-t**

`Folyamatok/tervek/preflight_checklist.md` tartalma:

```markdown
# Pre-flight checklist — Első fizikai futtatás előkészítése

Ez a dokumentum az első hardveres futtatás előtti ellenőrzési lista.
Minden sor elvégzése után tedd ki a jelölést: `[x]`.

---

## 1. Hardver bekötés (PSU mód szerint)

### INDEPENDENT mód (12V pack, max 1.5A)

```
PSU CH1+ ──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH1– ────────────────────────────────► Akku–
PSU CH2: nincs bekötve / kikapcsolva
```

- [ ] CH1+ → BY550 anód bekötve
- [ ] BY550 katód → Akku+ bekötve
- [ ] CH1– → Akku– bekötve
- [ ] CH2 lekapcsolva / nem bekötve
- [ ] `local_config.yaml`: `combination_mode: INDEPENDENT`

### PARALLEL mód (12V pack, max 3A — fizikai jumper szükséges!)

```
PSU CH1+ ──┐
           ├──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH2+ ──┘  (jumper CH1+–CH2+ között)
PSU CH1– ──┐
           └────────────────────────────────► Akku–
PSU CH2– ──┘  (jumper CH1––CH2– között)
```

- [ ] Jumper CH1+–CH2+ bekötve
- [ ] Jumper CH1––CH2– bekötve
- [ ] BY550 bekötve a közös + kimenetről
- [ ] `local_config.yaml`: `combination_mode: PARALLEL`
- [ ] `local_config.yaml`: `hardware_wiring_confirmed: true`
- [ ] GUI-ban megerősítés elvégezve (PARALLEL mód váltáskor kötelező)

### SERIES mód (24V pack, max 1.5A — fizikai bekötés szükséges!)

```
PSU CH1+ ──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH1– ──► PSU CH2+ (belső összekötés)
PSU CH2– ────────────────────────────────► Akku–
```

- [ ] CH1– → CH2+ összekötve (soros bekötés)
- [ ] CH1+ → BY550 anód bekötve
- [ ] CH2– → Akku– bekötve
- [ ] `local_config.yaml`: `combination_mode: SERIES`
- [ ] `local_config.yaml`: `hardware_wiring_confirmed: true`
- [ ] GUI-ban megerősítés elvégezve (SERIES mód váltáskor kötelező)

---

## 2. BY550 dióda ellenőrzés

- [ ] Polaritás helyes: anód a PSU CH1+ felé, katód az Akku+ felé
- [ ] Vizuális integritás: nincs repedés, sérülés, érintkezési hiba
- [ ] 3A felett (PARALLEL mód): hűtőborda felszerelve
  - Referencia: @1.5A → ~0.60V esés, @3.0A → ~0.85V esés (mért, BY550 char.txt)
  - 3A-nél disszipáció: ~2.55W — hőelvezetés ajánlott

---

## 3. USB/LAN kapcsolat előkészítés

- [ ] PSU USB kábel bekötve a PC-hez
- [ ] Load USB kábel bekötve a PC-hez
- [ ] NI-VISA vagy pyvisa-py + pyusb telepítve (`pip install pyvisa pyvisa-py pyusb`)
- [ ] DMM1 (voltage) LAN IP beállítva a műszeren
- [ ] DMM2 (temperature) LAN IP beállítva a műszeren
- [ ] DMM1 IP ping-elható: `ping 192.168.X.X`
- [ ] DMM2 IP ping-elható: `ping 192.168.X.X`

---

## 4. `local_config.yaml` kitöltési ellenőrzés

- [ ] Fájl létezik: `Prog/config/local_config.yaml`
  - Létrehozás: `copy Prog\config\local_config.template.yaml Prog\config\local_config.yaml`
- [ ] PSU resource string kitöltve (nem PLACEHOLDER)
- [ ] Load resource string kitöltve (nem PLACEHOLDER)
- [ ] DMM_V resource string kitöltve (nem PLACEHOLDER)
- [ ] DMM_T resource string kitöltve (nem PLACEHOLDER)
- [ ] `combination_mode` egyezik a fizikai bekötéssel
- [ ] `hardware_wiring_confirmed: true` (PARALLEL/SERIES módban)

---

## 5. `connection_test.py` futtatása

```bash
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python Prog/tools/connection_test.py
```

Elvárt kimenet minden bekötött műszerre:
```
  ✅ PSU   : KEITHLEY INSTRUMENTS,MODEL 2220-30-1,...
  ✅ Load  : KEITHLEY INSTRUMENTS,MODEL 2380-120-60,...
  ✅ DMM_V : Keysight Technologies,34465A,... | Mért: X.XXXX V
  ✅ DMM_T : Keysight Technologies,34465A,... | Mért: XX.XX °C
```

- [ ] Minden műszer ✅ OK
- Ha `⚠️ SKIP`: töltsd ki a `local_config.yaml` resource stringjét
- Ha `❌ FAIL`: ellenőrizd USB/LAN kapcsolatot, VISA drivert, IP-t

---

## 6. Első futás előtti safety ellenőrzés

- [ ] Akkumulátor OCV feszültsége mérve DMM-mel (manuálisan)
  - Elvárt 12V pack: 11.8V–12.9V (töltöttségtől függően)
  - Elvárt 24V pack: 23.6V–25.8V
- [ ] Kábelbilincsek szorosan, rövidzár vizuálisan kizárva
- [ ] GUI-ban Safe Off / Emergency Stop gomb látható és ismert
- [ ] Első futáshoz ajánlott: kis kapacitású akku (≤12Ah), CHARACTERIZATION plan
```

- [ ] **Step 4: Compile check — YAML szintaxis**

```
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python -c "import yaml; yaml.safe_load(open('Prog/config/local_config.template.yaml'))"
```

Elvárt: nincs kivétel (sima futás)

- [ ] **Step 5: Commit**

```bash
git add .gitignore Prog/config/local_config.template.yaml "Folyamatok/tervek/preflight_checklist.md"
git commit -m "add: fizikai teszt előkészítés — gitignore + config sablon + pre-flight checklist"
```

---

## Task 2: `Prog/tools/` package + `connection_test.py`

**Files:**
- Create: `Prog/tools/__init__.py`
- Create: `Prog/tools/connection_test.py`
- Create: `Prog/tests/tools/__init__.py`
- Create: `Prog/tests/tools/test_connection_test.py`

- [ ] **Step 1: Hozd létre a package scaffold fájlokat**

```bash
echo.> Prog/tools/__init__.py
mkdir Prog/tests/tools
echo.> Prog/tests/tools/__init__.py
```

Windows PowerShell alternatíva:
```powershell
New-Item -ItemType File Prog/tools/__init__.py
New-Item -ItemType Directory Prog/tests/tools
New-Item -ItemType File Prog/tests/tools/__init__.py
```

- [ ] **Step 2: Írj 3 failing tesztet**

`Prog/tests/tools/test_connection_test.py` tartalma:

```python
"""Prog/tools/connection_test.py pure funkcióinak tesztjei."""
import pytest
from pathlib import Path
from unittest.mock import patch, mock_open
import yaml


class TestIsPlaceholder:
    def test_returns_true_for_placeholder_string(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("USB0::PLACEHOLDER::INSTR") is True

    def test_returns_false_for_real_resource(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("USB0::0x05E6::0x2220::1234::INSTR") is False

    def test_returns_true_for_tcpip_placeholder(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("TCPIP0::192.168.X.X::inst0::INSTR") is False
        assert is_placeholder("TCPIP0::PLACEHOLDER::inst0::INSTR") is True


class TestLoadConfig:
    def test_loads_default_config_when_no_local(self, tmp_path):
        from Prog.tools.connection_test import load_config
        default_data = {"instruments": {"psu": {"resource": "TEST"}}}
        default_path = tmp_path / "default_config.yaml"
        default_path.write_text(yaml.dump(default_data), encoding="utf-8")
        result = load_config(
            local_path=tmp_path / "local_config.yaml",   # nem létezik
            default_path=default_path,
        )
        assert result["instruments"]["psu"]["resource"] == "TEST"

    def test_loads_local_config_when_present(self, tmp_path):
        from Prog.tools.connection_test import load_config
        local_data = {"instruments": {"psu": {"resource": "LOCAL"}}}
        default_data = {"instruments": {"psu": {"resource": "DEFAULT"}}}
        local_path = tmp_path / "local_config.yaml"
        default_path = tmp_path / "default_config.yaml"
        local_path.write_text(yaml.dump(local_data), encoding="utf-8")
        default_path.write_text(yaml.dump(default_data), encoding="utf-8")
        result = load_config(local_path=local_path, default_path=default_path)
        assert result["instruments"]["psu"]["resource"] == "LOCAL"
```

- [ ] **Step 3: Futtasd — ellenőrizd a bukásokat**

```
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python -m pytest Prog/tests/tools/test_connection_test.py -v
```

Elvárt: 5 FAILED — `ModuleNotFoundError: No module named 'Prog.tools.connection_test'`

- [ ] **Step 4: Implementáld a `connection_test.py`-t**

`Prog/tools/connection_test.py` tartalma:

```python
"""
Kapcsolat-ellenőrző script — minden műszer IDN + safe_off teszt.
Akkumulátor nélkül futtatható; PSU OUTPUT soha nem kapcsol ON-ra.

Futtatás:
    python Prog/tools/connection_test.py
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional

import yaml


# ------------------------------------------------------------------ #
# Config betöltés                                                      #
# ------------------------------------------------------------------ #

_ROOT = Path(__file__).parent.parent.parent


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
    """Igaz, ha a resource string még nincs kitöltve."""
    return "PLACEHOLDER" in resource


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
        psu.safe_off()
        _ok("PSU", idn.strip())
    except Exception as exc:
        _fail("PSU", str(exc))
    finally:
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
        load.safe_off()
        _ok("Load", idn.strip())
    except Exception as exc:
        _fail("Load", str(exc))
    finally:
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
    """Kiírja az összefoglalót, visszaadja az exit code-ot (0=OK, 1=FAIL)."""
    print("\n=== Összefoglaló ===")
    fail_count = sum(1 for r in _RESULTS if r[0] == "❌")
    skip_count = sum(1 for r in _RESULTS if r[0] == "⚠️")
    ok_count = sum(1 for r in _RESULTS if r[0] == "✅")
    for status, name, msg in _RESULTS:
        print(f"  {status} {name:<8s}: {msg}")
    print(f"\n  OK: {ok_count}  SKIP: {skip_count}  FAIL: {fail_count}")
    return 1 if fail_count > 0 else 0


def main() -> int:
    _RESULTS.clear()
    print("=== Akkuteszter — Connection Test ===")
    discover_resources()
    try:
        cfg = load_config()
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
    sys.exit(main())
```

- [ ] **Step 5: Futtasd — 5 teszt zöld**

```
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python -m pytest Prog/tests/tools/test_connection_test.py -v
```

Elvárt: 5 passed.

- [ ] **Step 6: Teljes suite — 368 teszt zöld**

```
python -m pytest -v
```

Elvárt: 370 passed (365 + 3 `TestIsPlaceholder` + 2 `TestLoadConfig`).

Ha eltér, futtasd:
```
python -m pytest --collect-only -q
```

- [ ] **Step 7: Compile check**

```
python -m compileall Prog/tools/connection_test.py
```

Elvárt: `Compiling ... OK`

- [ ] **Step 8: Script dry-run (PLACEHOLDER resource stringekkel)**

```
python Prog/tools/connection_test.py
```

Elvárt kimenet (mind PLACEHOLDER esetén):
```
=== Akkuteszter — Connection Test ===

=== VISA Resource Discovery ===
  (nem található VISA eszköz)   ← vagy listázza az USB eszközöket

=== Instrument Checks ===
  ⚠️ SKIP PSU     : resource nincs kitöltve: USB0::PLACEHOLDER::INSTR
  ⚠️ SKIP Load    : resource nincs kitöltve: USB0::PLACEHOLDER::INSTR
  ⚠️ SKIP DMM_V   : resource nincs kitöltve: TCPIP0::192.168.X.X::inst0::INSTR
  ⚠️ SKIP DMM_T   : resource nincs kitöltve: TCPIP0::192.168.X.X::inst0::INSTR

=== Összefoglaló ===
  ⚠️ PSU     : resource nincs kitöltve: ...
  ...
  OK: 0  SKIP: 4  FAIL: 0
```

Elvárt exit code: 0 (nincs FAIL)

- [ ] **Step 9: Commit**

```bash
git add Prog/tools/__init__.py Prog/tools/connection_test.py
git add Prog/tests/tools/__init__.py Prog/tests/tools/test_connection_test.py
git commit -m "feat: Prog/tools/connection_test.py — VISA resource discovery + IDN + safe_off teszt"
```

---

## Gyors referencia — érintett sorok

| Fájl | Mit keress |
|------|-----------|
| `.gitignore` | vége → `Prog/config/local_config.yaml` |
| `local_config.template.yaml` | `combination_mode`, `hardware_wiring_confirmed`, resource stringek |
| `preflight_checklist.md` | ASCII bekötési ábrák, BY550 checkboxok, safety lista |
| `connection_test.py` | `is_placeholder()`, `load_config()`, `check_psu/load/dmm_*()` |
| `test_connection_test.py` | `TestIsPlaceholder`, `TestLoadConfig` |
