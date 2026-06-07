from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from Prog.src.battery_profile import BatteryProfile


class PsuMode(Enum):
    INDEPENDENT = "INDEPENDENT"
    PARALLEL = "PARALLEL"
    SERIES = "SERIES"


class TempCompMode(Enum):
    OFF = "OFF"
    MONITOR_ONLY = "MONITOR_ONLY"
    ENABLED = "ENABLED"


class FaultCode(Enum):
    PSU_MODE_INCOMPATIBLE_WITH_PACK_VOLTAGE = auto()
    DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED = auto()
    BATTERY_OVERVOLTAGE = auto()
    BATTERY_UNDERVOLTAGE = auto()
    TEMPERATURE_MONITOR_LOST_CRITICAL = auto()
    INTEGRATION_FALLBACK_TOO_LONG = auto()
    CONCURRENT_PSU_LOAD_ON = auto()
    DMM_FEEDBACK_LOST = auto()
    SERIES_DROP_TOO_HIGH = auto()
    LOAD_POWER_LIMIT = auto()
    MAX_CHARGE_AH_REACHED = auto()
    MAX_CHARGE_TIME_REACHED = auto()
    PSU_COMM_LOST = auto()
    LOAD_COMM_LOST = auto()


class WarningCode(Enum):
    PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK = auto()
    TEMPERATURE_OUT_OF_RANGE = auto()
    TEMPERATURE_DMM_LOST = auto()
    HEADROOM_APPROACHING = auto()
    REDUCED_CHARGE_CURRENT_DUE_TO_PSU_LIMIT = auto()
    PSU_LEAKAGE_SUSPECTED = auto()
    INTEGRATION_FALLBACK_TO_SETPOINT = auto()
    PSU_OUTPUT_STATE_UNCERTAIN = auto()
    LOAD_INPUT_STATE_UNCERTAIN = auto()
    SERIES_DROP_HIGH = auto()
    DIODE_POWER_HIGH = auto()
    DIODE_POWER_TOO_HIGH = auto()


@dataclass
class SafetyResult:
    fault: Optional[FaultCode] = None
    warning: Optional[WarningCode] = None
    message: str = ""

    @property
    def is_ok(self) -> bool:
        return self.fault is None


@dataclass
class SafetyManager:
    profile: BatteryProfile
    psu_mode: PsuMode
    temp_comp_mode: TempCompMode = TempCompMode.MONITOR_ONLY
    temperature_dmm_fault_timeout_s: float = 60.0
    diode_power_warning_W: float = 2.0
    diode_power_critical_warning_W: float = 3.0
    warning_series_drop_V: float = 1.05
    fault_series_drop_V: float = 1.25

    def check_psu_mode_compatibility(self) -> SafetyResult:
        """
        [N3] PSU mód és pack feszültség kompatibilitás ellenőrzése.

        Szabályok (implementáld):
          - 24V pack (cell_count >= 12): SERIES mód kötelező.
            INDEPENDENT vagy PARALLEL → FaultCode.PSU_MODE_INCOMPATIBLE_WITH_PACK_VOLTAGE
          - 12V pack (cell_count <= 6) + SERIES mód:
            → WarningCode.PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK
            (nem tiltott, de szokatlan és kezelői figyelmeztető dialógus szükséges)
          - Minden más kombináció: SafetyResult() (fault=None, warning=None)

        Returns:
            SafetyResult — fault ha indítás tiltott, warning ha csak figyelmeztetés
        """
        if self.profile.cell_count >= 12 and self.psu_mode != PsuMode.SERIES:
            return SafetyResult(
                fault=FaultCode.PSU_MODE_INCOMPATIBLE_WITH_PACK_VOLTAGE,
                message=(
                    f"{self.profile.cell_count} cellás / 24V+ pack csak SERIES "
                    f"PSU módban indítható. Aktuális mód: {self.psu_mode.value}"
                ),
            )

        if self.profile.cell_count <= 6 and self.psu_mode == PsuMode.SERIES:
            return SafetyResult(
                warning=WarningCode.PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK,
                message=(
                    f"{self.profile.cell_count} cellás / 12V pack SERIES PSU módban "
                    f"szokatlan; kezelői megerősítés szükséges."
                ),
            )

        return SafetyResult()

    def check_precheck_voltage(self, measured_voltage_V: float) -> SafetyResult:
        """
        [N6] Precheck: mért akkufeszültség ésszerűségi ellenőrzése.

        12V pack (cell_count=6):
          < 6.0 V:  FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED
          6.0–10.5 V: ugyanaz (mélykisült, recovery NOT_IMPLEMENTED)
          10.5–batt_absolute_max_V: OK
          > batt_absolute_max_V: FaultCode.BATTERY_OVERVOLTAGE

        24V pack: határok kétszerese (12.0 / 21.0 / batt_absolute_max_V).
        """
        if self.profile.cell_count == 6:
            deep_discharge_threshold_V = 10.5
        else:
            deep_discharge_threshold_V = 21.0

        if measured_voltage_V > self.profile.batt_absolute_max_V:
            return SafetyResult(
                fault=FaultCode.BATTERY_OVERVOLTAGE,
                message=f"Precheck: feszültség {measured_voltage_V:.2f}V > "
                        f"max {self.profile.batt_absolute_max_V:.2f}V"
            )
        if measured_voltage_V < deep_discharge_threshold_V:
            return SafetyResult(
                fault=FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED,
                message=f"Precheck: mélykisült akku {measured_voltage_V:.2f}V < "
                        f"{deep_discharge_threshold_V:.2f}V. "
                        f"Recovery mód nincs implementálva."
            )
        return SafetyResult()

    def check_temperature_dmm_fault(self, elapsed_fault_s: float) -> SafetyResult:
        """
        [N2] Hőmérséklet-DMM kiesés kezelése mód szerint.

        ENABLED:      azonnali FAULT_FATAL (elapsed_fault_s bármennyi)
        MONITOR_ONLY: 0..timeout → WARNING; >= timeout → FAULT_FATAL
        OFF:          nincs fault, nincs warning
        """
        if self.temp_comp_mode == TempCompMode.OFF:
            return SafetyResult()

        if self.temp_comp_mode == TempCompMode.ENABLED:
            if elapsed_fault_s > 0.0:
                return SafetyResult(
                    fault=FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL,
                    message="Temp DMM kiesés ENABLED módban — azonnali leállítás"
                )
            return SafetyResult()

        # MONITOR_ONLY
        if elapsed_fault_s >= self.temperature_dmm_fault_timeout_s:
            return SafetyResult(
                fault=FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL,
                message=f"Temp DMM kiesés > {self.temperature_dmm_fault_timeout_s:.0f}s"
            )
        if elapsed_fault_s > 0.0:
            return SafetyResult(
                warning=WarningCode.TEMPERATURE_DMM_LOST,
                message=f"Temp DMM kiesés {elapsed_fault_s:.0f}s"
            )
        return SafetyResult()

    def check_battery_voltage(self, measured_V: float) -> SafetyResult:
        """Futó mérés közben: BATTERY_OVERVOLTAGE ellenőrzés."""
        if measured_V > self.profile.batt_absolute_max_V:
            return SafetyResult(
                fault=FaultCode.BATTERY_OVERVOLTAGE,
                message=f"U_batt {measured_V:.3f}V > max {self.profile.batt_absolute_max_V:.3f}V"
            )
        return SafetyResult()

    def check_diode_power(
        self, charge_current_A: float, u_drop_V: float
    ) -> SafetyResult:
        """[BY550] Dióda disszipáció ellenőrzés."""
        p_diode = abs(charge_current_A) * u_drop_V
        if p_diode > self.diode_power_critical_warning_W:
            return SafetyResult(
                warning=WarningCode.DIODE_POWER_TOO_HIGH,
                message=f"Dióda disszipáció {p_diode:.2f}W > {self.diode_power_critical_warning_W:.1f}W"
            )
        if p_diode > self.diode_power_warning_W:
            return SafetyResult(
                warning=WarningCode.DIODE_POWER_HIGH,
                message=f"Dióda disszipáció {p_diode:.2f}W > {self.diode_power_warning_W:.1f}W"
            )
        return SafetyResult()

    def check_concurrent_psu_load(
        self, psu_commanded_on: bool, load_commanded_on: bool
    ) -> SafetyResult:
        """PSU ON + Load ON egyszerre nem megengedett — hardware conflict detektálás."""
        if psu_commanded_on and load_commanded_on:
            return SafetyResult(
                fault=FaultCode.CONCURRENT_PSU_LOAD_ON,
                message="PSU és Load egyszerre aktív — hardware conflict!"
            )
        return SafetyResult()

    def check_series_drop(self, u_drop_V: float) -> SafetyResult:
        """[BY550] Soros dióda feszültségesés kétszintű ellenőrzés."""
        if u_drop_V > self.fault_series_drop_V:
            return SafetyResult(
                fault=FaultCode.SERIES_DROP_TOO_HIGH,
                message=f"U_drop {u_drop_V:.3f}V > fault limit {self.fault_series_drop_V:.2f}V"
            )
        if u_drop_V > self.warning_series_drop_V:
            return SafetyResult(
                warning=WarningCode.SERIES_DROP_HIGH,
                message=f"U_drop {u_drop_V:.3f}V > warning limit {self.warning_series_drop_V:.2f}V"
            )
        return SafetyResult()
