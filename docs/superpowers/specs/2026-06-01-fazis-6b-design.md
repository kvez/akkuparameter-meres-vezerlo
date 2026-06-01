# FÁZIS 6B — GUI: Warning/Fault Panel, CheckpointPanel, step-szintű státusz

**Dátum:** 2026-06-01
**Alapja:** d10.txt — felhasználói döntések, architektúra indoklással
**Megközelítés:** B — EventLogWidget és CheckpointPanel külön fájlban, step-szintű status bar a MainWindow-ban

---

## Összefoglaló

A 6A-ban elkészült GUI alapréteg három hiányzó elemet pótol:

1. `_run_manual_checkpoint` nem hívta meg `on_event`-et → `checkpoint_reached` signal soha nem sült el
2. `event_ready` signal sehova nem volt bekötve → futás közbeni események nem jelentek meg a GUI-ban
3. `status_changed` csak végállapotokat közvetített → az aktuális lépés neve nem volt látható

---

## 1. Backend változások

### 1.1 TestRunner — `_run_manual_checkpoint` javítás

**Probléma:** A metódus `STOPPED`-ot adott vissza és nem hívta meg `on_event`-et.

**Javítás:**

- `TestRunner.__init__` tárolja `self._active_plan = None`; a `run()` első sorában `self._active_plan = test_plan`
- `_run_manual_checkpoint` kiszámítja `next_step_index = list(self._active_plan.steps).index(step) + 1`
- Meghívja `on_event`-et a teljes event dict-tel
- Visszaad `TestResult(status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED", ...)`

**Event payload minimum:**
```python
{
    "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
    "event_message": "BQ learning fizikai ciklus kézi ellenőrzési pontja elérve.",
    "step_name": step.label,
    "status": "CHECKPOINT_STOPPED",
    "resume_possible": True,
    "next_step_index": next_step_index,
}
```

### 1.2 TestResult — új státusz

`TestResult.status` lehetséges értékei (string, nem enum — meglévő konvenció):

| Értéke | Jelentés |
|--------|----------|
| `"DONE"` | Teszt sikeresen lefutott |
| `"STOPPED"` | Felhasználói graceful stop |
| `"FAULT"` | Hiba, vészleállítás |
| `"CHECKPOINT_STOPPED"` | Tervezett BQ kézi checkpoint — folytatható |

### 1.3 TestRunner — `on_step_changed` callback

Új callback a `TestRunner`-en: `self.on_step_changed: Callable[[dict], None] | None = None`

A `_run_step()` elején hívódik, mielőtt a controller loop indul:
```python
if self.on_step_changed is not None:
    self.on_step_changed({
        "runner_status": "RUNNING",
        "step_kind": step.kind.value,
        "step_label": step.label,
        "step_index": list(self._active_plan.steps).index(step),
        "step_count": len(self._active_plan.steps),
    })
```

---

## 2. Worker változások (`Prog/gui/worker.py`)

### Új signalok

```python
step_changed = Signal(dict)   # {runner_status, step_kind, step_label, step_index, step_count}
```

### Bekötés `__init__`-ben

```python
self._runner.on_step_changed = self.step_changed.emit
```

### `run()` slot — CHECKPOINT_STOPPED ág

```python
elif result.status == "CHECKPOINT_STOPPED":
    self.status_changed.emit("CHECKPOINT_STOPPED")
    self.finished.emit(result)
```

### `request_continue_from_checkpoint`

6B-ben stub marad (`pass`). A jövőbeli resume engine: `TestRunner.run(test_plan, start_step_index=N)` vagy `TestPlan.remaining_from(N)`.

---

## 3. Új UI komponensek

### 3.1 `EventLogWidget` (`Prog/gui/panels/event_log_widget.py`)

Önálló `QWidget`, egyetlen `QListWidget`-et tartalmaz.

**Severity inferencia** (ha nincs `severity` mező az eventben):

| event_code tartalmaz | Severity |
|----------------------|----------|
| `"FAULT"` vagy `"EMERGENCY"` | FAULT |
| `"WARNING"` vagy `"HIGH"` vagy `"LOST"` | WARNING |
| `"CHECKPOINT"` | CHECKPOINT |
| egyéb | INFO |

**Sorszínek:**

| Severity | Háttérszín |
|----------|------------|
| INFO | alapértelmezett |
| WARNING | `#fff8e1` |
| FAULT | `#ffebee` |
| CHECKPOINT | `#fff3e0` |

**Sor formátum:** `[HH:MM:SS] SEVERITY  event_code — event_message`

Timestamp forrása: `event.get("timestamp_iso")` → lokális HH:MM:SS, ha hiányzik → `datetime.now()`.

Max sorok: 500 (régebbi sorok elvesznek, FIFO).

**Publikus API:**
```python
def append_event(self, event: dict) -> None: ...
def clear(self) -> None: ...
```

---

### 3.2 `CheckpointPanel` (`Prog/gui/panels/checkpoint_panel.py`)

Önálló `QWidget`, a MainWindow `"BQ Checkpoint"` tabjaként jelenik meg.

**Aktiválás:** alapból rejtett tab (vagy disabled index); `show_checkpoint(event)` slot híváskor aktívvá válik és a MainWindow átvált rá.

**Tartalom:**

- Fejléc `QLabel`: `"BQ_LEARNING_PHYSICAL — kézi BQ ellenőrzési pont elérve"` — narancs háttér (`#fff3e0`), félkövér
- Info szöveg: `"A mérés biztonságos állapotban megállt. PSU OFF, Load OFF."`
- Metadata (read-only labelek): step_name, next_step_index, total_charge_ah, total_discharge_ah
- **Checklist** (7 `QCheckBox`):
  1. BQ eszköz csatlakoztatva
  2. bqStudio / BQ tool elindítva
  3. UpdateStatus ellenőrizve
  4. Qmax ellenőrizve
  5. Ra table ellenőrizve / mentve
  6. gg.csv / golden image export mentve
  7. Kezelői jegyzet rögzítve
- **Gombok:**
  - `"Folytatás checkpointból"` — **disabled** (6B stub)
  - `"Teszt lezárása"` → `close_requested` signal
  - `"Safe Off"` → `emergency_stop_requested` signal

**Publikus API:**
```python
def show_checkpoint(self, event: dict) -> None:
    # 1. Fejléc és metadata labelek frissítése az event dict alapján
    # 2. Összes checkbox visszaállítása unchecked állapotba
    # 3. "Folytatás" gomb disabled marad (6B stub)

continue_requested = Signal()          # 6B-ben nincs bekötve
close_requested = Signal()
emergency_stop_requested = Signal()
```

---

## 4. Meglévő komponensek módosítása

### 4.1 `LivePanel` bővítés

Az `EventLogWidget` beágyazódik a grafikonok alá:

```
[Status box]
[Start / Stop / EmStop gombok]
[5 grafikon]
─────────────────────────
[Eseménynapló — EventLogWidget, fixHeight=150px]
```

Új slot:
```python
@Slot(dict)
def append_event(self, event: dict) -> None:
    self._event_log.append_event(event)
```

`reset_plots()` bővítése: `self._event_log.clear()`.

`set_status()` bővítése — CHECKPOINT_STOPPED szín:
```python
"CHECKPOINT_STOPPED": "#fff3e0",   # narancs
```

### 4.2 `MainWindow` változások

**Új tab:** `CheckpointPanel` — `"BQ Checkpoint"` — tab index tárolva `self._checkpoint_tab_index`.

**Új signal bekötések a `_start_test()`-ben:**
```python
self._worker.event_ready.connect(self._live_panel.append_event)
self._worker.step_changed.connect(self._on_step_changed)
self._worker.checkpoint_reached.connect(self._checkpoint_panel.show_checkpoint)
self._worker.checkpoint_reached.connect(self._on_checkpoint_reached)
```

**`_on_step_changed(payload: dict)`:**
```python
self._status_bar.showMessage(
    f"{payload['runner_status']} — {payload['step_label']}"
)
```

**`_on_checkpoint_reached(event: dict)`:**
```python
self._tabs.setTabEnabled(self._checkpoint_tab_index, True)
self._tabs.setCurrentIndex(self._checkpoint_tab_index)
```

**`_start_test()` elején** (reset új méréskor):
```python
self._tabs.setTabEnabled(self._checkpoint_tab_index, False)
```

**`_on_finished` bővítése** — CHECKPOINT_STOPPED ág:
```python
if result.status == "CHECKPOINT_STOPPED":
    # tab már aktiválva a checkpoint_reached slotban
    self._status_bar.showMessage("BQ kézi checkpoint elérve — végezd el a BQ műveletet")
```

---

## 5. Tesztstratégia

### 5.1 Új worker tesztek (`Prog/tests/gui/test_worker.py`)

| # | Teszt neve | Mit ellenőriz |
|---|-----------|---------------|
| 1 | `test_worker_emits_event_ready` | `on_event` → `event_ready` signal payload átmegy |
| 2 | `test_worker_emits_checkpoint_reached` | `event_code == "MANUAL_BQ_CHECKPOINT_REACHED"` → `checkpoint_reached` elsül |
| 3 | `test_worker_checkpoint_reached_not_emitted_for_other_events` | más event_code → `checkpoint_reached` NEM sül el |
| 4 | `test_worker_emits_step_changed` | `on_step_changed` → `step_changed` signal elsül |
| 5 | `test_worker_emits_checkpoint_stopped_status` | `TestResult("CHECKPOINT_STOPPED")` → `status_changed("CHECKPOINT_STOPPED")` |
| 6 | `test_worker_checkpoint_stopped_emits_finished` | `CHECKPOINT_STOPPED` → `finished` is elsül |
| 7 | `test_worker_request_continue_stub` | `request_continue_from_checkpoint()` nem dob kivételt |

### 5.2 Manuális smoke teszt checklist

```
[ ] python Prog/main.py elindul hibátlanul
[ ] Start → RUNNING — <step_label> látszik a status barban
[ ] sample_ready frissíti a grafikonokat és az állapotsorokat
[ ] event_ready esemény megjelenik az eseménylistában (LivePanel alján)
[ ] WARNING severity sárga sorral jelenik meg
[ ] FAULT severity piros sorral jelenik meg
[ ] MANUAL_BQ_CHECKPOINT_REACHED → narancs fejléc, CheckpointPanel tab aktív
[ ] "Folytatás" gomb disabled állapotban van
[ ] "Teszt lezárása" visszaállítja a főablakot
[ ] Stop gomb működik futás közben
[ ] Emergency Stop gomb működik
[ ] FAULT → piros status, fault label frissül
[ ] reset_plots() törli az eseménylistát is
```

---

## 6. Érintett fájlok

| Fájl | Változás típusa |
|------|----------------|
| `Prog/src/test_runner.py` | Módosítás — `_run_manual_checkpoint`, `on_step_changed`, `_active_plan` |
| `Prog/gui/worker.py` | Módosítás — `step_changed` signal, CHECKPOINT_STOPPED ág |
| `Prog/gui/panels/event_log_widget.py` | **Új fájl** |
| `Prog/gui/panels/checkpoint_panel.py` | **Új fájl** |
| `Prog/gui/panels/live_panel.py` | Módosítás — `EventLogWidget` beágyazás, `append_event`, `reset_plots` |
| `Prog/gui/main_window.py` | Módosítás — új bekötések, `_on_step_changed`, `_on_checkpoint_reached` |
| `Prog/tests/gui/test_worker.py` | Módosítás — 7 új teszt |

---

## 7. Nyitott kérdések / 6C-re halasztva

- `request_continue_from_checkpoint` tényleges implementációja: `TestRunner.run(test_plan, start_step_index=N)` vagy `TestPlan.remaining_from(N)` — döntés d10.txt alapján halasztva
- Checkpoint session_id / session_meta mentése a folytatáshoz
- Külön Events tab (EventsPanel / LogViewerPanel) szűréssel, exporttal
- Relax progress megjelenítése a LivePanelen (elapsed / target)
