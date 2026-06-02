# FÁZIS 6D Checkpoint Folytatás — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `request_continue_from_checkpoint()` teljes implementációja — `_do_run()` Worker refactorral, `reset_control_flags()` backend metódussal, és a MainWindow tab-váltás logikájával checkpoint-utáni folytatáshoz.

**Architecture:** B megközelítés — a Worker közös `_do_run(start_step_index)` metódust kap; `run()` és `request_continue_from_checkpoint()` mindkettő ezt hívja. A MainWindow a `status_changed("RUNNING")` signal alapján vált Live tabra (nem gombnyomásra). A jelenlegi `bq_learning_physical()` plan terminális checkpointtal végződik (`resume_possible=False`) — a "Folytatás" gomb disabled marad, de az infrastruktúra jövőbeli nem-terminális planekhez készen áll.

**Tech Stack:** Python 3.x, PySide6, pytest

---

## Fájl struktúra

| Fájl | Változás |
|------|----------|
| `Prog/src/test_runner.py` | `reset_control_flags()` új metódus |
| `Prog/gui/worker.py` | `_do_run()` + `run()` refactor + `request_continue_from_checkpoint()` teljes impl. |
| `Prog/gui/panels/checkpoint_panel.py` | `set_continuing()` új metódus |
| `Prog/gui/main_window.py` | `_on_finished()` fix + `_on_continue_requested()` + `_on_status_changed()` + `_start_test()` |
| `Prog/tests/test_test_runner.py` | 2 új teszt `TestCheckpointTerminal`-ban |
| `Prog/tests/gui/test_worker.py` | `_MockRunner` bővítés + `TestWorkerContinuation` 4 új teszttel |

**Elvárt végeredmény: 365 teszt zöld** (359 + 6 új)

---

## Task 1: TestRunner — `reset_control_flags()`

**Files:**
- Modify: `Prog/src/test_runner.py`
- Test: `Prog/tests/test_test_runner.py`

- [ ] **Step 1: Írj 2 új failing tesztet**

`Prog/tests/test_test_runner.py` — `TestCheckpointTerminal` osztály végéhez add hozzá (a `test_run_with_start_step_index_skips_steps` metódus után):

```python
    def test_reset_control_flags_clears_stop_requested(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.stop_requested = True
        runner.reset_control_flags()
        assert runner.stop_requested is False
        logger.close()

    def test_reset_control_flags_clears_emergency_stop(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.emergency_stop_requested = True
        runner.emergency_stop_reason = "TEST_REASON"
        runner.reset_control_flags()
        assert runner.emergency_stop_requested is False
        assert runner.emergency_stop_reason == ""
        logger.close()
```

- [ ] **Step 2: Futtasd — ellenőrizd a bukásokat**

```
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python -m pytest Prog/tests/test_test_runner.py::TestCheckpointTerminal::test_reset_control_flags_clears_stop_requested Prog/tests/test_test_runner.py::TestCheckpointTerminal::test_reset_control_flags_clears_emergency_stop -v
```

Elvárt: 2 FAILED — `AttributeError: 'TestRunner' object has no attribute 'reset_control_flags'`

- [ ] **Step 3: Implementáld a metódust**

`Prog/src/test_runner.py` — a `request_emergency_stop()` metódus (kb. sor 133–135) **után** add hozzá:

```python
    def reset_control_flags(self) -> None:
        self.stop_requested = False
        self.emergency_stop_requested = False
        self.emergency_stop_reason = ""
```

- [ ] **Step 4: Futtasd — 2 teszt zöld**

```
python -m pytest Prog/tests/test_test_runner.py::TestCheckpointTerminal::test_reset_control_flags_clears_stop_requested Prog/tests/test_test_runner.py::TestCheckpointTerminal::test_reset_control_flags_clears_emergency_stop -v
```

Elvárt: 2 passed.

- [ ] **Step 5: Teljes test_test_runner.py suite**

```
python -m pytest Prog/tests/test_test_runner.py -v
```

Elvárt: minden teszt zöld (82 passed).

- [ ] **Step 6: Commit**

```
git add Prog/src/test_runner.py Prog/tests/test_test_runner.py
git commit -m "feat(6D): TestRunner.reset_control_flags() — continuation előtti flag reset"
```

---

## Task 2: Worker — `_do_run()` refactor + `request_continue_from_checkpoint()`

**Files:**
- Modify: `Prog/gui/worker.py`
- Test: `Prog/tests/gui/test_worker.py`

- [ ] **Step 1: Bővítsd a `_MockRunner` osztályt**

`Prog/tests/gui/test_worker.py` — a `_MockRunner` osztályt cseréld le teljesen:

```python
class _MockRunner:
    """Mock TestRunner — azonnal visszatér, callback-eket hívja."""
    def __init__(self, result: TestResult, emit_sample: bool = True):
        self._result = result
        self._emit = emit_sample
        self.on_sample = None
        self.on_event = None
        self.on_step_changed = None
        self.stop_requested = False
        self.emergency_stop_requested = False
        self.emergency_stop_reason = ""
        self.reset_flags_called = False       # ← új
        self.last_start_step_index = 0        # ← új

    def run(self, plan, start_step_index: int = 0) -> TestResult:   # ← start_step_index
        self.last_start_step_index = start_step_index               # ← új
        if self._emit and self.on_sample:
            self.on_sample({"event_code": None, "battery_voltage_V": 12.5, "elapsed_s": 1.0})
        if self._emit and self.on_event:
            self.on_event({"event_code": "TEST_EVENT", "event_message": "ok"})
        if self._emit and self.on_step_changed:
            self.on_step_changed({
                "runner_status": "RUNNING",
                "step_kind": "CHARGE",
                "step_label": "charge",
                "step_index": 0,
                "step_count": 4,
            })
        return self._result

    def request_stop(self):
        self.stop_requested = True

    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP"):
        self.emergency_stop_requested = True
        self.emergency_stop_reason = reason

    def reset_control_flags(self) -> None:                          # ← új
        self.reset_flags_called = True
```

- [ ] **Step 2: Írj 4 új failing tesztet**

A `TestWorkerSignals6B` osztály végéhez (a `test_worker_stores_checkpoint_next_step_index` metódus **után**) add hozzá az új osztályt:

```python
class TestWorkerContinuation:
    def test_continue_emits_warning_when_no_index(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        assert worker._checkpoint_next_step_index is None
        events = []
        worker.event_ready.connect(events.append)
        worker.request_continue_from_checkpoint()
        assert any(
            e.get("event_code") == "CHECKPOINT_RESUME_INVALID_STATE"
            for e in events
        )

    def test_continue_resets_runner_flags(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker._checkpoint_next_step_index = 2
        worker.request_continue_from_checkpoint()
        assert mock_runner.reset_flags_called is True

    def test_continue_runs_with_start_index(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker._checkpoint_next_step_index = 3
        worker.request_continue_from_checkpoint()
        assert mock_runner.last_start_step_index == 3

    def test_continue_emits_running(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker._checkpoint_next_step_index = 2
        statuses = []
        worker.status_changed.connect(statuses.append)
        worker.request_continue_from_checkpoint()
        assert "RUNNING" in statuses
```

- [ ] **Step 3: Futtasd — ellenőrizd a bukásokat**

```
python -m pytest Prog/tests/gui/test_worker.py::TestWorkerContinuation -v
```

Elvárt: 4 FAILED — `request_continue_from_checkpoint` még a stub (`pass`), `_do_run` nem létezik.

- [ ] **Step 4: Implementáld a Worker változásokat — teljes fájl csere**

`Prog/gui/worker.py` teljes tartalma:

```python
"""
TestRunnerWorker — egyetlen PySide6 ↔ TestRunner bridge réteg.
A TestRunner GUI-független marad; a worker fordítja signalokra a callback-eket.
"""
from __future__ import annotations
from PySide6.QtCore import QObject, Signal, Slot

from Prog.src.test_runner import TestRunner, TestPlan


class TestRunnerWorker(QObject):
    sample_ready       = Signal(dict)
    event_ready        = Signal(dict)
    status_changed     = Signal(str)
    checkpoint_reached = Signal(dict)
    finished           = Signal(object)
    fault              = Signal(str)
    step_changed       = Signal(dict)

    def __init__(self, runner, test_plan: TestPlan) -> None:
        super().__init__()
        self._runner = runner
        self._test_plan = test_plan
        self._runner.on_sample = self.sample_ready.emit
        self._runner.on_event = self._handle_event
        self._runner.on_step_changed = self.step_changed.emit
        self._checkpoint_next_step_index: int | None = None

    def _handle_event(self, event: dict) -> None:
        self.event_ready.emit(event)
        if event.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED":
            self._checkpoint_next_step_index = event.get("next_step_index")
            self.checkpoint_reached.emit(event)

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

    @Slot()
    def run(self) -> None:
        self._do_run(start_step_index=0)

    @Slot()
    def request_stop(self) -> None:
        self._runner.request_stop()

    @Slot(str)
    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None:
        self._runner.request_emergency_stop(reason)

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

    @property
    def runner(self):
        return self._runner
```

- [ ] **Step 5: Futtasd — minden worker teszt zöld**

```
python -m pytest Prog/tests/gui/test_worker.py -v
```

Elvárt: 24 passed (20 régi + 4 új `TestWorkerContinuation`). A meglévő `test_worker_request_continue_stub_no_exception` is zöld marad — az új implementáció sem dob kivételt None index esetén.

- [ ] **Step 6: Commit**

```
git add Prog/gui/worker.py Prog/tests/gui/test_worker.py
git commit -m "feat(6D): Worker _do_run() refactor + request_continue_from_checkpoint() implementáció"
```

---

## Task 3: CheckpointPanel — `set_continuing()`

**Files:**
- Modify: `Prog/gui/panels/checkpoint_panel.py`

- [ ] **Step 1: Add hozzá a `set_continuing()` metódust**

`Prog/gui/panels/checkpoint_panel.py` — a `show_checkpoint()` metódus **után** add hozzá:

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

- [ ] **Step 2: Ellenőrizd a kompileálhatóságot**

```
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python -m compileall Prog/gui/panels/checkpoint_panel.py
```

Elvárt: `Compiling ... OK`

- [ ] **Step 3: Import ellenőrzés**

```
python -c "from Prog.gui.panels.checkpoint_panel import CheckpointPanel; print('OK')"
```

Elvárt: `OK`

- [ ] **Step 4: Commit**

```
git add Prog/gui/panels/checkpoint_panel.py
git commit -m "feat(6D): CheckpointPanel.set_continuing() — gombok inaktiválása folytatáskor"
```

---

## Task 4: MainWindow — folytatás logika

**Files:**
- Modify: `Prog/gui/main_window.py`

- [ ] **Step 1: Javítsd az `_on_finished()` metódust**

`Prog/gui/main_window.py` — `_on_finished()` metódus (kb. sor 112). Az első sor (`self._cleanup_thread()`) helyett:

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
            QMessageBox.information(
                self, "Teszt befejezve",
                f"Teszt kész — {result.status}\n"
                f"Töltve: {result.total_charge_ah:.4f} Ah\n"
                f"Kisütve: {result.total_discharge_ah:.4f} Ah",
            )
        elif result.status == "CHECKPOINT_STOPPED":
            pass  # tab és status bar már frissítve az _on_checkpoint_reached-ben
```

- [ ] **Step 2: Add hozzá az `_on_continue_requested()` és `_on_status_changed()` metódusokat**

Az `_on_checkpoint_close()` metódus **után** add hozzá:

```python
    def _on_continue_requested(self) -> None:
        if self._worker is None:
            return
        self._live_panel.reset_plots()
        self._checkpoint_panel.set_continuing()
        self._status_bar.showMessage("Folytatás indítása...")

    def _on_status_changed(self, status: str) -> None:
        if (status == "RUNNING"
                and self._tabs.currentIndex() == self._checkpoint_tab_index):
            self._tabs.setCurrentIndex(1)
```

- [ ] **Step 3: Bővítsd a `_start_test()` signal bekötéseit**

`_start_test()`-ben a `self._thread.start()` sor **elé** (a meglévő `checkpoint_reached` bekötések után) add hozzá:

```python
        self._checkpoint_panel.continue_requested.connect(self._on_continue_requested)
        self._checkpoint_panel.continue_requested.connect(
            self._worker.request_continue_from_checkpoint
        )
        self._worker.status_changed.connect(self._on_status_changed)
```

- [ ] **Step 4: Ellenőrizd a kompileálhatóságot**

```
python -m compileall Prog/gui/main_window.py
```

Elvárt: `Compiling ... OK`

- [ ] **Step 5: Commit**

```
git add Prog/gui/main_window.py
git commit -m "feat(6D): MainWindow folytatás logika — _on_finished keep_thread + _on_status_changed tab váltás"
```

---

## Task 5: Teljes suite smoke teszt

**Files:** nincs kódmódosítás

- [ ] **Step 1: Statikus ellenőrzés**

```
python -m compileall Prog
```

Elvárt: minden fájl OK, nincs SyntaxError.

- [ ] **Step 2: Teljes tesztsuite**

```
python -m pytest -v
```

Elvárt: **365 passed** (359 régi + 2 TestCheckpointTerminal + 4 TestWorkerContinuation).

Ha a szám eltér:
```
python -m pytest --collect-only -q
```

- [ ] **Step 3: d12.txt commitolása**

```
git status
```

Ha `Folyamatok/döntések/d12.txt` untracked:
```
git add "Folyamatok/döntések/d12.txt"
git commit -m "add: d12.txt — FÁZIS 6D tervezési döntések (tab váltás, reset_plots, flagek, set_continuing)"
```

- [ ] **Step 4: Manuális smoke teszt checklist**

```
python Prog/main.py
```

```
[ ] Ablak elindul, 3 tab
[ ] BQ Checkpoint tab disabled
[ ] _on_finished() terminális checkpointnál takarít fel (thread=None után)
[ ] compileall hiba nélkül
```

---

## Gyors referencia — érintett sorok

| Fájl | Mit keress |
|------|-----------|
| `test_runner.py` | `request_emergency_stop()` után → `reset_control_flags()` |
| `test_test_runner.py` | `TestCheckpointTerminal` vége → 2 új teszt |
| `worker.py` | Teljes csere — `_do_run()` + refactored `run()` + `request_continue_from_checkpoint()` |
| `test_worker.py` | `_MockRunner` csere + `TestWorkerContinuation` osztály |
| `checkpoint_panel.py` | `show_checkpoint()` után → `set_continuing()` |
| `main_window.py:_on_finished` | `self._cleanup_thread()` → `keep_thread` logika |
| `main_window.py` | `_on_checkpoint_close()` után → 2 új metódus |
| `main_window.py:_start_test` | `thread.start()` elé → 3 új signal bekötés |
