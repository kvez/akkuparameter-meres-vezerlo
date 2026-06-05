"""
DischargeController — CC kisütési állapotgép.
Állapotok: INIT → PRECHECK → DISCHARGE_CC_SETUP → DISCHARGE_CC_RUN →
           DISCHARGE_DONE / FAULT
[R1] Nincs relay hívás.
[R8] Integráció forrása: LOAD_READBACK.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import SafetyManager
from Prog.src.integrator import Integrator


class DischargeState(Enum):
    INIT = "INIT"
    PRECHECK = "PRECHECK"
    DISCHARGE_CC_SETUP = "DISCHARGE_CC_SETUP"
    DISCHARGE_CC_RUN = "DISCHARGE_CC_RUN"
    DISCHARGE_DONE = "DISCHARGE_DONE"
    FAULT = "FAULT"
    SAFE_OFF = "SAFE_OFF"


@dataclass
class DischargeConfig:
    discharge_current_A: float = 0.0    # 0 = auto C/5
    max_discharge_time_s: float = 86400.0
    max_discharge_Ah_factor: float = 1.10
    fallback_max_duration_s: float = 30.0


class DischargeController:
    def __init__(self, psu, load, dmm_voltage, dmm_temperature,
                 profile: BatteryProfile, safety: SafetyManager,
                 config: DischargeConfig):
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature
        self._profile = profile
        self._safety = safety
        self._config = config

        self._state = DischargeState.INIT
        self._elapsed_s: float = 0.0
        self._fault_reason: str = ""
        self._u_batt: float = 0.0
        self._dmm_valid: bool = True
        self._last_integration_source: str = "ZERO"
        self._battery_temperature_C: float = 20.0
        self._temp_dmm_fault_s: float = 0.0
        self._last_warning_code: str = ""
        self._integrator = Integrator(
            fallback_max_duration_s=config.fallback_max_duration_s
        )

    @property
    def state(self) -> DischargeState:
        return self._state

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def last_integration_source(self) -> str:
        return self._last_integration_source

    @property
    def last_warning_code(self) -> str:
        return self._last_warning_code

    @property
    def accumulated_discharge_Ah(self) -> float:
        return self._integrator.accumulated_discharge_Ah

    def advance(self, dt_s: float) -> DischargeState:
        self._elapsed_s += dt_s
        self._last_warning_code = ""

        if self._state == DischargeState.FAULT:
            return self._state

        self._dmm_valid = self._read_dmm(dt_s)

        if self._state in (
            DischargeState.DISCHARGE_CC_SETUP,
            DischargeState.DISCHARGE_CC_RUN,
        ):
            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return self._state

            if self._elapsed_s > self._config.max_discharge_time_s:
                self.emergency_stop("MAX_DISCHARGE_TIME_REACHED")
                return self._state

            t_result = self._safety.check_temperature_dmm_fault(self._temp_dmm_fault_s)
            if t_result.fault is not None:
                self.emergency_stop(t_result.fault.name)
                return self._state
            if t_result.warning is not None:
                self._last_warning_code = t_result.warning.name

        if self._state == DischargeState.INIT:
            self._state = DischargeState.PRECHECK

        elif self._state == DischargeState.PRECHECK:
            self._run_precheck()

        elif self._state == DischargeState.DISCHARGE_CC_SETUP:
            self._run_cc_setup()

        elif self._state == DischargeState.DISCHARGE_CC_RUN:
            self._run_cc(dt_s)

        return self._state

    def emergency_stop(self, reason: str) -> None:
        self._fault_reason = reason
        try:
            self._load.input_off()
        except Exception:
            pass
        try:
            self._psu.all_outputs_off()
        except Exception:
            pass
        # NINCS relay.safe_open() [R1]
        self._state = DischargeState.FAULT

    def _run_precheck(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

        voltage_result = self._safety.check_precheck_voltage(self._u_batt)
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return
        self._psu.all_outputs_off()  # PSU biztosan OFF kisütés előtt
        self._state = DischargeState.DISCHARGE_CC_SETUP

    def _run_cc_setup(self) -> None:
        i_discharge = (
            self._config.discharge_current_A
            if self._config.discharge_current_A > 0
            else self._profile.C5_discharge_current_A
        )
        self._load.set_mode_cc()
        self._load.set_current(i_discharge)
        self._load.input_on()
        self._state = DischargeState.DISCHARGE_CC_RUN

    def _run_cc(self, dt_s: float) -> None:
        i_load = self._read_load_current()
        if self._state == DischargeState.FAULT:
            return
        self._integrate(dt_s, signed_current_A=-i_load, source="LOAD_READBACK")

        terminate_V = self._profile.terminate_voltage_pack_V
        if self._u_batt <= terminate_V:
            self._load.input_off()
            self._state = DischargeState.DISCHARGE_DONE
            return

        if self._integrator.accumulated_discharge_Ah > (
            self._profile.nominal_capacity_Ah * self._config.max_discharge_Ah_factor
        ):
            self.emergency_stop("MAX_DISCHARGE_AH_REACHED")

    def _read_dmm(self, dt_s: float) -> bool:
        try:
            self._u_batt = self._dmm_v.read_voltage()
            voltage_ok = True
        except Exception:
            voltage_ok = False

        try:
            self._battery_temperature_C = self._dmm_t.read_temperature()
            self._temp_dmm_fault_s = 0.0
        except Exception:
            self._temp_dmm_fault_s += dt_s

        return voltage_ok

    def _read_load_current(self) -> float:
        try:
            return self._load.measure_current()
        except Exception:
            self.emergency_stop("LOAD_COMM_LOST")
            return 0.0

    def _integrate(self, dt_s, signed_current_A, source):
        self._last_integration_source = source
        v = self._u_batt if self._u_batt > 0 else self._profile.terminate_voltage_pack_V
        self._integrator.add_sample(signed_current_A, v, dt_s, source)
