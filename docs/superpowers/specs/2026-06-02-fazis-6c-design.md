# FÁZIS 6C — Checkpoint terminális detektálás + resume infrastruktúra

**Dátum:** 2026-06-02
**Alapja:** d11.txt — felhasználói döntések
**Megközelítés:** B — helyes flag propagation + UI frissítés + start_step_index infrastruktúra

---

## Összefoglaló

A 6B-ben implementált `CheckpointPanel` a `resume_possible` flaget `True`-ra hardkódolta, és a "Folytatás checkpointból" gombot disabled stubként hagyta. A 6C javítja ezt:

1. `_run_manual_checkpoint` helyesen számítja ki, hogy a checkpoint terminális-e
2. `resume_possible` flag propagál: `TestResult → Worker event → CheckpointPanel`
3. `CheckpointPanel` gomb szövege és állapota `resume_possible` alapján frissül
4. `TestRunner.run()` kap `start_step_index` paramétert (jövőbeli nem-terminális folytatáshoz)
5. `Worker` tárolja a `next_step_index`-et (6D-ben lesz felhasználva)

A jelenlegi `bq_learning_physical()` planben a `MANUAL_CHECKPOINT` az utolsó lépés
(`next_step_index = 9 >= len(steps) = 9`), ezért mindig `checkpoint_is_terminal = True`
és `resume_possible = False`. A "Folytatás" gomb disabled marad, de most a valóság alapján.

---

## 1. Backend változások (`Prog/src/test_runner.py`)

### 1.1 `TestResult` — új mezők

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

### 1.2 `_run_manual_checkpoint` — terminális detektálás

```python
def _run_manual_checkpoint(self, step: TestStep) -> TestResult:
    steps_list = list(self._active_plan.steps)
    next_step_index = steps_list.index(step) + 1
    checkpoint_is_terminal = (next_step_index >= len(steps_list))
    resume_possible = not checkpoint_is_terminal

    event = {
        "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
        "event_message": "BQ learning fizikai ciklus kézi ellenőrzési pontja elérve.",
        "step_name": step.label,
        "status": "CHECKPOINT_STOPPED",
        "resume_possible": resume_possible,        # ← volt: True (hardcode)
        "checkpoint_is_terminal": checkpoint_is_terminal,  # ← új
        "next_step_index": next_step_index,
        "total_charge_ah": self._total_charge_ah,
        "total_discharge_ah": self._total_discharge_ah,
    }
    ...
    return TestResult(
        status="CHECKPOINT_STOPPED",
        reason="MANUAL_BQ_CHECKPOINT_REACHED",
        total_charge_ah=self._total_charge_ah,
        total_discharge_ah=self._total_discharge_ah,
        resume_possible=resume_possible,     # ← új
        next_step_index=next_step_index,     # ← új
    )
```

### 1.3 `TestRunner.run()` — `start_step_index` paraméter

```python
def run(self, test_plan: TestPlan, start_step_index: int = 0) -> TestResult:
    self._active_plan = test_plan
    self._start_time = datetime.now(timezone.utc)
    self.status = "RUNNING"
    self._total_charge_ah = 0.0
    self._total_discharge_ah = 0.0

    try:
        for step in list(test_plan.steps)[start_step_index:]:   # ← slice
            ...
```

`start_step_index = 0` (default) → backward-compatible, minden meglévő hívás változatlan.

---

## 2. Worker változások (`Prog/gui/worker.py`)

### 2.1 `_checkpoint_next_step_index` tárolás

```python
def __init__(self, runner, test_plan: TestPlan) -> None:
    ...
    self._checkpoint_next_step_index: int | None = None   # ← új

def _handle_event(self, event: dict) -> None:
    self.event_ready.emit(event)
    if event.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED":
        self._checkpoint_next_step_index = event.get("next_step_index")  # ← új
        self.checkpoint_reached.emit(event)
```

### 2.2 `request_continue_from_checkpoint` — felkészített stub

```python
@Slot()
def request_continue_from_checkpoint(self) -> None:
    # 6D: QThread újraindítás + self._runner.run(self._test_plan, self._checkpoint_next_step_index)
    pass
```

A `next_step_index` már tárolva van, a threading logika 6D feladat.

---

## 3. CheckpointPanel változások (`Prog/gui/panels/checkpoint_panel.py`)

### 3.1 `show_checkpoint()` — gomb és fejléc frissítés

```python
@Slot(dict)
def show_checkpoint(self, event: dict) -> None:
    # meglévő metadata frissítés...

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

### 3.2 Változatlan elemek

- 3 gomb bekötése (`continue_requested`, `close_requested`, `emergency_stop_requested`) — változatlan
- Checklist elemek — változatlan
- `_build_ui()` — változatlan

---

## 4. MainWindow változások (`Prog/gui/main_window.py`)

### 4.1 `_on_checkpoint_reached()` — státuszsor üzenet finomítás

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

---

## 5. Tesztstratégia

### 5.1 Backend tesztek (`Prog/tests/test_test_runner.py`) — 5 új teszt

| # | Teszt | Mit ellenőriz |
|---|-------|---------------|
| 1 | `test_testresult_resume_possible_default_false` | `TestResult()` default resume_possible=False |
| 2 | `test_testresult_next_step_index_default_zero` | `TestResult()` default next_step_index=0 |
| 3 | `test_checkpoint_is_terminal_for_bq_learning` | `bq_learning_physical()` → resume_possible=False, next_step_index=9 |
| 4 | `test_checkpoint_resume_possible_in_event_dict` | event dict resume_possible=False + checkpoint_is_terminal=True |
| 5 | `test_run_with_start_step_index_skips_steps` | `run(plan, start_step_index=2)` → on_step_changed csak a 3. lépéstől hívódik |

### 5.2 Worker tesztek (`Prog/tests/gui/test_worker.py`) — 1 új teszt

| # | Teszt | Mit ellenőriz |
|---|-------|---------------|
| 1 | `test_worker_stores_checkpoint_next_step_index` | `_handle_event` MANUAL_BQ_CHECKPOINT_REACHED → `_checkpoint_next_step_index` beállítódik |

### 5.3 Manuális smoke teszt

```
[ ] python Prog/main.py elindul
[ ] CheckpointPanel megjelenik checkpoint eseménynél
[ ] "Folytatás — nem elérhető" feliratú, disabled gomb látszik
[ ] Tooltip mutatja: "Ez a checkpoint terminális — a teszt itt ért véget."
[ ] Fejléc: "BQ_LEARNING_PHYSICAL — kézi ellenőrzési pont (terminális)"
[ ] Status bar: "BQ checkpoint elérve — végezd el a BQ műveletet, majd zárd le a sessiont."
[ ] "Teszt lezárása" gomb visszaállít a konfigurációs tabra
```

---

## 6. Érintett fájlok

| Fájl | Változás típusa |
|------|----------------|
| `Prog/src/test_runner.py` | Módosítás — `TestResult`, `run(start_step_index)`, `_run_manual_checkpoint` |
| `Prog/gui/worker.py` | Módosítás — `_checkpoint_next_step_index` tárolás |
| `Prog/gui/panels/checkpoint_panel.py` | Módosítás — `show_checkpoint()` gomb + fejléc |
| `Prog/gui/main_window.py` | Módosítás — `_on_checkpoint_reached()` üzenet |
| `Prog/tests/test_test_runner.py` | Módosítás — 5 új teszt |
| `Prog/tests/gui/test_worker.py` | Módosítás — 1 új teszt |

**Elvárt végeredmény:** 353 + 6 = **359 teszt zöld**

---

## 7. Nyitott kérdések / 6D-re halasztva

- `request_continue_from_checkpoint` threading implementációja: QThread újraindítás vagy új Worker példány?
- Checkpoint session mentés folytatáshoz: `session_id`, `parent_session_id` kapcsolat
- Nem-terminális checkpoint tesztelése valós plannel (jelenleg csak stubs tesztelik)
- Esetleges `CheckpointPanel` widget tesztek (d11.txt: opcionális, nem blokkoló)
