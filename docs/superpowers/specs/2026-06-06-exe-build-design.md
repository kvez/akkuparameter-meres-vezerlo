# Design: Hordozható exe build — Akkuteszter

Dátum: 2026-06-06  
Státusz: Jóváhagyva

---

## Összefoglaló

PyInstaller onedir módban hordozható Windows exe-t készítünk az Akkuteszter GUI-jából. A cél: a `dist/akkuteszter/` mappát átmásolni egy másik Windows PC-re, NI-VISA telepítése után azonnal futtatható legyen — Python, pip, kézzel telepített csomagok nélkül.

---

## Scope

| Feladat | Benne van |
|---------|-----------|
| PyInstaller spec fájl | ✅ |
| First-run local_config.yaml auto-copy | ✅ |
| Eszközkereső gomb + dialógus a GUI-ban | ✅ |
| Telepítési útmutató (INSTALL.md a dist mellé) | ✅ |
| Automatikus NI-VISA telepítő | ❌ (OS driver, nem bundolható) |
| connection_test önálló exe | ❌ (GUI-ból gomb) |
| Onefile csomagolás | ❌ (onedir a config miatt) |

---

## 1. Könyvtárstruktúra a build után

```
dist/
└── akkuteszter/
    ├── akkuteszter.exe          ← belépési pont
    ├── local_config.yaml        ← auto-létrehozva első futásnál (template alapján)
    ├── INSTALL.md               ← NI-VISA prerekvizit + első futtatás útmutató
    ├── Mérések/                 ← session logok ide kerülnek (auto-létrehozva)
    ├── _internal/               ← PyInstaller runtime, dll-ek, PySide6, stb.
    └── [egyéb PyInstaller fájlok]
```

A `local_config.yaml` és `Mérések/` az exe-vel azonos mappában jönnek létre (`sys._MEIPASS` helyett az exe könyvtára, onedir esetén ezek egybeesnek).

---

## 2. PyInstaller spec fájl

**Fájl:** `akkuteszter.spec` (projekt gyökerében, verziókövetett)

**Kulcselemek:**

- `Analysis` — belépési pont: `Prog/main.py`
- `datas` — beágyazott adatfájlok:
  - `Prog/config/default_config.yaml` → `Prog/config/`
  - `Prog/config/FIAMM_12V.yaml` → `Prog/config/`
  - `Prog/config/FIAMM_24V.yaml` → `Prog/config/`
  - `Prog/config/local_config.template.yaml` → `Prog/config/`
- `hiddenimports` — pyvisa, pyvisa-py (ha LAN DMM-hez szükséges), pyqtgraph
- `windowed=True` — nincs konzolablak
- `name='akkuteszter'`
- `onedir=True` (implicit, nem onefile)

**PySide6 megjegyzés:** A PyInstaller PySide6 hook automatikusan kezeli a Qt plugin másolást. Ha mégsem, explicit `collect_all('PySide6')` szükséges.

---

## 3. First-run local_config.yaml auto-copy

**Hol:** `Prog/main.py` indítási logikájában, az alkalmazás létrehozása előtt.

**Logika:**

```
exe_dir = Path(sys.executable).parent   # onedir: exe mappája
config_path = exe_dir / "local_config.yaml"

if not config_path.exists():
    template = _find_bundled_file("Prog/config/local_config.template.yaml")
    shutil.copy(template, config_path)
```

**Bundled fájl elérése:** PyInstaller `sys._MEIPASS` alá csomagolja a `datas`-t. Fejlesztői módban (`sys._MEIPASS` nem létezik) a projekt gyökeréből olvas. Egy `_resource_path(relative)` helper kezeli mindkét esetet.

**ConfigPanel hatása:** A `ConfigPanel` jelenleg `local_config.yaml`-t keres. Az elérési út logikáját frissíteni kell: `exe_dir / "local_config.yaml"` (nem `Path("local_config.yaml")`).

---

## 4. Eszközkereső dialógus a GUI-ban

**Elhelyezés:** Konfiguráció tab — "Eszközök keresése..." gomb a resource string mezők felett.

**Működés:**

1. Gombnyomásra modális `QDialog` nyílik: "Keresés folyamatban..."
2. Háttérszálban fut (`QThread` + worker objektum — a projektben már használt minta):
   - `pyvisa.ResourceManager().list_resources()` — minden elérhető VISA resource
   - Minden resource-ra: IDN lekérés (timeout: 2s), hiba esetén SKIP
3. Eredmény lista a dialógusban:
   ```
   USB0::0x05E6::0x2220::...::INSTR  →  KEITHLEY 2220-30-1
   USB0::0x05E6::0x2380::...::INSTR  →  KEITHLEY 2380-120-60
   TCPIP::192.168.x.x::INSTR         →  KEYSIGHT 34465A
   ```
4. Nincs auto-kitöltés a mezőkbe — a user manuálisan másolja be a resource stringet. (Szándékos: a user tudja, melyik melyik.)
5. A dialógus "Bezár" gombbal zárható.

**Biztonság:** A keresés NEM küld `OUTPUT ON` vagy `INPUT ON` parancsot — csak `*IDN?`-t. Safe_off sem szükséges, mert semmit nem kapcsol be.

**Hiba eset:** Ha `pyvisa` nem importálható (NI-VISA nincs telepítve), a dialógus ezt jelzi: "NI-VISA nem található. Telepítsd a National Instruments VISA driverét."

---

## 5. INSTALL.md

A `dist/akkuteszter/` mappába kerül: PyInstaller `datas`-ban szerepel (`INSTALL.md` → `.`), így a spec egyetlen build paranccsal kezeli.

**Tartalom:**
- NI-VISA letöltési link (ni.com/visa) + minimális verzió
- USB driver megjegyzés (Keithley USBTMC Windows-on NI-VISA-t igényel)
- Első futtatás lépései:
  1. NI-VISA telepítés + reboot
  2. USB eszközök bedugása
  3. `akkuteszter.exe` futtatása → `local_config.yaml` auto-létrejön
  4. Konfiguráció tab → "Eszközök keresése" gomb
  5. Resource string-ek bemásolása a `local_config.yaml`-ba (vagy GUI mezőkbe)
  6. Teszt indítása

---

## 6. Érintett fájlok

| Fájl | Módosítás típusa |
|------|-----------------|
| `akkuteszter.spec` | Új fájl |
| `Prog/main.py` | First-run config logika hozzáadása |
| `Prog/gui/panels/config_panel.py` | `local_config.yaml` elérési út fix (exe-relatív) |
| `Prog/gui/panels/device_search_dialog.py` | Új fájl — eszközkereső dialógus |
| `Prog/gui/main_window.py` | "Eszközök keresése" gomb bekötése |
| `INSTALL.md` | Új fájl (dist mappába kerül) |

---

## 7. Tesztelési terv

- `python -m compileall Prog` — importálható-e minden modul
- `pyinstaller akkuteszter.spec` — sikeres build?
- Onedir mappa futtatása fejlesztői gépen: GUI megnyílik, minden tab látható
- `local_config.yaml` hiányában auto-létrejön-e?
- Eszközkereső dialógus: NI-VISA nélkül helyes hibaüzenet-e?
- Eszközkereső dialógus: NI-VISA-val USB + LAN eszközök listázódnak-e?
- Teljes teszt target PC-n (NI-VISA telepített, Python nem telepített)

---

## 8. Ami NEM változik

- A meglévő `connection_test.py` fejlesztői eszköz marad (nem kerül az exe-be)
- A tesztcsomag (pytest) nem kerül az exe-be
- A mock driverek nem kerülnek az exe-be
- A GUI logikája, állapotgépek, driverek — semmi nem változik

---

## Nyitott kérdések

- **pyqtgraph:** PyInstaller hook lefedi-e? Ha nem, `collect_all('pyqtgraph')` szükséges a spec-ben.
- **pyvisa backend:** LAN DMM-ekhez pyvisa-py is kell (NI-VISA nélkül elérhető), vagy elég a NI-VISA backend? Valószínűleg NI-VISA backend elegendő (mindkét protokollt kezeli).
