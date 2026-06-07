"""
OcvSocController — OCV-SOC lépcsős karakterizációs állapotgép.
Automatikus teljes töltés → 5h relax → 20× SOC lépés (5%-os)
Minden lépésben: partial discharge (Ah-limit) + relax + OCV mérés + Rb impulzus.
[R1] Nincs relay hívás.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import SafetyManager
from Prog.src.integrator import Integrator

if TYPE_CHECKING:
    from Prog.src.charge_controller import ChargeController


@dataclass
class OcvSocConfig:
    step_percent: float = 5.0                         # SOC lépésnagyság %
    discharge_rate_divisor: int = 10                  # C/10 alapértelmezett
    relax_default_s: float = 7200.0                   # 2h alap
    relax_keypoint_s: float = 18000.0                 # 5h kulcspontokon (100%,50%,20%,10%,0%)
    impulse_current_rate_divisor: int = 5             # C/5 Rb impulzushoz
    impulse_duration_s: float = 30.0                  # 30s impulzus (tickelés méri, nem sleep)


_KEYPOINTS = frozenset({100.0, 50.0, 20.0, 10.0, 0.0})


class OcvSocState(Enum):
    INIT = "INIT"
    PRECHARGE = "PRECHARGE"                   # teljes töltés (belső ChargeController)
    PRECHARGE_RELAX = "PRECHARGE_RELAX"       # 5h relax 100%-on
    STEP_DISCHARGE = "STEP_DISCHARGE"         # partial kisütés step_Ah-ig
    STEP_RELAX = "STEP_RELAX"                 # relax
    IMPULSE_PREP = "IMPULSE_PREP"             # OCV mérés relax végén
    IMPULSE_ON = "IMPULSE_ON"                 # impulzus start
    IMPULSE_WAIT_1S = "IMPULSE_WAIT_1S"       # várakozás 1s mérési pontig
    IMPULSE_WAIT_10S = "IMPULSE_WAIT_10S"     # várakozás 10s mérési pontig
    IMPULSE_WAIT_30S = "IMPULSE_WAIT_30S"     # várakozás 30s mérési pontig
    LOG_SOC_POINT = "LOG_SOC_POINT"           # SOC pont rögzítése
    DONE = "DONE"
    FAULT = "FAULT"


class OcvSocController:
    """OCV-SOC lépcsős karakterizáció állapotgép."""

    def __init__(
        self,
        psu,
        load,
        dmm_voltage,
        dmm_temperature,
        profile: BatteryProfile,
        safety: SafetyManager,
        config: OcvSocConfig,
        measured_capacity_Ah: Optional[float] = None,
    ):
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature
        self._profile = profile
        self._safety = safety
        self._config = config

        self._total_capacity_Ah: float = (
            measured_capacity_Ah
            if measured_capacity_Ah is not None and measured_capacity_Ah > 0
            else profile.nominal_capacity_Ah
        )

        self._state = OcvSocState.INIT
        self._fault_reason: str = ""

        self._charge_ctrl: Optional["ChargeController"] = None

        self._step_integrator = Integrator()

        # SOC index: 100%-ról indul, lépésenként csökken step_percent-tel
        self._soc_index: float = 100.0

        # Összesített eltávolított Ah
        self._removed_Ah_total: float = 0.0

        # Lépésbeli eltávolított Ah
        self._step_removed_Ah: float = 0.0

        # Relax időzítő
        self._relax_elapsed_s: float = 0.0
        self._current_relax_s: float = config.relax_keypoint_s  # első: 5h (100%)

        # DMM olvasás
        self._u_batt: float = 0.0
        self._dmm_valid: bool = True
        self._battery_temperature_C: float = 20.0
        self._temp_dmm_fault_s: float = 0.0
        self._dmm_fault_during_relax: bool = False

        # Impulzus állapot
        self._impulse_elapsed_s: float = 0.0
        self._v_before_impulse: float = 0.0
        self._v_1s: float = 0.0
        self._v_10s: float = 0.0

        # Impulzus eredmények
        self._last_ocv_V: float = 0.0
        self._rb_1s: float = 0.0
        self._rb_10s: float = 0.0
        self._rb_30s: float = 0.0
        self._impulse_current_A: float = 0.0

        # Callback
        self.on_soc_point: Optional[Callable[[dict], None]] = None

        self._discharge_current_A: float = (
            profile.nominal_capacity_Ah / config.discharge_rate_divisor
        )

        self._impulse_current_A_set: float = (
            profile.nominal_capacity_Ah / config.impulse_current_rate_divisor
        )

    # ------------------------------------------------------------------ #
    # Nyilvános tulajdonságok                                              #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> OcvSocState:
        return self._state

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def soc_index(self) -> float:
        return self._soc_index

    @property
    def removed_Ah_total(self) -> float:
        return self._removed_Ah_total

    # ------------------------------------------------------------------ #
    # Fő advance() — egy vezérlési ciklus                                  #
    # ------------------------------------------------------------------ #

    def advance(self, dt_s: float) -> OcvSocState:
        if self._state in (OcvSocState.DONE, OcvSocState.FAULT):
            return self._state

        self._dmm_valid = self._read_dmm(dt_s)

        if self._state == OcvSocState.INIT:
            self._enter_precharge()

        elif self._state == OcvSocState.PRECHARGE:
            self._run_precharge(dt_s)

        elif self._state in (OcvSocState.PRECHARGE_RELAX, OcvSocState.STEP_RELAX):
            self._run_relax_phase(dt_s)

        elif self._state == OcvSocState.STEP_DISCHARGE:
            self._run_step_discharge(dt_s)

        elif self._state == OcvSocState.IMPULSE_PREP:
            self._run_impulse_prep()

        elif self._state == OcvSocState.IMPULSE_ON:
            self._run_impulse_on()

        elif self._state in (
            OcvSocState.IMPULSE_WAIT_1S,
            OcvSocState.IMPULSE_WAIT_10S,
            OcvSocState.IMPULSE_WAIT_30S,
        ):
            self._run_impulse_wait(dt_s)

        elif self._state == OcvSocState.LOG_SOC_POINT:
            self._run_log_soc_point()

        return self._state

    # ------------------------------------------------------------------ #
    # Vészleállítás [R1] — LOAD OFF → PSU OFF, relay soha                  #
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
        self._state = OcvSocState.FAULT

    # ------------------------------------------------------------------ #
    # Állapot implementációk                                               #
    # ------------------------------------------------------------------ #

    def _enter_precharge(self) -> None:
        """INIT → PRECHARGE: belső ChargeController létrehozása."""
        from Prog.src.charge_controller import ChargeController, ChargeConfig
        self._charge_ctrl = ChargeController(
            self._psu,
            self._load,
            self._dmm_v,
            self._dmm_t,
            self._profile,
            self._safety,
            ChargeConfig(),
        )
        self._state = OcvSocState.PRECHARGE

    def _run_precharge(self, dt_s: float) -> None:
        from Prog.src.charge_controller import ChargeState
        assert self._charge_ctrl is not None
        charge_state = self._charge_ctrl.advance(dt_s)

        if charge_state == ChargeState.FAULT:
            self.emergency_stop(
                self._charge_ctrl.fault_reason or "PRECHARGE_FAULT"
            )
        elif charge_state == ChargeState.CHARGE_DONE:
            self._relax_elapsed_s = 0.0
            self._current_relax_s = self._config.relax_keypoint_s
            self._dmm_fault_during_relax = False
            self._state = OcvSocState.PRECHARGE_RELAX

    def _run_relax_phase(self, dt_s: float) -> None:
        """Közös relax logika PRECHARGE_RELAX és STEP_RELAX számára."""
        if not self._dmm_valid:
            self._dmm_fault_during_relax = True
        self._relax_elapsed_s += dt_s

        if self._relax_elapsed_s >= self._current_relax_s:
            # Mindkét relax állapotból → OCV mérés (IMPULSE_PREP)
            # PRECHARGE_RELAX-ból is: a 100%-os OCV adatpont rögzítendő
            self._state = OcvSocState.IMPULSE_PREP

    def _begin_step_discharge(self) -> None:
        """Új kisütési lépés előkészítése: integrátor reset, terhelés beállítás."""
        self._step_integrator = Integrator()
        self._step_removed_Ah = 0.0
        try:
            self._psu.all_outputs_off()
            self._load.set_mode_cc()
            self._load.set_current(self._discharge_current_A)
            self._load.input_on()
        except Exception as exc:
            self.emergency_stop(f"STEP_DISCHARGE_SETUP_FAILED: {exc}")

    def _run_step_discharge(self, dt_s: float) -> None:
        """Részleges kisütés Ah-limitig."""
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

        try:
            i_load = self._load.measure_current()
        except Exception:
            self.emergency_stop("LOAD_COMM_LOST")
            return

        v = self._u_batt if self._u_batt > 0 else self._profile.terminate_voltage_pack_V
        self._step_integrator.add_sample(-i_load, v, dt_s)
        step_removed = abs(self._step_integrator.accumulated_discharge_Ah)
        self._step_removed_Ah = step_removed

        terminate_V = self._profile.terminate_voltage_pack_V
        if self._u_batt <= terminate_V:
            # Akku alján vagyunk — 0% SOC pont relax+impulzus után rögzítendő
            self._removed_Ah_total += step_removed
            self._load.input_off()
            self._soc_index = 0.0
            self._current_relax_s = self._config.relax_keypoint_s
            self._relax_elapsed_s = 0.0
            self._dmm_fault_during_relax = False
            self._state = OcvSocState.STEP_RELAX
            return

        step_Ah = self._total_capacity_Ah * self._config.step_percent / 100.0
        if step_removed >= step_Ah:
            self._removed_Ah_total += step_removed
            self._load.input_off()
            soc_after_step = self._soc_index - self._config.step_percent
            if round(soc_after_step, 6) in _KEYPOINTS:
                self._current_relax_s = self._config.relax_keypoint_s
            else:
                self._current_relax_s = self._config.relax_default_s
            self._relax_elapsed_s = 0.0
            self._dmm_fault_during_relax = False
            self._state = OcvSocState.STEP_RELAX

    def _run_impulse_prep(self) -> None:
        """OCV mérés a relax végén."""
        try:
            self._last_ocv_V = self._dmm_v.read_voltage()
        except Exception:
            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return
            self._last_ocv_V = self._u_batt
        self._state = OcvSocState.IMPULSE_ON

    def _run_impulse_on(self) -> None:
        """Impulzus start: terhelés bekapcsolás, OCV tárolás → IMPULSE_WAIT_1S."""
        try:
            self._v_before_impulse = self._dmm_v.read_voltage()
            self._load.set_mode_cc()
            self._load.set_current(self._impulse_current_A_set)
            self._load.input_on()
        except Exception as exc:
            self.emergency_stop(f"IMPULSE_START_FAILED: {exc}")
            return
        self._impulse_elapsed_s = 0.0
        self._impulse_current_A = self._impulse_current_A_set
        self._state = OcvSocState.IMPULSE_WAIT_1S

    def _run_impulse_wait(self, dt_s: float) -> None:
        """Tick-alapú impulzus várakozás: 1s, 10s, 30s mintavétel."""
        self._impulse_elapsed_s += dt_s

        try:
            v_now = self._dmm_v.read_voltage()
        except Exception:
            try:
                self._load.input_off()
            except Exception:
                pass
            self.emergency_stop("DMM_FEEDBACK_LOST_DURING_IMPULSE")
            return

        i_set = self._impulse_current_A_set

        if self._state == OcvSocState.IMPULSE_WAIT_1S and self._impulse_elapsed_s >= 1.0:
            self._v_1s = v_now
            self._rb_1s = (self._v_before_impulse - self._v_1s) / i_set if i_set > 0 else 0.0
            self._state = OcvSocState.IMPULSE_WAIT_10S

        elif self._state == OcvSocState.IMPULSE_WAIT_10S and self._impulse_elapsed_s >= 10.0:
            self._v_10s = v_now
            self._rb_10s = (self._v_before_impulse - self._v_10s) / i_set if i_set > 0 else 0.0
            self._state = OcvSocState.IMPULSE_WAIT_30S

        elif self._state == OcvSocState.IMPULSE_WAIT_30S and self._impulse_elapsed_s >= 30.0:
            v_30s = v_now
            self._rb_30s = (self._v_before_impulse - v_30s) / i_set if i_set > 0 else 0.0
            try:
                self._load.input_off()
            except Exception as exc:
                self.emergency_stop(f"IMPULSE_LOAD_OFF_FAILED: {exc}")
                return
            self._state = OcvSocState.LOG_SOC_POINT

    def _run_log_soc_point(self) -> None:
        """SOC pont rögzítése és döntés a következő lépésről."""
        quality = "DEGRADED_DMM_LOSS_DURING_RELAX" if self._dmm_fault_during_relax else "OK"
        self._dmm_fault_during_relax = False

        if self.on_soc_point is not None:
            self.on_soc_point({
                "soc_percent": self._soc_index,
                "removed_Ah_total": self._removed_Ah_total,
                "ocv_V": self._last_ocv_V,
                "rb_1s_ohm": self._rb_1s,
                "rb_10s_ohm": self._rb_10s,
                "rb_30s_ohm": self._rb_30s,
                "temperature_C": self._battery_temperature_C,
                "relax_duration_s": self._current_relax_s,
                "measurement_quality": quality,
            })

        self._soc_index -= self._config.step_percent

        if self._soc_index <= 0.0:
            self._state = OcvSocState.DONE
        else:
            self._state = OcvSocState.STEP_DISCHARGE
            self._begin_step_discharge()

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
