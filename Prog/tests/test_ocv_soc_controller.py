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
        impulse_measure_times_s=(1.0, 10.0, 30.0),
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
        """Gyorsan STEP_DISCHARGE állapotba juttat."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        # INIT → PRECHARGE
        ctrl.advance(0.0)
        # Belső ChargeController force-DONE
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        # PRECHARGE → PRECHARGE_RELAX
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX
        # Teljes relax idő lejárat (18000s)
        ctrl.advance(18000.0)
        assert ctrl.state == OcvSocState.STEP_DISCHARGE
        return ctrl, load

    def test_step_discharge_stops_at_step_ah(self):
        """Lépés-Ah limit elérésekor STEP_RELAX-ba kell lépni."""
        ctrl, load = self._setup_in_step_discharge()
        # step_Ah = 7.0 * 5% / 100 = 0.35 Ah
        # Kisütési ráta: C/10 = 7.0/10 = 0.7A
        # A MockLoad measure_current() visszaadja a set current-t ha input_on
        # dt_s = 1800s → 0.7A * 1800s / 3600 = 0.35 Ah → pontosan step_Ah
        # Viszont az integráció signed: -i_load, accumulated_discharge_Ah = abs
        assert load.input_commanded_on  # terhelés be van kapcsolva

        # Adunk elég dt-t a step_Ah-hoz
        # step_Ah = 0.35 Ah, i_load = 0.7A → t = 0.35/0.7 * 3600 = 1800s
        ctrl.advance(1800.0)
        assert ctrl.state == OcvSocState.STEP_RELAX
        assert not load.input_commanded_on  # terhelés le van kapcsolva

    def test_step_discharge_stops_at_terminate_voltage(self):
        """Terminate voltage elérésekor LOG_SOC_POINT-ba (majd DONE irányba) kell lépni."""
        ctrl, load = self._setup_in_step_discharge()
        # Csökkentjük a DMM feszültséget a terminate alá (6 × 1.80V = 10.8V)
        ctrl._dmm_v.voltage_V = 10.5  # type: ignore[attr-defined]
        # Kis dt, de a feszültség már terminate alatt van
        ctrl.advance(1.0)
        # Terminate voltage esetén LOG_SOC_POINT állapotba kell kerülni
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
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
        """Nem keypoint SOC-on (pl. 95%) → 7200s relax."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        # INIT → PRECHARGE → PRECHARGE_RELAX → lejár 18000s → STEP_DISCHARGE
        ctrl.advance(0.0)
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        ctrl.advance(18000.0)
        # Most 100%-on voltunk, STEP_DISCHARGE indult
        assert ctrl.state == OcvSocState.STEP_DISCHARGE
        # Gyorsan lépjük a step_Ah-t → STEP_RELAX (soc_index most 100%, következő: 95%)
        ctrl.advance(1800.0)
        # STEP_RELAX-ban vagyunk, soc_index még 100% (csak LOG_SOC_POINT-ban csökken)
        assert ctrl.state == OcvSocState.STEP_RELAX
        # 95% nem keypoint → default relax
        assert ctrl._current_relax_s == pytest.approx(7200.0)

    def test_precharge_relax_transitions_to_step_discharge(self):
        """PRECHARGE_RELAX lejárta után STEP_DISCHARGE kezdődik."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl.advance(0.0)
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX
        ctrl.advance(18000.0)
        assert ctrl.state == OcvSocState.STEP_DISCHARGE

    def test_step_relax_transitions_to_impulse_prep(self):
        """STEP_RELAX lejárta után IMPULSE_PREP következik."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl.advance(0.0)
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        ctrl.advance(18000.0)  # PRECHARGE_RELAX lejár
        ctrl.advance(1800.0)   # STEP_DISCHARGE → STEP_RELAX (step_Ah=0.35Ah, 0.7A * 1800s = 0.35Ah)
        assert ctrl.state == OcvSocState.STEP_RELAX
        # Relax lejárat (7200s)
        ctrl.advance(7200.0)
        assert ctrl.state == OcvSocState.IMPULSE_PREP


# ------------------------------------------------------------------ #
# TestOcvSocImpulse                                                    #
# ------------------------------------------------------------------ #

class TestOcvSocImpulse:
    def test_rb_calculation(self):
        """
        Rb számítás ellenőrzése:
        ocv=13.5V, v_1s=13.2V, i=1.4A → rb_1s = 0.3/1.4 ≈ 0.214 Ohm
        """
        psu = MockPSU()
        load = MockLoad(voltage_V=13.5)
        # DMM-et programozzuk: read_voltage() sorozat
        # IMPULSE_ON hívások sorban: ocv_V, v_1s, v_10s, v_30s
        dmm_v = MockDMM(voltage_V=13.5)
        dmm_t = MockDMM(temperature_C=22.0)
        profile = _make_profile(capacity_Ah=7.0)
        safety = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )
        cfg = OcvSocConfig(
            step_percent=5.0,
            discharge_rate_divisor=10,
            relax_default_s=0.0,   # 0 → azonnal lejár
            relax_keypoint_s=0.0,
            impulse_current_rate_divisor=5,
            impulse_duration_s=30.0,
            impulse_measure_times_s=(1.0, 10.0, 30.0),
        )
        ctrl = OcvSocController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        # Beállítjuk az impulzus áramot: C/5 = 7.0/5 = 1.4A
        assert ctrl._impulse_current_A_set == pytest.approx(1.4)

        # Az impulzus szimulálásához be kell állítani a DMM feszültség értékeit.
        # read_voltage() sorozat: ocv=13.5, v_1s=13.2, v_10s=13.0, v_30s=12.9
        voltage_sequence = [13.5, 13.2, 13.0, 12.9]
        call_idx = [0]

        def mock_read_voltage() -> float:
            v = voltage_sequence[min(call_idx[0], len(voltage_sequence) - 1)]
            call_idx[0] += 1
            return v

        ctrl._dmm_v.read_voltage = mock_read_voltage  # type: ignore[method-assign]

        # Mockoljuk a time.sleep-et (ne várjunk valóban)
        import unittest.mock
        with unittest.mock.patch("time.sleep"):
            ctrl._run_impulse_on()

        assert ctrl._rb_1s == pytest.approx(0.3 / 1.4, rel=1e-3)
        assert ctrl._rb_10s == pytest.approx(0.5 / 1.4, rel=1e-3)
        assert ctrl._rb_30s == pytest.approx(0.6 / 1.4, rel=1e-3)
        assert ctrl.state == OcvSocState.LOG_SOC_POINT

    def test_impulse_on_turns_off_load_after_measurement(self):
        """Az impulzus befejezése után a terhelésnek ki kell kapcsolni."""
        psu = MockPSU()
        load = MockLoad(voltage_V=12.5)
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0)
        profile = _make_profile(capacity_Ah=7.0)
        safety = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )
        cfg = OcvSocConfig(relax_default_s=0.0, relax_keypoint_s=0.0)
        ctrl = OcvSocController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        import unittest.mock
        with unittest.mock.patch("time.sleep"):
            ctrl._run_impulse_on()

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
