# Akkuparaméter mérés vezérlő

> **⚠️ FEJLESZTÉS ALATT — Work in Progress**
>
> A szoftver aktív fejlesztés és tesztelés alatt áll. Nem minden funkció működik még stabilan. Éles laborhasználat előtt tesztelj mock módban!

AGM/VRLA akkumulátor paraméter mérő és labor vezérlő szoftver.
Fő célja BQ34Z110PWR fuel gauge IC golden image előkészítéséhez szükséges fizikai akkumulátor ciklusok automatizált végrehajtása és dokumentálása.

---

## Képernyőképek

| Program | Program |
|---------|---------|
| ![Főablak 1](pic_prog_1.png) | ![Főablak 2](pic_prog_2.png) |

## Labor fotók

| | | |
|--|--|--|
| ![Labor 1](photo_2026-06-21_10-44-32.jpg) | ![Labor 2](photo_2026-06-21_10-44-33.jpg) | ![Labor 3](photo_2026-06-21_10-44-43.jpg) |

---

## Hardver

| Műszer | Szerepe | Kapcsolat |
|--------|---------|-----------|
| Keithley 2220-30-1 | Programozható tápegység (töltés) | USB (NI-VISA) |
| Keithley 2380-120-60 | Elektronikus terhelés (kisütés) | USB (NI-VISA) |
| Keysight 34465A | DMM — akkufeszültség mérés | LAN (SCPI) |

A tápegység és az elektronikus terhelés NI-VISA drivert igényel Windows alatt.
A DMM LAN-on, SCPI protokollon keresztül csatlakozik.

---

## Főbb funkciók

| Funkció | Állapot |
|---------|---------|
| Eszközök automatikus keresése (VISA scan) | ✅ Működik |
| DMM-alapú diódaesés-kompenzált töltés (CC/CV) | ✅ Működik |
| Kisütési ciklus elektronikus terheléssel | ✅ Működik |
| Relaxációs várakozás | ✅ Működik |
| OCV-SOC lépcsős mérés | ✅ Működik |
| Valós idejű mérés grafikon | ✅ Működik |
| Session log és CSV export | ✅ Működik |
| Checkpoint / folytatás megszakított mérés után | ✅ Működik |
| BQ34Z110PWR fizikai golden image ciklus | 🔧 Tesztelés alatt |
| Hőmérséklet kompenzáció (második DMM) | 🔧 Részleges |
| Impedancia jellegű mérés | 📋 Tervezett |

---

## Biztonság

A szoftver safety-first elvek szerint épül fel:

- Hiba esetén: elektronikus terhelés OFF → tápegység output OFF → relé bontás
- DMM feedback elvesztése esetén töltés azonnal megáll
- Minden esemény logolva van

---

## Telepítés és első futtatás

Részletes leírás: **[INSTALL.md](INSTALL.md)**

Rövid összefoglaló:
1. Telepítsd az **NI-VISA** drivert (kötelező az USB műszerekhez)
2. Indítsd újra a PC-t
3. Futtasd az `akkuteszter.exe`-t (első indításkor létrehozza a `local_config.yaml`-t)
4. **Konfiguráció → "Eszközök keresése…"** — azonosítsd a VISA resource stringeket
5. Írd be a stringeket a `local_config.yaml`-ba
6. Indítsd újra

### Futtatás forráskódból

```bash
pip install -r requirements.txt
python Prog/main.py
```

### EXE build (PyInstaller)

```bash
pip install pyinstaller
pyinstaller akkuteszter.spec
# Kimenet: dist/akkuteszter/akkuteszter.exe
```

---

## Tesztek futtatása

```bash
python -m pytest
```

Statikus ellenőrzések:

```bash
python -m ruff check Prog
python -m mypy Prog
```

A tesztek mock driverekkel futnak, fizikai műszer nem szükséges.

---

## Akkumulátor profilok

A `Prog/config/battery_profiles/` mappában YAML formátumú profilok találhatók:

- `FIAMM_12V.yaml` — FIAMM 12V AGM/VRLA
- `FIAMM_24V.yaml` — FIAMM 24V AGM/VRLA

---

## Projekt struktúra

```
Prog/
├── main.py                  # Belépési pont
├── drivers/                 # Műszer driverek (PSU, Load, DMM)
├── src/                     # Üzleti logika (töltés, kisütés, OCV, relax)
├── gui/                     # PySide6 GUI
├── config/                  # Alapértelmezett konfig, battery profilok
├── tests/                   # Pytest tesztek (mock driverekkel)
└── tools/                   # Segédeszközök (VISA kapcsolat teszt)
akkuteszter.spec             # PyInstaller build leírás
requirements.txt             # Python függőségek
INSTALL.md                   # Részletes telepítési útmutató
```

---

## Licenc

[MIT](LICENSE) — Copyright (c) 2026 kvez
