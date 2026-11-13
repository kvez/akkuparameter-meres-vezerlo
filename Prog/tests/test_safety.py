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
    return BatteryProfile(**defaults)  # type: ignore[arg-type]


def make_24v_profile(**kwargs) -> BatteryProfile:
    defaults = dict(
        battery_name="Teszt", manufacturer="FIAMM", model="FGH21803",
        nominal_capacity_Ah=18.0, cell_count=12, nominal_voltage_V=24.0,
    )
    defaults.update(kwargs)
    return BatteryProfile(**defaults)  # type: ignore[arg-type]


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

    def test_8_cell_pack_threshold_scales_with_cell_count(self):
        """[regresszió, Teljes kódbázis review 20. kör (2026-09-06), Hardver/
        Műszervezérlés P1 lelet] korábban a küszöb bináris (6 vs. minden-más)
        elágazás volt — 7-11 cellás packnél a 21.0V-os "minden-más" ág egy
        ténylegesen jó, teljesen feltöltött akkut is tévesen mélykisültnek
        minősített volna (14.0V lenne a helyes, cell_count-tal skálázott
        küszöb: 1.75×8=14.0V)."""
        profile = make_12v_profile(cell_count=8, nominal_voltage_V=16.0)
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.SERIES)
        result = sm.check_precheck_voltage(measured_voltage_V=15.0)
        assert result.fault is None

    def test_18_cell_pack_deep_discharge_uses_scaled_threshold_not_12v_binary_fallback(self):
        """[regresszió, Teljes kódbázis review 20. kör (2026-09-06), Hardver/
        Műszervezérlés P1 lelet] a VESZÉLYES irány: 13-24 cellás packnél a
        korábbi hardkódolt 21.0V-os küszöb egy ténylegesen mélykisült,
        nagyobb cellaszámú packet "OK"-nak minősített volna (a helyes,
        skálázott küszöb 1.75×18=31.5V — 25.0V ez alatt van, FAULT várt)."""
        profile = make_12v_profile(cell_count=18, nominal_voltage_V=36.0)
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.SERIES)
        result = sm.check_precheck_voltage(measured_voltage_V=25.0)
        assert result.fault == FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED


class TestReversePolarityPrecheck:
    """SAFE-009: negatív/nulla feszültség NEM lehet mélykisült akku — külön fault."""

    def test_negative_voltage_is_reverse_polarity_not_deep_discharge(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_precheck_voltage(measured_voltage_V=-0.5)
        assert result.fault == FaultCode.REVERSE_POLARITY_OR_WIRING_FAULT

    def test_zero_voltage_is_reverse_polarity(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_precheck_voltage(measured_voltage_V=0.0)
        assert result.fault == FaultCode.REVERSE_POLARITY_OR_WIRING_FAULT

    def test_small_positive_deep_discharge_unaffected(self):
        """Regresszió: legitim, alacsony-de-pozitív feszültség marad DEEPLY_DISCHARGED."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_precheck_voltage(measured_voltage_V=5.0)
        assert result.fault == FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED


class TestCheckTestTemperatureRange:
    """[regresszió, Teljes kódbázis review 20. kör (2026-09-06), Metrológia/
    Adatintegritás P2 lelet] a BatteryProfile.test_temperature_min_C/max_C
    mezőket a GUI ténylegesen betölti a YAML-ból, de korábban semmi nem
    vetette össze a mért hőmérséklettel — a WarningCode.TEMPERATURE_OUT_OF_
    RANGE enum-tag sosem került ténylegesen kiadásra."""

    def test_within_range_is_ok(self):
        profile = make_12v_profile()  # test_temperature_min/max_C default 15.0/30.0
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_test_temperature_range(battery_temperature_C=22.0)
        assert result.fault is None
        assert result.warning is None

    def test_below_min_is_warning_not_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_test_temperature_range(battery_temperature_C=5.0)
        assert result.fault is None
        assert result.warning == WarningCode.TEMPERATURE_OUT_OF_RANGE

    def test_above_max_is_warning_not_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_test_temperature_range(battery_temperature_C=35.0)
        assert result.fault is None
        assert result.warning == WarningCode.TEMPERATURE_OUT_OF_RANGE

    def test_at_exact_boundaries_is_ok(self):
        profile = make_12v_profile(
            test_temperature_min_C=15.0, test_temperature_max_C=30.0
        )
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        assert sm.check_test_temperature_range(15.0).warning is None
        assert sm.check_test_temperature_range(30.0).warning is None

    def test_custom_profile_range_respected(self):
        """Egy szélesebb, explicit deklarált tartomány (pl. ipari akkuhoz)
        ne váltson ki figyelmeztetést a saját, tágabb határain belül."""
        profile = make_12v_profile(
            test_temperature_min_C=-10.0, test_temperature_max_C=50.0
        )
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_test_temperature_range(battery_temperature_C=5.0)
        assert result.warning is None


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

    def test_enabled_mode_no_fault_when_dmm_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.ENABLED,
            temperature_dmm_fault_timeout_s=60,
        )
        result = sm.check_temperature_dmm_fault(elapsed_fault_s=0.0)
        assert result.fault is None

    def test_enabled_mode_immediate_fault_on_any_loss(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.ENABLED,
            temperature_dmm_fault_timeout_s=60,
        )
        result = sm.check_temperature_dmm_fault(elapsed_fault_s=0.1)
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


class TestAmbientTemperatureRise:
    """CHG-004: battery_temp > ambient + 10°C → FAULT, csak MANUAL/EXTERNAL_SENSOR
    esetén kényszeríthető ki — UNKNOWN alapértelmezés mellett nem."""

    def test_unknown_source_never_faults(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_ambient_temperature_rise(
            battery_temperature_C=60.0,
            ambient_temperature_source="UNKNOWN",
            ambient_temperature_C=None,
        )
        assert result.fault is None

    def test_manual_source_without_value_does_not_fault(self):
        """Ha a forrás MANUAL de az érték hiányzik, ne dobjon hamis faultot —
        a ChargeConfig.__post_init__ ezt az esetet validáció szinten már tiltja,
        de a safety-metódus önmagában is legyen fail-safe."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_ambient_temperature_rise(
            battery_temperature_C=60.0,
            ambient_temperature_source="MANUAL",
            ambient_temperature_C=None,
        )
        assert result.fault is None

    def test_manual_source_over_10c_rise_faults(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_ambient_temperature_rise(
            battery_temperature_C=35.1,
            ambient_temperature_source="MANUAL",
            ambient_temperature_C=25.0,
        )
        assert result.fault == FaultCode.BATTERY_TEMPERATURE_RISE_OVER_AMBIENT

    def test_manual_source_at_exactly_10c_rise_does_not_fault(self):
        """Határeset: pontosan +10°C még NEM sérti a '>' feltételt."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_ambient_temperature_rise(
            battery_temperature_C=35.0,
            ambient_temperature_source="MANUAL",
            ambient_temperature_C=25.0,
        )
        assert result.fault is None

    def test_external_sensor_source_over_10c_rise_faults(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_ambient_temperature_rise(
            battery_temperature_C=40.0,
            ambient_temperature_source="EXTERNAL_SENSOR",
            ambient_temperature_C=20.0,
        )
        assert result.fault == FaultCode.BATTERY_TEMPERATURE_RISE_OVER_AMBIENT

    def test_initial_battery_approx_source_never_faults(self):
        """INITIAL_BATTERY_APPROX jelen körben nem implementált — nem
        kényszerítheti ki hamisan a feltételt."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_ambient_temperature_rise(
            battery_temperature_C=60.0,
            ambient_temperature_source="INITIAL_BATTERY_APPROX",
            ambient_temperature_C=20.0,
        )
        assert result.fault is None

    def test_manual_source_within_10c_does_not_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_ambient_temperature_rise(
            battery_temperature_C=28.0,
            ambient_temperature_source="MANUAL",
            ambient_temperature_C=25.0,
        )
        assert result.fault is None


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

    def test_negative_voltage_is_reverse_polarity(self):
        """SAFE-009: aktív mérés közben is külön hiba, nem csendes OK."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_battery_voltage(measured_V=-1.0)
        assert result.fault == FaultCode.REVERSE_POLARITY_OR_WIRING_FAULT

    def test_zero_voltage_is_reverse_polarity_during_run(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_battery_voltage(measured_V=0.0)
        assert result.fault == FaultCode.REVERSE_POLARITY_OR_WIRING_FAULT


class TestConcurrentPsuLoad:
    """CONCURRENT_PSU_LOAD_ON: PSU ON + Load ON egyszerre → FAULT."""

    def test_both_off_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_concurrent_psu_load(psu_commanded_on=False, load_commanded_on=False)
        assert result.fault is None

    def test_only_psu_on_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_concurrent_psu_load(psu_commanded_on=True, load_commanded_on=False)
        assert result.fault is None

    def test_only_load_on_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_concurrent_psu_load(psu_commanded_on=False, load_commanded_on=True)
        assert result.fault is None

    def test_both_on_is_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_concurrent_psu_load(psu_commanded_on=True, load_commanded_on=True)
        assert result.fault == FaultCode.CONCURRENT_PSU_LOAD_ON


class TestCheckLoadPower:
    """Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P1 lelet:
    Keithley 2380-120-60 250W hardver-teljesítménykorlát ellenőrzés — a
    beágyazott (inline) kisütési fázisok (chemid/golden_gg/traditional_
    learning/ocv_soc_controller.py) ezt eddig nem tartalmazták, ellentétben
    a standalone DischargeController._run_cc()-vel."""

    def test_under_limit_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_power(measured_voltage_V=13.0, measured_current_A=10.0)
        assert result.fault is None

    def test_over_default_limit_is_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_power(measured_voltage_V=13.0, measured_current_A=25.0)
        assert result.fault == FaultCode.LOAD_POWER_LIMIT

    def test_exact_limit_boundary_is_ok(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_power(measured_voltage_V=10.0, measured_current_A=25.0)  # 250.0W
        assert result.fault is None

    def test_custom_max_power_respected(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_power(
            measured_voltage_V=13.0, measured_current_A=10.0, max_load_power_W=100.0,
        )
        assert result.fault == FaultCode.LOAD_POWER_LIMIT

    def test_instance_field_is_the_single_default_source(self):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), Szoftver/
        Architektúra + Hardver/Műszervezérlés P2 lelet, dedup] korábban
        max_load_power_W egy HÍVÁSONKÉNTI paraméter-defaultként (250.0)
        élt, teljesen függetlenül a standalone DischargeController saját
        DischargeConfig.max_load_power_W mezőjétől — két, egymástól
        független forrás, nincs mechanizmus egy alacsonyabb limit egységes
        átvételére. Mostantól a SafetyManager.max_load_power_W az EGYETLEN
        forrás — a konstruktoron beállított érték paraméter-override
        nélkül is érvényesül."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT, max_load_power_W=50.0)
        result = sm.check_load_power(measured_voltage_V=13.0, measured_current_A=10.0)
        assert result.fault == FaultCode.LOAD_POWER_LIMIT
        assert "max 50.0W" in result.message


class TestCheckLoadCurrentSetpoint:
    """Review-kör 4 (2026-09-09), Hardver/Műszervezérlés HW4-02 (P1,
    DFAM-001): a Load CC-módú setpointja és a ténylegesen mért árama
    korábban sehol nem került összehasonlításra -- egy elmaradó áram
    (védelmi latch, CC-tartomány-elutasítás, csendes SCPI-elutasítás)
    fizikailag hihető, de HAMIS adatfolyamként jelent volna meg."""

    def test_within_tolerance_is_no_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_current_setpoint(measured_current_A=9.6, setpoint_current_A=10.0)
        assert result.warning is None

    def test_exceeding_default_tolerance_is_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_current_setpoint(measured_current_A=8.0, setpoint_current_A=10.0)
        assert result.warning == WarningCode.LOAD_CURRENT_SETPOINT_NOT_REACHED

    def test_exact_tolerance_boundary_is_no_warning(self):
        """[regresszió, MAX verifier (review-kör 4, HW4-02) N3 lelete] a
        korábbi két teszt (9.6/10.0 és 8.0/10.0) messze a 10%-os határtól
        futott -- egy `>` -> `>=` mutáns mindkettőt túlélte volna. Pontosan
        a határon (relatív eltérés == 0.10) a `>` (nem `>=`) miatt NEM
        szabad figyelmeztetést adnia."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_current_setpoint(measured_current_A=9.0, setpoint_current_A=10.0)
        assert result.warning is None

    def test_just_over_tolerance_boundary_is_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_current_setpoint(measured_current_A=8.9, setpoint_current_A=10.0)
        assert result.warning == WarningCode.LOAD_CURRENT_SETPOINT_NOT_REACHED

    def test_current_stuck_at_zero_is_warning(self):
        """A failure scenario szó szerinti alakzata: a Load elutasítja a
        setpointot (pl. CC-tartomány/latch), az áram az előző (itt: 0 A)
        szinten ragad."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_current_setpoint(measured_current_A=0.0, setpoint_current_A=12.4)
        assert result.warning == WarningCode.LOAD_CURRENT_SETPOINT_NOT_REACHED

    def test_no_active_setpoint_is_no_warning(self):
        """setpoint_current_A<=0 -- nincs aktív setpoint (pl. Load még
        nincs bekapcsolva) -- nincs mit ellenőrizni, nem oszt nullával."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_current_setpoint(measured_current_A=0.0, setpoint_current_A=0.0)
        assert result.warning is None

    def test_never_a_fault(self):
        """A reviewer saját javaslata szerint ez CSAK figyelmeztetés --
        nem indokolja emergency_stop()-ot önmagában."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_current_setpoint(measured_current_A=0.0, setpoint_current_A=12.4)
        assert result.fault is None

    def test_custom_tolerance_factor_respected(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_load_current_setpoint(
            measured_current_A=8.0, setpoint_current_A=10.0, tolerance_factor=0.30,
        )
        assert result.warning is None

    def test_instance_field_is_the_single_default_source(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile, psu_mode=PsuMode.INDEPENDENT,
            load_current_setpoint_tolerance_factor=0.30,
        )
        result = sm.check_load_current_setpoint(measured_current_A=8.0, setpoint_current_A=10.0)
        assert result.warning is None


class TestCheckPsuChargeCurrentSetpoint:
    """Kor 18, Batch C7 R17-10 (DFAM-001): a check_load_current_setpoint()
    PSU-oldali párja -- a HW4-02 failure scenario töltési megfelelője. A
    `compliance_headroom_exhausted` scoping KÖTELEZŐ: CC fázisban a mért
    áram LEGITIMEN elmaradhat a setponttól, amíg a compliance-emelés még
    dolgozik -- csak a plafon elérése után jelent a hiány valódi hibát."""

    def test_warns_when_below_tolerance_and_headroom_exhausted(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_psu_charge_current_setpoint(
            measured_current_A=5.0, setpoint_current_A=10.0,
            compliance_headroom_exhausted=True,
        )
        assert result.warning == WarningCode.PSU_CHARGE_CURRENT_SETPOINT_NOT_REACHED

    def test_no_warning_while_headroom_remains(self):
        """A CV/ramp hamis-pozitív diszkriminátor: UGYANAZ a mért/setpoint
        pár, de compliance_headroom_exhausted=False -- ez a normál CC-
        rampa, NEM hiba."""
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_psu_charge_current_setpoint(
            measured_current_A=5.0, setpoint_current_A=10.0,
            compliance_headroom_exhausted=False,
        )
        assert result.warning is None

    def test_no_warning_within_tolerance(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_psu_charge_current_setpoint(
            measured_current_A=9.6, setpoint_current_A=10.0,
            compliance_headroom_exhausted=True,
        )
        assert result.warning is None

    def test_zero_setpoint_returns_empty(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_psu_charge_current_setpoint(
            measured_current_A=0.0, setpoint_current_A=0.0,
            compliance_headroom_exhausted=True,
        )
        assert result.warning is None

    def test_never_returns_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_psu_charge_current_setpoint(
            measured_current_A=5.0, setpoint_current_A=10.0,
            compliance_headroom_exhausted=True,
        )
        assert result.fault is None

    def test_custom_tolerance_factor_respected(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        result = sm.check_psu_charge_current_setpoint(
            measured_current_A=8.0, setpoint_current_A=10.0,
            compliance_headroom_exhausted=True, tolerance_factor=0.30,
        )
        assert result.warning is None

    def test_instance_field_is_the_single_default_source(self):
        profile = make_12v_profile()
        sm = SafetyManager(
            profile=profile, psu_mode=PsuMode.INDEPENDENT,
            psu_charge_current_setpoint_tolerance_factor=0.30,
        )
        result = sm.check_psu_charge_current_setpoint(
            measured_current_A=8.0, setpoint_current_A=10.0,
            compliance_headroom_exhausted=True,
        )
        assert result.warning is None


class TestDiodePowerWarning:
    """[BY550] Dióda disszipáció ellenőrzés"""

    def test_low_power_no_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT,
                           diode_power_warning_W=2.0, diode_power_critical_warning_W=3.0)
        result = sm.check_diode_power(charge_current_A=1.0, u_drop_V=0.85)
        assert result.fault is None
        assert result.warning is None

    def test_high_power_warning(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT,
                           diode_power_warning_W=2.0, diode_power_critical_warning_W=3.0)
        result = sm.check_diode_power(charge_current_A=2.5, u_drop_V=0.85)
        assert result.warning == WarningCode.DIODE_POWER_HIGH

    def test_very_high_power_fault(self):
        profile = make_12v_profile()
        sm = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT,
                           diode_power_warning_W=2.0, diode_power_critical_warning_W=3.0)
        result = sm.check_diode_power(charge_current_A=3.0, u_drop_V=1.05)
        assert result.warning == WarningCode.DIODE_POWER_TOO_HIGH


class TestR11B1TemperatureHistory:
    def test_fresh_read_resets_history_and_new_manager_is_clean(self):
        profile = make_12v_profile()
        safety = SafetyManager(profile, PsuMode.INDEPENDENT)
        safety.record_temperature_dmm_read(valid=False, dt_s=58.0)
        assert safety.check_temperature_dmm_fault(
            safety.temperature_dmm_fault_s,
        ).warning == WarningCode.TEMPERATURE_DMM_LOST
        safety.record_temperature_dmm_read(valid=False, dt_s=2.0)
        assert safety.check_temperature_dmm_fault(
            safety.temperature_dmm_fault_s,
        ).fault == FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL
        other = SafetyManager(profile, PsuMode.INDEPENDENT)
        assert other.temperature_dmm_fault_s == 0.0
        assert safety.temperature_dmm_fault_s == 60.0
        safety.record_temperature_dmm_read(valid=True, dt_s=0.0)
        assert safety.temperature_dmm_fault_s == 0.0
        assert safety.check_temperature_dmm_fault(
            safety.temperature_dmm_fault_s,
        ).is_ok


class TestR11B4BatteryOvertemperature:
    """Q-14: 60 C PROJECT_POLICY; minden eszkoz a tesztgyarak mockja."""

    @staticmethod
    def _controller(kind):
        from importlib import import_module

        factory_name = {
            "discharge": "make_discharge_controller",
            "iec_dc_resistance": "make_iec_controller",
            "ocv_soc": "make_ocv_soc_controller",
        }.get(kind, "make_controller")
        module = import_module(f"Prog.tests.test_{kind}_controller")
        kwargs = {"dmm_voltage_V": 13.8} if kind == "charge" else {}
        return getattr(module, factory_name)(**kwargs)[0]

    @staticmethod
    def _trace_off(monkeypatch, ctrl, fail=""):
        calls = []
        for device, method, label in (
            (ctrl._load, "input_off", "load"),
            (ctrl._psu, "all_outputs_off", "psu"),
        ):
            original = getattr(device, method)

            def off(original=original, label=label):
                calls.append(label)
                if label == fail:
                    raise RuntimeError(f"{label} OFF failed")
                original()

            monkeypatch.setattr(device, method, off)
        return calls

    @staticmethod
    def _assert_no_active_command(ctrl):
        assert "input_on()" not in ctrl._load.calls
        assert "output_on()" not in ctrl._psu.calls
        assert not any(
            call.startswith(("set_output_", "set_current(", "set_mode_", "fire_transient"))
            for call in ctrl._psu.calls + ctrl._load.calls
        )

    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("temp,valid,trip", [
        (59.9, True, False), (60.0, True, True), (85.0, True, True),
        (85.0, False, False), (None, False, False),
        (float("nan"), True, False), (float("inf"), True, False),
    ])
    def test_policy_boundary_and_validity(self, mode, temp, valid, trip):
        sm = SafetyManager(make_12v_profile(), PsuMode.INDEPENDENT, temp_comp_mode=mode)
        result = sm.check_battery_overtemperature(temp, valid)
        if trip:
            assert result.fault is FaultCode.BATTERY_OVERTEMPERATURE
            assert "PROJECT_POLICY" in result.message
        else:
            assert result.fault is None

    def test_test_temperature_window_remains_warning(self):
        sm = SafetyManager(make_12v_profile(), PsuMode.INDEPENDENT)
        result = sm.check_test_temperature_range(60.0)
        assert result.fault is None
        assert result.warning is WarningCode.TEMPERATURE_OUT_OF_RANGE

    @pytest.mark.parametrize("kind,phase", [
        ("charge", "PRECHECK"), ("charge", "PSU_PRESET"),
        ("charge", "CHARGE_CC"), ("charge", "CHARGE_CV_DMM_CONTROL"),
        ("charge", "TAPER_HOLD"),
        ("discharge", "PRECHECK"), ("discharge", "DISCHARGE_CC_SETUP"),
        ("discharge", "DISCHARGE_CC_RUN"),
    ])
    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("temp", [59.9, 60.0, 85.0])
    def test_standalone_before_and_during_energy(self, monkeypatch, kind, phase, mode, temp):
        ctrl = self._controller(kind)
        ctrl._safety.temp_comp_mode = mode
        if phase not in ("PRECHECK", "PSU_PRESET", "DISCHARGE_CC_SETUP"):
            for _ in range(3):
                ctrl.advance(1.0)
        ctrl._state = getattr(type(ctrl.state), phase)
        ctrl._dmm_t.temperature_C = temp
        ctrl._psu.calls.clear()
        ctrl._load.calls.clear()
        off = self._trace_off(monkeypatch, ctrl)
        ctrl.advance(1.0)
        if temp >= 60.0:
            assert ctrl.state.name == "FAULT"
            assert ctrl.fault_reason == "BATTERY_OVERTEMPERATURE"
            assert off == ["load", "psu"]
            ctrl.advance(1.0)
            self._assert_no_active_command(ctrl)
        else:
            assert ctrl.state.name != "FAULT"
            if phase == "PRECHECK":
                self._assert_no_active_command(ctrl)

    @pytest.mark.parametrize("kind", ["charge", "discharge"])
    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("stale", [22.0, 85.0])
    def test_failed_fresh_read_uses_existing_dropout_policy(self, kind, mode, stale):
        ctrl = self._controller(kind)
        for _ in range(3):
            ctrl.advance(1.0)
        ctrl._safety.temp_comp_mode = mode
        ctrl._battery_temperature_C = stale
        ctrl._dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False
        if mode is TempCompMode.ENABLED:
            assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
        else:
            assert ctrl.state.name != "FAULT"
            if mode is TempCompMode.MONITOR_ONLY:
                ctrl.advance(60.0)
                assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"

    @pytest.mark.parametrize("kind", ["charge", "discharge"])
    @pytest.mark.parametrize("fail", ["load", "psu"])
    def test_off_failure_does_not_skip_other_off_or_reenergize(self, monkeypatch, kind, fail):
        ctrl = self._controller(kind)
        for _ in range(3):
            ctrl.advance(1.0)
        ctrl._dmm_t.temperature_C = 60.0
        ctrl._psu.calls.clear()
        ctrl._load.calls.clear()
        off = self._trace_off(monkeypatch, ctrl, fail)
        ctrl.advance(1.0)
        assert ctrl.state.name == "FAULT"
        assert ctrl.fault_reason.startswith("BATTERY_OVERTEMPERATURE | ")
        assert off == ["load", "psu"]
        ctrl.advance(1.0)
        self._assert_no_active_command(ctrl)

    @pytest.mark.parametrize("kind,phase", [
        ("chemid", "C20_DISCHARGE_TO_MANUFACTURER_MINIMUM"),
        ("golden_gg", "SYSTEM_RATE_DISCHARGE"),
        ("traditional_learning", "DISCHARGE_1"),
        ("traditional_learning", "DISCHARGE_2"),
        ("traditional_learning", "REBOUND_CHECK_1"),
        ("traditional_learning", "REBOUND_CHECK_2"),
        ("iec_dc_resistance", "LOAD_1"), ("iec_dc_resistance", "LOAD_2"),
        ("ocv_soc", "STEP_DISCHARGE"),
        ("ocv_soc", "IMPULSE_ON"), ("ocv_soc", "IMPULSE_ACQUIRE"),
        ("ocv_soc", "IMPULSE_FAST_ON"), ("ocv_soc", "IMPULSE_FAST_ACQUIRE"),
    ])
    @pytest.mark.parametrize("mode", list(TempCompMode))
    def test_composite_active_gate(self, monkeypatch, kind, phase, mode):
        ctrl = self._controller(kind)
        ctrl._safety.temp_comp_mode = mode
        ctrl._state = getattr(type(ctrl.state), phase)
        ctrl._dmm_t.temperature_C = 60.0
        ctrl._psu.calls.clear()
        ctrl._load.calls.clear()
        off = self._trace_off(monkeypatch, ctrl)
        ctrl.advance(1.0)
        assert ctrl.state.name == "FAULT"
        assert ctrl.fault_reason == "BATTERY_OVERTEMPERATURE"
        assert off == ["load", "psu"]
        ctrl.advance(1.0)
        self._assert_no_active_command(ctrl)
        if kind == "traditional_learning":
            from Prog.src.traditional_learning_controller import LEARNING_RESULT_FAILED
            assert ctrl.result_summary.result == LEARNING_RESULT_FAILED

    @pytest.mark.parametrize("kind", ["chemid", "golden_gg", "traditional_learning", "ocv_soc"])
    @pytest.mark.parametrize("temp", [59.9, 60.0])
    @pytest.mark.parametrize("mode", list(TempCompMode))
    def test_composite_direct_load_start(self, monkeypatch, kind, temp, mode):
        ctrl = self._controller(kind)
        ctrl._safety.temp_comp_mode = mode
        ctrl._dmm_t.temperature_C = temp
        ctrl._dmm_valid = ctrl._read_dmm(1.0)
        off = self._trace_off(monkeypatch, ctrl)
        if kind == "traditional_learning":
            from Prog.src.traditional_learning_controller import LearningState
            ctrl._begin_discharge(LearningState.DISCHARGE_1)
        else:
            method = {
                "chemid": "_begin_c20_discharge",
                "golden_gg": "_begin_system_rate_discharge",
                "ocv_soc": "_begin_step_discharge",
            }[kind]
            getattr(ctrl, method)()
        if temp == 60.0:
            assert ctrl.state.name == "FAULT"
            assert ctrl.fault_reason == "BATTERY_OVERTEMPERATURE"
            assert off == ["load", "psu"]
            self._assert_no_active_command(ctrl)
        else:
            assert ctrl.state.name != "FAULT"
            assert "input_on()" in ctrl._load.calls

    @pytest.mark.parametrize("mode", list(TempCompMode))
    def test_chemid_relax_to_discharge_uses_current_temperature(self, mode):
        ctrl = self._controller("chemid")
        ctrl._safety.temp_comp_mode = mode
        ctrl._state = type(ctrl.state).RELAX_5H_FULL
        ctrl._relax_elapsed_s = ctrl._config.relax_full_min_s
        ctrl._dmm_t.temperature_C = 60.0
        ctrl.advance(1.0)
        assert ctrl.fault_reason == "BATTERY_OVERTEMPERATURE"
        self._assert_no_active_command(ctrl)

    def test_tlc_verify_to_discharge_does_not_use_stale_temperature(self):
        ctrl = self._controller("traditional_learning")
        ctrl._state = type(ctrl.state).VERIFY_VOK_QEN
        ctrl._phase_elapsed_s = ctrl._config.verify_settle_s
        ctrl._battery_temperature_C = 22.0
        ctrl._dmm_valid = True
        ctrl._temp_dmm_valid = True
        ctrl._dmm_t.temperature_C = 60.0
        ctrl.advance(1.0)
        assert ctrl.fault_reason == "BATTERY_OVERTEMPERATURE"
        self._assert_no_active_command(ctrl)

    def test_fault_reaches_runner_sample_log_and_event(self):
        from unittest.mock import MagicMock

        from Prog.src import test_runner as runner_module
        from Prog.src.instrument_manager import InstrumentManager

        ctrl = self._controller("charge")
        ctrl._dmm_t.temperature_C = 60.0
        ctrl._config.max_charge_time_s = 0.0  # A javitas elotti futas is veges.
        logger = MagicMock()
        runner = runner_module.TestRunner(
            instrument_manager=InstrumentManager(
                psu=ctrl._psu, load=ctrl._load, dmm_voltage=ctrl._dmm_v,
                dmm_temperature=ctrl._dmm_t,
            ),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=runner_module.TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: ctrl, discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
        )
        events = []
        runner.on_event = events.append
        result = runner.run(runner_module.TestPlan(
            test_type=runner_module.TestType.CHARGE_ONLY,
            steps=(runner_module.TestStep(runner_module.StepKind.CHARGE, "R11-B4"),),
        ))
        assert result.status == "FAULT"
        assert result.reason == "BATTERY_OVERTEMPERATURE"
        from Prog.src.report_generator import ReportGenerator
        report = ReportGenerator().generate({
            "status": result.status, "reason": result.reason,
        })
        assert report["status"] == "FAULT"
        assert report["reason"] == "BATTERY_OVERTEMPERATURE"
        logger.log_event.assert_any_call(
            "EMERGENCY_STOP", "BATTERY_OVERTEMPERATURE", is_critical=True,
        )
        assert {
            "event_code": "EMERGENCY_STOP", "event_message": "BATTERY_OVERTEMPERATURE",
        } in events
        assert any(
            call.args[0].get("fault_flags") == "BATTERY_OVERTEMPERATURE"
            and call.args[0].get("battery_temperature_C") == 60.0
            for call in logger.log_sample.call_args_list
        )
        self._assert_no_active_command(ctrl)

    def test_direct_overtemperature_event_flushes(self):
        from unittest.mock import MagicMock

        from Prog.src.logger import Logger

        logger = Logger.__new__(Logger)
        logger._event_writer = MagicMock()
        logger.flush_all = MagicMock()
        logger.log_event("BATTERY_OVERTEMPERATURE", "R11-B4")
        logger.flush_all.assert_called_once()

    @pytest.mark.parametrize("mode", list(TempCompMode))
    def test_ocv_pulse_reads_fresh_temperature_without_voltage_read(self, monkeypatch, mode):
        from unittest.mock import Mock

        ctrl = self._controller("ocv_soc")
        ctrl._safety.temp_comp_mode = mode
        ctrl._state = type(ctrl.state).IMPULSE_PREP
        ctrl.advance(1.0)
        assert ctrl.state.name == "IMPULSE_ON"
        read_v = Mock(wraps=ctrl._dmm_v.read_voltage)
        read_t = Mock(wraps=ctrl._dmm_t.read_temperature)
        monkeypatch.setattr(ctrl._dmm_v, "read_voltage", read_v)
        monkeypatch.setattr(ctrl._dmm_t, "read_temperature", read_t)
        ctrl._dmm_t.temperature_C = 60.0
        ctrl.advance(1.0)
        assert ctrl.fault_reason == "BATTERY_OVERTEMPERATURE"
        read_t.assert_called_once()
        read_v.assert_not_called()
        self._assert_no_active_command(ctrl)

    @pytest.mark.parametrize("mode", list(TempCompMode))
    def test_ocv_pulse_invalid_temperature_keeps_dropout_policy(self, monkeypatch, mode):
        from unittest.mock import Mock

        ctrl = self._controller("ocv_soc")
        ctrl._safety.temp_comp_mode = mode
        ctrl._state = type(ctrl.state).IMPULSE_ON
        ctrl._battery_temperature_C = 85.0
        ctrl._dmm_t.simulate_temp_failure = True
        handler = Mock()
        monkeypatch.setattr(ctrl, "_run_impulse_on", handler)
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False
        if mode is TempCompMode.ENABLED:
            assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
            handler.assert_not_called()
        else:
            assert ctrl.fault_reason == ""
            handler.assert_called_once()
            if mode is TempCompMode.MONITOR_ONLY:
                ctrl.advance(60.0)
                assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"

    @pytest.mark.parametrize("fail", ["load", "psu"])
    def test_composite_start_off_failures(self, monkeypatch, fail):
        ctrl = self._controller("chemid")
        ctrl._dmm_t.temperature_C = 60.0
        ctrl._dmm_valid = ctrl._read_dmm(1.0)
        off = self._trace_off(monkeypatch, ctrl, fail)
        ctrl._begin_c20_discharge()
        assert ctrl.fault_reason.startswith("BATTERY_OVERTEMPERATURE | ")
        assert ctrl.state.name == "FAULT"
        assert off == ["load", "psu"]
        ctrl.advance(1.0)
        self._assert_no_active_command(ctrl)


class TestR16B16aIndependentFloor:
    @pytest.mark.parametrize("is_bq_mode", [False, True])
    @pytest.mark.parametrize("cells", [6, 12])
    def test_raw_valid_sample_below_floor_faults_even_with_lower_cutoff(self, is_bq_mode, cells):
        profile = make_12v_profile(cell_count=cells)
        safety = SafetyManager(profile, PsuMode.INDEPENDENT, is_bq_mode=is_bq_mode)
        result = safety.check_normal_discharge(1.79 * cells, 0.36, 1.80 * cells)
        assert result.fault == FaultCode.BATTERY_UNDERVOLTAGE
        result = safety.check_normal_discharge(2.0 * cells, 0.36, 1.75 * cells)
        assert result.fault == FaultCode.DISCHARGE_CONFIG_INVALID

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
    def test_nonfinite_configuration_and_sample_are_rejected(self, bad):
        safety = SafetyManager(make_12v_profile(), PsuMode.INDEPENDENT)
        assert safety.check_normal_discharge(12.0, 0.36, bad).fault == FaultCode.DISCHARGE_CONFIG_INVALID
        assert safety.check_normal_discharge(bad, 0.36, 10.8).fault == FaultCode.DMM_FEEDBACK_LOST


def _r16_b16af1_normal_case(kind, cells, voltage=None):
    # Mind a hat normál út valódi controllerrel, kizárólag mock műszerekkel.
    if kind in ("DISCHARGE", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"):
        from Prog.src.discharge_controller import DischargeState
        from Prog.tests.test_discharge_controller import make_discharge_controller
        parts = make_discharge_controller()
        ctrl = parts[0]
        ctrl._state = DischargeState.DISCHARGE_CC_SETUP
        begin = ctrl._run_cc_setup
    elif kind == "CHEMID":
        from Prog.src.chemid_controller import ChemIdState
        from Prog.tests.test_chemid_controller import make_controller
        parts = make_controller()
        ctrl = parts[0]
        ctrl._state = ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        begin = ctrl._begin_c20_discharge
    elif kind == "GOLDEN_GG":
        from Prog.src.golden_gg_controller import GoldenGgState
        from Prog.tests.test_golden_gg_controller import make_controller
        parts = make_controller()
        ctrl = parts[0]
        ctrl._state = GoldenGgState.SYSTEM_RATE_DISCHARGE
        begin = ctrl._begin_system_rate_discharge
    else:
        from Prog.src.ocv_soc_controller import OcvSocState
        from Prog.tests.test_ocv_soc_controller import make_ocv_soc_controller
        parts = make_ocv_soc_controller()
        ctrl = parts[0]
        ctrl._state = OcvSocState.STEP_DISCHARGE
        begin = ctrl._begin_step_discharge
    ctrl, psu, load, dmm = parts[:4]
    ctrl._profile.cell_count = cells
    ctrl._dmm_valid = True
    ctrl._u_batt = dmm.voltage_V = 2.1 * cells if voltage is None else voltage
    begin()
    return ctrl, psu, load, dmm


@pytest.mark.parametrize("kind", [
    "DISCHARGE", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL", "CHEMID", "OCV_SOC", "GOLDEN_GG",
])
@pytest.mark.parametrize("cells", [6, 12])
class TestR16B16aF1NormalCutoff:
    @pytest.mark.parametrize("undershoot_V", [0.001, 0.3])
    def test_first_crossing_finishes_and_logs_exact_overshoot(self, kind, cells, undershoot_V):
        ctrl, _psu, load, dmm = _r16_b16af1_normal_case(kind, cells)
        events = []
        ctrl.on_event = events.append
        assert load.input_commanded_on
        dmm.voltage_V = 1.8 * cells - undershoot_V
        ctrl.advance(1.0)
        expected = {
            "CHEMID": "RELAX_5H_EMPTY", "OCV_SOC": "STEP_RELAX", "GOLDEN_GG": "RELAX_5H",
        }.get(kind, "DISCHARGE_DONE")
        assert ctrl.state.value == expected
        assert not load.input_commanded_on
        assert ctrl.accumulated_discharge_Ah > 0.0
        overshoots = [e for e in events if e["event_code"] == "NORMAL_DISCHARGE_CUTOFF_OVERSHOOT"]
        assert len(overshoots) == 1
        event = overshoots[0]
        assert event["measured_V"] == pytest.approx(dmm.voltage_V)
        assert event["minimum_V"] == pytest.approx(1.8 * cells)
        assert event["overshoot_V"] == pytest.approx(1.8 * cells - dmm.voltage_V)
        assert event["severity"] == "WARNING"
        assert not any(e["event_code"] == "BATTERY_UNDERVOLTAGE" for e in events)

    @pytest.mark.parametrize("stuck_in_run", [False, True])
    def test_second_loaded_sample_faults_after_exact_cutoff(self, kind, cells, stuck_in_run):
        ctrl, psu, load, dmm = _r16_b16af1_normal_case(kind, cells)
        run_state = ctrl.state
        dmm.voltage_V = 1.8 * cells
        ctrl.advance(1.0)
        assert ctrl.state.value != "FAULT"
        assert not load.input_commanded_on
        if stuck_in_run:
            # A terminate-ág elmaradásának modellje, új setup nélkül.
            ctrl._state = run_state
        load.input_commanded_on = True
        calls, events = [], []
        load_off, psu_off = load.input_off, psu.all_outputs_off
        def off_load():
            calls.append("load")
            load_off()
        def off_psu():
            calls.append("psu")
            psu_off()
        load.input_off, psu.all_outputs_off = off_load, off_psu
        ctrl.on_event = events.append
        dmm.voltage_V = 1.8 * cells - 0.001
        ctrl.advance(1.0)
        assert ctrl.state.value == "FAULT"
        assert ctrl.fault_reason == "BATTERY_UNDERVOLTAGE"
        assert calls[:2] == ["load", "psu"]
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert any(e["event_code"] == "BATTERY_UNDERVOLTAGE" for e in events)
        on_count = sum("input_on" in c for c in load.calls)
        ctrl.advance(1.0)
        assert sum("input_on" in c for c in load.calls) == on_count

    def test_start_below_floor_never_energizes(self, kind, cells):
        ctrl, psu, load, _dmm = _r16_b16af1_normal_case(kind, cells, 1.8 * cells - 0.001)
        assert ctrl.state.value == "FAULT"
        assert ctrl.fault_reason == "BATTERY_UNDERVOLTAGE"
        assert not load.called("input_on")
        assert load.called("input_off") and psu.called("all_outputs_off")

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
    def test_nonfinite_loaded_sample_still_faults(self, kind, cells, bad):
        ctrl, psu, load, dmm = _r16_b16af1_normal_case(kind, cells)
        dmm.voltage_V = bad
        ctrl.advance(1.0)
        assert ctrl.state.value == "FAULT"
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert not load.input_commanded_on and not psu.output_commanded_on

    def test_overshoot_is_logged_without_event_callback(self, kind, cells, caplog):
        ctrl, _psu, load, dmm = _r16_b16af1_normal_case(kind, cells)
        dmm.voltage_V = 1.8 * cells - 0.001
        ctrl.advance(1.0)
        assert ctrl.state.value != "FAULT" and not load.input_commanded_on
        assert "GOV-029/Q-35" in caplog.text
        assert "minimum=" in caplog.text and "overshoot=" in caplog.text

    def test_throwing_overshoot_callback_cannot_leave_load_on(self, kind, cells):
        ctrl, psu, load, dmm = _r16_b16af1_normal_case(kind, cells)
        def fail_event(_event):
            raise RuntimeError("naplóhiba")
        ctrl.on_event = fail_event
        dmm.voltage_V = 1.8 * cells - 0.001
        with pytest.raises(RuntimeError, match="naplóhiba"):
            ctrl.advance(1.0)
        assert ctrl.state.value == "FAULT"
        assert not load.input_commanded_on and not psu.output_commanded_on

    def test_throwing_logging_handler_cannot_leave_load_on(self, kind, cells, monkeypatch):
        import logging

        ctrl, psu, load, dmm = _r16_b16af1_normal_case(kind, cells)
        def fail_log(*args, **kwargs):
            raise RuntimeError("naplózóhiba")
        monkeypatch.setattr(logging.getLogger("Prog.src.safety"), "warning", fail_log)
        dmm.voltage_V = 1.8 * cells - 0.001
        with pytest.raises(RuntimeError, match="naplózóhiba"):
            ctrl.advance(1.0)
        assert ctrl.state.value == "FAULT"
        assert not load.input_commanded_on and not psu.output_commanded_on

    def test_unloaded_runtime_sample_cannot_use_cutoff_exception(self, kind, cells):
        ctrl, psu, load, dmm = _r16_b16af1_normal_case(kind, cells)
        load.input_commanded_on = False
        dmm.voltage_V = 1.8 * cells - 0.001
        ctrl.advance(1.0)
        assert ctrl.state.value == "FAULT"
        assert ctrl.fault_reason == "BATTERY_UNDERVOLTAGE"
        assert load.called("input_off") and psu.called("all_outputs_off")


class TestR16B17FaultLogging:
    @pytest.mark.parametrize("callback", [False, True])
    def test_fault_is_logged_after_stop_with_and_without_callback(self, caplog, callback):
        import logging

        sm = SafetyManager(profile=make_12v_profile(), psu_mode=PsuMode.INDEPENDENT)
        trace, events = [], []

        class Handler(logging.Handler):
            def emit(self, record):
                trace.append("log")
                assert trace[0] == "stop"

        def stop(reason):
            assert reason == "BATTERY_UNDERVOLTAGE"
            trace.append("stop")

        def on_event(event):
            trace.append("event")
            events.append(event)

        logger = logging.getLogger("Prog.src.safety")
        handler = Handler()
        logger.addHandler(handler)
        try:
            assert not sm.enforce_normal_discharge(
                10.79, 0.7, 10.8, stop, on_event if callback else None,
            )
        finally:
            logger.removeHandler(handler)
        assert trace == (["stop", "log", "event"] if callback else ["stop", "log"])
        record, = [r for r in caplog.records if r.name == "Prog.src.safety"]
        assert record.levelno >= logging.ERROR
        assert "GOV-029" in record.getMessage()
        assert "U_batt=10.790000 V" in record.getMessage()
        assert "10.800000 V" in record.getMessage()
        if callback:
            assert events[0]["event_message"] == record.getMessage()

    def test_throwing_fault_logger_runs_after_stop(self, monkeypatch):
        import logging

        sm = SafetyManager(profile=make_12v_profile(), psu_mode=PsuMode.INDEPENDENT)
        trace = []

        def fail_log(_message):
            trace.append("log")
            raise RuntimeError("B17 naplóhiba")

        monkeypatch.setattr(logging.getLogger("Prog.src.safety"), "error", fail_log)
        with pytest.raises(RuntimeError, match="B17 naplóhiba"):
            sm.enforce_normal_discharge(10.79, 0.7, 10.8, lambda _reason: trace.append("stop"))
        assert trace == ["stop", "log"]


class TestR16B26bDischargeConfigCode:
    @pytest.mark.parametrize("kind,value", [
        ("current", float("nan")), ("current", float("inf")),
        ("current", -float("inf")), ("current", 0.0), ("current", -0.1),
        ("origin", float("nan")), ("origin", 6.0),
        ("cutoff", 9.0), ("cutoff", float("nan")),
        ("cutoff", float("inf")), ("cutoff", -float("inf")),
    ])
    @pytest.mark.parametrize("first_crossing", [False, True])
    def test_config_fault_stops_then_emits(self, monkeypatch, kind, value, first_crossing):
        from Prog.src.battery_profile import DischargeRateOrigin
        from Prog.tests.test_discharge_controller import make_discharge_controller

        ctrl, psu, load, _dmm = make_discharge_controller()
        current, cutoff = 0.7, 10.8
        origin = DischargeRateOrigin()
        if kind == "current":
            current = value
        elif kind == "cutoff":
            cutoff = value
        else:
            origin = DischargeRateOrigin("C/20", "NOMINAL_C10_CAPACITY", value)
        trace, events = [], []
        load_off, psu_off = load.input_off, psu.all_outputs_off

        def stop_load():
            trace.append("LOAD")
            load_off()

        def stop_psu():
            trace.append("PSU")
            psu_off()

        def event(data):
            trace.append("EVENT")
            assert ctrl.state.value == "FAULT"
            events.append(data)

        monkeypatch.setattr(load, "input_off", stop_load)
        monkeypatch.setattr(psu, "all_outputs_off", stop_psu)
        result = ctrl._safety.check_normal_discharge(
            12.8, current, cutoff, origin, first_cutoff_crossing=first_crossing,
        )
        assert result.fault is FaultCode.DISCHARGE_CONFIG_INVALID
        assert result.message.startswith("GOV-029:")
        assert not ctrl._safety.enforce_normal_discharge(
            12.8, current, cutoff, ctrl.emergency_stop, event, origin,
            first_cutoff_crossing=first_crossing,
        )
        assert trace == ["LOAD", "PSU", "EVENT"]
        assert ctrl.fault_reason == "DISCHARGE_CONFIG_INVALID"
        assert events[0]["event_code"] == "DISCHARGE_CONFIG_INVALID"
        assert events[0]["severity"] == "CRITICAL"
        assert events[0]["event_message"] == result.message
        ctrl.advance(0.1)
        assert not load.called("input_on") and not psu.called("output_on")

    def test_real_undervoltage_keeps_original_code(self):
        safety = SafetyManager(make_12v_profile(), PsuMode.INDEPENDENT)
        assert safety.check_normal_discharge(10.79, 0.7, 10.8).fault is FaultCode.BATTERY_UNDERVOLTAGE


@pytest.mark.parametrize("cells", [6, 12])
@pytest.mark.parametrize("vpc,allowed", [(1.6999, False), (1.70, True), (1.73, True), (1.75, True)])
def test_b23b_q42_precheck_is_explicit_tlc_only(cells, vpc, allowed):
    profile = make_12v_profile(cell_count=cells, nominal_voltage_V=2.0 * cells)
    safety = SafetyManager(profile=profile, psu_mode=PsuMode.SERIES if cells == 12 else PsuMode.INDEPENDENT)
    voltage = vpc * cells
    normal = safety.check_precheck_voltage(voltage)
    tlc = safety.check_precheck_voltage(voltage, _traditional_learning=True)
    assert (tlc.fault is None) is allowed
    assert (normal.fault is None) is (vpc >= 1.75)
    assert safety.check_precheck_voltage(voltage) == normal
    if not allowed:
        assert tlc.fault == FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED
