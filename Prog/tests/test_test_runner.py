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

    _cc = cc if cc is not None else _StubChargeCtrl(steps_to_done=3)
    _dc = dc if dc is not None else _StubDischargeCtrl(steps_to_done=3)
    _rc = rc if rc is not None else _StubRelaxCtrl(steps_to_done=2)

    runner = TestRunner(
        instrument_manager=instruments,
        safety=safety,
        logger=logger,
        profile=profile,
        config=config,
        charge_ctrl_factory=lambda: _cc,
        discharge_ctrl_factory=lambda: _dc,
        relax_ctrl_factory=lambda: _rc,
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


# ------------------------------------------------------------------ #
# Task 3: TestRunner __init__ és stop API                            #
# ------------------------------------------------------------------ #

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


# ------------------------------------------------------------------ #
# Task 4: Segédpredikátum metódusok                                  #
# ------------------------------------------------------------------ #

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

    def test_step_can_interrupt_charge(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.CHARGE, "charge")
        assert runner._step_can_be_gracefully_interrupted(step) is True
        logger.close()

    def test_step_can_interrupt_discharge(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.DISCHARGE, "discharge")
        assert runner._step_can_be_gracefully_interrupted(step) is True
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


# ------------------------------------------------------------------ #
# Task 5: _build_sample                                               #
# ------------------------------------------------------------------ #

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
        # MockPSU: voltage_V=14.4, MockDMM: voltage_V=12.5 → u_drop = 14.4 - 12.5 = 1.9
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


# ------------------------------------------------------------------ #
# Task 6: Stop metódusok (_emergency_stop, _graceful_stop, _run_manual_checkpoint)  #
# ------------------------------------------------------------------ #

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
        runner._active_plan = TestPlan.bq_learning_physical()
        runner._total_charge_ah = 1.5
        runner._total_discharge_ah = 1.4
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_manual_checkpoint(8, step)
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        assert result.total_charge_ah == pytest.approx(1.5)
        assert result.total_discharge_ah == pytest.approx(1.4)
        logger.close()

    def test_manual_checkpoint_calls_on_event(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        events = []
        runner.on_event = events.append
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        runner._run_manual_checkpoint(8, step)
        assert len(events) == 1
        assert events[0]["event_code"] == "MANUAL_BQ_CHECKPOINT_REACHED"
        assert events[0]["status"] == "CHECKPOINT_STOPPED"
        assert events[0]["resume_possible"] is False          # ← volt: True
        assert events[0]["checkpoint_is_terminal"] is True   # ← új assertion
        assert "next_step_index" in events[0]
        logger.close()


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
        result = runner._run_manual_checkpoint(8, step)
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
        runner._run_manual_checkpoint(8, step)
        assert events[0]["resume_possible"] is False
        assert events[0]["checkpoint_is_terminal"] is True
        logger.close()

    def test_terminal_checkpoint_auto_closes_logger(self, tmp_path):
        """Terminális checkpoint után run() automatikusan zárja a logger-t."""
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.bq_learning_physical())
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.resume_possible is False
        assert logger._closed is True
        logger.close()  # idempotens — nem dob hibát

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


# ------------------------------------------------------------------ #
# Task 7: _run_step                                                   #
# ------------------------------------------------------------------ #

class TestRunStep:
    def test_charge_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=3))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "DONE"
        logger.close()

    def test_discharge_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, dc=_StubDischargeCtrl(steps_to_done=3))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.DISCHARGE, "discharge")
        result = runner._run_step(0, step)
        assert result.status == "DONE"
        logger.close()

    def test_relax_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, rc=_StubRelaxCtrl(steps_to_done=2))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        result = runner._run_step(0, step)
        assert result.status == "DONE"
        logger.close()

    def test_charge_step_fault_returns_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(fault_at_step=2))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "FAULT"
        assert "FAULT" in result.reason
        logger.close()

    def test_emergency_stop_interrupts_charge(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.emergency_stop_requested = True
        runner.emergency_stop_reason = "USER_EMSTOP"
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "FAULT"
        assert "USER_EMSTOP" in result.reason
        logger.close()

    def test_stop_request_interrupts_charge(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.stop_requested = True
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "STOPPED"
        logger.close()

    def test_stop_request_interrupts_discharge(self, tmp_path):
        runner, logger = _make_runner(tmp_path, dc=_StubDischargeCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.stop_requested = True
        step = TestStep(StepKind.DISCHARGE, "discharge")
        result = runner._run_step(0, step)
        assert result.status == "STOPPED"
        logger.close()

    def test_stop_request_interrupts_relax(self, tmp_path):
        runner, logger = _make_runner(tmp_path, rc=_StubRelaxCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.stop_requested = True
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        result = runner._run_step(0, step)
        assert result.status == "STOPPED"
        logger.close()

    def test_manual_checkpoint_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_step(0, step)
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        logger.close()

    def test_charge_accumulates_ah(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=5)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        runner._run_step(0, step)
        # 5 advance() × 0.01 Ah = 0.05 Ah
        assert runner._total_charge_ah == pytest.approx(0.05, abs=0.001)
        logger.close()

    def test_discharge_accumulates_ah(self, tmp_path):
        dc = _StubDischargeCtrl(steps_to_done=4)
        runner, logger = _make_runner(tmp_path, dc=dc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.DISCHARGE, "discharge")
        runner._run_step(0, step)
        # 4 advance() × 0.01 Ah = 0.04 Ah
        assert runner._total_discharge_ah == pytest.approx(0.04, abs=0.001)
        logger.close()

    def test_controller_reset_called_if_available(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        # Első ciklus
        runner._run_step(0, TestStep(StepKind.CHARGE, "charge_1"))
        assert cc.state == ChargeState.CHARGE_DONE
        # Második ciklus — reset() visszaállítja INIT-re
        runner._run_step(0, TestStep(StepKind.CHARGE, "charge_2"))
        assert cc.state == ChargeState.CHARGE_DONE
        logger.close()


# ------------------------------------------------------------------ #
# Task 8: run() integrációs tesztek                                  #
# ------------------------------------------------------------------ #

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
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        logger.close()

    def test_bq_learning_accumulates_two_cycles(self, tmp_path):
        # Factory-mintával friss stub-ot ad minden lépésnél — így 2 teljes ciklus összeadódik.
        # 3 tick × 0.01 Ah × 2 töltés = 0.06 Ah; 2 tick × 0.01 Ah × 2 kisütés = 0.04 Ah
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
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=3),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=2),
            relax_ctrl_factory=lambda: _StubRelaxCtrl(steps_to_done=2),
        )
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
        def _bad_run_step(step_index, step):
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


# ------------------------------------------------------------------ #
# Task 1: Callback tesztek                                           #
# ------------------------------------------------------------------ #

class TestCallbacks:
    def test_on_sample_called_for_each_tick(self, tmp_path):
        """on_sample callback minden tickben meghívódik."""
        runner, _ = _make_runner(tmp_path)
        samples = []
        runner.on_sample = samples.append

        runner.run(TestPlan.characterization())

        assert len(samples) > 0
        assert all("battery_voltage_V" in s for s in samples)
        assert all("elapsed_s" in s for s in samples)

    def test_on_sample_none_does_not_crash(self, tmp_path):
        """on_sample=None esetén nincs hiba."""
        runner, _ = _make_runner(tmp_path)
        runner.on_sample = None
        result = runner.run(TestPlan.characterization())
        assert result.status == "DONE"

    def test_on_event_called_on_emergency_stop(self, tmp_path):
        """on_event callback meghívódik emergency stop esetén."""
        runner, _ = _make_runner(tmp_path)
        events = []
        runner.on_event = events.append
        runner.request_emergency_stop("TEST_REASON")
        runner.run(TestPlan.characterization())
        codes = [e["event_code"] for e in events]
        assert "EMERGENCY_STOP" in codes

    def test_on_event_called_on_graceful_stop(self, tmp_path):
        """on_event callback meghívódik graceful stop esetén."""
        runner, _ = _make_runner(
            tmp_path,
            rc=_StubRelaxCtrl(steps_to_done=100),
        )
        events = []
        runner.on_event = events.append
        runner.request_stop()
        runner.run(TestPlan.characterization())
        codes = [e["event_code"] for e in events]
        assert "GRACEFUL_STOP" in codes

    def test_on_event_none_does_not_crash(self, tmp_path):
        """on_event=None esetén nincs hiba."""
        runner, _ = _make_runner(tmp_path)
        runner.on_event = None
        runner.request_emergency_stop("TEST")
        result = runner.run(TestPlan.characterization())
        assert result.status == "FAULT"

    def test_on_sample_receives_step_name(self, tmp_path):
        """on_sample sample dict tartalmazza a step_name mezőt."""
        runner, _ = _make_runner(tmp_path)
        samples = []
        runner.on_sample = samples.append
        runner.run(TestPlan.characterization())
        step_names = {s["step_name"] for s in samples}
        assert "charge" in step_names


# ------------------------------------------------------------------ #
# Task 1 (6B): on_step_changed callback                              #
# ------------------------------------------------------------------ #

class TestStepChanged:
    def test_on_step_changed_called_for_each_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        calls = []
        runner.on_step_changed = calls.append
        runner.run(TestPlan.characterization())
        # characterization: 4 lépés
        assert len(calls) == 4
        assert calls[0]["step_label"] == "charge"
        assert calls[0]["runner_status"] == "RUNNING"
        assert calls[0]["step_index"] == 0
        assert calls[0]["step_count"] == 4
        logger.close()

    def test_on_step_changed_payload_has_step_kind(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        calls = []
        runner.on_step_changed = calls.append
        runner.run(TestPlan.characterization())
        assert calls[0]["step_kind"] == "CHARGE"
        assert calls[1]["step_kind"] == "RELAX"
        assert calls[2]["step_kind"] == "DISCHARGE"
        logger.close()


class TestRealDtIntegration:
    """K1: _run_step() valós dt_s-t ad az advance()-nek, nem nominálisát."""

    def test_advance_receives_actual_elapsed_not_nominal(self, tmp_path):
        from unittest.mock import patch, MagicMock

        recorded_dt: list[float] = []

        class DtRecordingRelax:
            def __init__(self):
                self._tick = 0

            @property
            def state(self) -> RelaxState:
                return RelaxState.RELAX_DONE if self._tick >= 2 else RelaxState.RELAXING

            def advance(self, dt_s: float) -> RelaxState:
                recorded_dt.append(dt_s)
                self._tick += 1
                return self.state

            def reset(self) -> None:
                pass

        runner, logger = _make_runner(tmp_path, rc=DtRecordingRelax())

        # perf_counter: 0.0 → inicializálás, majd 2.4s-os I/O-val emulált tick
        counter_values = [0.0, 2.4, 4.8, 7.2]
        counter_idx = [0]

        def mock_perf() -> float:
            val = counter_values[min(counter_idx[0], len(counter_values) - 1)]
            counter_idx[0] += 1
            return val

        step = TestStep(StepKind.RELAX, "relax_dt_test")
        plan = TestPlan(test_type=None, steps=(step,))

        with patch("Prog.src.test_runner.time") as mock_time:
            mock_time.perf_counter.side_effect = mock_perf
            mock_time.sleep = MagicMock()
            runner.run(plan)

        assert len(recorded_dt) >= 1
        assert abs(recorded_dt[0] - 2.4) < 0.05, (
            f"Elvárt ~2.4s valós dt_s, kapott {recorded_dt[0]}s (nominális tick=1.0s)"
        )
        logger.close()

    def test_first_tick_dt_is_non_negative(self, tmp_path):
        """P1-F: Az első tick dt_s >= 0.0 (perf_counter garantálja, nincs negatív drift)."""
        recorded_dt: list[float] = []

        class DtRecordingRelax:
            def __init__(self):
                self._tick = 0

            @property
            def state(self) -> RelaxState:
                return RelaxState.RELAX_DONE if self._tick >= 1 else RelaxState.RELAXING

            def advance(self, dt_s: float) -> RelaxState:
                recorded_dt.append(dt_s)
                self._tick += 1
                return self.state

            def reset(self) -> None:
                pass

        runner, logger = _make_runner(tmp_path, rc=DtRecordingRelax())
        step = TestStep(StepKind.RELAX, "relax_first_tick")
        plan = TestPlan(test_type=None, steps=(step,))
        runner.run(plan)

        assert len(recorded_dt) >= 1
        assert all(dt >= 0.0 for dt in recorded_dt), (
            f"Negatív dt_s érték: {[dt for dt in recorded_dt if dt < 0.0]}"
        )
        logger.close()


class TestLoggerCloseOnTermination:
    """E5: Logger.close() terminális állapotokban hívódik."""

    def test_logger_close_called_on_done(self, tmp_path):
        from unittest.mock import MagicMock
        from Prog.src.logger import Logger, LogConfig

        real_logger = Logger(tmp_path, LogConfig())
        logger_mock = MagicMock(wraps=real_logger)

        runner, _ = _make_runner(tmp_path / "unused", rc=_StubRelaxCtrl(steps_to_done=1))
        runner._logger = logger_mock

        step = TestStep(StepKind.RELAX, "relax")
        plan = TestPlan(test_type=None, steps=(step,))
        runner.run(plan)

        logger_mock.close.assert_called()
        real_logger.close()

    def test_logger_close_called_on_fault(self, tmp_path):
        from unittest.mock import MagicMock
        from Prog.src.logger import Logger, LogConfig

        real_logger = Logger(tmp_path, LogConfig())
        logger_mock = MagicMock(wraps=real_logger)

        fault_cc = _StubChargeCtrl(steps_to_done=5, fault_at_step=2)
        runner, _ = _make_runner(tmp_path / "unused2", cc=fault_cc)
        runner._logger = logger_mock

        plan = TestPlan(test_type=None, steps=(TestStep(StepKind.CHARGE, "charge"),))
        runner.run(plan)

        logger_mock.close.assert_called()
        real_logger.close()
