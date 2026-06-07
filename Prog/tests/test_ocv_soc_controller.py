"""
Tesztek: OcvSocController állapotgép.
Minimális lefedettség: INIT→PRECHARGE, lépéses kisütés, relax keypoint/default,
Rb impulzus számítás.
"""
from __future__ import annotations

import pytest

from Prog.src.battery_profile import BatteryProfile
from Prog.src.ocv_soc_controller import OcvSocConfig, OcvSocController, OcvSocState
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode
from Prog.tests.mock_drivers.mock_dmm import MockDMM
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_psu import MockPSU


# ------------------------------------------------------------------ #
# Gyártó segédfüggvény                                                 #
# ------------------------------------------------------------------ #

def _make_profile(capacity_Ah: float = 7.0) -> BatteryProfile:
    return BatteryProfile(
        battery_name="TEST",
        manufacturer="TEST",
        model="TST7AH",
        nominal_voltage_V=12.0,
        cell_count=6,
        nominal_capacity_Ah=capacity_Ah,
    )


def make_ocv_soc_controller(
    dmm_voltage_V: float = 13.0,
    capacity_Ah: float = 7.0,
    config: OcvSocConfig | None = None,
    measured_capacity_Ah: float | None = None,
) -> tuple[OcvSocController, MockPSU, MockLoad, MockDMM, MockDMM]:
    psu = MockPSU()
    load = MockLoad(voltage_V=dmm_voltage_V)
    dmm_v = MockDMM(voltage_V=dmm_voltage_V)
    dmm_t = MockDMM(temperature_C=22.0)
    profile = _make_profile(capacity_Ah)
    safety = SafetyManager(
        profile=profile,
        psu_mode=PsuMode.INDEPENDENT,
        temp_comp_mode=TempCompMode.MONITOR_ONLY,
    )
    cfg = config or OcvSocConfig(
        step_percent=5.0,
        discharge_rate_divisor=10,
        relax_default_s=7200.0,
        relax_keypoint_s=18000.0,
        impulse_current_rate_divisor=5,
        impulse_duration_s=30.0,
    )
    ctrl = OcvSocController(
        psu, load, dmm_v, dmm_t, profile, safety, cfg,
        measured_capacity_Ah=measured_capacity_Ah,
    )
    return ctrl, psu, load, dmm_v, dmm_t


# ------------------------------------------------------------------ #
# TestOcvSocInit                                                       #
# ------------------------------------------------------------------ #

class TestOcvSocInit:
    def test_starts_in_init(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.state == OcvSocState.INIT

    def test_advances_to_precharge(self):
        """Első advance() hívásra INIT → PRECHARGE átmenet."""
        ctrl, *_ = make_ocv_soc_controller()
        state = ctrl.advance(0.0)
        assert state == OcvSocState.PRECHARGE

    def test_precharge_relax_after_charge_done(self):
        """Ha a belső ChargeController CHARGE_DONE-ba lép, átmegyünk PRECHARGE_RELAX-ba."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(dmm_voltage_V=14.4)
        # Haladunk a PRECHARGE fázisba
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.PRECHARGE

        # A belső ChargeController-t közvetlenül manipuláljuk a gyors teszteléshez:
        # töltsük fel úgy, hogy a DMM feszültsége a charge_voltage_pack_V-n legyen
        # (6 × 2.40 = 14.40V) és az áram 0 (taper kész)
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        # Manipuláljuk a belső állapotot közvetlenül a gyors teszt érdekében
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        state = ctrl.advance(1.0)
        assert state == OcvSocState.PRECHARGE_RELAX

    def test_soc_index_starts_at_100(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.soc_index == 100.0

    def test_removed_ah_starts_at_zero(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.removed_Ah_total == 0.0

    def test_fault_reason_empty_initially(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.fault_reason == ""

    def test_measured_capacity_override(self):
        """Ha measured_capacity_Ah-t adunk meg, azt kell használni."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0, measured_capacity_Ah=6.5)
        assert ctrl._total_capacity_Ah == pytest.approx(6.5)

    def test_measured_capacity_fallback_to_nominal(self):
        """Ha nincs measured_capacity_Ah, a névleges kapacitást kell használni."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0, measured_capacity_Ah=None)
        assert ctrl._total_capacity_Ah == pytest.approx(7.0)


# ------------------------------------------------------------------ #
# TestOcvSocStepDischarge                                              #
# ------------------------------------------------------------------ #

class TestOcvSocStepDischarge:
    def _setup_in_step_discharge(self) -> tuple[OcvSocController, MockLoad]:
        """Direktbe STEP_DISCHARGE állapotba: state + begin_step_discharge()."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._begin_step_discharge()
        return ctrl, load

    def test_step_discharge_stops_at_step_ah(self):
        """Lépés-Ah limit elérésekor STEP_RELAX-ba kell lépni."""
        ctrl, load = self._setup_in_step_discharge()
        # step_Ah = 7.0 * 5% / 100 = 0.35 Ah
        # Kisütési ráta: C/10 = 7.0/10 = 0.7A
        # dt_s = 1800s → 0.7A * 1800s / 3600 = 0.35 Ah → pontosan step_Ah
        assert load.input_commanded_on  # terhelés be van kapcsolva
        ctrl.advance(1800.0)
        assert ctrl.state == OcvSocState.STEP_RELAX
        assert not load.input_commanded_on  # terhelés le van kapcsolva

    def test_step_discharge_stops_at_terminate_voltage(self):
        """Terminate voltage elérésekor STEP_RELAX-ba kell lépni (relax+impulzus után 0% log)."""
        ctrl, load = self._setup_in_step_discharge()
        ctrl._dmm_v.voltage_V = 10.5  # type: ignore[attr-defined]
        ctrl.advance(1.0)
        # Terminate voltage → STEP_RELAX (nem LOG_SOC_POINT; relax+impulzus után logolunk)
        assert ctrl.state == OcvSocState.STEP_RELAX
        assert not load.input_commanded_on


# ------------------------------------------------------------------ #
# TestOcvSocRelax                                                      #
# ------------------------------------------------------------------ #

class TestOcvSocRelax:
    def test_keypoint_uses_long_relax(self):
        """100% keypoint → 18000s relax."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller()
        # INIT → PRECHARGE
        ctrl.advance(0.0)
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        # PRECHARGE → PRECHARGE_RELAX (100% = keypoint)
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX
        assert ctrl._current_relax_s == pytest.approx(18000.0)

    def test_non_keypoint_uses_short_relax(self):
        """Nem keypoint SOC-on (95%) → 7200s relax.

        Az első STEP_DISCHARGE soc_index=95%-ra dolgozik (100% után LOG-ban dekrementált).
        soc_after_step = 95% - 5% = 90% → nem keypoint → default relax 7200s.
        """
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        # 100% LOG után soc_index = 95%, indítsuk az első STEP_DISCHARGE-t
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._soc_index = 95.0
        ctrl._begin_step_discharge()
        # step_Ah = 7.0 * 5% = 0.35 Ah → 0.7A * 1800s = 0.35 Ah
        ctrl.advance(1800.0)
        assert ctrl.state == OcvSocState.STEP_RELAX
        # 90% nem keypoint → default relax
        assert ctrl._current_relax_s == pytest.approx(7200.0)

    def test_precharge_relax_transitions_to_impulse_prep(self):
        """PRECHARGE_RELAX lejárta után IMPULSE_PREP kezdődik (100% OCV rögzítéséhez)."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl.advance(0.0)
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX
        ctrl.advance(18000.0)
        assert ctrl.state == OcvSocState.IMPULSE_PREP

    def test_step_relax_transitions_to_impulse_prep(self):
        """STEP_RELAX lejárta után IMPULSE_PREP következik."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.STEP_RELAX
        ctrl._current_relax_s = 7200.0
        ctrl._relax_elapsed_s = 0.0
        ctrl.advance(7200.0)
        assert ctrl.state == OcvSocState.IMPULSE_PREP


# ------------------------------------------------------------------ #
# TestOcvSocImpulse                                                    #
# ------------------------------------------------------------------ #

class TestOcvSocImpulse:
    def _advance_impulse_cycle(self, ctrl) -> None:
        """IMPULSE_ON → IMPULSE_WAIT_1S → 10S → 30S → LOG_SOC_POINT tick-sorozat."""
        ctrl.advance(0.0)   # IMPULSE_ON → IMPULSE_WAIT_1S
        ctrl.advance(1.0)   # IMPULSE_WAIT_1S → IMPULSE_WAIT_10S (elapsed >= 1.0)
        ctrl.advance(9.0)   # IMPULSE_WAIT_10S → IMPULSE_WAIT_30S (elapsed >= 10.0)
        ctrl.advance(20.0)  # IMPULSE_WAIT_30S → LOG_SOC_POINT (elapsed >= 30.0)

    def test_rb_calculation(self):
        """
        Rb számítás tick-alapú impulzussal:
        v_before=13.5V, v_1s=13.2V, i=1.4A → rb_1s = 0.3/1.4 ≈ 0.214 Ohm

        advance() hívássorban: _read_dmm() és _run_impulse_wait() párosával olvas DMM-et.
        """
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.IMPULSE_ON
        assert ctrl._impulse_current_A_set == pytest.approx(1.4)

        # Minden advance() hívásban _read_dmm() + _run_impulse_wait() is olvas DMM-et.
        # Sorozat (páronként: _read_dmm értéke, _run_impulse_wait értéke):
        # advance(0.0)/IMPULSE_ON: _read_dmm→13.5, _run_impulse_on→13.5 (v_before)
        # advance(1.0)/WAIT_1S:   _read_dmm→13.5, _run_impulse_wait→13.2 (v_1s)
        # advance(9.0)/WAIT_10S:  _read_dmm→13.5, _run_impulse_wait→13.0 (v_10s)
        # advance(20.0)/WAIT_30S: _read_dmm→13.5, _run_impulse_wait→12.9 (v_30s)
        voltage_sequence = [13.5, 13.5, 13.5, 13.2, 13.5, 13.0, 13.5, 12.9]
        call_idx = [0]

        def mock_read_voltage() -> float:
            v = voltage_sequence[min(call_idx[0], len(voltage_sequence) - 1)]
            call_idx[0] += 1
            return v

        ctrl._dmm_v.read_voltage = mock_read_voltage  # type: ignore[method-assign]

        self._advance_impulse_cycle(ctrl)

        i_set = ctrl._impulse_current_A_set
        assert ctrl._rb_1s == pytest.approx((13.5 - 13.2) / i_set, rel=1e-3)
        assert ctrl._rb_10s == pytest.approx((13.5 - 13.0) / i_set, rel=1e-3)
        assert ctrl._rb_30s == pytest.approx((13.5 - 12.9) / i_set, rel=1e-3)
        assert ctrl.state == OcvSocState.LOG_SOC_POINT

    def test_impulse_on_turns_off_load_after_measurement(self):
        """Az impulzus befejezése után a terhelésnek ki kell kapcsolni."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.5)
        ctrl._state = OcvSocState.IMPULSE_ON

        self._advance_impulse_cycle(ctrl)

        assert not load.input_commanded_on

    def test_on_soc_point_callback_called(self):
        """LOG_SOC_POINT-ban az on_soc_point callbacket meg kell hívni."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)

        # Kézzel tesszük be a LOG_SOC_POINT állapotba
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._soc_index = 95.0
        ctrl._last_ocv_V = 12.3
        ctrl._rb_1s = 0.010
        ctrl._rb_10s = 0.012
        ctrl._rb_30s = 0.013

        ctrl._run_log_soc_point()

        assert len(collected) == 1
        point = collected[0]
        assert point["soc_percent"] == pytest.approx(95.0)
        assert point["ocv_V"] == pytest.approx(12.3)
        assert point["rb_1s_ohm"] == pytest.approx(0.010)

    def test_soc_index_decrements_after_log(self):
        """LOG_SOC_POINT után a soc_index csökken step_percent-tel."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._soc_index = 95.0
        ctrl._run_log_soc_point()
        assert ctrl._soc_index == pytest.approx(90.0)

    def test_done_when_soc_reaches_zero(self):
        """soc_index <= 0 után DONE állapotba kell lépni."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._soc_index = 5.0  # utolsó lépés, 5-5=0 → DONE
        ctrl._run_log_soc_point()
        assert ctrl.state == OcvSocState.DONE


# ------------------------------------------------------------------ #
# TestOcvSocFault                                                      #
# ------------------------------------------------------------------ #

class TestOcvSocFault:
    def test_emergency_stop_sets_fault(self):
        ctrl, psu, load, *_ = make_ocv_soc_controller()
        ctrl.emergency_stop("TEST_FAULT")
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == "TEST_FAULT"

    def test_emergency_stop_turns_off_load_and_psu(self):
        ctrl, psu, load, *_ = make_ocv_soc_controller()
        load.input_on()
        ctrl.emergency_stop("TEST")
        assert not load.input_commanded_on
        assert not psu.output_commanded_on

    def test_advance_noop_in_fault(self):
        ctrl, *_ = make_ocv_soc_controller()
        ctrl.emergency_stop("PRIOR_FAULT")
        state = ctrl.advance(1.0)
        assert state == OcvSocState.FAULT

    def test_advance_noop_in_done(self):
        ctrl, *_ = make_ocv_soc_controller()
        ctrl._state = OcvSocState.DONE
        state = ctrl.advance(1.0)
        assert state == OcvSocState.DONE
