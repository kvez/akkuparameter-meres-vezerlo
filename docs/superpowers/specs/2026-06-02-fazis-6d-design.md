# FÁZIS 6D — Checkpoint folytatás implementálása

**Dátum:** 2026-06-02
**Alapja:** d12.txt — felhasználói döntések
**Megközelítés:** B — Worker `_do_run()` refactor + `request_continue_from_checkpoint()` teljes implementáció

---

## Összefoglaló

A 6C-ben a `request_continue_from_checkpoint()` stub maradt (`pass`). A 6D megvalósítja a tényleges checkpoint-utáni folytatást nem-terminális checkpointoknál (`resume_possible=True`).

A jelenlegi `bq_learning_physical()` plan terminális checkpointtal végződik, ezért a "Folytatás" gomb jelenleg is disabled marad — de az infrastruktúra most kerül a helyére, hogy jövőbeli nem-terminális planek esetén működjön.

**Kulcs döntések (d12.txt):**
- Tab váltás csak `status_changed("RUNNING")` után (nem gombnyomásra)
- `reset_plots()` hívódik continuation előtt (új session szemantika)
- `TestRunner.reset_control_flags()` a Worker hívja continuation előtt
- CheckpointPanel látható marad, de gombok inaktiválódnak
- Hiányzó `next_step_index` → WARNING event + no-op

---

## 1. Backend változások (`Prog/src/test_runner.py`)

### 1.1 `reset_control_flags()` — új metódus

A `request_emergency_stop()` után:

```python
def reset_control_flags(self) -> None:
    self.stop_requested = False
    self.emergency_stop_requested = False
    self.emergency_stop_reason = ""
```

Szerepe: Worker hívja continuation előtt, hogy az előző run lefutásából ne maradjon bent hibás flag. A `run()` csak az Ah akkumulátorokat és a `status`-t reseteli automatikusan — a kontroll flageket nem.

---

## 2. Worker változások (`Prog/gui/worker.py`)

### 2.1 `_do_run()` — közös logika kiemelése

```python
def _do_run(self, start_step_index: int = 0) -> None:
    self.status_changed.emit("RUNNING")
    try:
        result = self._runner.run(self._test_plan, start_step_index)
    except Exception as exc:
        self.status_changed.emit("FAULT")
        self.fault.emit(str(exc))
        return

    if result.status == "FAULT":
        self.status_changed.emit("FAULT")
        self.fault.emit(result.reason or "UNKNOWN_FAULT")
    elif result.status == "STOPPED":
        self.status_changed.emit("STOPPED")
        self.finished.emit(result)
    elif result.status == "CHECKPOINT_STOPPED":
        self.status_changed.emit("CHECKPOINT_STOPPED")
        self.finished.emit(result)
    else:
        self.status_changed.emit("DONE")
        self.finished.emit(result)
```

### 2.2 `run()` — delegál `_do_run()`-hoz

```python
@Slot()
def run(self) -> None:
    self._do_run(start_step_index=0)
```

### 2.3 `request_continue_from_checkpoint()` — teljes implementáció

```python
@Slot()
def request_continue_from_checkpoint(self) -> None:
    if self._checkpoint_next_step_index is None:
        self.event_ready.emit({
            "event_code": "CHECKPOINT_RESUME_INVALID_STATE",
            "event_message": "Folytatás kérése érkezett, de nincs érvényes next_step_index.",
            "severity": "WARNING",
        })
        return
    self._runner.reset_control_flags()
    self._do_run(start_step_index=self._checkpoint_next_step_index)
```

---

## 3. CheckpointPanel változások (`Prog/gui/panels/checkpoint_panel.py`)

### 3.1 `set_continuing()` — új metódus

```python
def set_continuing(self) -> None:
    """Folytatás indításakor hívódik — gombok inaktiválva, Safe Off megmarad."""
    self._continue_btn.setEnabled(False)
    self._close_btn.setEnabled(False)
    self._emstop_btn.setEnabled(True)
    self._header_lbl.setText(
        "BQ_LEARNING_PHYSICAL — folytatás indítása..."
    )
```

A Safe Off (`_emstop_btn`) mindig elérhető — biztonsági funkció, soha nem tiltódik le.

---

## 4. MainWindow változások (`Prog/gui/main_window.py`)

### 4.1 `_on_finished()` — ne takarítson fel resume_possible=True esetén

```python
def _on_finished(self, result) -> None:
    keep_thread = (result.status == "CHECKPOINT_STOPPED"
                   and result.resume_possible)
    if not keep_thread:
        self._cleanup_thread()
    self._status_bar.showMessage(
        f"Kész | Töltve: {result.total_charge_ah:.4f} Ah | "
        f"Kisütve: {result.total_discharge_ah:.4f} Ah"
    )
    if result.status == "DONE":
        QMessageBox.information(...)
    elif result.status == "CHECKPOINT_STOPPED":
        pass  # tab és status bar az _on_checkpoint_reached-ben frissült
```

**Miért:** terminális checkpointnál (`resume_possible=False`) a thread cleanup változatlan. Nem-terminális checkpointnál a Worker és Thread életben marad — a worker thread event loopja fut, és képes fogadni a `request_continue_from_checkpoint` queued signal-t.

### 4.2 `_on_continue_requested()` — új slot (main thread)

```python
def _on_continue_requested(self) -> None:
    if self._worker is None:
        return
    self._live_panel.reset_plots()
    self._checkpoint_panel.set_continuing()
    self._status_bar.showMessage("Folytatás indítása...")
```

Tab váltás NEM történik itt — csak `status_changed("RUNNING")` után vált (ld. 4.3).

### 4.3 `_on_status_changed()` — tab váltás RUNNING-nál ha Checkpoint tab aktív

```python
def _on_status_changed(self, status: str) -> None:
    if (status == "RUNNING"
            and self._tabs.currentIndex() == self._checkpoint_tab_index):
        self._tabs.setCurrentIndex(1)
```

Ez az egyetlen hely, ahol a continuation tab-váltás történik. A feltétel (`currentIndex() == checkpoint_tab_index`) biztosítja, hogy az inicializális `_start_test()` RUNNING-ja nem okoz felesleges tab váltást.

### 4.4 `_start_test()` — 3 új signal bekötés

```python
# A self._thread.start() sor elé:
self._checkpoint_panel.continue_requested.connect(self._on_continue_requested)
self._checkpoint_panel.continue_requested.connect(
    self._worker.request_continue_from_checkpoint
)
self._worker.status_changed.connect(self._on_status_changed)
```

**Bekötési mechanizmus:**
- `continue_requested → _on_continue_requested`: direct connection (main thread)
- `continue_requested → request_continue_from_checkpoint`: queued connection (Worker a QThread-ben él → Qt automatikusan queued connection-t használ)
- A main thread rész szinkron fut → a Worker slot aszinkron fut a worker thread event loopján

---

## 5. Tesztstratégia

### 5.1 Backend tesztek (`Prog/tests/test_test_runner.py`) — 2 új teszt

| # | Teszt | Mit ellenőriz |
|---|-------|---------------|
| 1 | `test_reset_control_flags_clears_stop_requested` | `stop_requested=True` után `reset_control_flags()` → `False` |
| 2 | `test_reset_control_flags_clears_emergency_stop` | `emergency_stop_requested=True` + reason után reset → `False` + `""` |

### 5.2 Worker tesztek (`Prog/tests/gui/test_worker.py`) — `_MockRunner` bővítés + 4 új teszt

**`_MockRunner` bővítés:**

```python
class _MockRunner:
    def __init__(self, ...):
        ...
        self.reset_flags_called = False
        self.last_start_step_index = 0

    def run(self, plan, start_step_index: int = 0) -> TestResult:
        self.last_start_step_index = start_step_index
        ...

    def reset_control_flags(self) -> None:
        self.reset_flags_called = True
```

**Új tesztek (`TestWorkerContinuation` osztály):**

| # | Teszt | Mit ellenőriz |
|---|-------|---------------|
| 1 | `test_continue_emits_warning_when_no_index` | `_checkpoint_next_step_index=None` → `event_ready` WARNING event |
| 2 | `test_continue_resets_runner_flags` | `reset_control_flags()` meghívódik continuation előtt |
| 3 | `test_continue_runs_with_start_index` | runner.run() helyes `start_step_index`-szel hívódik |
| 4 | `test_continue_emits_running` | `status_changed("RUNNING")` elsül |

### 5.3 Manuális smoke teszt

```
[ ] python Prog/main.py elindul
[ ] Jelenlegi bq_learning_physical plan: "Folytatás" gomb disabled — változatlan
[ ] _on_finished() terminális checkpointnál takarít fel (thread None)
[ ] compileall Prog — nincs szintaxishiba
```

---

## 6. Érintett fájlok

| Fájl | Változás típusa |
|------|----------------|
| `Prog/src/test_runner.py` | Módosítás — `reset_control_flags()` új metódus |
| `Prog/gui/worker.py` | Módosítás — `_do_run()` + `run()` refactor + `request_continue_from_checkpoint()` |
| `Prog/gui/panels/checkpoint_panel.py` | Módosítás — `set_continuing()` új metódus |
| `Prog/gui/main_window.py` | Módosítás — `_on_finished()` + `_on_continue_requested()` + `_on_status_changed()` + `_start_test()` |
| `Prog/tests/test_test_runner.py` | Módosítás — 2 új teszt |
| `Prog/tests/gui/test_worker.py` | Módosítás — `_MockRunner` bővítés + 4 új teszt |

**Elvárt végeredmény: 365 teszt zöld** (359 + 6 új)

---

## 7. Nyitott kérdések / 6E-re halasztva

- Checkpoint session mentés folytatáshoz: `parent_session_id`, `related_sessions` — Logger szintű kapcsolat
- `EventLogWidget` elválasztó sor új session indításakor ("Új session indult checkpoint után")
- Külön Events tab szűréssel/exporttal (`LogViewerPanel`)
- Nem-terminális checkpoint valós planben (jelenleg csak tesztelve stub kontrollerekkel)
- `set_continuing()` után visszaállítás DONE/FAULT esetén (`set_idle()` vagy `show_checkpoint()` újrahívás)
