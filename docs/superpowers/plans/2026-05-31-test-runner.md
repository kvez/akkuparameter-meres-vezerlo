# TestRunner (FÁZIS 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Megvalósítani a `Prog/src/test_runner.py` főfolyamat-vezérlőt, amely CHARACTERIZATION és BQ_LEARNING_PHYSICAL teszttípusokat hajt végre mock és valós instrumentekkel egyaránt.

**Architecture:** Tiszta Python üzleti logika osztály; blokkoló `run(test_plan)` metódus; stop flag-ek (`stop_requested`, `emergency_stop_requested`); nincs GUI/thread/asyncio. A TestRunner maga gyűjti össze a logger sample dict-et az InstrumentManager.read_all() + controller state alapján. Döntési alap: d4.txt, d5.txt, d6.txt.

**Tech Stack:** Python 3.11+, pytest, meglévő mock driverek (MockPSU, MockLoad, MockDMM), meglévő kontrollerek (ChargeController, DischargeController, RelaxController).

---

## Fájlstruktúra

| Fájl | Művelet | Tartalom |
|------|---------|---------|
| `Prog/src/test_runner.py` | CREATE | TestType, StepKind, TestStep, TestPlan, TestResult, TestRunnerConfig, TestRunner |
| `Prog/tests/test_test_runner.py` | CREATE | Stub kontrollerek + unit tesztek |

**Megjegyzés a stub kontrollerekről:** A TestRunner orchestrációs logikáját stub kontrollerekkel teszteljük. Ezek ugyanolyan state property-kel és advance() metódussal rendelkeznek, mint a valós kontrollerek, de egyszerűen N lépés után DONE/FAULT állapotba kerülnek. A valós kontrollereket (ChargeController stb.) a saját tesztfájljaik már tesztelik.

**Megjegyzés a BQ_LEARNING_PHYSICAL reset()-ről:** Ugyanaz a charge_controller kétszer fut (cycle 1 és cycle 2). Ha a kontroller implementál `reset()` metódust, a TestRunner meghívja azt az ismételt lépés előtt. A stub kontrollerek implementálnak reset()-et. A valós kontrollerek esetén FÁZIS 5-ben friss instance-eket kell injektálni az ismételt ciklusokhoz.

---

## Task 1: Adatstruktúrák (TestType, StepKind, TestStep, TestPlan, TestResult, TestRunnerConfig)

**Files:**
- Create: `Prog/src/test_runner.py`
- Create: `Prog/tests/test_test_runner.py`

- [ ] **Step 1.1: Tesztfájl skeleton létrehozása stub kontrollerekkel**

Hozd létre a `Prog/tests/test_test_runner.py` fájlt:

```python
"""
TestRunner unit tesztek.
Stub kontrollereket használ az orchestrációs logika izolált teszteléséhez.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from Prog.src.charge_controller import ChargeState
from Prog.src.discharge_controller import DischargeState
from Prog.src.relax_controller import RelaxState
from Prog.src.test_runner import (
    TestType, StepKind, TestStep, TestPlan,
    TestResult, TestRunnerConfig, TestRunner,
)


# ------------------------------------------------------------------ #
# Stub kontrollerek az orchestrációs logika teszteléséhez            #
# ------------------------------------------------------------------ #

class _StubChargeCtrl:
    """N advance() hívás után CHARGE_DONE állapotba kerül."""
    def __init__(self, steps_to_done: int = 3, fault_at_step: int | None = None):
        self._state = ChargeState.INIT
        self._steps = steps_to_done
        self._fault_at = fault_at_step
        self._tick = 0
        self.accumulated_charge_Ah: float = 0.0
        self.fault_reason: str = ""
        self.last_integration_source: str = "PSU_READBACK"

    @property
    def state(self) -> ChargeState:
        return self._state

    def advance(self, dt_s: float) -> ChargeState:
        self._tick += 1
        self.accumulated_charge_Ah += 0.01
        if self._fault_at is not None and self._tick >= self._fault_at:
            self.fault_reason = "STUB_CHARGE_FAULT"
            self._state = ChargeState.FAULT
        elif self._tick >= self._steps:
            self._state = ChargeState.CHARGE_DONE
        return self._state

    def reset(self) -> None:
        self._state = ChargeState.INIT
        self._tick = 0


class _StubDischargeCtrl:
    """N advance() hívás után DISCHARGE_DONE állapotba kerül."""
    def __init__(self, steps_to_done: int = 3, fault_at_step: int | None = None):
        self._state = DischargeState.INIT
        self._steps = steps_to_done
        self._fault_at = fault_at_step
        self._tick = 0
        self.accumulated_discharge_Ah: float = 0.0
        self.fault_reason: str = ""
        self.last_integration_source: str = "LOAD_READBACK"

    @property
    def state(self) -> DischargeState:
        return self._state

    def advance(self, dt_s: float) -> DischargeState:
        self._tick += 1
        self.accumulated_discharge_Ah += 0.01
        if self._fault_at is not None and self._tick >= self._fault_at:
            self.fault_reason = "STUB_DISCHARGE_FAULT"
            self._state = DischargeState.FAULT
        elif self._tick >= self._steps:
            self._state = DischargeState.DISCHARGE_DONE
        return self._state

    def reset(self) -> None:
        self._state = DischargeState.INIT
        self._tick = 0


class _StubRelaxCtrl:
    """N advance() hívás után RELAX_DONE állapotba kerül."""
    def __init__(self, steps_to_done: int = 2):
        self._state = RelaxState.INIT
        self._steps = steps_to_done
        self._tick = 0

    @property
    def state(self) -> RelaxState:
        return self._state

    def advance(self, dt_s: float) -> RelaxState:
        self._tick += 1
        if self._tick >= self._steps:
            self._state = RelaxState.RELAX_DONE
        return self._state

    def reset(self) -> None:
        self._state = RelaxState.INIT
        self._tick = 0


# ------------------------------------------------------------------ #
# Fixture                                                             #
# ------------------------------------------------------------------ #

def _make_runner(tmp_path, cc=None, dc=None, rc=None):
    from Prog.src.battery_profile import BatteryProfile
    from Prog.src.safety import SafetyManager, PsuMode
    from Prog.src.logger import Logger, LogConfig
    from Prog.src.instrument_manager import InstrumentManager
    from Prog.tests.mock_drivers.mock_psu import MockPSU
    from Prog.tests.mock_drivers.mock_load import MockLoad
    from Prog.tests.mock_drivers.mock_dmm import MockDMM

    profile = BatteryProfile(
        battery_name="Test", manufacturer="FIAMM", model="FG",
        nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
    )
    instruments = InstrumentManager(
        psu=MockPSU(voltage_V=14.4, current_A=1.75),
        load=MockLoad(voltage_V=12.5, current_A=0.7),
        dmm_voltage=MockDMM(voltage_V=12.5, temperature_C=25.0),
        dmm_temperature=MockDMM(voltage_V=0.0, temperature_C=25.0),
    )
    safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
    logger = Logger(session_dir=tmp_path, config=LogConfig())
    config = TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="test")

    runner = TestRunner(
        instrument_manager=instruments,
        safety=safety,
        logger=logger,
        profile=profile,
        config=config,
        charge_controller=cc or _StubChargeCtrl(steps_to_done=3),
        discharge_controller=dc or _StubDischargeCtrl(steps_to_done=3),
        relax_controller=rc or _StubRelaxCtrl(steps_to_done=2),
    )
    return runner, logger
```

- [ ] **Step 1.2: Adatstruktúra tesztek hozzáadása**

Fűzd hozzá a `Prog/tests/test_test_runner.py` végére:

```python
# ------------------------------------------------------------------ #
# Task 1: Adatstruktúrák                                             #
# ------------------------------------------------------------------ #

class TestDataStructures:
    def test_test_type_values(self):
        assert TestType.CHARACTERIZATION.value == "CHARACTERIZATION"
        assert TestType.BQ_LEARNING_PHYSICAL.value == "BQ_LEARNING_PHYSICAL"

    def test_step_kind_values(self):
        for kind in StepKind:
            assert kind.value == kind.name

    def test_test_step_frozen(self):
        step = TestStep(kind=StepKind.CHARGE, label="charge")
        with pytest.raises(Exception):
            step.kind = StepKind.DISCHARGE  # type: ignore[misc]

    def test_test_plan_frozen(self):
        plan = TestPlan(test_type=TestType.CHARACTERIZATION, steps=[])
        with pytest.raises(Exception):
            plan.test_type = TestType.BQ_LEARNING_PHYSICAL  # type: ignore[misc]

    def test_test_result_defaults(self):
        result = TestResult(status="DONE")
        assert result.reason == ""
        assert result.total_charge_ah == 0.0
        assert result.total_discharge_ah == 0.0

    def test_runner_config_defaults(self):
        config = TestRunnerConfig()
        assert config.runner_tick_s == 2.0
        assert config.sleep_enabled is True
        assert config.test_name == "unnamed"
```

- [ ] **Step 1.3: Futtatás — várható FAIL (ImportError)**

```
python -m pytest Prog/tests/test_test_runner.py::TestDataStructures -v
```

Várható: `ImportError: cannot import name 'TestType' from 'Prog.src.test_runner'` (a fájl még nem létezik).

- [ ] **Step 1.4: `Prog/src/test_runner.py` létrehozása — adatstruktúrák**

```python
"""
TestRunner — fő folyamatvezérlő.
CHARACTERIZATION és BQ_LEARNING_PHYSICAL teszttípusok.
GUI-független, blokkoló run(), stop flag alapú megállítás.
[d4.txt][d5.txt][d6.txt]
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from Prog.src.battery_profile import BatteryProfile
from Prog.src.charge_controller import ChargeState
from Prog.src.discharge_controller import DischargeState
from Prog.src.instrument_manager import InstrumentManager
from Prog.src.logger import Logger, CSV_COLUMNS
from Prog.src.relax_controller import RelaxState
from Prog.src.safety import SafetyManager


class TestType(Enum):
    CHARACTERIZATION = "CHARACTERIZATION"
    BQ_LEARNING_PHYSICAL = "BQ_LEARNING_PHYSICAL"


class StepKind(Enum):
    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"
    RELAX = "RELAX"
    MANUAL_CHECKPOINT = "MANUAL_CHECKPOINT"


@dataclass(frozen=True)
class TestStep:
    kind: StepKind
    label: str


@dataclass(frozen=True)
class TestPlan:
    test_type: TestType
    steps: list

    @staticmethod
    def characterization() -> "TestPlan":
        raise NotImplementedError

    @staticmethod
    def bq_learning_physical() -> "TestPlan":
        raise NotImplementedError


@dataclass
class TestResult:
    status: str
    reason: str = ""
    total_charge_ah: float = 0.0
    total_discharge_ah: float = 0.0


@dataclass
class TestRunnerConfig:
    runner_tick_s: float = 2.0
    test_name: str = "unnamed"
    sleep_enabled: bool = True
```

- [ ] **Step 1.5: Futtatás — várható PASS**

```
python -m pytest Prog/tests/test_test_runner.py::TestDataStructures -v
```

Várható: `6 passed`.

---

## Task 2: TestPlan gyártómetódusok

**Files:**
- Modify: `Prog/src/test_runner.py` (TestPlan factory methods)
- Modify: `Prog/tests/test_test_runner.py` (új tesztek)

- [ ] **Step 2.1: Tesztek hozzáadása**

Fűzd hozzá a `Prog/tests/test_test_runner.py` végére:

```python
class TestTestPlanFactories:
    def test_characterization_step_count(self):
        plan = TestPlan.characterization()
        assert len(plan.steps) == 4

    def test_characterization_step_order(self):
        plan = TestPlan.characterization()
        kinds = [s.kind for s in plan.steps]
        assert kinds == [
            StepKind.CHARGE,
            StepKind.RELAX,
            StepKind.DISCHARGE,
            StepKind.RELAX,
        ]

    def test_characterization_labels(self):
        plan = TestPlan.characterization()
        labels = [s.label for s in plan.steps]
        assert labels == [
            "charge",
            "relax_after_charge",
            "discharge",
            "relax_after_discharge",
        ]

    def test_characterization_type(self):
        plan = TestPlan.characterization()
        assert plan.test_type == TestType.CHARACTERIZATION

    def test_bq_learning_step_count(self):
        plan = TestPlan.bq_learning_physical()
        assert len(plan.steps) == 9

    def test_bq_learning_last_step_is_manual_checkpoint(self):
        plan = TestPlan.bq_learning_physical()
        assert plan.steps[-1].kind == StepKind.MANUAL_CHECKPOINT
        assert plan.steps[-1].label == "manual_bq_checkpoint"

    def test_bq_learning_has_two_charge_steps(self):
        plan = TestPlan.bq_learning_physical()
        charge_steps = [s for s in plan.steps if s.kind == StepKind.CHARGE]
        assert len(charge_steps) == 2
        assert charge_steps[0].label == "charge_1"
        assert charge_steps[1].label == "charge_2"

    def test_bq_learning_type(self):
        plan = TestPlan.bq_learning_physical()
        assert plan.test_type == TestType.BQ_LEARNING_PHYSICAL
```

- [ ] **Step 2.2: Futtatás — várható FAIL (NotImplementedError)**

```
python -m pytest Prog/tests/test_test_runner.py::TestTestPlanFactories -v
```

Várható: `NotImplementedError`.

- [ ] **Step 2.3: Gyártómetódusok implementálása**

Cseréld ki a `TestPlan` osztályt `Prog/src/test_runner.py`-ban:

```python
@dataclass(frozen=True)
class TestPlan:
    test_type: TestType
    steps: list

    @staticmethod
    def characterization() -> "TestPlan":
        return TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=[
                TestStep(StepKind.CHARGE,     "charge"),
                TestStep(StepKind.RELAX,      "relax_after_charge"),
                TestStep(StepKind.DISCHARGE,  "discharge"),
                TestStep(StepKind.RELAX,      "relax_after_discharge"),
            ],
        )

    @staticmethod
    def bq_learning_physical() -> "TestPlan":
        return TestPlan(
            test_type=TestType.BQ_LEARNING_PHYSICAL,
            steps=[
                TestStep(StepKind.CHARGE,             "charge_1"),
                TestStep(StepKind.RELAX,              "relax_after_charge_1"),
                TestStep(StepKind.DISCHARGE,          "discharge_1"),
                TestStep(StepKind.RELAX,              "relax_after_discharge_1"),
                TestStep(StepKind.CHARGE,             "charge_2"),
                TestStep(StepKind.RELAX,              "relax_after_charge_2"),
                TestStep(StepKind.DISCHARGE,          "discharge_2"),
                TestStep(StepKind.RELAX,              "relax_after_discharge_2"),
                TestStep(StepKind.MANUAL_CHECKPOINT,  "manual_bq_checkpoint"),
            ],
        )
```

- [ ] **Step 2.4: Futtatás — várható PASS**

```
python -m pytest Prog/tests/test_test_runner.py::TestTestPlanFactories -v
```

Várható: `8 passed`.

- [ ] **Step 2.5: Commit**

```
git add Prog/src/test_runner.py Prog/tests/test_test_runner.py
git commit -m "add: test_runner — adatstruktúrák és TestPlan gyártómetódusok (Task 1-2)"
```

---

## Task 3: TestRunner.__init__ és stop API

**Files:**
- Modify: `Prog/src/test_runner.py` (TestRunner class)
- Modify: `Prog/tests/test_test_runner.py`

- [ ] **Step 3.1: Tesztek hozzáadása**

```python
class TestTestRunnerInit:
    def test_initial_status_is_idle(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        assert runner.status == "IDLE"
        logger.close()

    def test_initial_flags_false(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        assert runner.stop_requested is False
        assert runner.emergency_stop_requested is False
        logger.close()

    def test_request_stop_sets_flag(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.request_stop()
        assert runner.stop_requested is True
        logger.close()

    def test_request_emergency_stop_sets_flag_and_reason(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.request_emergency_stop("TEST_REASON")
        assert runner.emergency_stop_requested is True
        assert runner.emergency_stop_reason == "TEST_REASON"
        logger.close()

    def test_request_emergency_stop_default_reason(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.request_emergency_stop()
        assert runner.emergency_stop_reason == "USER_EMERGENCY_STOP"
        logger.close()
```

- [ ] **Step 3.2: Futtatás — várható FAIL**

```
python -m pytest Prog/tests/test_test_runner.py::TestTestRunnerInit -v
```

Várható: `ImportError` (TestRunner még nem létezik).

- [ ] **Step 3.3: TestRunner osztály alapváz hozzáadása**

Fűzd hozzá a `Prog/src/test_runner.py` végére (TestRunnerConfig után):

```python
class TestRunner:
    def __init__(
        self,
        instrument_manager: InstrumentManager,
        safety: SafetyManager,
        logger: Logger,
        profile: BatteryProfile,
        config: TestRunnerConfig,
        charge_controller,
        discharge_controller,
        relax_controller,
    ) -> None:
        self._instruments = instrument_manager
        self._safety = safety
        self._logger = logger
        self._profile = profile
        self._config = config
        self._charge_ctrl = charge_controller
        self._discharge_ctrl = discharge_controller
        self._relax_ctrl = relax_controller

        self.stop_requested: bool = False
        self.emergency_stop_requested: bool = False
        self.emergency_stop_reason: str = ""

        self.status: str = "IDLE"
        self.current_step: Optional[TestStep] = None

        self._total_charge_ah: float = 0.0
        self._total_discharge_ah: float = 0.0
        self._start_time: Optional[datetime] = None

    def request_stop(self) -> None:
        self.stop_requested = True

    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None:
        self.emergency_stop_requested = True
        self.emergency_stop_reason = reason

    def run(self, test_plan: TestPlan) -> TestResult:
        raise NotImplementedError
```

- [ ] **Step 3.4: Futtatás — várható PASS**

```
python -m pytest Prog/tests/test_test_runner.py::TestTestRunnerInit -v
```

Várható: `5 passed`.

---

## Task 4: Segédprediokátum metódusok

**Files:**
- Modify: `Prog/src/test_runner.py`
- Modify: `Prog/tests/test_test_runner.py`

- [ ] **Step 4.1: Tesztek hozzáadása**

```python
class TestHelperPredicates:
    def test_is_finished_charge_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_charge_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.FAULT
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_charge_running(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        assert runner._is_finished(stub) is False
        logger.close()

    def test_is_finished_discharge_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_relax_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAX_DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_relax_relaxing(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAXING
        assert runner._is_finished(stub) is False
        logger.close()

    def test_controller_faulted_charge_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.FAULT
        assert runner._controller_faulted(stub) is True
        logger.close()

    def test_controller_faulted_charge_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_DONE
        assert runner._controller_faulted(stub) is False
        logger.close()

    def test_controller_faulted_relax(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAXING
        assert runner._controller_faulted(stub) is False
        logger.close()

    def test_step_can_interrupt_relax(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.RELAX, "relax")
        assert runner._step_can_be_gracefully_interrupted(step) is True
        logger.close()

    def test_step_can_interrupt_manual_checkpoint(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        assert runner._step_can_be_gracefully_interrupted(step) is True
        logger.close()

    def test_step_cannot_interrupt_charge(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.CHARGE, "charge")
        assert runner._step_can_be_gracefully_interrupted(step) is False
        logger.close()

    def test_step_cannot_interrupt_discharge(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.DISCHARGE, "discharge")
        assert runner._step_can_be_gracefully_interrupted(step) is False
        logger.close()

    def test_controller_for_charge_step(self, tmp_path):
        cc = _StubChargeCtrl()
        runner, logger = _make_runner(tmp_path, cc=cc)
        step = TestStep(StepKind.CHARGE, "charge")
        assert runner._controller_for_step(step) is cc
        logger.close()

    def test_controller_for_unknown_step_raises(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "x")
        with pytest.raises(ValueError):
            runner._controller_for_step(step)
        logger.close()
```

- [ ] **Step 4.2: Futtatás — várható FAIL**

```
python -m pytest Prog/tests/test_test_runner.py::TestHelperPredicates -v
```

Várható: `AttributeError: 'TestRunner' object has no attribute '_is_finished'`.

- [ ] **Step 4.3: Segédmetódusok implementálása**

Fűzd hozzá a TestRunner osztályba (a `run()` után):

```python
    def _is_finished(self, controller) -> bool:
        state = getattr(controller, "state", None)
        if isinstance(state, ChargeState):
            return state in (ChargeState.CHARGE_DONE, ChargeState.FAULT, ChargeState.SAFE_OFF)
        if isinstance(state, DischargeState):
            return state in (DischargeState.DISCHARGE_DONE, DischargeState.FAULT, DischargeState.SAFE_OFF)
        if isinstance(state, RelaxState):
            return state == RelaxState.RELAX_DONE
        return True

    def _controller_faulted(self, controller) -> bool:
        state = getattr(controller, "state", None)
        if isinstance(state, ChargeState):
            return state in (ChargeState.FAULT, ChargeState.SAFE_OFF)
        if isinstance(state, DischargeState):
            return state in (DischargeState.FAULT, DischargeState.SAFE_OFF)
        return False

    def _step_can_be_gracefully_interrupted(self, step: TestStep) -> bool:
        return step.kind in (StepKind.RELAX, StepKind.MANUAL_CHECKPOINT)

    def _controller_for_step(self, step: TestStep):
        if step.kind == StepKind.CHARGE:
            return self._charge_ctrl
        if step.kind == StepKind.DISCHARGE:
            return self._discharge_ctrl
        if step.kind == StepKind.RELAX:
            return self._relax_ctrl
        raise ValueError(f"No controller for step kind: {step.kind}")
```

- [ ] **Step 4.4: Futtatás — várható PASS**

```
python -m pytest Prog/tests/test_test_runner.py::TestHelperPredicates -v
```

Várható: `15 passed`.

---

## Task 5: _build_sample

**Files:**
- Modify: `Prog/src/test_runner.py`
- Modify: `Prog/tests/test_test_runner.py`

- [ ] **Step 5.1: Tesztek hozzáadása**

```python
class TestBuildSample:
    def test_charge_step_fields(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["test_name"] == "test"
        assert sample["step_name"] == "charge"
        assert sample["state"] == "CHARGE_CC"
        assert sample["isolation_state"] == "PSU_OUTPUT_OFF_ONLY"
        assert sample["psu_mode"] == "INDEPENDENT"
        assert sample["discharge_current_A"] == 0.0
        assert sample["charge_current_A"] is not None
        assert sample["signed_current_A"] is not None
        assert sample["accumulated_charge_Ah"] == stub.accumulated_charge_Ah
        logger.close()

    def test_discharge_step_fields(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_CC_RUN
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert sample["step_name"] == "discharge"
        assert sample["state"] == "DISCHARGE_CC_RUN"
        assert sample["charge_current_A"] == 0.0
        assert sample["isolation_state"] == "PSU_OUTPUT_OFF_ONLY"
        logger.close()

    def test_relax_step_fields(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAXING
        step = TestStep(StepKind.RELAX, "relax_after_charge")

        sample = runner._build_sample(step, stub)

        assert sample["step_name"] == "relax_after_charge"
        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == 0.0
        logger.close()

    def test_u_drop_computed(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        # MockPSU: voltage_V=14.4, MockDMM: voltage_V=12.5 → u_drop=1.9
        stub = _StubChargeCtrl()
        step = TestStep(StepKind.CHARGE, "charge")
        sample = runner._build_sample(step, stub)
        assert sample["u_drop_V"] == pytest.approx(14.4 - 12.5, abs=0.01)
        logger.close()

    def test_timestamp_present(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        step = TestStep(StepKind.CHARGE, "charge")
        sample = runner._build_sample(step, stub)
        assert sample["timestamp_iso"] is not None
        assert sample["elapsed_s"] is not None
        logger.close()
```

- [ ] **Step 5.2: Futtatás — várható FAIL**

```
python -m pytest Prog/tests/test_test_runner.py::TestBuildSample -v
```

Várható: `AttributeError: 'TestRunner' object has no attribute '_build_sample'`.

- [ ] **Step 5.3: _build_sample implementálása**

Fűzd hozzá a TestRunner osztályba:

```python
    def _build_sample(self, step: TestStep, controller) -> dict:
        now = datetime.now(timezone.utc)
        readings = self._instruments.read_all()

        sample: dict = {col: None for col in CSV_COLUMNS}

        elapsed = (now - self._start_time).total_seconds() if self._start_time else 0.0
        sample["timestamp_iso"] = now.isoformat()
        sample["elapsed_s"] = round(elapsed, 3)
        sample["test_name"] = self._config.test_name
        sample["step_name"] = step.label
        sample["state"] = controller.state.value if hasattr(controller, "state") else None

        sample["battery_voltage_V"] = readings.get("battery_voltage_V")
        sample["battery_temperature_C"] = readings.get("battery_temperature_C")
        sample["psu_readback_voltage_V"] = readings.get("psu_readback_voltage_V")
        sample["psu_readback_current_A"] = readings.get("psu_readback_current_A")
        sample["load_readback_voltage_V"] = readings.get("load_readback_voltage_V")
        sample["load_readback_current_A"] = readings.get("load_readback_current_A")
        sample["psu_mode"] = self._safety.psu_mode.value
        sample["isolation_state"] = "PSU_OUTPUT_OFF_ONLY"

        u_batt = readings.get("battery_voltage_V")
        u_psu = readings.get("psu_readback_voltage_V")
        i_psu = readings.get("psu_readback_current_A")
        i_load = readings.get("load_readback_current_A")

        if u_batt is not None and u_psu is not None:
            sample["u_drop_V"] = round(u_psu - u_batt, 4)
            if i_psu is not None:
                sample["diode_power_W"] = round(sample["u_drop_V"] * i_psu, 4)

        sample["dmm_voltage_valid"] = u_batt is not None
        sample["dmm_temperature_valid"] = readings.get("battery_temperature_C") is not None

        if step.kind == StepKind.CHARGE:
            sample["charge_current_A"] = i_psu
            sample["discharge_current_A"] = 0.0
            sample["signed_current_A"] = i_psu
            sample["accumulated_charge_Ah"] = getattr(controller, "accumulated_charge_Ah", None)
            sample["accumulated_discharge_Ah"] = self._total_discharge_ah
            sample["integration_current_source"] = getattr(controller, "last_integration_source", None)
        elif step.kind == StepKind.DISCHARGE:
            sample["charge_current_A"] = 0.0
            sample["discharge_current_A"] = i_load
            sample["signed_current_A"] = -(i_load or 0.0)
            sample["accumulated_charge_Ah"] = self._total_charge_ah
            sample["accumulated_discharge_Ah"] = getattr(controller, "accumulated_discharge_Ah", None)
            sample["integration_current_source"] = getattr(controller, "last_integration_source", None)
        else:
            sample["charge_current_A"] = 0.0
            sample["discharge_current_A"] = 0.0
            sample["signed_current_A"] = 0.0
            sample["accumulated_charge_Ah"] = self._total_charge_ah
            sample["accumulated_discharge_Ah"] = self._total_discharge_ah

        return sample
```

- [ ] **Step 5.4: Futtatás — várható PASS**

```
python -m pytest Prog/tests/test_test_runner.py::TestBuildSample -v
```

Várható: `5 passed`.

---

## Task 6: _emergency_stop, _graceful_stop, _run_manual_checkpoint

**Files:**
- Modify: `Prog/src/test_runner.py`
- Modify: `Prog/tests/test_test_runner.py`

- [ ] **Step 6.1: Tesztek hozzáadása**

```python
class TestStopMethods:
    def test_emergency_stop_returns_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        result = runner._emergency_stop("TEST_REASON")
        assert result.status == "FAULT"
        assert result.reason == "TEST_REASON"
        assert runner.status == "FAULT"
        logger.close()

    def test_emergency_stop_accumulates_ah(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_charge_ah = 1.5
        runner._total_discharge_ah = 1.2
        result = runner._emergency_stop("TEST")
        assert result.total_charge_ah == pytest.approx(1.5)
        assert result.total_discharge_ah == pytest.approx(1.2)
        logger.close()

    def test_graceful_stop_returns_stopped(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        result = runner._graceful_stop("USER_STOP")
        assert result.status == "STOPPED"
        assert result.reason == "USER_STOP"
        assert runner.status == "STOPPED"
        logger.close()

    def test_manual_checkpoint_returns_stopped(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_charge_ah = 1.5
        runner._total_discharge_ah = 1.4
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_manual_checkpoint(step)
        assert result.status == "STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        assert result.total_charge_ah == pytest.approx(1.5)
        assert result.total_discharge_ah == pytest.approx(1.4)
        logger.close()
```

- [ ] **Step 6.2: Futtatás — várható FAIL**

```
python -m pytest Prog/tests/test_test_runner.py::TestStopMethods -v
```

Várható: `AttributeError: 'TestRunner' object has no attribute '_emergency_stop'`.

- [ ] **Step 6.3: Három metódus implementálása**

Fűzd hozzá a TestRunner osztályba:

```python
    def _emergency_stop(self, reason: str) -> TestResult:
        self.status = "FAULT"
        self._instruments.safe_all_off()
        self._logger.log_event("EMERGENCY_STOP", reason, is_critical=True)
        self._logger.write_checkpoint({"status": "FAULT", "reason": reason})
        self._logger.flush_all()
        return TestResult(
            status="FAULT",
            reason=reason,
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
        )

    def _graceful_stop(self, reason: str) -> TestResult:
        self.status = "STOPPED"
        self._instruments.safe_all_off()
        self._logger.log_event("GRACEFUL_STOP", reason)
        self._logger.write_checkpoint({"status": "STOPPED", "reason": reason})
        self._logger.flush_all()
        return TestResult(
            status="STOPPED",
            reason=reason,
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
        )

    def _run_manual_checkpoint(self, step: TestStep) -> TestResult:
        self._logger.log_event("MANUAL_BQ_CHECKPOINT_REACHED", step.label)
        self._logger.write_checkpoint({
            "status": "MANUAL_CHECKPOINT",
            "step": step.label,
            "charge_ah": self._total_charge_ah,
            "discharge_ah": self._total_discharge_ah,
        })
        self._logger.flush_all()
        return TestResult(
            status="STOPPED",
            reason="MANUAL_BQ_CHECKPOINT_REACHED",
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
        )
```

- [ ] **Step 6.4: Futtatás — várható PASS**

```
python -m pytest Prog/tests/test_test_runner.py::TestStopMethods -v
```

Várható: `4 passed`.

- [ ] **Step 6.5: Commit**

```
git add Prog/src/test_runner.py Prog/tests/test_test_runner.py
git commit -m "add: test_runner — segédmetódusok, _build_sample, stop/emergency/checkpoint (Task 3-6)"
```

---

## Task 7: _run_step

**Files:**
- Modify: `Prog/src/test_runner.py`
- Modify: `Prog/tests/test_test_runner.py`

- [ ] **Step 7.1: Tesztek hozzáadása**

```python
class TestRunStep:
    def test_charge_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=3))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(step)
        assert result.status == "DONE"
        logger.close()

    def test_discharge_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, dc=_StubDischargeCtrl(steps_to_done=3))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.DISCHARGE, "discharge")
        result = runner._run_step(step)
        assert result.status == "DONE"
        logger.close()

    def test_relax_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, rc=_StubRelaxCtrl(steps_to_done=2))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        result = runner._run_step(step)
        assert result.status == "DONE"
        logger.close()

    def test_charge_step_fault_returns_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(fault_at_step=2))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(step)
        assert result.status == "FAULT"
        assert "FAULT" in result.reason
        logger.close()

    def test_emergency_stop_interrupts_charge(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.emergency_stop_requested = True
        runner.emergency_stop_reason = "USER_EMSTOP"
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(step)
        assert result.status == "FAULT"
        assert "USER_EMSTOP" in result.reason
        logger.close()

    def test_stop_request_does_not_interrupt_charge(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=3))
        runner._start_time = datetime.now(timezone.utc)
        runner.stop_requested = True
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(step)
        # CHARGE nem szakítható meg graceful stop-pal — végigfut DONE-ig
        assert result.status == "DONE"
        logger.close()

    def test_stop_request_interrupts_relax(self, tmp_path):
        runner, logger = _make_runner(tmp_path, rc=_StubRelaxCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.stop_requested = True
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        result = runner._run_step(step)
        assert result.status == "STOPPED"
        logger.close()

    def test_manual_checkpoint_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_step(step)
        assert result.status == "STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        logger.close()

    def test_charge_accumulates_ah(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=5)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        runner._run_step(step)
        assert runner._total_charge_ah == pytest.approx(0.05, abs=0.001)  # 5 × 0.01
        logger.close()

    def test_discharge_accumulates_ah(self, tmp_path):
        dc = _StubDischargeCtrl(steps_to_done=4)
        runner, logger = _make_runner(tmp_path, dc=dc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.DISCHARGE, "discharge")
        runner._run_step(step)
        assert runner._total_discharge_ah == pytest.approx(0.04, abs=0.001)  # 4 × 0.01
        logger.close()

    def test_controller_reset_called_if_available(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        # Első ciklus
        runner._run_step(TestStep(StepKind.CHARGE, "charge_1"))
        assert cc.state == ChargeState.CHARGE_DONE
        # Második ciklus — reset() visszaállítja INIT-re
        runner._run_step(TestStep(StepKind.CHARGE, "charge_2"))
        assert cc.state == ChargeState.CHARGE_DONE
        logger.close()
```

- [ ] **Step 7.2: Futtatás — várható FAIL**

```
python -m pytest Prog/tests/test_test_runner.py::TestRunStep -v
```

Várható: `NotImplementedError` (a `run()` stub visszaadja, de `_run_step` még hiányzik).

- [ ] **Step 7.3: _run_step implementálása**

Cseréld ki a `run()` stub-ot és fűzd hozzá a `_run_step`-et:

```python
    def run(self, test_plan: TestPlan) -> TestResult:
        raise NotImplementedError

    def _run_step(self, step: TestStep) -> TestResult:
        if step.kind == StepKind.MANUAL_CHECKPOINT:
            return self._run_manual_checkpoint(step)

        controller = self._controller_for_step(step)

        reset_fn = getattr(controller, "reset", None)
        if callable(reset_fn):
            reset_fn()

        charge_ah_before = getattr(controller, "accumulated_charge_Ah", 0.0)
        discharge_ah_before = getattr(controller, "accumulated_discharge_Ah", 0.0)

        while not self._is_finished(controller):
            if self.emergency_stop_requested:
                return self._emergency_stop(self.emergency_stop_reason)

            if self.stop_requested and self._step_can_be_gracefully_interrupted(step):
                return self._graceful_stop("USER_STOP_REQUESTED")

            controller.advance(self._config.runner_tick_s)

            sample = self._build_sample(step, controller)
            self._logger.log_sample(sample)
            self._logger.flush_all()
            self._logger.write_checkpoint({
                "status": "RUNNING",
                "step": step.label,
                "elapsed_s": sample.get("elapsed_s"),
                "charge_ah": self._total_charge_ah,
                "discharge_ah": self._total_discharge_ah,
            })

            if self._controller_faulted(controller):
                return TestResult(
                    status="FAULT",
                    reason=getattr(controller, "fault_reason", "CONTROLLER_FAULT"),
                )

            if self._config.sleep_enabled:
                time.sleep(self._config.runner_tick_s)

        if step.kind == StepKind.CHARGE:
            self._total_charge_ah += (
                getattr(controller, "accumulated_charge_Ah", 0.0) - charge_ah_before
            )
        elif step.kind == StepKind.DISCHARGE:
            self._total_discharge_ah += (
                getattr(controller, "accumulated_discharge_Ah", 0.0) - discharge_ah_before
            )

        return TestResult(status="DONE")
```

- [ ] **Step 7.4: Futtatás — várható PASS**

```
python -m pytest Prog/tests/test_test_runner.py::TestRunStep -v
```

Várható: `11 passed`.

---

## Task 8: run() + integrációs tesztek

**Files:**
- Modify: `Prog/src/test_runner.py`
- Modify: `Prog/tests/test_test_runner.py`

- [ ] **Step 8.1: Integrációs tesztek hozzáadása**

```python
class TestRunIntegration:
    def test_characterization_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.characterization())
        assert result.status == "DONE"
        assert runner.status == "DONE"
        logger.close()

    def test_characterization_accumulates_ah(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=5)   # 5 × 0.01 = 0.05 Ah
        dc = _StubDischargeCtrl(steps_to_done=4) # 4 × 0.01 = 0.04 Ah
        runner, logger = _make_runner(tmp_path, cc=cc, dc=dc)
        result = runner.run(TestPlan.characterization())
        assert result.status == "DONE"
        assert result.total_charge_ah == pytest.approx(0.05, abs=0.001)
        assert result.total_discharge_ah == pytest.approx(0.04, abs=0.001)
        logger.close()

    def test_bq_learning_stops_at_manual_checkpoint(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.bq_learning_physical())
        assert result.status == "STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        logger.close()

    def test_bq_learning_accumulates_two_cycles(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=3)    # 3 × 0.01 × 2 = 0.06 Ah total
        dc = _StubDischargeCtrl(steps_to_done=2) # 2 × 0.01 × 2 = 0.04 Ah total
        runner, logger = _make_runner(tmp_path, cc=cc, dc=dc)
        result = runner.run(TestPlan.bq_learning_physical())
        assert result.total_charge_ah == pytest.approx(0.06, abs=0.001)
        assert result.total_discharge_ah == pytest.approx(0.04, abs=0.001)
        logger.close()

    def test_emergency_stop_before_first_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.emergency_stop_requested = True
        runner.emergency_stop_reason = "PRE_TEST_FAULT"
        result = runner.run(TestPlan.characterization())
        assert result.status == "FAULT"
        assert "PRE_TEST_FAULT" in result.reason
        logger.close()

    def test_graceful_stop_before_first_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.stop_requested = True
        result = runner.run(TestPlan.characterization())
        assert result.status == "STOPPED"
        logger.close()

    def test_fault_in_charge_step_triggers_emergency(self, tmp_path):
        cc = _StubChargeCtrl(fault_at_step=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        result = runner.run(TestPlan.characterization())
        assert result.status == "FAULT"
        logger.close()

    def test_unhandled_exception_returns_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        # A run_step dob kivételt
        def _bad_run_step(step):
            raise RuntimeError("simulated crash")
        runner._run_step = _bad_run_step
        result = runner.run(TestPlan.characterization())
        assert result.status == "FAULT"
        assert "UNHANDLED_EXCEPTION" in result.reason
        logger.close()

    def test_samples_written_to_csv(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.run(TestPlan.characterization())
        logger.close()
        csv_path = tmp_path / "samples.csv"
        assert csv_path.exists()
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) > 2  # header + legalább 1 adat sor

    def test_events_written_to_events_csv(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.run(TestPlan.characterization())
        logger.close()
        events_path = tmp_path / "events.csv"
        assert events_path.exists()

    def test_checkpoint_json_written(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.run(TestPlan.characterization())
        logger.close()
        import json
        cp = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
        assert "status" in cp
```

- [ ] **Step 8.2: Futtatás — várható FAIL (NotImplementedError)**

```
python -m pytest Prog/tests/test_test_runner.py::TestRunIntegration -v
```

Várható: `NotImplementedError` (run() még nem implementált).

- [ ] **Step 8.3: run() implementálása**

Cseréld ki a `run()` NotImplementedError stub-ot:

```python
    def run(self, test_plan: TestPlan) -> TestResult:
        self._start_time = datetime.now(timezone.utc)
        self.status = "RUNNING"
        self._total_charge_ah = 0.0
        self._total_discharge_ah = 0.0

        try:
            for step in test_plan.steps:
                self.current_step = step

                if self.emergency_stop_requested:
                    return self._emergency_stop(self.emergency_stop_reason)

                if self.stop_requested:
                    return self._graceful_stop("USER_STOP_REQUESTED")

                step_result = self._run_step(step)

                if step_result.status == "FAULT":
                    return self._emergency_stop(step_result.reason or "STEP_FAULT")

                if step_result.status == "STOPPED":
                    return step_result

        except Exception as exc:
            return self._emergency_stop(f"UNHANDLED_EXCEPTION: {exc}")

        self.status = "DONE"
        return TestResult(
            status="DONE",
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
        )
```

- [ ] **Step 8.4: Futtatás — várható PASS**

```
python -m pytest Prog/tests/test_test_runner.py::TestRunIntegration -v
```

Várható: `11 passed`.

---

## Task 9: Teljes tesztcsomag futtatása és commit

- [ ] **Step 9.1: Teljes tesztcsomag futtatása**

```
python -m pytest --tb=short -q
```

Várható: minden teszt zöld (régi 244 + új tesztek).

- [ ] **Step 9.2: compileall ellenőrzés**

```
python -m compileall Prog -q
```

Várható: hibaüzenet nélkül lefut.

- [ ] **Step 9.3: Commit**

```
git add Prog/src/test_runner.py Prog/tests/test_test_runner.py docs/superpowers/
git commit -m "add: FÁZIS 5 — TestRunner (CHARACTERIZATION + BQ_LEARNING_PHYSICAL)"
```

---

## Összefoglalás

| Task | Fájl | Mit ad hozzá |
|------|------|-------------|
| 1–2 | test_runner.py | TestType, StepKind, TestStep, TestPlan (factory methods), TestResult, TestRunnerConfig |
| 3 | test_runner.py | TestRunner.__init__, request_stop, request_emergency_stop |
| 4 | test_runner.py | _is_finished, _controller_faulted, _step_can_be_gracefully_interrupted, _controller_for_step |
| 5 | test_runner.py | _build_sample (CSV_COLUMNS szerinti sample dict) |
| 6 | test_runner.py | _emergency_stop, _graceful_stop, _run_manual_checkpoint |
| 7 | test_runner.py | _run_step (blokkoló step ciklus, stop logika, Ah akkumulálás) |
| 8 | test_runner.py | run() (külső ciklus, lépések közötti stop, hibakezelés) |
| 9 | — | Teljes tesztcsomag + commit |

**Ismert korlát (FÁZIS 5):** A BQ_LEARNING_PHYSICAL valós kontrollerekkel csak akkor fut helyesen, ha a controller instance-ek `reset()` metódust implementálnak. A stub kontrollerek igen; a valós ChargeController/DischargeController nem — ezek esetén egymástól független instance-eket kell injektálni.
