"""
TestRunner — fő folyamatvezérlő.
CHARACTERIZATION és BQ_LEARNING_PHYSICAL teszttípusok.
GUI-független, blokkoló run(), stop flag alapú megállítás.
[d4.txt][d5.txt][d6.txt]
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

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
    steps: tuple[TestStep, ...]

    @staticmethod
    def characterization() -> "TestPlan":
        return TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(
                TestStep(StepKind.CHARGE,     "charge"),
                TestStep(StepKind.RELAX,      "relax_after_charge"),
                TestStep(StepKind.DISCHARGE,  "discharge"),
                TestStep(StepKind.RELAX,      "relax_after_discharge"),
            ),
        )

    @staticmethod
    def bq_learning_physical() -> "TestPlan":
        return TestPlan(
            test_type=TestType.BQ_LEARNING_PHYSICAL,
            steps=(
                TestStep(StepKind.CHARGE,             "charge_1"),
                TestStep(StepKind.RELAX,              "relax_after_charge_1"),
                TestStep(StepKind.DISCHARGE,          "discharge_1"),
                TestStep(StepKind.RELAX,              "relax_after_discharge_1"),
                TestStep(StepKind.CHARGE,             "charge_2"),
                TestStep(StepKind.RELAX,              "relax_after_charge_2"),
                TestStep(StepKind.DISCHARGE,          "discharge_2"),
                TestStep(StepKind.RELAX,              "relax_after_discharge_2"),
                TestStep(StepKind.MANUAL_CHECKPOINT,  "manual_bq_checkpoint"),
            ),
        )


@dataclass
class TestResult:
    status: str
    reason: str = ""
    total_charge_ah: float = 0.0
    total_discharge_ah: float = 0.0
    resume_possible: bool = False    # ← új
    next_step_index: int = 0         # ← új


@dataclass
class TestRunnerConfig:
    runner_tick_s: float = 2.0
    test_name: str = "unnamed"
    sleep_enabled: bool = True


class TestRunner:
    """Fő folyamatvezérlő — CHARACTERIZATION és BQ_LEARNING_PHYSICAL teszttípusok."""

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
        self.on_sample: Optional[Callable[[dict], None]] = None
        self.on_event: Optional[Callable[[dict], None]] = None
        self.on_device_error: Optional[Callable[[dict], None]] = None
        self.current_step: Optional[TestStep] = None

        self._total_charge_ah: float = 0.0
        self._total_discharge_ah: float = 0.0
        self._start_time: Optional[datetime] = None
        self._active_plan: Optional[TestPlan] = None
        self.on_step_changed: Optional[Callable[[dict], None]] = None

    def request_stop(self) -> None:
        self.stop_requested = True

    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None:
        self.emergency_stop_requested = True
        self.emergency_stop_reason = reason

    def reset_control_flags(self) -> None:
        self.stop_requested = False
        self.emergency_stop_requested = False
        self.emergency_stop_reason = ""

    def run(self, test_plan: TestPlan, start_step_index: int = 0) -> TestResult:
        self._active_plan = test_plan
        self._start_time = datetime.now(timezone.utc)
        self.status = "RUNNING"
        self._total_charge_ah = 0.0
        self._total_discharge_ah = 0.0

        try:
            for step_index, step in enumerate(
                list(test_plan.steps)[start_step_index:],
                start=start_step_index,
            ):
                self.current_step = step

                if self.emergency_stop_requested:
                    return self._emergency_stop(self.emergency_stop_reason)

                if self.stop_requested:
                    return self._graceful_stop("USER_STOP_REQUESTED")

                step_result = self._run_step(step_index, step)

                if step_result.status == "FAULT":
                    return self._emergency_stop(step_result.reason or "STEP_FAULT")

                if step_result.status in ("STOPPED", "CHECKPOINT_STOPPED"):
                    if step_result.status == "CHECKPOINT_STOPPED" and not step_result.resume_possible:
                        self._logger.close()
                    return step_result

        except Exception as exc:
            return self._emergency_stop(f"UNHANDLED_EXCEPTION: {exc}")

        self.status = "DONE"
        self._logger.close()
        return TestResult(
            status="DONE",
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
        )

    def _run_step(self, step_index: int, step: TestStep) -> TestResult:
        if self.on_step_changed is not None and self._active_plan is not None:
            self.on_step_changed({
                "runner_status": "RUNNING",
                "step_kind": step.kind.value,
                "step_label": step.label,
                "step_index": step_index,
                "step_count": len(self._active_plan.steps),
            })

        if step.kind == StepKind.MANUAL_CHECKPOINT:
            return self._run_manual_checkpoint(step_index, step)

        controller = self._controller_for_step(step)

        reset_fn = getattr(controller, "reset", None)
        if callable(reset_fn):
            reset_fn()

        charge_ah_before = getattr(controller, "accumulated_charge_Ah", 0.0)
        discharge_ah_before = getattr(controller, "accumulated_discharge_Ah", 0.0)

        _last_tick_t = time.perf_counter()

        while not self._is_finished(controller):
            if self.emergency_stop_requested:
                return self._emergency_stop(self.emergency_stop_reason)

            if self.stop_requested and self._step_can_be_gracefully_interrupted(step):
                return self._graceful_stop("USER_STOP_REQUESTED")

            _t_now = time.perf_counter()
            # Első tickben actual_dt_s ≈ 0 (INIT/PRECHECK fázis, integráció nem fut).
            actual_dt_s = _t_now - _last_tick_t
            _last_tick_t = _t_now
            controller.advance(actual_dt_s)

            sample = self._build_sample(step, controller)
            self._logger.log_sample(sample)
            if self.on_sample is not None:
                self.on_sample(sample)

            self._poll_device_errors()

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
                    total_charge_ah=self._total_charge_ah,
                    total_discharge_ah=self._total_discharge_ah,
                )

            if self._config.sleep_enabled:
                _elapsed = time.perf_counter() - _last_tick_t
                time.sleep(max(0.0, self._config.runner_tick_s - _elapsed))

        if step.kind == StepKind.CHARGE:
            self._total_charge_ah += (
                getattr(controller, "accumulated_charge_Ah", 0.0) - charge_ah_before
            )
        elif step.kind == StepKind.DISCHARGE:
            self._total_discharge_ah += (
                getattr(controller, "accumulated_discharge_Ah", 0.0) - discharge_ah_before
            )

        return TestResult(status="DONE")

    def _poll_device_errors(self) -> None:
        errors = self._instruments.poll_device_errors()
        for err in errors:
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self._logger.log_device_error(err["device"], err["error"])
            if self.on_device_error is not None:
                self.on_device_error({
                    "timestamp_iso": ts,
                    "device": err["device"],
                    "error": err["error"],
                })

    def _emergency_stop(self, reason: str) -> TestResult:
        """Vészleállítás — FAULT státusz, biztonságos leállítás."""
        self.status = "FAULT"
        self._instruments.safe_all_off()
        self._logger.log_event("EMERGENCY_STOP", reason, is_critical=True)
        self._logger.write_checkpoint({"status": "FAULT", "reason": reason})
        self._logger.close()
        if self.on_event is not None:
            self.on_event({"event_code": "EMERGENCY_STOP", "event_message": reason})
        return TestResult(
            status="FAULT",
            reason=reason,
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
        )

    def _graceful_stop(self, reason: str) -> TestResult:
        """Kérésre történő leállítás — STOPPED státusz, biztonságos leállítás."""
        self.status = "STOPPED"
        self._instruments.safe_all_off()
        self._logger.log_event("GRACEFUL_STOP", reason)
        self._logger.write_checkpoint({"status": "STOPPED", "reason": reason})
        self._logger.close()
        if self.on_event is not None:
            self.on_event({"event_code": "GRACEFUL_STOP", "event_message": reason})
        return TestResult(
            status="STOPPED",
            reason=reason,
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
        )

    def _run_manual_checkpoint(self, step_index: int, step: TestStep) -> TestResult:
        """BQ kézi ellenőrzési pont — CHECKPOINT_STOPPED státusz, on_event hívással."""
        assert self._active_plan is not None
        next_step_index = step_index + 1
        checkpoint_is_terminal = (next_step_index >= len(self._active_plan.steps))
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
        return True

    def _controller_for_step(self, step: TestStep):
        if step.kind == StepKind.CHARGE:
            return self._charge_ctrl
        if step.kind == StepKind.DISCHARGE:
            return self._discharge_ctrl
        if step.kind == StepKind.RELAX:
            return self._relax_ctrl
        raise ValueError(f"No controller for step kind: {step.kind}")

    def _build_sample(self, step: TestStep, controller) -> dict:
        now = datetime.now(timezone.utc)
        readings = self._instruments.read_all()

        sample: dict = {col: None for col in CSV_COLUMNS}

        elapsed = (now - self._start_time).total_seconds() if self._start_time else 0.0
        sample["timestamp_iso"] = now.isoformat()
        sample["elapsed_s"] = round(elapsed, 3)
        sample["test_name"] = self._config.test_name
        sample["step_name"] = step.label
        state_obj = getattr(controller, "state", None)
        sample["state"] = state_obj.value if state_obj is not None else None

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
            sample["charge_current_A"] = i_psu if i_psu is not None else 0.0
            sample["discharge_current_A"] = 0.0
            sample["signed_current_A"] = i_psu if i_psu is not None else 0.0
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

        # P1-C: driver állapot + integrátor minőségi mezők
        sample["psu_output_commanded_on"] = getattr(
            self._instruments.psu, "output_commanded_on", None
        )
        sample["load_input_commanded_on"] = getattr(
            self._instruments.load, "input_commanded_on", None
        )

        sample["fault_flags"] = getattr(controller, "fault_reason", None) or None
        sample["warning_flags"] = getattr(controller, "last_warning_code", None) or None

        integrator = getattr(controller, "_integrator", None)
        if integrator is not None:
            sample["integration_valid"] = integrator.integration_valid
            sample["capacity_result_quality"] = integrator.capacity_result_quality
            sample["accumulated_charge_Wh"] = integrator.accumulated_charge_Wh
            sample["accumulated_discharge_Wh"] = integrator.accumulated_discharge_Wh

        return sample
