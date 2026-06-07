"""
ChargeController — DMM-kompenzált CC/CV töltési állapotgép.
Állapotok: INIT → PRECHECK → PSU_PRESET → CHARGE_CC →
           CHARGE_CV_DMM_CONTROL → TAPER_HOLD → CHARGE_DONE / FAULT
[R1] Nincs relay hívás.
[N7] Csak mód-agnosztikus PSU API hívható.
[N9] output_commanded_on nyilvántartás a PSU driveren keresztül.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode
from Prog.src.integrator import Integrator


class ChargeState(Enum):
    INIT = "INIT"
    PRECHECK = "PRECHECK"
    PSU_PRESET = "PSU_PRESET"
    CHARGE_CC = "CHARGE_CC"
    CHARGE_CV_DMM_CONTROL = "CHARGE_CV_DMM_CONTROL"
    TAPER_HOLD = "TAPER_HOLD"
    CHARGE_DONE = "CHARGE_DONE"
    FAULT = "FAULT"
    SAFE_OFF = "SAFE_OFF"


@dataclass
class ChargeConfig:
    # CV szabályozás
    deadband_V: float = 0.010
    max_step_up_V: float = 0.050
    # Aszimmetrikus: lefelé gyors (dióda-esés csökkenésekor az akku felkúszik,
    # a 2s tick alatt 50mV lassú volt — 500mV elegendő reakció a 150mV headroomhoz)
    max_step_down_V: float = 0.500
    cv_entry_margin_V: float = 0.100
    max_expected_series_drop_V: float = 0.90

    # Taper [R9]
    taper_hold_s: float = 600.0
    taper_current_tolerance_factor: float = 1.05
    cv_voltage_tolerance_V_per_cell: float = 0.003

    # Safety
    max_charge_time_s: float = 86400.0
    max_charge_Ah_factor: float = 1.20
    temperature_dmm_fault_timeout_s: float = 60.0

    # Integrátor
    fallback_max_duration_s: float = 30.0


class ChargeController:
    def __init__(
        self,
        psu,
        load,
        dmm_voltage,
        dmm_temperature,
        profile: BatteryProfile,
        safety: SafetyManager,
        config: ChargeConfig,
    ):
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature
        self._profile = profile
        self._safety = safety
        self._config = config

        self._state = ChargeState.INIT
        self._u_psu_set: float = 0.0
        self._taper_timer_s: float = 0.0
        self._elapsed_s: float = 0.0
        self._temp_dmm_fault_s: float = 0.0
        self._fault_reason: str = ""
        self._last_integration_source: str = "ZERO"
        self._integrator = Integrator(
            fallback_max_duration_s=config.fallback_max_duration_s
        )

        self._u_batt: float = 0.0
        self._i_charge: float = 0.0
        self._dmm_valid: bool = True
        self._last_warning_code: str = ""
        self._battery_temperature_C: float = 20.0

    # ------------------------------------------------------------------ #
    # Nyilvános tulajdonságok                                              #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> ChargeState:
        return self._state

    @property
    def taper_timer_s(self) -> float:
        return self._taper_timer_s

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
    def accumulated_charge_Ah(self) -> float:
        return self._integrator.accumulated_charge_Ah

    @property
    def u_psu_set_V(self) -> float:
        return self._u_psu_set

    # ------------------------------------------------------------------ #
    # Fő advance() — egy vezérlési ciklus                                 #
    # ------------------------------------------------------------------ #

    def advance(self, dt_s: float) -> ChargeState:
        self._elapsed_s += dt_s
        self._last_warning_code = ""

        if self._state == ChargeState.FAULT:
            return self._state

        # Olvasások (hibabiztos)
        self._dmm_valid = self._read_dmm(dt_s)

        if self._state in (
            ChargeState.CHARGE_CC,
            ChargeState.CHARGE_CV_DMM_CONTROL,
            ChargeState.TAPER_HOLD,
        ):
            concurrent_result = self._safety.check_concurrent_psu_load(
                psu_commanded_on=self._psu.output_commanded_on,
                load_commanded_on=self._load.input_commanded_on,
            )
            if concurrent_result.fault is not None:
                self.emergency_stop(concurrent_result.fault.name)
                return self._state

            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return self._state

            v_result = self._safety.check_battery_voltage(self._u_batt)
            if v_result.fault is not None:
                self.emergency_stop(v_result.fault.name)
                return self._state

            t_result = self._safety.check_temperature_dmm_fault(self._temp_dmm_fault_s)
            if t_result.fault is not None:
                self.emergency_stop(t_result.fault.name)
                return self._state
            if t_result.warning is not None:
                self._last_warning_code = t_result.warning.name

        # Állapotgép átmenetek
        if self._state == ChargeState.INIT:
            self._state = ChargeState.PRECHECK

        elif self._state == ChargeState.PRECHECK:
            self._run_precheck()

        elif self._state == ChargeState.PSU_PRESET:
            self._run_psu_preset()

        elif self._state == ChargeState.CHARGE_CC:
            self._run_charge_cc(dt_s)

        elif self._state == ChargeState.CHARGE_CV_DMM_CONTROL:
            self._run_charge_cv(dt_s)

        elif self._state == ChargeState.TAPER_HOLD:
            self._run_taper_hold(dt_s)

        return self._state

    # ------------------------------------------------------------------ #
    # Vészleállítás [R1] — LOAD OFF → PSU OFF, relay soha                 #
    # ------------------------------------------------------------------ #

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
        # NINCS relay.safe_open() — nincs relé hardware [R1]
        self._state = ChargeState.FAULT

    # ------------------------------------------------------------------ #
    # Állapot implementációk                                               #
    # ------------------------------------------------------------------ #

    def _run_precheck(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

        mode_result = self._safety.check_psu_mode_compatibility()
        if mode_result.fault is not None:
            self.emergency_stop(mode_result.fault.name)
            return

        voltage_result = self._safety.check_precheck_voltage(self._u_batt)
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return

        self._state = ChargeState.PSU_PRESET

    def _run_psu_preset(self) -> None:
        target_V = self._effective_charge_target_V()
        # PSU compliance voltage = akkucél + max dióda-esés
        # A PSU áramkorlátozva indul (CC mód) — a 0.9 faktor nem kell,
        # mert az akkufeszültségnél magasabb preset szükséges az áramfolyáshoz.
        self._u_psu_set = target_V + self._config.max_expected_series_drop_V
        self._psu.set_output_voltage(self._u_psu_set)
        # Keithley 2220-30-1 hardver limit: INDEPENDENT/SERIES → 1.5A/csatorna,
        # PARALLEL → 3.0A. Ha a kiszámított töltőáram meghaladja ezt, a PSU
        # visszautasítja a parancsot (nem clampel!) és az előző érték marad érvényes.
        psu_hw_max_A = {
            PsuMode.INDEPENDENT: 1.5,
            PsuMode.PARALLEL: 3.0,
            PsuMode.SERIES: 1.5,
        }.get(self._safety.psu_mode, 1.5)
        charge_A = min(self._profile.effective_max_charge_A, psu_hw_max_A)
        self._psu.set_output_current(charge_A)
        self._psu.output_on()
        self._state = ChargeState.CHARGE_CC

    def _check_charge_limits(self) -> bool:
        """K2: Ah és idő limit ellenőrzés. True = limit elérve, emergency_stop meghívva."""
        if self._elapsed_s > self._config.max_charge_time_s:
            self.emergency_stop("MAX_CHARGE_TIME_REACHED")
            return True
        if self._integrator.accumulated_charge_Ah > (
            self._profile.nominal_capacity_Ah * self._config.max_charge_Ah_factor
        ):
            self.emergency_stop("MAX_CHARGE_AH_REACHED")
            return True
        return False

    def _run_charge_cc(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        self._integrate(dt_s, signed_current_A=self._i_charge, source="PSU_READBACK")

        if self._check_charge_limits():
            return

        if self._check_series_safety():
            return

        target_V = self._effective_charge_target_V()
        if self._u_batt >= target_V - self._config.cv_entry_margin_V:
            u_psu_now = self._read_psu_voltage()
            if u_psu_now is not None:
                # CV belépési PSU preset = cél + aktuálisan mért esés (dióda + kábel).
                # A PSU-t AZONNAL csökkentjük — különben marad a 15.30V compliance-en
                # egy teljes tickig, ami elegendő OV-t okozni, ha u_batt már közel van
                # a határhoz. Clamp: ha u_batt < target (normál belépés), ne menjük
                # compliance fölé (target+drop > compliance lenne).
                u_drop = max(u_psu_now - self._u_batt, 0.0)
                self._u_psu_set = min(
                    target_V + u_drop,
                    target_V + self._config.max_expected_series_drop_V,
                )
            else:
                self._u_psu_set = target_V + self._config.max_expected_series_drop_V
            self._psu.set_output_voltage(self._u_psu_set)
            self._state = ChargeState.CHARGE_CV_DMM_CONTROL

    def _run_charge_cv(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        self._regulate_cv()
        self._integrate(dt_s, signed_current_A=self._i_charge, source="PSU_READBACK")

        if self._check_charge_limits():
            return

        if self._check_series_safety():
            return

        if self._check_taper_condition():
            self._state = ChargeState.TAPER_HOLD

    def _run_taper_hold(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        # Csak lefelé szabályoz: ha u_batt > target, PSU csökkenti a feszültséget.
        # Felfelé lépés tiltott — OV-védelmi garancia a taper hold teljes idejére.
        self._regulate_cv(up_allowed=False)
        self._integrate(dt_s, signed_current_A=self._i_charge, source="PSU_READBACK")

        if self._check_charge_limits():
            return

        if self._check_series_safety():
            return

        if not self._check_taper_condition():
            self._taper_timer_s = 0.0
            self._state = ChargeState.CHARGE_CV_DMM_CONTROL
            return

        self._taper_timer_s += dt_s
        if self._taper_timer_s >= self._config.taper_hold_s:
            self._psu.output_off()
            self._state = ChargeState.CHARGE_DONE

    # ------------------------------------------------------------------ #
    # CV szabályozóhurok                                                   #
    # ------------------------------------------------------------------ #

    def _regulate_cv(self, up_allowed: bool = True) -> None:
        target_V = self._effective_charge_target_V()
        error = target_V - self._u_batt

        if abs(error) <= self._config.deadband_V:
            return

        if error > 0:
            if not up_allowed:
                return
            step = min(error, self._config.max_step_up_V)
            self._u_psu_set += step
        else:
            step = min(abs(error), self._config.max_step_down_V)
            self._u_psu_set -= step

        psu_mode_max = {
            PsuMode.INDEPENDENT: 30.0,
            PsuMode.PARALLEL: 30.0,
            PsuMode.SERIES: 60.0,
        }.get(self._safety.psu_mode, 30.0)

        self._u_psu_set = min(self._u_psu_set, psu_mode_max)
        self._u_psu_set = min(
            self._u_psu_set,
            target_V + self._config.max_expected_series_drop_V
        )
        self._u_psu_set = max(self._u_psu_set, 0.0)

        self._psu.set_output_voltage(self._u_psu_set)

    # ------------------------------------------------------------------ #
    # Taper feltétel [R9]                                                  #
    # Implementáld ezt a metódust (5-8 sor):                              #
    # ------------------------------------------------------------------ #

    def _check_taper_condition(self) -> bool:
        """
        [R9] Taper feltétel formális ellenőrzés.

        TAPER_HOLD IGAZ, ha MINDEN feltétel teljesül:
          1. U_batt_DMM >= (U_target - cv_voltage_tolerance_V)
          2. I_charge <= taper_current_A  (self._profile.effective_taper_A)
          3. dmm_voltage_valid == True    (self._dmm_valid)

        HAMIS (timer reset), ha bármely feltétel sérül:
          - I_charge > taper_current_A * taper_current_tolerance_factor
          - U_batt < target - tolerance
          - not dmm_valid

        Returns:
            bool — True: taper feltétel teljesül, TAPER_HOLD állapot tartható
        """
        if not self._dmm_valid:
            return False

        cv_tolerance_V = (
            self._profile.cell_count
            * self._config.cv_voltage_tolerance_V_per_cell
        )

        voltage_ok = self._u_batt >= (
            self._effective_charge_target_V() - cv_tolerance_V
        )
        current_ok = self._i_charge <= self._profile.effective_taper_A

        return voltage_ok and current_ok

    # ------------------------------------------------------------------ #
    # Segédek                                                              #
    # ------------------------------------------------------------------ #

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

    def _read_psu_current(self) -> float:
        try:
            return self._psu.measure_output_current()
        except Exception:
            self.emergency_stop("PSU_COMM_LOST")
            return 0.0  # hívó a FAULT állapotot ellenőrzi

    def _effective_charge_target_V(self) -> float:
        if self._safety.temp_comp_mode == TempCompMode.ENABLED:
            return self._profile.compensated_charge_voltage_V(self._battery_temperature_C)
        return self._profile.charge_voltage_pack_V

    def _read_psu_voltage(self) -> Optional[float]:
        try:
            return self._psu.measure_output_voltage()
        except Exception:
            return None

    def _check_series_safety(self) -> bool:
        """K3: series_drop és diode_power ellenőrzés. True = fault triggerelt."""
        u_psu = self._read_psu_voltage()
        if u_psu is None or self._u_batt <= 0:
            return False
        u_drop_V = u_psu - self._u_batt
        if u_drop_V < 0:
            return False  # PSU feszültség akkuszint alatt — skip

        drop_result = self._safety.check_series_drop(u_drop_V)
        if drop_result.fault is not None:
            self.emergency_stop(drop_result.fault.name)
            return True

        power_result = self._safety.check_diode_power(self._i_charge, u_drop_V)
        if power_result.warning is not None:
            self._last_warning_code = power_result.warning.name

        return False

    def _integrate(self, dt_s: float, signed_current_A: float, source: str) -> None:
        self._last_integration_source = source
        v = self._u_batt if self._u_batt > 0 else self._profile.charge_voltage_pack_V
        self._integrator.add_sample(signed_current_A, v, dt_s, source)
