# Fizikai tesztelés előkészítése — Design Spec

**Dátum:** 2026-06-03  
**Megközelítés:** B — elkülönített három artifact  
**Cél:** Az első fizikai hardveres futtatás előkészítése: instrument connectivity teszt, config sablon, pre-flight checklist

---

## Összefoglaló

A szoftver (365 teszt, mind zöld) készen áll. A fizikai tesztelés előkészítése három egymástól független artifactból áll:

| Artifact | Fájl | Cél |
|----------|------|-----|
| Connection test script | `Prog/tools/connection_test.py` | Műszer-kapcsolat ellenőrzés hardveren, IDN + safe_off |
| Config sablon | `Prog/config/local_config.template.yaml` | Kitölthető resource string sablon (nem git-követett éles verzió) |
| Pre-flight checklist | `Folyamatok/tervek/preflight_checklist.md` | Labor referencia dokumentum az első futás előtt |

Az első fizikai futtatás célja: **csak instrument connectivity** — akkumulátor nélkül, PSU OUTPUT OFF marad, csak IDN lekérés + safe_off teszt.

---

## 1. `Prog/tools/connection_test.py`

### Hatáskör

- Önállóan futtatható: `python Prog/tools/connection_test.py`
- Nem importál GUI-t, nem indít TestRunner-t
- Csak a négy driverre épít: `Keithley2220PSU`, `Keithley2380Load`, `Keysight34465ADMM` (×2)
- Nem állít be feszültséget/áramot, nem kapcsol OUTPUT ON-t

### Lépések sorban

**1. Resource discovery**  
`pyvisa.ResourceManager().list_resources()` — kilistázza az összes elérhető VISA eszközt (USB + LAN), számozva kiírja. Segít azonosítani a resource stringeket config kitöltéshez.

**2. Config betöltés**  
`local_config.yaml` ha létezik, különben `default_config.yaml`. A resource stringeket ebből olvassa.

**3. Instrument connect + IDN (sorban: PSU → Load → DMM_V → DMM_T)**  
Minden műszerre:
- Ha resource string `PLACEHOLDER` tartalmaz → ⚠️ SKIP, figyelmeztetés
- `connect()` hívás → sikeres: `*IDN?` lekérés → IDN string kiírva
- Exception esetén → ❌ FAIL + hibaüzenet

**4. Safe_off teszt**  
Minden sikeresen csatlakoztatott műszeren `safe_off()` hívás. Ez az első valós hardware interakció — PSU: `OUTP OFF`, Load: `INP OFF`, DMM: no-op.

**5. DMM alap mérés**  
- DMM1 (voltage): egyetlen `measure_voltage()` → kimenet: lebegő érték várható akkumulátor nélkül
- DMM2 (hőmérséklet): egyetlen `measure_temperature()` → szoba-hőmérséklet várható (~20–25°C)

**6. Összefoglaló táblázat**  
```
PSU      : ✅ OK  — KEITHLEY INSTRUMENTS,MODEL 2220-30-1,...
Load     : ✅ OK  — KEITHLEY INSTRUMENTS,MODEL 2380-120-60,...
DMM_V    : ⚠️ SKIP — resource string: PLACEHOLDER
DMM_T    : ❌ FAIL — ConnectionError: timeout
```

### Hibakezelés

- Minden instrument önálló try/except blokk — egy műszer hibája nem állítja meg a többit
- `disconnect()` minden instrument végén (finally blokk)
- Script exit code: 0 ha minden ✅, 1 ha bármely ❌

---

## 2. `Prog/config/local_config.template.yaml`

### Tartalom

Csak az eszközspecifikus szekciók, amiket a felhasználónak ki kell töltenie. A többi szekció (`timing`, `safety`, `series_diode`, `taper`, `dmm_voltage`) a `default_config.yaml`-ból öröklődik — nem szükséges másolni.

```yaml
# ================================================================
# local_config.yaml — helyi műszer konfiguráció
# Másold át local_config.yaml névre és töltsd ki.
# Ez a fájl NINCS git-követve (lásd .gitignore).
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
    # PARALLEL vagy SERIES módban kötelező True-ra állítani a fizikai bekötés megerősítéséhez!
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

### `.gitignore` kiegészítés

`Prog/config/local_config.yaml` sor hozzáadása — a kitöltött config nem kerül be a repositoryba.

---

## 3. `Folyamatok/tervek/preflight_checklist.md`

### Tartalom és szekciók

**1. Hardver bekötés (PSU mód szerint)**

Mindhárom módhoz ASCII bekötési ábra + checkbox lista:

*INDEPENDENT mód (12V pack):*
```
PSU CH1+ ──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH1– ─────────────────────────────────► Akku–
PSU CH2: kikapcsolt / nem bekötve
```

*PARALLEL mód (12V pack, 3A):*
```
PSU CH1+ ──┐
           ├──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH2+ ──┘  (jumper CH1+–CH2+ között)
PSU CH1– ──┐
           └──────────────────────────────► Akku–
PSU CH2– ──┘  (jumper CH1––CH2– között)
SCPI: INST:COMB PAR + GUI megerősítés kötelező
```

*SERIES mód (24V pack):*
```
PSU CH1+ ──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH1– ──► PSU CH2+ (belső bekötés)
PSU CH2– ──────────────────────────────► Akku–
SCPI: INST:COMB SER + GUI megerősítés kötelező
```

**2. BY550 dióda ellenőrzés**
- [ ] Polaritás: anód a PSU felé, katód az akku felé
- [ ] 3A felett (PARALLEL mód): hűtőborda felszerelve
- [ ] Vizuális integritás: nincs repedés, sérülés, érintkezési hiba
- [ ] Mért esés referencia: @1.5A → ~0.60V, @3.0A → ~0.85V (BY550 char.txt)

**3. USB/LAN kapcsolat előkészítés**
- [ ] PSU USB kábel bekötve
- [ ] Load USB kábel bekötve
- [ ] NI-VISA vagy pyvisa-py + pyusb telepítve
- [ ] DMM1 LAN IP beállítva, ping-elható
- [ ] DMM2 LAN IP beállítva, ping-elható
- [ ] `connection_test.py` futtatva → mind ✅

**4. `local_config.yaml` kitöltési ellenőrzés**
- [ ] Fájl létezik: `Prog/config/local_config.yaml`
- [ ] Nincs `PLACEHOLDER` resource string
- [ ] `combination_mode` egyezik a fizikai bekötéssel
- [ ] `hardware_wiring_confirmed: true` (PARALLEL/SERIES módban)

**5. Első futás előtti safety ellenőrzés**
- [ ] Akkumulátor OCV feszültsége mérve DMM-mel (manuálisan): elvárt ~12.X V (12V pack)
- [ ] OCV normális tartományban: 11.8V–12.9V (FIAMM AGM, töltöttségtől függően)
- [ ] Kábelbilincsek szorosan, rövidzár kizárva
- [ ] Emergency stop (GUI: Safe Off gomb) elérhető és ismert
- [ ] Első futáshoz ajánlott: kis kapacitású akku (≤12Ah), CHARACTERIZATION plan

**6. `connection_test.py` futtatási útmutató**
```bash
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python Prog/tools/connection_test.py
```
Elvárt kimenet minden bekötött műszerre: `✅ OK — <IDN string>`  
Ha `⚠️ SKIP`: töltsd ki a `local_config.yaml` resource stringjét  
Ha `❌ FAIL`: ellenőrizd USB/LAN kapcsolatot, VISA drivert, IP-t

---

## Érintett fájlok

| Fájl | Változás típusa |
|------|----------------|
| `Prog/tools/__init__.py` | Új — package scaffold |
| `Prog/tools/connection_test.py` | Új — connection test script |
| `Prog/config/local_config.template.yaml` | Új — config sablon |
| `Folyamatok/tervek/preflight_checklist.md` | Új — labor checklist |
| `.gitignore` | Módosítás — `Prog/config/local_config.yaml` hozzáadása |

---

## Nyitott kérdések / nem része ennek a fázisnak

- `local_config.yaml` merge logika a `ConfigPanel`-be (GUI-ból való betöltés) — 6E téma
- PT100 kalibrációs ellenőrzés (ismert hőmérsékletű referencia)
- PARALLEL/SERIES mód GUI megerősítés flow részletei
