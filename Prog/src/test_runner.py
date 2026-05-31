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
        self.current_step = None

        self._total_charge_ah: float = 0.0
        self._total_discharge_ah: float = 0.0
        self._start_time = None

    def request_stop(self) -> None:
        self.stop_requested = True

    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None:
        self.emergency_stop_requested = True
        self.emergency_stop_reason = reason

    def run(self, test_plan: TestPlan) -> TestResult:
        raise NotImplementedError

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
