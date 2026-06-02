# FÁZIS 6C Checkpoint — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `resume_possible` flag helyesen propagál a TestRunner-től a CheckpointPanel UI-ig; `TestRunner.run()` kap `start_step_index` paramétert a jövőbeli folytatáshoz.

**Architecture:** B megközelítés — `_run_manual_checkpoint` kiszámolja `checkpoint_is_terminal = (next_step_index >= len(steps))`; flag végigmegy: `TestResult → event dict → Worker._checkpoint_next_step_index → CheckpointPanel.show_checkpoint()`; MainWindow status bar üzenet frissül. A `request_continue_from_checkpoint()` stub marad (6D).

**Tech Stack:** Python 3.x, PySide6, pytest

---

## Fájl struktúra

| Fájl | Változás |
|------|----------|
| `Prog/src/test_runner.py` | `TestResult` + `_run_manual_checkpoint` + `run(start_step_index)` |
| `Prog/gui/worker.py` | `_checkpoint_next_step_index` tárolás |
| `Prog/gui/panels/checkpoint_panel.py` | `show_checkpoint()` — gomb + fejléc frissítés |
| `Prog/gui/main_window.py` | `_on_checkpoint_reached()` status bar üzenet |
| `Prog/tests/test_test_runner.py` | 1 meglévő teszt frissítés + 5 új teszt |
| `Prog/tests/gui/test_worker.py` | 1 új teszt |

**Elvárt végeredmény: 359 teszt zöld** (353 + 6 új)

---

## Task 1: TestResult új mezők + `_run_manual_checkpoint` fix

**Files:**
- Modify: `Prog/src/test_runner.py`
- Test: `Prog/tests/test_test_runner.py`

- [ ] **Step 1: Frissítsd a meglévő `test_manual_checkpoint_calls_on_event` tesztet**

`Prog/tests/test_test_runner.py` — `TestStopMethods.test_manual_checkpoint_calls_on_event` (sor 499–512):

```python
    def test_manual_checkpoint_calls_on_event(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        events = []
        runner.on_event = events.append
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        runner._run_manual_checkpoint(step)
        assert len(events) == 1
        assert events[0]["event_code"] == "MANUAL_BQ_CHECKPOINT_REACHED"
        assert events[0]["status"] == "CHECKPOINT_STOPPED"
        assert events[0]["resume_possible"] is False          # ← volt: True
        assert events[0]["checkpoint_is_terminal"] is True   # ← új assertion
        assert "next_step_index" in events[0]
        logger.close()
```

- [ ] **Step 2: Írj 4 új failing tesztet — `TestCheckpointTerminal` osztály**

A `TestStopMethods` osztály lezáró sora (`logger.close()`) után add hozzá:

```python
# ------------------------------------------------------------------ #
# Task 1 (6C): resume_possible + checkpoint_is_terminal              #
# ------------------------------------------------------------------ #

class TestCheckpointTerminal:
    def test_testresult_resume_possible_default_false(self):
        r = TestResult(status="DONE")
        assert r.resume_possible is False

    def test_testresult_next_step_index_default_zero(self):
        r = TestResult(status="DONE")
        assert r.next_step_index == 0

    def test_checkpoint_is_terminal_for_bq_learning(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_manual_checkpoint(step)
        assert result.resume_possible is False
        assert result.next_step_index == 9
        logger.close()

    def test_checkpoint_resume_possible_in_event_dict(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        events = []
        runner.on_event = events.append
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        runner._run_manual_checkpoint(step)
        assert events[0]["resume_possible"] is False
        assert events[0]["checkpoint_is_terminal"] is True
        logger.close()
```

- [ ] **Step 3: Futtasd — ellenőrizd a bukásokat**

```
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python -m pytest Prog/tests/test_test_runner.py::TestCheckpointTerminal Prog/tests/test_test_runner.py::TestStopMethods::test_manual_checkpoint_calls_on_event -v
```

Elvárt: 5 hiba — `AttributeError: resume_possible` (2 db) és assertion error (3 db).

- [ ] **Step 4: Add hozzá a két új mezőt a `TestResult` dataclass-hoz**

`Prog/src/test_runner.py` — `TestResult` dataclass (sor 78–82):

```python
@dataclass
class TestResult:
    status: str
    reason: str = ""
    total_charge_ah: float = 0.0
    total_discharge_ah: float = 0.0
    resume_possible: bool = False    # ← új
    next_step_index: int = 0         # ← új
```

- [ ] **Step 5: Javítsd a `_run_manual_checkpoint` metódust**

`Prog/src/test_runner.py` — `_run_manual_checkpoint` (sor 271–300) teljes csere:

```python
    def _run_manual_checkpoint(self, step: TestStep) -> TestResult:
        """BQ kézi ellenőrzési pont — CHECKPOINT_STOPPED státusz, on_event hívással."""
        steps = self._active_plan.steps
        next_step_index = steps.index(step) + 1
        checkpoint_is_terminal = (next_step_index >= len(steps))
        resume_possible = not checkpoint_is_terminal
        event = {
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
            "event_message": "BQ learning fizikai ciklus kézi ellenőrzési pontja elérve.",
            "step_name": step.label,
            "status": "CHECKPOINT_STOPPED",
            "resume_possible": resume_possible,
            "checkpoint_is_terminal": checkpoint_is_terminal,
            "next_step_index": next_step_index,
            "total_charge_ah": self._total_charge_ah,
            "total_discharge_ah": self._total_discharge_ah,
        }
        self._logger.log_event("MANUAL_BQ_CHECKPOINT_REACHED", step.label)
        self._logger.write_checkpoint({
            "status": "CHECKPOINT_STOPPED",
            "step": step.label,
            "charge_ah": self._total_charge_ah,
            "discharge_ah": self._total_discharge_ah,
        })
        self._logger.flush_all()
        if self.on_event is not None:
            self.on_event(event)
        return TestResult(
            status="CHECKPOINT_STOPPED",
            reason="MANUAL_BQ_CHECKPOINT_REACHED",
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
            resume_possible=resume_possible,
            next_step_index=next_step_index,
        )
```

- [ ] **Step 6: Futtasd — ellenőrizd, hogy a 4 új teszt + a frissített teszt zöld**

```
python -m pytest Prog/tests/test_test_runner.py::TestCheckpointTerminal Prog/tests/test_test_runner.py::TestStopMethods::test_manual_checkpoint_calls_on_event -v
```

Elvárt: 5 passed.

- [ ] **Step 7: Teljes test_test_runner.py suite zöld**

```
python -m pytest Prog/tests/test_test_runner.py -v
```

Elvárt: minden teszt zöld (nincs törés).

- [ ] **Step 8: Commit**

```
git add Prog/src/test_runner.py Prog/tests/test_test_runner.py
git commit -m "feat(6C): TestResult resume_possible + checkpoint_is_terminal detektálás"
```

---

## Task 2: `TestRunner.run()` — `start_step_index` paraméter

**Files:**
- Modify: `Prog/src/test_runner.py`
- Test: `Prog/tests/test_test_runner.py`

- [ ] **Step 1: Írj 1 új failing tesztet**

`Prog/tests/test_test_runner.py` — `TestCheckpointTerminal` osztály végéhez add hozzá:

```python
    def test_run_with_start_step_index_skips_steps(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        calls = []
        runner.on_step_changed = calls.append
        runner.run(TestPlan.characterization(), start_step_index=2)
        # characterization: charge(0), relax(1), discharge(2), relax(3)
        # start_step_index=2 → csak discharge és relax_after_discharge fut
        assert len(calls) == 2
        assert calls[0]["step_label"] == "discharge"
        assert calls[1]["step_label"] == "relax_after_discharge"
        logger.close()
```

- [ ] **Step 2: Futtasd — ellenőrizd a bukást**

```
python -m pytest Prog/tests/test_test_runner.py::TestCheckpointTerminal::test_run_with_start_step_index_skips_steps -v
```

Elvárt: FAILED — `TypeError: run() got an unexpected keyword argument 'start_step_index'`

- [ ] **Step 3: Add hozzá a `start_step_index` paramétert a `run()` metódushoz**

`Prog/src/test_runner.py` — `run()` metódus aláírása (sor 137) és loop sora (sor 145):

```python
    def run(self, test_plan: TestPlan, start_step_index: int = 0) -> TestResult:
        self._active_plan = test_plan
        self._start_time = datetime.now(timezone.utc)
        self.status = "RUNNING"
        self._total_charge_ah = 0.0
        self._total_discharge_ah = 0.0

        try:
            for step in list(test_plan.steps)[start_step_index:]:
```

A metódus többi része változatlan marad.

- [ ] **Step 4: Futtasd — ellenőrizd, hogy zöld**

```
python -m pytest Prog/tests/test_test_runner.py::TestCheckpointTerminal::test_run_with_start_step_index_skips_steps -v
```

Elvárt: 1 passed.

- [ ] **Step 5: Teljes test_test_runner.py suite zöld**

```
python -m pytest Prog/tests/test_test_runner.py -v
```

Elvárt: minden teszt zöld.

- [ ] **Step 6: Commit**

```
git add Prog/src/test_runner.py Prog/tests/test_test_runner.py
git commit -m "feat(6C): TestRunner.run() start_step_index paraméter — jövőbeli checkpoint folytatáshoz"
```

---

## Task 3: Worker — `_checkpoint_next_step_index` tárolás

**Files:**
- Modify: `Prog/gui/worker.py`
- Test: `Prog/tests/gui/test_worker.py`

- [ ] **Step 1: Írj 1 új failing tesztet**

`Prog/tests/gui/test_worker.py` — `TestWorkerSignals6B` osztály végéhez add hozzá:

```python
    def test_worker_stores_checkpoint_next_step_index(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        assert worker._checkpoint_next_step_index is None
        worker._handle_event({
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
            "event_message": "checkpoint",
            "step_name": "manual_bq_checkpoint",
            "next_step_index": 9,
        })
        assert worker._checkpoint_next_step_index == 9
```

- [ ] **Step 2: Futtasd — ellenőrizd a bukást**

```
python -m pytest Prog/tests/gui/test_worker.py::TestWorkerSignals6B::test_worker_stores_checkpoint_next_step_index -v
```

Elvárt: FAILED — `AttributeError: 'TestRunnerWorker' object has no attribute '_checkpoint_next_step_index'`

- [ ] **Step 3: Implementáld a Worker változásokat**

`Prog/gui/worker.py` — `__init__` végéhez add hozzá az új mezőt, `_handle_event`-et frissítsd, `request_continue_from_checkpoint`-ot javítsd:

```python
    def __init__(self, runner, test_plan: TestPlan) -> None:
        super().__init__()
        self._runner = runner
        self._test_plan = test_plan
        self._runner.on_sample = self.sample_ready.emit
        self._runner.on_event = self._handle_event
        self._runner.on_step_changed = self.step_changed.emit
        self._checkpoint_next_step_index: int | None = None   # ← új

    def _handle_event(self, event: dict) -> None:
        self.event_ready.emit(event)
        if event.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED":
            self._checkpoint_next_step_index = event.get("next_step_index")  # ← új
            self.checkpoint_reached.emit(event)

    @Slot()
    def request_continue_from_checkpoint(self) -> None:
        # 6D: QThread újraindítás + self._runner.run(self._test_plan, self._checkpoint_next_step_index)
        pass
```

- [ ] **Step 4: Futtasd — minden worker teszt zöld**

```
python -m pytest Prog/tests/gui/test_worker.py -v
```

Elvárt: 20 passed (19 régi + 1 új).

- [ ] **Step 5: Commit**

```
git add Prog/gui/worker.py Prog/tests/gui/test_worker.py
git commit -m "feat(6C): Worker _checkpoint_next_step_index tárolás — 6D folytatás infrastruktúra"
```

---

## Task 4: CheckpointPanel — `show_checkpoint()` frissítés

**Files:**
- Modify: `Prog/gui/panels/checkpoint_panel.py`

- [ ] **Step 1: Frissítsd a `show_checkpoint()` metódust**

`Prog/gui/panels/checkpoint_panel.py` — `show_checkpoint()` metódus teljes csere:

```python
    @Slot(dict)
    def show_checkpoint(self, event: dict) -> None:
        """Metaadatok frissítése + checkboxok reset. Checkpoint_reached slotban hívódik."""
        self._step_lbl.setText(str(event.get("step_name", "–")))
        self._next_idx_lbl.setText(str(event.get("next_step_index", "–")))

        charge = event.get("total_charge_ah")
        disch  = event.get("total_discharge_ah")
        self._charge_lbl.setText(f"{charge:.4f} Ah" if charge is not None else "–")
        self._disch_lbl.setText(f"{disch:.4f} Ah"  if disch  is not None else "–")

        resume_possible = event.get("resume_possible", False)
        if resume_possible:
            self._continue_btn.setEnabled(True)
            self._continue_btn.setText("Folytatás checkpointból")
            self._continue_btn.setToolTip("")
            self._header_lbl.setText(
                "BQ_LEARNING_PHYSICAL — kézi ellenőrzési pont (folytatható)"
            )
        else:
            self._continue_btn.setEnabled(False)
            self._continue_btn.setText("Folytatás — nem elérhető")
            self._continue_btn.setToolTip(
                "Ez a checkpoint terminális — a teszt itt ért véget."
            )
            self._header_lbl.setText(
                "BQ_LEARNING_PHYSICAL — kézi ellenőrzési pont (terminális)"
            )

        for cb in self._checkboxes:
            cb.setChecked(False)
```

- [ ] **Step 2: Ellenőrizd a kompileálhatóságot**

```
python -m compileall Prog/gui/panels/checkpoint_panel.py
```

Elvárt: `Compiling ... OK`

- [ ] **Step 3: Commit**

```
git add Prog/gui/panels/checkpoint_panel.py
git commit -m "feat(6C): CheckpointPanel show_checkpoint — resume_possible alapú gomb + fejléc frissítés"
```

---

## Task 5: MainWindow — `_on_checkpoint_reached()` üzenet finomítás

**Files:**
- Modify: `Prog/gui/main_window.py`

- [ ] **Step 1: Frissítsd a `_on_checkpoint_reached()` metódust**

`Prog/gui/main_window.py` — `_on_checkpoint_reached()` metódus teljes csere:

```python
    def _on_checkpoint_reached(self, event: dict) -> None:
        self._tabs.setTabEnabled(self._checkpoint_tab_index, True)
        self._tabs.setCurrentIndex(self._checkpoint_tab_index)
        if event.get("resume_possible", False):
            msg = "BQ checkpoint elérve — végezd el a BQ műveletet, majd folytathatod."
        else:
            msg = "BQ checkpoint elérve — végezd el a BQ műveletet, majd zárd le a sessiont."
        self._status_bar.showMessage(msg)
```

- [ ] **Step 2: Ellenőrizd a kompileálhatóságot**

```
python -m compileall Prog/gui/main_window.py
```

Elvárt: `Compiling ... OK`

- [ ] **Step 3: Commit**

```
git add Prog/gui/main_window.py
git commit -m "feat(6C): MainWindow _on_checkpoint_reached — resume_possible alapú status bar üzenet"
```

---

## Task 6: Teljes suite smoke teszt

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

Elvárt: **359 passed** (353 régi + 5 új TestCheckpointTerminal + 1 új TestWorkerSignals6B).

Ha a count eltér, futtasd:
```
python -m pytest --collect-only -q
```
és ellenőrizd, melyik teszt hiányzik.

- [ ] **Step 3: Manuális smoke teszt checklist**

```
python Prog/main.py
```

```
[ ] Ablak elindul, 3 tab látszik
[ ] CheckpointPanel tab disabled (szürke)
[ ] Ha checkpoint esemény érkezne:
    [ ] Fejléc: "BQ_LEARNING_PHYSICAL — kézi ellenőrzési pont (terminális)"
    [ ] Gomb szövege: "Folytatás — nem elérhető"
    [ ] Gomb disabled állapotban
    [ ] Tooltip: "Ez a checkpoint terminális — a teszt itt ért véget."
    [ ] Status bar: "BQ checkpoint elérve — ... zárd le a sessiont."
```

- [ ] **Step 4: Összefoglaló commit (ha van unstaged változás)**

```
git add .
git commit -m "feat: FÁZIS 6C kész — resume_possible flag, start_step_index, terminális checkpoint UI, 359 teszt zöld"
```

---

## Gyors referencia — érintett sorok

| Fájl | Mit keress |
|------|-----------|
| `test_runner.py:78` | `TestResult` dataclass — 2 új mező után |
| `test_runner.py:137` | `run()` — aláírás + loop sor |
| `test_runner.py:271` | `_run_manual_checkpoint` — teljes csere |
| `test_test_runner.py:510` | `resume_possible is True` → `is False` + `checkpoint_is_terminal` |
| `worker.py:__init__` | `_checkpoint_next_step_index = None` sor hozzáadása |
| `worker.py:_handle_event` | `_checkpoint_next_step_index` beállítás |
| `worker.py:request_continue_from_checkpoint` | komment frissítés |
| `checkpoint_panel.py:show_checkpoint` | teljes metódus csere |
| `main_window.py:_on_checkpoint_reached` | teljes metódus csere |
