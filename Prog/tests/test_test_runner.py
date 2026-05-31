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
        self.accumulated_charge_Ah = 0.0


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
        self.accumulated_discharge_Ah = 0.0


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
# Fixture helper                                                      #
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
        plan = TestPlan(test_type=TestType.CHARACTERIZATION, steps=())
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
