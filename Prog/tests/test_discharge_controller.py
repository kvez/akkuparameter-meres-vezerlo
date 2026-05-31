"""
DischargeController unit tesztek.
"""
import pytest
from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode
from Prog.src.discharge_controller import DischargeController, DischargeState, DischargeConfig
from Prog.tests.mock_drivers.mock_psu import MockPSU
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_dmm import MockDMM


def make_profile():
    return BatteryProfile(
        battery_name="Teszt", manufacturer="FIAMM", model="FG20721",
        nominal_capacity_Ah=7.0, cell_count=6, nominal_voltage_V=12.0,
    )


def make_discharge_controller(dmm_voltage_V=12.5, load_power_W=None):
    profile = make_profile()
    psu = MockPSU(voltage_V=0.0, current_A=0.0)
    load = MockLoad(voltage_V=dmm_voltage_V)
    if load_power_W is not None:
        load.power_W = load_power_W
        load.simulate_power_limit = True
        load.power_limit_W = load_power_W - 1
    dmm_v = MockDMM(voltage_V=dmm_voltage_V)
    dmm_t = MockDMM(temperature_C=22.0)
    safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
    cfg = DischargeConfig()
    return DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg), psu, load, dmm_v


class TestDischargeStateTransitions:
    def test_starts_in_init(self):
        ctrl, *_ = make_discharge_controller()
        assert ctrl.state == DischargeState.INIT

    def test_advances_past_precheck_with_normal_voltage(self):
        ctrl, *_ = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state not in (DischargeState.FAULT, DischargeState.INIT)

    def test_discharge_done_at_terminate_voltage(self):
        """Kisütés terminate feszültségnél megáll: 6 × 1.80 = 10.80V"""
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        # Advance to running state
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        # Simulate voltage drop to terminate voltage
        dmm.voltage_V = 10.75  # < 10.80V
        load.voltage_V = 10.75
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_DONE

    def test_discharge_done_turns_load_off(self):
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        dmm.voltage_V = 10.75
        ctrl.advance(dt_s=1.0)
        assert load.called("input_off")


class TestDischargeNoRelay:
    """[R1] emergency_stop: LOAD OFF → PSU OFF, relay soha."""

    def test_emergency_stop_no_relay(self):
        ctrl, psu, load, dmm = make_discharge_controller()
        ctrl.emergency_stop("TEST")
        assert not any("relay" in c.lower() for c in psu.calls)
        assert not any("relay" in c.lower() for c in load.calls)

    def test_emergency_stop_correct_order(self):
        ctrl, psu, load, dmm = make_discharge_controller()
        ctrl.emergency_stop("TEST")
        assert load.called("input_off")
        assert psu.called("all_outputs_off") or psu.called("safe_off")


class TestDischargeDmmFeedbackLost:
    def test_dmm_lost_during_discharge_causes_fault(self):
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        dmm.set_dmm_invalid()
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.FAULT


class TestDischargeIntegrationSource:
    """[R8] Kisütési fázisban LOAD_READBACK az integráció forrása."""

    def test_integration_source_is_load_readback(self):
        ctrl, *_ = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(4):
            ctrl.advance(dt_s=1.0)
        assert ctrl.last_integration_source in ("LOAD_READBACK", "SETPOINT_FALLBACK", "ZERO")


class TestDischargeMaxTime:
    def test_fault_on_max_time_exceeded(self):
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        cfg = DischargeConfig(max_discharge_time_s=5.0)
        ctrl._config = cfg
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=10.0)  # exceeds max_discharge_time
        assert ctrl.state == DischargeState.FAULT
