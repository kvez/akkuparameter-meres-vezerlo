"""
SafetyManager tesztek — FÁZIS 1.
Csak az állapotfüggetlen és konfigurálható policy-ket teszteli.
A tényleges mérési ciklus safety (BATTERY_OVERVOLTAGE stb.) a charge_controller
tesztjeinél kerül ellenőrzésre mock driverekkel.
"""
import pytest
from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import (
    SafetyManager,
    PsuMode,
    TempCompMode,
    FaultCode,
    WarningCode,
)


def make_12v_profile(**kwargs) -> BatteryProfile:
    defaults = dict(
        battery_name="Teszt", manufacturer="FIAMM", model="FG20721",
        nominal_capacity_Ah=7.0, cell_count=6, nominal_voltage_V=12.0,
    )
    defaults.update(kwargs)
    return BatteryProfile(**defaults)


def make_24v_profile(**kwargs) -> BatteryProfile:
    defaults = dict(
        battery_name="Teszt", manufacturer="FIAMM", model="FGH21803",
        nominal_capacity_Ah=18.0, cell_count=12, nominal_voltage_V=24.0,
    )
    defaults.update(kwargs)
    return BatteryProfile(**defaults)


class TestPsuModeCompatibility:
    """[N3] 24V pack + SERIES ≠ kötelező → ERROR; 12V + SERIES → WARNING"""

    def test_24v_pack_with_independent_mode_is_error(self):
        profile = make_24v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_psu_mode_compatibility()
        assert result.fault == FaultCode.PSU_MODE_INCOMPATIBLE_WITH_PACK_VOLTAGE

    def test_24v_pack_with_parallel_mode_is_error(self):
        profile = make_24v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.PARALLEL)
        result = sm.check_psu_mode_compatibility()
        assert result.fault == FaultCode.PSU_MODE_INCOMPATIBLE_WITH_PACK_VOLTAGE

    def test_24v_pack_with_series_mode_is_ok(self):
        profile = make_24v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.SERIES)
        result = sm.check_psu_mode_compatibility()
        assert result.fault is None

    def test_12v_pack_with_independent_mode_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_psu_mode_compatibility()
        assert result.fault is None
        assert result.warning is None

    def test_12v_pack_with_parallel_mode_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.PARALLEL)
        result = sm.check_psu_mode_compatibility()
        assert result.fault is None

    def test_12v_pack_with_series_mode_is_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.SERIES)
        result = sm.check_psu_mode_compatibility()
        assert result.fault is None
        assert result.warning == WarningCode.PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK


class TestPrecheckVoltageRange:
    """[N6] Feszültségtartomány ellenőrzés — mélykisült akku recovery NOT_IMPLEMENTED"""

    def test_12v_normal_voltage_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_precheck_voltage(measured_voltage_V=12.5)
        assert result.fault is None

    def test_12v_deep_discharge_is_error(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_precheck_voltage(measured_voltage_V=9.0)
        assert result.fault == FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED

    def test_12v_at_lower_limit_of_normal_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_precheck_voltage(measured_voltage_V=10.5)
        assert result.fault is None

    def test_12v_below_lower_limit_is_error(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_precheck_voltage(measured_voltage_V=5.0)
        assert result.fault == FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED

    def test_12v_overvoltage_is_error(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_precheck_voltage(measured_voltage_V=15.5)
        assert result.fault == FaultCode.BATTERY_OVERVOLTAGE

    def test_24v_normal_voltage_is_ok(self):
        profile = make_24v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.SERIES)
        result = sm.check_precheck_voltage(measured_voltage_V=25.0)
        assert result.fault is None

    def test_24v_deep_discharge_is_error(self):
        profile = make_24v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.SERIES)
        result = sm.check_precheck_voltage(measured_voltage_V=18.0)
        assert result.fault == FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED


class TestTemperatureDmmFaultEscalation:
    """[N2] MONITOR_ONLY: 60s után FAULT_FATAL; ENABLED: azonnali; OFF: INFO"""

    def test_monitor_only_short_fault_is_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
            temperature_dmm_fault_timeout_s=60,
        )
        result = sm.check_temperature_dmm_fault(elapsed_fault_s=30)
        assert result.fault is None
        assert result.warning == WarningCode.TEMPERATURE_DMM_LOST

    def test_monitor_only_timeout_exceeded_is_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
            temperature_dmm_fault_timeout_s=60,
        )
        result = sm.check_temperature_dmm_fault(elapsed_fault_s=70)
        assert result.fault == FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL

    def test_monitor_only_at_timeout_boundary_is_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
            temperature_dmm_fault_timeout_s=60,
        )
        result = sm.check_temperature_dmm_fault(elapsed_fault_s=60)
        assert result.fault == FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL

    def test_enabled_mode_immediate_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.ENABLED,
            temperature_dmm_fault_timeout_s=60,
        )
        result = sm.check_temperature_dmm_fault(elapsed_fault_s=0)
        assert result.fault == FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL

    def test_off_mode_no_fault_no_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.OFF,
            temperature_dmm_fault_timeout_s=60,
        )
        result = sm.check_temperature_dmm_fault(elapsed_fault_s=120)
        assert result.fault is None
        assert result.warning is None


class TestBatteryOvervoltage:
    def test_voltage_below_limit_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_battery_voltage(measured_V=14.40)
        assert result.fault is None

    def test_voltage_at_limit_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_battery_voltage(measured_V=14.55)
        assert result.fault is None

    def test_voltage_above_limit_is_overvoltage(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_battery_voltage(measured_V=14.56)
        assert result.fault == FaultCode.BATTERY_OVERVOLTAGE

    def test_24v_pack_voltage_limit(self):
        profile = make_24v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.SERIES)
        result = sm.check_battery_voltage(measured_V=29.11)
        assert result.fault == FaultCode.BATTERY_OVERVOLTAGE


class TestDiodePowerWarning:
    """[BY550] Dióda disszipáció ellenőrzés"""

    def test_low_power_no_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT,
                           diode_power_warning_W=2.0, diode_power_fault_W=3.0)
        result = sm.check_diode_power(charge_current_A=1.0, u_drop_V=0.85)
        assert result.fault is None
        assert result.warning is None

    def test_high_power_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT,
                           diode_power_warning_W=2.0, diode_power_fault_W=3.0)
        result = sm.check_diode_power(charge_current_A=2.5, u_drop_V=0.85)
        assert result.warning == WarningCode.DIODE_POWER_HIGH

    def test_very_high_power_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT,
                           diode_power_warning_W=2.0, diode_power_fault_W=3.0)
        result = sm.check_diode_power(charge_current_A=3.0, u_drop_V=1.05)
        assert result.warning == WarningCode.DIODE_POWER_TOO_HIGH
