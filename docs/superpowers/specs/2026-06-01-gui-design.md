# FÁZIS 6 — GUI Design Spec
**Dátum:** 2026-06-01  
**Státusz:** ELFOGADVA  
**Döntési fájlok:** d7.txt, d8.txt

---

## 1. Összefoglaló

PySide6 + pyqtgraph alapú labor műszerfal az Akkuteszter projekthez.  
Három fő funkció: konfiguráció, real-time monitoring/vezérlés, post-mortem log elemzés.  
Implementációs sorrend: FÁZIS 6A → 6B → 6C → 6D.

---

## 2. Fájlstruktúra

```
Prog/
  gui/
    __init__.py
    main_window.py          # QMainWindow, tab koordináció, QThread életciklus
    worker.py               # TestRunnerWorker(QObject) — egyetlen bridge réteg
    log_loader.py           # LoadedSession, CsvLogLoader, SqliteLogLoader
    panels/
      __init__.py
      config_panel.py       # Setup: YAML betöltés + GUI override → SessionConfig
      live_panel.py         # Real-time grafikonok + status + vezérlőgombok
      checkpoint_panel.py   # MANUAL_CHECKPOINT kezelő panel
      log_viewer_panel.py   # Post-mortem CSV/SQLite megjelenítő
  main.py                   # Belépési pont, QApplication
```

---

## 3. Architektúra és réteghatárok

### Thread modell

```
GUI thread                       Worker thread
────────────────────────────     ─────────────────────────────────
MainWindow
  ConfigPanel
  LivePanel           ←───────   TestRunnerWorker(QObject)
  CheckpointPanel     ←───────     signals: sample_ready, event_ready,
  LogViewerPanel                            status_changed, checkpoint_reached,
                                            finished, fault
                                     └── TestRunner.run(plan)  [blokkoló]
                                           on_sample / on_event callbacks
```

### Import határok

| Modul | Importálhat |
|---|---|
| `Prog/gui/*` | `Prog.src.*`, `Prog.drivers.*`, PySide6, pyqtgraph |
| `Prog/src/*` | **NEM** importálhat `Prog.gui.*`-t vagy PySide6-ot |

**Invariáns:** `TestRunner` soha nem importál PySide6-ot és nincs közvetlen QThread függősége.

---

## 4. Komponensek

### 4.1 TestRunnerWorker (`worker.py`)

```python
class TestRunnerWorker(QObject):
    sample_ready       = Signal(dict)    # minden tick sample dict
    event_ready        = Signal(dict)    # logger event
    status_changed     = Signal(str)     # IDLE/RUNNING/STOPPING/WAITING_FOR_MANUAL_CHECKPOINT/DONE/FAULT/STOPPED
    checkpoint_reached = Signal(dict)    # MANUAL_CHECKPOINT elérve
    finished           = Signal(object)  # TestResult
    fault              = Signal(str)     # hibaüzenet

    # Slotok
    def run(): ...
    def request_stop(): ...
    def request_emergency_stop(reason: str): ...
    def request_continue_from_checkpoint(): ...
```

**Szabályok:**
- Worker soha ne frissítsen közvetlenül QLabel-t, grafikont, gombot
- Csak signalokat emitáljon
- `TestRunner.on_sample = self.sample_ready.emit`
- `TestRunner.on_event = self.event_ready.emit`

**QThread életciklus** a MainWindow koordinálja:
1. `worker.moveToThread(thread)`
2. Signalok bekötése
3. `thread.start()`
4. `finished` / `fault` signal után: `thread.quit()` + `thread.wait()`

### 4.2 ConfigPanel (`config_panel.py`)

Visszaad: `SessionConfig` dataclass — nem épít `TestRunner`-t vagy `BatteryProfile`-t közvetlenül.

**GUI-ban szerkeszthető mezők:**

| Szekció | Mezők |
|---|---|
| Akkumulátor | Profil (FIAMM_12V/24V), modell, capacity Ah, sample ID |
| Műszerek | PSU/Load/DMM voltage/DMM temp resource string + Connect/IDN check gomb |
| PSU mód | INDEPENDENT/PARALLEL/SERIES + `hardware_wiring_confirmed` checkbox |
| Teszt | CHARACTERIZATION / BQ_LEARNING_PHYSICAL, `runner_tick_s`, `taper_hold_s` |
| Hőkompenzáció | OFF / MONITOR_ONLY / ENABLED |

**YAML-ban maradó paraméterek** (read-only a GUI-ban):
- Safety limitek, DMM NPLC, fallback timeout, BY550 paraméterek, logger flush/commit periódusok

**Betöltési sorrend:** `default_config.yaml` → `local_config.yaml` (gitignore-ban, resource stringeknek)

**Mentési modell:**
- Resource stringek → `local_config.yaml`
- Futtatott konfiguráció → `session_meta.json`
- `default_config.yaml` soha nem íródik felül automatikusan

### 4.3 LivePanel (`live_panel.py`)

**Status bar (felül):**
- Aktuális state, U_batt, I_signed, T_batt, Ah charge/discharge
- psu_mode, isolation_state, dmm_valid, warning_flags, fault_flags

**5 pyqtgraph PlotWidget** (időtengely: `elapsed_s`):
1. `battery_voltage_V`
2. `signed_current_A`
3. `battery_temperature_C`
4. `u_drop_V`
5. `accumulated_charge_Ah` + `accumulated_discharge_Ah` (két görbe)

**Gombok:** Start · Stop · Emergency Stop

**Start előtti validáció** (MainWindow végzi):
- `nominal_capacity_Ah > 0`, modell nem üres, resource stringek nem üresek
- PSU mód kompatibilis pack feszültséggel (24V pack → SERIES)
- `hardware_wiring_confirmed = True` ha PARALLEL vagy SERIES
- DMM voltage elérhető; temp DMM elérhető ha MONITOR_ONLY vagy ENABLED

### 4.4 CheckpointPanel (`checkpoint_panel.py`)

Aktiválódik: `checkpoint_reached` signal hatására. Tartalmaz:
- Állapot szöveg: "MANUAL BQ CHECKPOINT — kezelői ellenőrzés szükséges"
- Checklist: BQ eszköz, bqStudio, Qmax, Ra table, export/mentés, kezelői megerősítés
- Gombok: **Checkpoint kész / Folytatás** · **Teszt lezárása** · Emergency Stop
- Opcionális: kezelői megjegyzés szövegmező
- Opcionális popup értesítés (nem fő vezérlő elem)

**GUI állapot checkpoint közben:**
- Start gomb: disabled
- Stop gomb: "Teszt lezárása"
- Folytatás gomb: enabled
- Emergency Stop: enabled
- Grafikonok és logok továbbra is láthatók

### 4.5 LogLoader (`log_loader.py`)

```python
@dataclass
class LoadedSession:
    samples: list[dict]
    events: list[dict]
    session_meta: dict
    source_type: str   # "CSV" | "SQLITE"
    source_path: str

class CsvLogLoader:
    def load(path: str) -> LoadedSession
    # Automatikusan keresi: events.csv, session_meta.json, report.json

class SqliteLogLoader:
    def load(path: str) -> LoadedSession
    # Betölti: samples, events, session_meta táblák; hiányzó tábla → warning, részleges betöltés OK
```

**Grafikon panel soha nem tudja, CSV vagy SQLite volt a forrás.**

### 4.6 LogViewerPanel (`log_viewer_panel.py`)

- File → Open CSV... / File → Open SQLite session... / Recent sessions
- Ugyanaz az 5 grafikon mint LivePanel, de `LoadedSession.samples`-ből
- Event / warning / fault táblázat
- Metadata panel (psu_mode, profil, capacity, test_type, stb.)

---

## 5. Adatfolyam

### 5.1 Start → futás

```
ConfigPanel.get_session_config() → SessionConfig
MainWindow validál
MainWindow épít: BatteryProfile, SafetyManager, InstrumentManager, TestPlan, TestRunner
MainWindow létrehozza: TestRunnerWorker + QThread
worker.moveToThread(thread)
Signalok bekötése
thread.start() → worker.run() → TestRunner.run(plan)

Futás közben (worker thread):
  tick → _build_sample() → on_sample(sample) → sample_ready.emit(sample)
    → live_panel.update(sample)
```

### 5.2 Leállítás

```
Stop gomb       → worker.request_stop()           [flag set, GUI nem blokkol]
EmStop gomb     → worker.request_emergency_stop() [flag set]
worker.finished.emit(result) → MainWindow.on_test_finished()
thread.quit() + thread.wait()
```

---

## 6. Hibakezelés

| Esemény | TestRunner | Worker signal | GUI reakció |
|---|---|---|---|
| DMM elvész | `_emergency_stop()` | `fault(str)` | Piros státusz, hibaüzenet, gombok reset |
| PSU comm lost | `_emergency_stop()` | `fault(str)` | ua. |
| Kontroller FAULT | `_emergency_stop()` | `fault(str)` | ua. |
| User Stop | `_graceful_stop()` | `finished(STOPPED)` | Narancssárga státusz |
| User EmStop | `_emergency_stop()` | `fault(str)` | Piros státusz |
| MANUAL_CHECKPOINT | `_run_manual_checkpoint()` | `checkpoint_reached(dict)` | Checkpoint panel aktív |
| Worker kivétel | `except` blokk | `fault(str)` | Piros státusz |

**Invariáns:** Ha `fault` signal érkezik, `safe_all_off()` már lefutott a worker threadben. GUI csak állapotot frissít.

---

## 7. Tesztelés

| Réteg | Megközelítés |
|---|---|
| `worker.py` | Unit tesztelhető Mock TestRunner-rel |
| `log_loader.py` | Unit tesztelhető, nincs GUI függőség |
| `SessionConfig` validáció | Unit tesztelhető |
| GUI panelek | Manuális smoke test checklist |
| Meglévő 310 teszt | Változatlan |

**Manuális smoke test checklist (6A):**
- [ ] Indítás, konfig betöltés, resource string megadás
- [ ] Connect / IDN check
- [ ] Start → live grafikonok frissülnek
- [ ] Stop → graceful leállás
- [ ] Emergency Stop → azonnali safe_off, piros státusz
- [ ] Hibás resource → hibaüzenet, nincs crash

---

## 8. MVP fázisok

| Fázis | Tartalom |
|---|---|
| **6A** | ConfigPanel + Start/Stop/EmStop + LivePanel (status bar + 5 grafikon) |
| **6B** | Warning/fault panel részletezése, CheckpointPanel, `status_changed` teljes kezelése |
| **6C** | LogViewerPanel — CSV megnyitás, majd SQLite; közös LoadedSession adatmodell |
| **6D** | Összehasonlító nézetek, OCV-SOC görbe, riport megtekintés |

**Első implementációs cél: FÁZIS 6A.**

---

## 9. TestRunner bővítés (szükséges)

A meglévő `TestRunner.__init__` két opcionális callbackkel bővül:

```python
on_sample: Callable[[dict], None] | None = None
on_event: Callable[[dict], None] | None = None
```

A `_run_step()` tickben:
```python
if self.on_sample:
    self.on_sample(sample)
```

Ez az egyetlen módosítás a `Prog/src/test_runner.py`-ban. Nem töri a meglévő teszteket.
