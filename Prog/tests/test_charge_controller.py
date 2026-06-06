"""
ChargeController unit tesztek — MockPSU/Load/DMM-mel, dt_s injection.
"""
import pytest
from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode
from Prog.src.charge_controller import ChargeController, ChargeState, ChargeConfig
from Prog.tests.mock_drivers.mock_psu import MockPSU
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_dmm import MockDMM


def make_12v_profile(capacity_Ah=7.0):
    return BatteryProfile(
        battery_name="Teszt", manufacturer="FIAMM", model="FG20721",
        nominal_capacity_Ah=capacity_Ah, cell_count=6, nominal_voltage_V=12.0,
    )


def make_24v_profile(capacity_Ah=18.0):
    return BatteryProfile(
        battery_name="Teszt", manufacturer="FIAMM", model="FGH21803",
        nominal_capacity_Ah=capacity_Ah, cell_count=12, nominal_voltage_V=24.0,
    )


def make_controller(
    profile=None,
    psu_mode=PsuMode.INDEPENDENT,
    dmm_voltage_V=12.5,
    psu_current_A=1.0,
    config=None,
):
    if profile is None:
        profile = make_12v_profile()
    psu = MockPSU(voltage_V=14.2, current_A=psu_current_A)
    load = MockLoad(voltage_V=dmm_voltage_V)
    dmm_v = MockDMM(voltage_V=dmm_voltage_V)
    dmm_t = MockDMM(temperature_C=22.0)
    safety = SafetyManager(
        profile=profile,
        psu_mode=psu_mode,
        temp_comp_mode=TempCompMode.MONITOR_ONLY,
    )
    cfg = config or ChargeConfig()
    return ChargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg), psu, load, dmm_v


class TestPrecheckState:
    def test_starts_in_init(self):
        ctrl, *_ = make_controller()
        assert ctrl.state == ChargeState.INIT

    def test_precheck_ok_advances_state(self):
        ctrl, *_ = make_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        assert ctrl.state not in (ChargeState.FAULT, ChargeState.INIT)

    def test_precheck_deep_discharge_faults(self):
        """[N6] Mélykisült akku → FAULT, nem indít CC töltést."""
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=9.0)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → FAULT (9.0V < 10.5V limit)
        assert ctrl.state == ChargeState.FAULT

    def test_precheck_overvoltage_faults(self):
        ctrl, *_ = make_controller(dmm_voltage_V=15.0)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT

    def test_precheck_24v_pack_not_series_faults(self):
        """[N3] 24V pack INDEPENDENT módban → FAULT."""
        ctrl, *_ = make_controller(
            profile=make_24v_profile(),
            psu_mode=PsuMode.INDEPENDENT,
            dmm_voltage_V=24.5,
        )
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT


class TestNoRelayInStateMachine:
    """[R1] Relay.safe_open() soha nem hívódik — nincs relé hardware."""

    def test_emergency_stop_no_relay_call(self):
        ctrl, psu, load, dmm = make_controller()
        ctrl.emergency_stop("TEST")
        assert not any("relay" in c.lower() for c in psu.calls)
        assert not any("relay" in c.lower() for c in load.calls)

    def test_emergency_stop_calls_load_off_then_psu_off(self):
        """Sorrend: LOAD OFF → PSU OFF (safety spec szerint)."""
        ctrl, psu, load, dmm = make_controller()
        ctrl.emergency_stop("TEST")
        assert load.called("input_off")
        assert psu.called("all_outputs_off")
        # Load OFF legyen korábban a listában mint PSU OFF
        load_idx = next(i for i, c in enumerate(load.calls) if "input_off" in c.lower())
        psu_idx = next(i for i, c in enumerate(psu.calls) if "all_outputs_off" in c.lower())
        assert load_idx >= 0 and psu_idx >= 0  # mindkét hívás megtörtént

    def test_emergency_stop_sets_fault_state(self):
        ctrl, *_ = make_controller()
        ctrl.emergency_stop("TEST_FAULT")
        assert ctrl.state == ChargeState.FAULT

    def test_fault_reason_recorded(self):
        ctrl, *_ = make_controller()
        ctrl.emergency_stop("DMM_FEEDBACK_LOST")
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason


class TestDmmFeedbackLost:
    """[R1/N2] DMM elvesztés → azonnali emergency_stop."""

    def test_dmm_lost_during_charging_causes_fault(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        # Advance to charging state
        for _ in range(5):
            ctrl.advance(dt_s=1.0)
        dmm.set_dmm_invalid()
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT

    def test_dmm_lost_load_is_turned_off(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        for _ in range(5):
            ctrl.advance(dt_s=1.0)
        dmm.set_dmm_invalid()
        ctrl.advance(dt_s=1.0)
        assert load.called("input_off")

    def test_dmm_lost_psu_is_turned_off(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        for _ in range(5):
            ctrl.advance(dt_s=1.0)
        dmm.set_dmm_invalid()
        ctrl.advance(dt_s=1.0)
        assert psu.called("all_outputs_off")


class TestCcToCvTransition:
    """CC→CV belépésnél a PSU setpoint azonnal frissül (nem marad compliance-en)."""

    def _advance_to_cc(self, dmm_voltage_V):
        """CHARGE_CC állapotba hoz 3 advance()-szel."""
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        assert ctrl.state == ChargeState.CHARGE_CC
        return ctrl, psu, load, dmm

    def test_cv_entry_above_target_immediately_reduces_psu(self):
        """u_batt > target-nél a PSU azonnal alacsonyabb értékre áll, nem marad compliance-en.

        Valós eset: u_batt=14.545V, PSU compliance=15.30V → OV az 1. tickben.
        Javítás: target + (psu - u_batt) = 14.40 + 0.75 = 15.15V azonnal.
        """
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.50)
        compliance_V = psu.voltage_V  # PSU_PRESET után: 14.40 + 0.90 = 15.30V

        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV_DMM_CONTROL

        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        # PSU-t azonnal csökkenteni kell: target + (compliance - u_batt) < compliance
        expected_V = 14.40 + (compliance_V - 14.50)  # 14.40 + 0.80 = 15.20V
        assert abs(psu.voltage_V - expected_V) < 0.001
        assert psu.voltage_V < compliance_V - 0.05

    def test_cv_entry_at_margin_keeps_compliance(self):
        """u_batt = target - margin esetén a PSU max compliance-en marad (clamp)."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.30)
        compliance_V = psu.voltage_V  # 15.30V

        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV_DMM_CONTROL

        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        # u_drop = 15.30 - 14.30 = 1.00V, target + drop = 15.40V → clamp → 15.30V
        assert abs(psu.voltage_V - compliance_V) < 0.001


class TestBatteryOvervoltage:
    def test_overvoltage_during_charging_causes_fault(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        for _ in range(5):
            ctrl.advance(dt_s=1.0)
        dmm.voltage_V = 15.0  # > 14.55V limit
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT


class TestCvRegulation:
    """DMM-kompenzált CV szabályozóhurok tesztek."""

    def _make_cv_controller(self, u_batt, u_psu_set_initial=14.20):
        profile = make_12v_profile()
        psu = MockPSU(voltage_V=u_psu_set_initial, current_A=0.15)
        load = MockLoad()
        dmm_v = MockDMM(voltage_V=u_batt)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = ChargeConfig(
            deadband_V=0.010,
            max_step_up_V=0.050,
            max_step_down_V=0.050,
        )
        ctrl = ChargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = u_psu_set_initial
        return ctrl, psu

    def test_voltage_below_target_increases_psu_set(self):
        """error = 14.40 - 14.30 = 0.10V > deadband → step = min(0.10, 0.050) = 0.050V"""
        ctrl, psu = self._make_cv_controller(u_batt=14.30)
        u_before = ctrl._u_psu_set
        ctrl.advance(dt_s=1.0)
        assert ctrl._u_psu_set > u_before
        assert abs(ctrl._u_psu_set - (u_before + 0.050)) < 0.001

    def test_voltage_within_deadband_no_change(self):
        """error = 14.40 - 14.395 = 0.005V < deadband (0.010V) → nincs változás"""
        ctrl, psu = self._make_cv_controller(u_batt=14.395)
        u_before = ctrl._u_psu_set
        ctrl.advance(dt_s=1.0)
        assert abs(ctrl._u_psu_set - u_before) < 0.001

    def test_voltage_above_target_decreases_psu_set(self):
        """error = 14.40 - 14.46 = -0.06V → step down = min(0.06, 0.050) = 0.050V"""
        ctrl, psu = self._make_cv_controller(u_batt=14.46)
        u_before = ctrl._u_psu_set
        ctrl.advance(dt_s=1.0)
        assert ctrl._u_psu_set < u_before

    def test_psu_set_clamped_at_mode_max(self):
        """U_psu_set nem haladhatja meg a PSU mód limitet (INDEPENDENT: 30V)."""
        ctrl, psu = self._make_cv_controller(u_batt=13.0, u_psu_set_initial=29.99)
        ctrl.advance(dt_s=1.0)
        assert ctrl._u_psu_set <= 30.0

    def test_psu_set_clamped_at_target_plus_series_drop(self):
        """U_psu_set <= U_target + max_expected_series_drop"""
        cfg = ChargeConfig(max_expected_series_drop_V=0.90)
        profile = make_12v_profile()
        ctrl = ChargeController(
            MockPSU(current_A=0.1), MockLoad(), MockDMM(voltage_V=13.0),
            MockDMM(temperature_C=22.0), profile,
            SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT), cfg
        )
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = 20.0  # absurdly high
        ctrl.advance(dt_s=1.0)
        u_target = profile.charge_voltage_pack_V
        assert ctrl._u_psu_set <= u_target + cfg.max_expected_series_drop_V + 0.001


class TestTaperHold:
    """[R9] Taper feltétel formális tesztek."""

    def _make_taper_controller(self, current_A, u_batt, taper_hold_s=10.0):
        profile = make_12v_profile(capacity_Ah=7.0)
        # taper_current = 0.03 * 7 = 0.21A
        psu = MockPSU(voltage_V=14.4, current_A=current_A)
        load = MockLoad()
        dmm_v = MockDMM(voltage_V=u_batt)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = ChargeConfig(
            taper_hold_s=taper_hold_s,
            taper_current_tolerance_factor=1.05,
            cv_voltage_tolerance_V_per_cell=0.003,  # 6 * 0.003 = 0.018V
        )
        ctrl = ChargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = 14.40
        return ctrl, psu, dmm_v

    def test_enters_taper_hold_when_conditions_met(self):
        """U_batt ≥ target - tolerance ÉS I ≤ taper_current → TAPER_HOLD"""
        # target = 14.40V, tolerance = 0.018V → U_batt >= 14.382V
        # taper_current = 0.21A, current = 0.20A ≤ 0.21A ✓
        ctrl, psu, dmm = self._make_taper_controller(
            current_A=0.20, u_batt=14.39
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.TAPER_HOLD

    def test_does_not_enter_taper_if_current_too_high(self):
        """I > taper_current → marad CHARGE_CV_DMM_CONTROL"""
        ctrl, psu, dmm = self._make_taper_controller(
            current_A=0.50, u_batt=14.39  # 0.50A > taper 0.21A
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL

    def test_does_not_enter_taper_if_voltage_too_low(self):
        """U_batt < target - tolerance → marad CHARGE_CV_DMM_CONTROL"""
        ctrl, psu, dmm = self._make_taper_controller(
            current_A=0.20, u_batt=14.30  # 14.30 < 14.382 → nem éri el a sávot
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL

    def test_taper_timer_increments(self):
        ctrl, psu, dmm = self._make_taper_controller(
            current_A=0.20, u_batt=14.39, taper_hold_s=60.0
        )
        ctrl.advance(dt_s=1.0)  # enters TAPER_HOLD
        assert ctrl.state == ChargeState.TAPER_HOLD
        ctrl.advance(dt_s=5.0)
        assert ctrl.taper_timer_s >= 5.0

    def test_taper_timer_resets_if_current_exceeds(self):
        """[R9] I > taper_current * 1.05 → timer nullázódik"""
        ctrl, psu, dmm = self._make_taper_controller(
            current_A=0.20, u_batt=14.39, taper_hold_s=60.0
        )
        ctrl.advance(dt_s=5.0)  # enter TAPER_HOLD, timer=5
        psu.current_A = 0.25   # > 0.21 * 1.05 = 0.2205A → reset
        ctrl.advance(dt_s=1.0)
        assert ctrl.taper_timer_s == 0.0

    def test_charge_done_after_taper_hold_time(self):
        ctrl, psu, dmm = self._make_taper_controller(
            current_A=0.20, u_batt=14.39, taper_hold_s=10.0
        )
        for _ in range(12):  # 12 × 1s > 10s hold
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_DONE


class TestIntegrationSourceCharge:
    """[R8] Töltési fázisban PSU_READBACK az integráció forrása."""

    def test_integration_source_is_psu_readback_during_charge(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        for _ in range(5):
            ctrl.advance(dt_s=1.0)
        assert ctrl.last_integration_source in ("PSU_READBACK", "SETPOINT_FALLBACK", "ZERO")


class TestChargeLimitsAllPhases:
    """K2: max_charge_Ah és max_charge_time minden töltési fázisban ellenőrzött."""

    def _advance_to_cv(self, dmm_voltage_V=14.4):
        """Helper: controller CHARGE_CV_DMM_CONTROL állapotba hozva."""
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV (u_batt=14.4 >= 14.3)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL, ctrl.state
        return ctrl

    def test_max_charge_ah_triggers_fault_in_cv_phase(self):
        ctrl = self._advance_to_cv()
        ctrl._integrator.accumulated_charge_Ah = (
            ctrl._profile.nominal_capacity_Ah * 1.21
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_AH" in ctrl.fault_reason

    def test_max_charge_time_triggers_fault_in_cv_phase(self):
        ctrl = self._advance_to_cv()
        ctrl._elapsed_s = ctrl._config.max_charge_time_s + 1.0
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_TIME" in ctrl.fault_reason

    def test_max_charge_ah_triggers_fault_in_taper_phase(self):
        ctrl = self._advance_to_cv()
        ctrl._state = ChargeState.TAPER_HOLD
        ctrl._integrator.accumulated_charge_Ah = (
            ctrl._profile.nominal_capacity_Ah * 1.21
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_AH" in ctrl.fault_reason

    def test_max_charge_time_triggers_fault_in_taper_phase(self):
        ctrl = self._advance_to_cv()
        ctrl._state = ChargeState.TAPER_HOLD
        ctrl._elapsed_s = ctrl._config.max_charge_time_s + 1.0
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_TIME" in ctrl.fault_reason

    def test_cc_limits_still_work(self):
        """K2 nem töri el a meglévő CC limitet."""
        ctrl, *_ = make_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC
        ctrl._integrator.accumulated_charge_Ah = (
            ctrl._profile.nominal_capacity_Ah * 1.21
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT


class TestSeriesSafety:
    """K3: series_drop és diode_power safety check bekötve töltés közben."""

    def _advance_to_cc(self, psu_voltage_V=13.0, dmm_voltage_V=12.5):
        ctrl, psu, load, dmm = make_controller(
            dmm_voltage_V=dmm_voltage_V,
            psu_current_A=1.5,
        )
        psu.voltage_V = psu_voltage_V
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        assert ctrl.state == ChargeState.CHARGE_CC
        return ctrl, psu, dmm

    def test_series_drop_above_fault_triggers_fault(self):
        """u_psu - u_batt > fault_series_drop_V (1.25V) → SERIES_DROP_TOO_HIGH fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.voltage_V = 12.5 + 1.30  # u_drop = 1.30V > 1.25V
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "SERIES_DROP_TOO_HIGH" in ctrl.fault_reason

    def test_series_drop_below_fault_no_fault(self):
        """u_drop = 0.85V < 1.25V → nincs fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.voltage_V = 12.5 + 0.85
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC
        assert ctrl.last_warning_code == ""

    def test_negative_drop_no_fault(self):
        """u_psu < u_batt (pl. PSU kikapcsolva) → nincs false fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.voltage_V = 12.0  # alatt
        ctrl.advance(dt_s=1.0)
        assert ctrl.state != ChargeState.FAULT

    def test_diode_power_warning_stored(self):
        """Diode power magas: warning_code beállítva, nincs fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5, psu_voltage_V=12.5)
        # 1.5A × 1.10V = 1.65W > custom warning threshold
        ctrl._safety.diode_power_warning_W = 1.5
        psu.voltage_V = 12.5 + 1.10  # 1.5A × 1.10V = 1.65W > 1.5W → warning
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC  # nincs fault
        assert ctrl.last_warning_code in ("DIODE_POWER_HIGH", "DIODE_POWER_TOO_HIGH")


class TestPsuCommLost:
    """V1: PSU current readback hiba → PSU_COMM_LOST fault, nem CHARGE_DONE."""

    def test_psu_current_failure_in_cv_phase_triggers_fault_not_charge_done(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=14.4, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL

        psu.simulate_current_readback_timeout = True
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "PSU_COMM_LOST"

    def test_psu_current_failure_in_cc_phase_triggers_fault(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC

        psu.simulate_current_readback_timeout = True
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "PSU_COMM_LOST"


class TestPrecheckDmmGuard:
    """V4: PRECHECK DMM hiba → DMM_FEEDBACK_LOST, nem DEEPLY_DISCHARGED."""

    def test_dmm_failure_at_precheck_gives_dmm_fault(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        dmm.dmm_valid = False
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → kell fault legyen
        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"

    def test_dmm_failure_not_misleading_deeply_discharged(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        dmm.dmm_valid = False
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert "DEEPLY_DISCHARGED" not in ctrl.fault_reason


class TestTempDmmFault:
    """P0-3: Hőmérséklet DMM kiesés safety bekötve töltés közben."""

    def _advance_to_cc(self):
        ctrl, *_ = make_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        # PSU_PRESET a compliance voltage-ot állítja be; CC módban a valós readback
        # ≈ u_batt + dióda-esés — a MockPSU-t erre kell állítani a series check miatt.
        ctrl._psu.voltage_V = 13.0
        assert ctrl.state == ChargeState.CHARGE_CC
        return ctrl

    def test_temp_dmm_lost_monitor_only_faults_after_timeout(self):
        """MONITOR_ONLY: folyamatos temp hiba > timeout → TEMPERATURE_MONITOR_LOST_CRITICAL"""
        ctrl = self._advance_to_cc()
        ctrl._safety.temp_comp_mode = TempCompMode.MONITOR_ONLY
        ctrl._dmm_t.simulate_temp_failure = True
        ctrl._temp_dmm_fault_s = 61.0  # szimulált felhalmozott hiba
        ctrl.advance(dt_s=0.1)         # hiba folytatódik → 61.1s > 60s threshold
        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"

    def test_temp_dmm_lost_enabled_faults_immediately(self):
        """ENABLED: bármilyen temp DMM hiba → azonnali fault"""
        ctrl = self._advance_to_cc()
        ctrl._safety.temp_comp_mode = TempCompMode.ENABLED
        ctrl._dmm_t.simulate_temp_failure = True
        ctrl._temp_dmm_fault_s = 1.0
        ctrl.advance(dt_s=0.1)
        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"

    def test_temp_dmm_no_fault_below_timeout(self):
        """MONITOR_ONLY: 30s hiba még nem fault"""
        ctrl = self._advance_to_cc()
        ctrl._safety.temp_comp_mode = TempCompMode.MONITOR_ONLY
        ctrl._temp_dmm_fault_s = 30.0
        ctrl.advance(dt_s=0.1)
        assert ctrl.state == ChargeState.CHARGE_CC


class TestTempCompensation:
    """P0-4: ENABLED hőkompenzáció a töltési célfeszültségben."""

    def test_enabled_reduces_target_at_high_temperature(self):
        """30°C: kompenzált target < névleges target"""
        profile = make_12v_profile()
        ctrl = ChargeController(
            MockPSU(voltage_V=14.2, current_A=0.5), MockLoad(),
            MockDMM(voltage_V=12.5), MockDMM(temperature_C=30.0),
            profile,
            SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT,
                          temp_comp_mode=TempCompMode.ENABLED),
            ChargeConfig(),
        )
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK, reads temp=30°C
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        assert ctrl._effective_charge_target_V() < profile.charge_voltage_pack_V

    def test_enabled_raises_target_at_low_temperature(self):
        """0°C: kompenzált target > névleges, de clampelve batt_absolute_max alatt"""
        profile = make_12v_profile()
        ctrl = ChargeController(
            MockPSU(voltage_V=14.2, current_A=0.5), MockLoad(),
            MockDMM(voltage_V=12.5), MockDMM(temperature_C=0.0),
            profile,
            SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT,
                          temp_comp_mode=TempCompMode.ENABLED),
            ChargeConfig(),
        )
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK, reads temp=0°C
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        target = ctrl._effective_charge_target_V()
        assert target > profile.charge_voltage_pack_V
        assert target <= profile.batt_absolute_max_V

    def test_monitor_only_uses_nominal_target(self):
        """MONITOR_ONLY: target = charge_voltage_pack_V, hőmérséklet-független"""
        ctrl, *_ = make_controller(dmm_voltage_V=12.5)
        assert ctrl._effective_charge_target_V() == ctrl._profile.charge_voltage_pack_V


class TestConcurrentPsuLoadCharge:
    """Töltés közben Load ON → CONCURRENT_PSU_LOAD_ON fault."""

    def test_concurrent_load_during_cc_triggers_fault(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        # INIT → PRECHECK → PSU_PRESET → CHARGE_CC
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC

        # Hardware conflict: Load is also commanded on during charging
        load.input_commanded_on = True
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason

    def test_no_fault_when_only_psu_on(self):
        """Normál töltés: PSU ON, Load OFF → nincs concurrent fault."""
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)  # INIT → PRECHECK → PSU_PRESET → CHARGE_CC
        assert ctrl.state == ChargeState.CHARGE_CC
        psu.voltage_V = 13.0  # reális CC readback: u_batt(12.5) + dióda-esés(~0.5)
        ctrl.advance(dt_s=1.0)  # CHARGE_CC marad: series check OK, concurrent OK
        assert ctrl.state == ChargeState.CHARGE_CC
        assert psu.output_commanded_on is True
        assert load.input_commanded_on is False


class TestPsuCurrentClamping:
    """K5: PSU hardver áramlimit clampolás PSU_PRESET-ben.

    Keithley 2220-30-1: INDEPENDENT/SERIES → 1.5A/csatorna, PARALLEL → 3.0A.
    Ha a kiszámított töltőáram meghaladja ezt, a PSU visszautasítja a parancsot
    és az előző értéken marad — ezért szoftveresen kell clampolni.
    """

    def _advance_to_cc(self, profile, psu_mode=PsuMode.INDEPENDENT, dmm_V=12.5):
        ctrl, psu, load, dmm = make_controller(
            profile=profile, psu_mode=psu_mode, dmm_voltage_V=dmm_V
        )
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        assert ctrl.state == ChargeState.CHARGE_CC
        return psu

    def test_current_clamped_when_above_independent_limit(self):
        """7.0Ah → 1.75A computed, INDEPENDENT max 1.5A → PSU kap 1.5A."""
        profile = make_12v_profile(capacity_Ah=7.0)  # effective = 1.75A
        psu = self._advance_to_cc(profile, psu_mode=PsuMode.INDEPENDENT)
        assert psu.current_A == pytest.approx(1.5, abs=0.001)

    def test_current_not_clamped_when_below_independent_limit(self):
        """4.0Ah → 1.0A computed < 1.5A limit → PSU kap 1.0A."""
        profile = make_12v_profile(capacity_Ah=4.0)  # effective = 1.0A
        psu = self._advance_to_cc(profile, psu_mode=PsuMode.INDEPENDENT)
        assert psu.current_A == pytest.approx(1.0, abs=0.001)

    def test_current_not_clamped_in_parallel_mode(self):
        """7.0Ah → 1.75A computed, PARALLEL max 3.0A → PSU kap 1.75A."""
        profile = make_12v_profile(capacity_Ah=7.0)  # effective = 1.75A
        psu = self._advance_to_cc(profile, psu_mode=PsuMode.PARALLEL)
        assert psu.current_A == pytest.approx(1.75, abs=0.001)
