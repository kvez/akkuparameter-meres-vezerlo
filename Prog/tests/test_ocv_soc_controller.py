"""
Tesztek: OcvSocController állapotgép.
Minimális lefedettség: INIT→PRECHARGE, lépéses kisütés, relax keypoint/default,
Rb impulzus számítás.
"""
from __future__ import annotations

import pytest

from Prog.src.battery_profile import BatteryProfile
from Prog.src.charge_controller import ChargeState
from Prog.src.ocv_soc_controller import OcvSocConfig, OcvSocController, OcvSocState
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode
from Prog.tests.mock_drivers.fake_clock import (
    FakeMonotonicClock,
    InstrumentLatencyClock,
    bind_pulse_instrument_latency,
)
from Prog.tests.mock_drivers.fast_loop_charge import (
    DT_S,
    EXTRA_S,
    PERIOD_S,
    SUBSTEPS_PER_TICK,
    ScriptedChargeReadings,
    assert_parent_delta_equals_independent_expectation,
    cv_currents,
    expected_delta,
    fast_loop_charge_config,
    force_child_state,
    install_fast_loop_clock,
    run_tick,
    taper_currents,
    varying_voltages,
)
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
    charge_config=None,
    pulse_time_source=None,
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
    # WP-AK-04 2. review-kör: a PulseMeasurementService valós (monotonic-clock
    # alapú) időzítést használ — teszthez "ideális, holtidő nélküli hardver"
    # szimulációval helyettesítjük, hogy a t1/t10/t30_actual_s determinisztikus
    # maradjon (l. FakeMonotonicClock, pulse_measurement_service.py docstring).
    clock = pulse_time_source or FakeMonotonicClock(
        step_s=cfg.pulse_chunk_sample_count * cfg.pulse_sample_interval_s
    )
    ctrl = OcvSocController(
        psu, load, dmm_v, dmm_t, profile, safety, cfg,
        measured_capacity_Ah=measured_capacity_Ah,
        pulse_time_source=clock,
        charge_config=charge_config,
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

    def test_u_batt_v_reflects_dmm_reading_after_precharge_tick(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P1 lelet] u_batt_V property hiánya miatt test_runner.py::
        _build_sample() getattr(controller, "u_batt_V", None)-je mindig
        None-t adott vissza, ezért InstrumentManager.read_all() MÁSODSZOR
        is lekérdezte ugyanazt a fizikai feszültség-DMM-et minden ticken
        (CLAUDE.md §9 "egy DMM-et egy tickben egy komponens olvasson")."""
        ctrl, *_ = make_ocv_soc_controller(dmm_voltage_V=12.34)
        ctrl.advance(0.0)  # INIT -> PRECHARGE (belső ChargeController létrejön)
        ctrl.advance(1.0)  # belső ChargeController._advance_single() olvassa a DMM-et
        assert ctrl.u_batt_V == pytest.approx(12.34)

    def test_battery_temperature_c_reflects_dmm_reading_after_precharge_tick(self):
        """L. test_u_batt_v_reflects_dmm_reading_after_precharge_tick
        docstringje ugyanerről a hiányzó mintáról — a hőmérséklet-DMM-re."""
        ctrl, *_ = make_ocv_soc_controller(dmm_voltage_V=12.34)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.battery_temperature_C == pytest.approx(22.0)

    def test_dmm_valid_property_reflects_actual_dmm_read_result(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P2 lelet #4] a ChargeController már exponálja a dmm_valid
        property-t, ezt test_runner.py::_build_sample() a dmm_voltage_valid
        mezőhöz olvassa — az OcvSocController hiányzott, emiatt
        getattr(controller, "dmm_valid", None) mindig None-t adott, és a
        fallback (`u_batt is not None`) gyakorlatilag MINDIG True-t írt,
        akkor is, ha a DMM-olvasás ténylegesen hibázott."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.34)
        ctrl.advance(0.0)  # INIT -> PRECHARGE, DMM-olvasás
        assert ctrl.dmm_valid is True

        dmm_v.dmm_valid = False
        ctrl.advance(1.0)  # a következő DMM-olvasás hibázik
        assert ctrl.dmm_valid is False

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

    def test_charge_ctrl_reference_cleared_after_charge_done(self):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31),
        Metrológia/Adatintegritás P2 lelet] a beágyazott ChargeController
        referenciáját CHARGE_DONE után NEM dobta el a controller (ellentétben
        golden_gg_controller.py/traditional_learning_controller.py mintájával)
        — a proxy-property-k (u_psu_set_V/taper_timer_s/last_integration_
        source) emiatt a PRECHARGE utolsó tickjén befagyott, elévült értéket
        adtak volna vissza a session hátralévő részében (STEP_DISCHARGE/
        STEP_RELAX/IMPULSE_*/LOG_SOC_POINT alatt), holott a PSU ténylegesen
        ki van kapcsolva."""
        ctrl, *_ = make_ocv_soc_controller(dmm_voltage_V=14.4)
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.PRECHARGE

        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)

        assert ctrl._charge_ctrl is None
        assert ctrl.u_psu_set_V == 0.0
        assert ctrl.i_psu_set_A == 0.0
        assert ctrl.taper_timer_s == 0.0
        assert ctrl.last_integration_source == "ZERO"

    def test_relax_extreme_dt_s_reflected_in_session_property(self):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31),
        Metrológia/Adatintegritás P2 lelet] a relax-tick-ek dt_s-e korábban
        sosem jutott el a session-szintű extreme_dt_s_count/max_observed_
        dt_s property-ig — egy extrém dt_s-tüske PRECHARGE_RELAX/STEP_RELAX
        alatt csendben elveszett volna a capacity_result_quality kapu elől."""
        ctrl, *_ = make_ocv_soc_controller(dmm_voltage_V=14.4)
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.PRECHARGE

        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        state = ctrl.advance(1.0)
        assert state == OcvSocState.PRECHARGE_RELAX
        assert ctrl.extreme_dt_s_count == 0

        ctrl.advance(301.0)  # extrém dt_s (>300s küszöb) relax alatt

        assert ctrl.extreme_dt_s_count == 1
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)

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

    def test_precharge_uses_injected_charge_config_not_bare_default(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P1 lelet] A beágyazott ChargeController korábban mindig
        bare ChargeConfig()-ot kapott PRECHARGE alatt, figyelmen kívül hagyva
        a hívó (GUI/session) által átadott charge_config-ot (pl. a 2026-08-23
        OV-incidens miatt bevezetett fast_loop_enabled=True-t) — ellentétben
        a TraditionalLearningController/GoldenGgController-rel."""
        from Prog.src.charge_controller import ChargeConfig

        psu = MockPSU()
        load = MockLoad(voltage_V=13.0)
        dmm_v = MockDMM(voltage_V=13.0)
        dmm_t = MockDMM(temperature_C=22.0)
        profile = _make_profile(7.0)
        safety = SafetyManager(
            profile=profile, psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )
        injected_cfg = ChargeConfig(fast_loop_enabled=True, charge_current_A_override=1.23)
        ctrl = OcvSocController(
            psu, load, dmm_v, dmm_t, profile, safety, OcvSocConfig(),
            charge_config=injected_cfg,
        )

        ctrl.advance(0.0)  # INIT -> PRECHARGE

        assert ctrl.state == OcvSocState.PRECHARGE
        assert ctrl._charge_ctrl is not None
        assert ctrl._charge_ctrl._config is injected_cfg
        assert ctrl._charge_ctrl._config.fast_loop_enabled is True

    def test_charge_config_property_reflects_injected_config(self):
        """Teljes kódbázis review 16. kör (2026-09-04), Session/Workflow
        P2 lelet: a testvér GoldenGgController/TraditionalLearningController/
        ChemIdCharacterizationController charge_config property-jével
        azonos, korábban hiányzó public API."""
        from Prog.src.charge_controller import ChargeConfig

        injected_cfg = ChargeConfig(charge_current_A_override=0.8)
        psu = MockPSU()
        load = MockLoad(voltage_V=13.0)
        dmm_v = MockDMM(voltage_V=13.0)
        dmm_t = MockDMM(temperature_C=22.0)
        profile = _make_profile(7.0)
        safety = SafetyManager(
            profile=profile, psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )
        ctrl = OcvSocController(
            psu, load, dmm_v, dmm_t, profile, safety, OcvSocConfig(),
            charge_config=injected_cfg,
        )
        assert ctrl.charge_config is injected_cfg
        assert ctrl.charge_config.charge_current_A_override == pytest.approx(0.8)

    def test_charge_config_property_reflects_default_when_not_injected(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.charge_config.charge_current_A_override == 0.0


# ------------------------------------------------------------------ #
# TestOcvSocPhaseElapsedS                                              #
# [CF-19, governance-v2 kör 2, DFAM-023 opaque_composite_step_hides_   #
# progress] a phase_elapsed_s property-nek NULLÁZÓDNIA kell minden     #
# belső állapotváltáskor, és NÖVEKEDNIE ugyanabban az állapotban       #
# maradva -- korábban ez a bookkeeping teljesen hiányzott.             #
# ------------------------------------------------------------------ #

class TestOcvSocPhaseElapsedS:
    def test_starts_at_zero(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.phase_elapsed_s == 0.0

    def test_grows_while_state_unchanged(self):
        ctrl, *_ = make_ocv_soc_controller()
        ctrl.advance(0.0)  # INIT -> PRECHARGE
        ctrl.advance(5.0)
        ctrl.advance(3.0)
        assert ctrl.state == OcvSocState.PRECHARGE
        assert ctrl.phase_elapsed_s == pytest.approx(8.0)

    def test_resets_on_state_transition(self):
        from Prog.src.charge_controller import ChargeState
        ctrl, *_ = make_ocv_soc_controller()
        ctrl.advance(0.0)  # INIT -> PRECHARGE
        ctrl.advance(100.0)
        assert ctrl.phase_elapsed_s == pytest.approx(100.0)
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)  # PRECHARGE -> PRECHARGE_RELAX
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX
        assert ctrl.phase_elapsed_s == pytest.approx(0.0)


# ------------------------------------------------------------------ #
# TestOcvSocC10CapacityBasis                                           #
# [regresszió, Teljes kódbázis review 8. kör (2026-08-30), Metrológia/ #
# Adatintegritás P2 lelet] a kapacitásbázis-fallback és a kisütési/     #
# impulzus-áram setpont korábban profile.nominal_capacity_Ah-t         #
# használt — a saját normatív forrás (SUP01-GRID-003) explicit         #
# nominal_C10_capacity_Ah-t ír elő. CLAUDE.md §11.1 "C10 és C20 nem     #
# cserélhető fel". FG-sorozatú (C20-nominal) profillal, ahol            #
# nominal_capacity_Ah != C10_capacity_Ah, kell megkülönböztetni.       #
# ------------------------------------------------------------------ #

class TestOcvSocC10CapacityBasis:
    def _make_fg_series_controller(self, measured_capacity_Ah=None):
        fg_profile = BatteryProfile(
            battery_name="TEST_FG", manufacturer="TEST", model="FG20AH",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=20.0,
            C10_capacity_Ah=14.0, terminate_voltage_per_cell_V=1.80,
        )
        psu = MockPSU()
        load = MockLoad(voltage_V=13.0)
        dmm_v = MockDMM(voltage_V=13.0)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(
            profile=fg_profile, psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )
        cfg = OcvSocConfig(
            step_percent=5.0, discharge_rate_divisor=10,
            relax_default_s=7200.0, relax_keypoint_s=18000.0,
            impulse_current_rate_divisor=5, impulse_duration_s=30.0,
        )
        ctrl = OcvSocController(
            psu, load, dmm_v, dmm_t, fg_profile, safety, cfg,
            measured_capacity_Ah=measured_capacity_Ah,
        )
        return ctrl, fg_profile

    def test_capacity_basis_fallback_uses_c10_not_nominal(self):
        """Mért kapacitás nélkül (fallback ág) a C10_capacity_Ah=14.0-et
        kell használnia, NEM a nominal_capacity_Ah=20.0-at."""
        ctrl, fg_profile = self._make_fg_series_controller()
        assert ctrl._total_capacity_Ah == pytest.approx(14.0)
        assert ctrl._total_capacity_Ah != pytest.approx(fg_profile.nominal_capacity_Ah)
        assert ctrl._capacity_basis_quality == "NOMINAL_CAPACITY_BASED_SOC"

    def test_discharge_current_a_uses_c10_not_nominal(self):
        """C/10 = 14.0/10 = 1.4A, NEM 20.0/10 = 2.0A."""
        ctrl, _fg_profile = self._make_fg_series_controller()
        assert ctrl._discharge_current_A == pytest.approx(1.4)

    def test_impulse_current_a_set_uses_c10_not_nominal(self):
        """C/5 = 14.0/5 = 2.8A, NEM 20.0/5 = 4.0A."""
        ctrl, _fg_profile = self._make_fg_series_controller()
        assert ctrl._impulse_current_A_set == pytest.approx(2.8)

    def test_measured_capacity_still_takes_priority_over_c10(self):
        """SUP01-GRID-003: mért kapacitás ELSŐDLEGES, a C10-fallback csak
        akkor aktiválódik, ha nincs mért érték — ez a fix nem változtat
        ezen az elsőbbségi soron."""
        ctrl, _fg_profile = self._make_fg_series_controller(measured_capacity_Ah=12.5)
        assert ctrl._total_capacity_Ah == pytest.approx(12.5)
        assert ctrl._capacity_basis_quality == "MEASURED_CAPACITY_BASED"


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
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_step_discharge()
        return ctrl, load

    def test_begin_step_discharge_faults_on_concurrent_psu_load_before_energizing(self):
        """[Kör 20 Batch E13a, F20-S3, DFAM-001] `_begin_step_discharge()`
        korábban a `try:` blokk BELSEJÉBEN, feltétel nélkül futtatta
        `psu.all_outputs_off()`-t, majd `load.input_on()`-t -- egy stale
        `psu.output_commanded_on=True` flaget az `all_outputs_off()`
        CSENDBEN letörölt volna, MIELŐTT bármi észlelhetné a konfliktust.
        Ez a teszt a fix ELŐTTI kódon bukna: `ctrl.state` NEM lenne FAULT,
        és `load.input_commanded_on` True lenne."""
        ctrl, psu, load, _dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        psu.output_commanded_on = True

        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_step_discharge()

        assert ctrl.state == OcvSocState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason
        assert load.input_commanded_on is False

    def test_begin_step_discharge_dmm_invalid_faults_before_energizing(self):
        """[regresszió, Claude-Codex loop R1 B3, F-02, DFAM-001] a
        `_begin_step_discharge()` korábban ismerten hibás DMM-olvasás
        mellett is Load INPUT ON-t adott -- a per-tick DMM-kapu csak a
        KÖVETKEZŐ ticken futott."""
        ctrl, psu, load, _dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._dmm_valid = False

        ctrl._begin_step_discharge()

        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls

    def test_log_soc_point_with_invalid_dmm_faults_instead_of_next_step_discharge(self):
        """[regresszió, R1 B3, F-02] lánc: LOG_SOC_POINT-tickben hibás
        top-of-tick DMM-olvasás -> a következő lépés `_begin_step_
        discharge()` DMM-őre FAULT-ot ad energizálás nélkül."""
        ctrl, _psu, load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._last_ocv_V = 12.3
        load.reset_calls()
        dmm_v.simulate_timeout = True

        ctrl.advance(1.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert "input_on()" not in load.calls
        assert load.input_commanded_on is False

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

    def test_step_discharge_current_far_below_setpoint_gives_warning(self):
        """[regresszió, Hardver/Műszervezérlés HW4-02 (review-kör 4, P1,
        DFAM-001)] l. discharge_controller.py testvér tesztjének
        docstringje ugyanerről a hiányzó setpoint-vs-mért kapuról."""
        ctrl, load = self._setup_in_step_discharge()
        load.current_A = ctrl._discharge_current_A * 0.1  # a setpoint töredéke -- "ragadt"
        ctrl.advance(1.0)
        assert ctrl.state != OcvSocState.FAULT
        assert ctrl.last_warning_code == "LOAD_CURRENT_SETPOINT_NOT_REACHED"

    def test_step_discharge_setpoint_warning_tick_counter_survives_higher_priority_warning(self):
        """[regresszió, MAX verifier (review-kör 4, HW4-02, 329c1a3) N1
        lelete] l. discharge_controller.py testvér tesztjének docstringje
        ugyanerről a hiányzó, a _last_warning_code slot-tól független
        tick-számlálóról."""
        ctrl, load = self._setup_in_step_discharge()
        ctrl._dmm_t.temperature_C = 40.0  # type: ignore[attr-defined]  # profil névleges tartományán kívül
        load.current_A = ctrl._discharge_current_A * 0.1  # a setpoint töredéke -- "ragadt"
        assert ctrl.load_current_setpoint_warning_ticks == 0
        ctrl.advance(1.0)
        assert ctrl.last_warning_code == "TEMPERATURE_OUT_OF_RANGE"
        assert ctrl.load_current_setpoint_warning_ticks == 1

    def test_step_discharge_stops_at_terminate_voltage(self):
        """Terminate voltage elérésekor STEP_RELAX-ba kell lépni (relax+impulzus után 0% log)."""
        ctrl, load = self._setup_in_step_discharge()
        ctrl._dmm_v.voltage_V = 10.5  # type: ignore[attr-defined]
        ctrl.advance(1.0)
        # Terminate voltage → STEP_RELAX (nem LOG_SOC_POINT; relax+impulzus után logolunk)
        assert ctrl.state == OcvSocState.STEP_RELAX
        assert not load.input_commanded_on

    def test_terminate_voltage_sets_grid_truncated_flag_and_skip_count(self):
        """Review-kör 2 (2026-09-08), Session/Workflow/Állapotgép S-5 lelet:
        ha a végfeszültség a racs szerinti köztes cél ELŐTT érkezik,
        _finish_discharge_step() a _grid_pos-t egyenesen a racs UTOLSÓ
        elemére ugratja -- a köztes racspontok (itt: 21 pontos, 5%-os
        egyenletes rács, _grid_pos=0-ból indulva -> 19 köztes pont)
        kimaradnak. Korábban ez semmilyen jelzést nem kapott."""
        ctrl, load = self._setup_in_step_discharge()
        assert len(ctrl._grid) == 21
        ctrl._dmm_v.voltage_V = 10.5  # type: ignore[attr-defined]

        ctrl.advance(1.0)

        assert ctrl._grid_pos == 20
        assert ctrl._grid_truncated_by_terminate_voltage is True
        assert ctrl._grid_points_skipped == 19

    def test_step_ah_reached_does_not_set_grid_truncated_flag(self):
        """Kontroll: a NORMÁL (racspont-cél Ah elérve, nem terminate voltage)
        kilépési ág egyáltalán nem ugrik -- nem szabad truncated-jelzést
        kapnia."""
        ctrl, load = self._setup_in_step_discharge()

        ctrl.advance(1800.0)  # l. test_step_discharge_stops_at_step_ah

        assert ctrl._grid_pos == 1
        assert ctrl._grid_truncated_by_terminate_voltage is False
        assert ctrl._grid_points_skipped == 0

    def test_terminate_voltage_at_second_to_last_grid_point_is_not_flagged_truncated(self):
        """MAX verifier follow-up (F2, 2026-09-09): határeset -- ha a
        végfeszültség PONT akkor érkezik, amikor a szándékolt köztes cél
        MÁR a rács utolsó eleme (nincs mit kihagyni), a truncated-jelzés
        NEM állhat be, `grid_points_skipped` 0 kell maradjon. Egy
        `flag = True` feltétel nélküli hibás implementáció ezt a tesztet
        buktatná el (a 2 fenti teszt ezt nem fedte le)."""
        ctrl, load = self._setup_in_step_discharge()
        ctrl._grid_pos = len(ctrl._grid) - 2  # a köztes cél MÁR az utolsó racspont
        ctrl._dmm_v.voltage_V = 10.5  # type: ignore[attr-defined]

        ctrl.advance(1.0)

        assert ctrl._grid_pos == len(ctrl._grid) - 1
        assert ctrl._grid_truncated_by_terminate_voltage is False
        assert ctrl._grid_points_skipped == 0

    def test_step_discharge_load_off_failure_at_terminate_voltage_escalates_to_fault(self):
        """[regresszió, Teljes kódbázis review 16. kör (2026-09-04), Hardver/
        Műszervezérlés P1 lelet] a testvér GoldenGgController/
        ChemIdCharacterizationController/TraditionalLearningController
        mindegyike explicit try/except-be csomagolja a kisütés-lezáráskori
        load.input_off()-ot ("a hibáját nem szabad csendben elnyelni, mert
        a relax 0A-feltételezését érvénytelenítené") — az OcvSocController
        volt az egyetlen, ahol ez hiányzott a STEP_DISCHARGE terminate-
        voltage ágán."""
        ctrl, load = self._setup_in_step_discharge()

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl._dmm_v.voltage_V = 10.5  # type: ignore[attr-defined]
        ctrl.advance(1.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "STEP_DISCHARGE_LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason

    def test_step_discharge_load_off_failure_at_step_ah_escalates_to_fault(self):
        """Ugyanaz a védelem a másik kilépési ágon (racspont-cél Ah elérve,
        nem terminate voltage) — l. a fenti teszt docstringje."""
        ctrl, load = self._setup_in_step_discharge()

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.advance(1800.0)  # step_Ah = 0.35 Ah elérve (l. test_step_discharge_stops_at_step_ah)

        assert ctrl.state == OcvSocState.FAULT
        assert "STEP_DISCHARGE_LOAD_OFF_FAILED" in ctrl.fault_reason

    def test_step_discharge_faults_on_max_discharge_time_reached(self):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), Session/
        Workflow P2 lelet] a 6f21269 commit (4. kör) szándékosan kihagyta a
        STEP_DISCHARGE fázist az Ah-alapú runaway-backstopból ("implicit
        korlátozott a target_removed_Ah által") — ez nem zárja ki a kettős
        szenzorhiba esetét (feszültség-DMM ÉS áram-readback egyszerre
        rögzült/hibás): ha a target_removed_Ah cél sosem érkezik el, egy
        tisztán idő-alapú (szenzorfüggetlen) felső korlátnak kell
        lezárnia a ciklust, ugyanúgy, mint a testvér fázisoknál (chemid/
        golden_gg/traditional_learning)."""
        ctrl, load = self._setup_in_step_discharge()
        # A feszültség-DMM (12.5V, sosem éri el a terminate_V-t) mellett a
        # kisütés időben korlátlanul futna, ha nem lenne időalapú backstop —
        # a lentihez hasonló egyetlen, nagy dt_s tick a valós, tick-enkénti
        # felhalmozódást szimulálja (l. test_step_discharge_stops_at_step_ah
        # hasonló mintája).
        ctrl.advance(86400.1)
        assert ctrl.state == OcvSocState.FAULT
        assert "MAX_DISCHARGE_TIME_REACHED" in ctrl.fault_reason
        assert not load.input_commanded_on

    def test_step_discharge_max_time_respects_safety_manager_override(self):
        """[regresszió, Teljes kódbázis review 8. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] max_discharge_time_s korábban egy privát,
        hardkódolt modul-konstansként élt itt — egy SafetyManager-en
        beállított egyedi érték NEM ért volna el ide. Mostantól
        self._safety.max_discharge_time_s a kanonikus forrás, a standalone
        DischargeController mintáját követve (5-8. kör konszolidáció)."""
        ctrl, load = self._setup_in_step_discharge()
        ctrl._safety.max_discharge_time_s = 5.0  # << a 86400s alapértéktől

        ctrl.advance(10.0)  # a szűkített limit fölött, de az alapérték alatt

        assert ctrl.state == OcvSocState.FAULT
        assert "MAX_DISCHARGE_TIME_REACHED" in ctrl.fault_reason
        assert not load.input_commanded_on

    def test_reverse_polarity_during_step_discharge_faults_not_relax(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív
        STEP_DISCHARGE alatt egy fizikailag ésszerűtlen (<=0V) DMM-olvasás
        korábban csendben "elértük a terminate voltage-ot"-ként STEP_RELAX-ot
        okozott volna, NEM FAULT-ot (CLAUDE.md §10.6/SAFE-009)."""
        ctrl, _load = self._setup_in_step_discharge()
        ctrl._dmm_v.voltage_V = 0.0  # type: ignore[attr-defined]
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"

    def test_concurrent_psu_during_step_discharge_faults(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív
        STEP_DISCHARGE alatt egy PSU ON hardveres konfliktust korábban a
        beágyazott ág sosem ellenőrizte (CLAUDE.md §10.2 defense-in-depth
        hiány)."""
        ctrl, _load = self._setup_in_step_discharge()
        ctrl._psu.output_commanded_on = True  # type: ignore[attr-defined]
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason

    def test_step_discharge_faults_on_load_power_limit_exceeded(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P1 lelet] A standalone DischargeController._run_cc() minden
        ticken ellenőrzi a Keithley 2380 250W hardver-teljesítménykorlátot —
        a beágyazott _run_step_discharge() korábban nem."""
        ctrl, load = self._setup_in_step_discharge()
        load.current_A = 25.0  # 12.5V * 25A = 312.5W > 250W max
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert "LOAD_POWER_LIMIT" in ctrl.fault_reason


# ------------------------------------------------------------------ #
# TestOcvSocEnergyIntegration                                          #
# ------------------------------------------------------------------ #

class TestOcvSocEnergyIntegration:
    """Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: az
    accumulated_charge_Ah/accumulated_discharge_Ah property hiánya miatt a
    TestRunner._flush_totals() mindig 0 deltát látott — a report.json/
    checkpoint total_charge_ah/total_discharge_ah mezője egy teljes OCV-SOC
    session alatt is 0-án maradt (ugyanaz a minta, mint a GOLDEN_GG-nél)."""

    def test_accumulated_charge_ah_nonzero_during_precharge(self):
        """[frissítve, Teljes kódbázis review 5. kör (2026-08-29), Metrológia/
        Adatintegritás P1 lelet] a beágyazott ChargeController mostantól a
        SAJÁT, fault-kezelt i_psu_measured_A property-jét adja a session-
        integrátornak (nem egy redundáns, önálló PSU-olvasást) — ez csak
        akkor nem-nulla, ha a beágyazott ChargeController TÉNYLEGESEN eljut
        a CHARGE_CC állapotba (INIT→PRECHECK→PSU_PRESET→CHARGE_CC, 3 saját
        advance()-tick, l. test_charge_controller.py::_advance_to_cc())."""
        ctrl, psu, _load, _dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.0)
        ctrl.advance(0.0)  # INIT → PRECHARGE (belső ChargeController létrejön, INIT)
        assert ctrl.state == OcvSocState.PRECHARGE
        psu.current_A = 1.75
        ctrl.advance(1.0)  # belső: INIT → PRECHECK
        ctrl.advance(1.0)  # belső: PRECHECK → PSU_PRESET
        ctrl.advance(1.0)  # belső: PSU_PRESET → CHARGE_CC
        ctrl.advance(1.0)  # belső: CHARGE_CC tick — ITT olvasódik a PSU-áram
        assert ctrl.accumulated_charge_Ah > 0.0
        assert ctrl.accumulated_discharge_Ah == 0.0

    def test_accumulated_discharge_ah_nonzero_during_step_discharge(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_step_discharge()  # load.set_current() -> load.current_A beáll
        ctrl.advance(1.0)
        assert ctrl.accumulated_discharge_Ah > 0.0
        assert ctrl.accumulated_charge_Ah == 0.0

    def test_accumulated_ah_zero_before_any_advance(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.accumulated_charge_Ah == 0.0
        assert ctrl.accumulated_discharge_Ah == 0.0

    def test_accumulated_discharge_ah_includes_impulse_ah(self):
        """[regresszió, Teljes kódbázis review 15. kör (2026-09-04),
        Metrológia/Adatintegritás P2 lelet] az IMPULSE-fázisok (C/5
        árammal, akár 30s-ig terhelő diagnosztikai impulzusok) alatt
        ténylegesen mért Ah-fogyasztás korábban KIZÁRÓLAG a rács-SOC
        kumulátorba (removed_Ah_total) jutott el, a session-szintű
        accumulated_discharge_Ah-ba (report.json total_discharge_ah)
        soha — belsőleg inkonzisztens riportot eredményezve ugyanabban
        a session-ben."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.5)
        dmm_v.timed_voltage_fn = lambda t: 12.3
        ctrl._state = OcvSocState.IMPULSE_ON
        assert ctrl.accumulated_discharge_Ah == 0.0

        state = _run_full_impulse(ctrl)

        assert state == OcvSocState.LOG_SOC_POINT
        assert ctrl.accumulated_discharge_Ah > 0.0
        assert ctrl.accumulated_discharge_Ah == pytest.approx(ctrl._impulse_removed_Ah)

    def test_accumulated_discharge_wh_includes_impulse_wh(self):
        """[regresszió, Teljes kódbázis review 16. kör (2026-09-04),
        Metrológia/Adatintegritás P2 lelet] a fenti Ah-fix (15. kör)
        testvérpárja — a PulseAcquisitionResult korábban nem exponált
        Wh-adatot, ezért az accumulated_discharge_Wh csak PRECHARGE/
        STEP_DISCHARGE fázisból táplálkozott, belsőleg inkonzisztens
        módon az accumulated_discharge_Ah-hoz képest (állandó feszültség
        mellett a Wh-nak pontosan Ah×V-nek kell lennie)."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.5)
        dmm_v.timed_voltage_fn = lambda t: 12.3
        ctrl._state = OcvSocState.IMPULSE_ON
        assert ctrl.accumulated_discharge_Wh == 0.0

        state = _run_full_impulse(ctrl)

        assert state == OcvSocState.LOG_SOC_POINT
        assert ctrl.accumulated_discharge_Wh > 0.0
        assert ctrl.accumulated_discharge_Wh == pytest.approx(
            ctrl.accumulated_discharge_Ah * 12.3, rel=1e-3
        )

    def test_accumulated_charge_wh_nonzero_during_precharge(self):
        """[frissítve, Teljes kódbázis review 5. kör (2026-08-29), Metrológia/
        Adatintegritás P1 lelet] l. test_accumulated_charge_ah_nonzero_
        during_precharge docstringje ugyanerről a mintáról — a beágyazott
        ChargeController-nek TÉNYLEGESEN CHARGE_CC-be kell jutnia."""
        ctrl, psu, _load, _dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.0)
        ctrl.advance(0.0)  # INIT → PRECHARGE
        psu.current_A = 1.75
        ctrl.advance(1.0)  # belső: INIT → PRECHECK
        ctrl.advance(1.0)  # belső: PRECHECK → PSU_PRESET
        ctrl.advance(1.0)  # belső: PSU_PRESET → CHARGE_CC
        ctrl.advance(1.0)  # belső: CHARGE_CC tick — ITT olvasódik a PSU-áram
        assert ctrl.accumulated_charge_Wh > 0.0
        assert ctrl.accumulated_discharge_Wh == 0.0

    def test_accumulated_discharge_wh_nonzero_during_step_discharge(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_step_discharge()
        ctrl.advance(1.0)
        assert ctrl.accumulated_discharge_Wh > 0.0
        assert ctrl.accumulated_charge_Wh == 0.0

    def test_u_psu_set_v_reflects_embedded_charge_controller_during_precharge(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P2] a
        ténylegesen parancsolt PSU-feszültség setpoint korábban nem volt
        elérhető OcvSocController-en — test_runner.py::_build_sample()
        generikus getattr(controller, "u_psu_set_V", None)-je mindig None-t
        adott vissza, holott a beágyazott ChargeController ismeri."""
        ctrl, _psu, _load, _dmm_v, _dmm_t = make_ocv_soc_controller()
        ctrl.advance(0.0)  # INIT → PRECHARGE, létrehozza a _charge_ctrl-t
        assert ctrl.state == OcvSocState.PRECHARGE
        # A beágyazott ChargeController saját INIT/PRECHECK/PSU_PRESET
        # állapotgépén kell végighaladnia, mielőtt ténylegesen PSU-
        # feszültséget parancsolna — néhány tick szükséges hozzá.
        for _ in range(5):
            ctrl.advance(1.0)
            if ctrl.u_psu_set_V > 0.0:
                break
        assert ctrl.u_psu_set_V > 0.0

    def test_i_load_set_a_reflects_discharge_current_during_step_discharge(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_step_discharge()
        assert ctrl.i_load_set_A == pytest.approx(ctrl._discharge_current_A)
        assert ctrl.i_load_set_A > 0.0


class TestChargeEnergyDeltaFromEmbeddedFastLoop:
    """[regresszió, R1 B8a, F-20/DFAM-035 -- a korábbi C-06
    TestChargeEnergyIntegratorIncludesFastLoopExtraTime utódja] A session-
    szintű Ah/Wh a beágyazott, VALÓDI ChargeController fast-loop
    résztickjeinek (hívásonként változó áram ÉS feszültség) elintegrált
    deltáját veszi át (l. Prog/tests/mock_drivers/fast_loop_charge.py). A
    régi C-06 teszt a _last_fast_loop_extra_s-t belső integrálás nélkül
    injektálta, és az elvárást ugyanabból a hibás modellből (utolsó áram ×
    (dt + extra)) számolta (tautologikus fixture, DFAM-006) -- a javítás
    előtti kódon ezek a tesztek buknak. Megőrzött assertek: a publikus
    last_fast_loop_extra_s proxy (MAX verifier V-5) és a dt-diagnosztika
    (max_observed_dt_s == dt + extra), nulla delta mellett is."""

    def _reach_child(self, monkeypatch, **charge_cfg_kwargs):
        install_fast_loop_clock(monkeypatch)
        cfg = fast_loop_charge_config(**charge_cfg_kwargs)
        ctrl, psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.0, charge_config=cfg,
        )
        ctrl.advance(0.0)  # INIT -> PRECHARGE (beágyazott ChargeController létrejön)
        assert ctrl.state == OcvSocState.PRECHARGE
        assert ctrl._charge_ctrl is not None
        return ctrl, psu, dmm_v

    def test_multi_tick_variable_current_and_voltage(self, monkeypatch):
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V)
        )
        for _ in range(3):
            rec = run_tick(ctrl, child, readings)
            assert child.state == ChargeState.CHARGE_CV_DMM_CONTROL
            assert_parent_delta_equals_independent_expectation(
                rec, SUBSTEPS_PER_TICK, DT_S + EXTRA_S
            )
            assert ctrl.last_fast_loop_extra_s == child.last_fast_loop_extra_s == EXTRA_S
        assert ctrl.max_observed_dt_s == DT_S + EXTRA_S
        assert ctrl.extreme_dt_s_count == 0

    def test_partial_success_then_fault_tick_keeps_integrated_substeps(self, monkeypatch):
        """A 4. résztick PSU-áramolvasása hibázik (PSU_COMM_LOST): az első
        3 résztick már elintegrált töltése a session-szintű Ah/Wh-ba is
        bekerül (a régi kód a FAULT-tick 0,0 áramával mindet elveszítette)."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V), fail_current_at=3
        )
        rec = run_tick(ctrl, child, readings)
        assert child.state == ChargeState.FAULT
        assert child.fault_reason == "PSU_COMM_LOST"
        assert ctrl.state == OcvSocState.FAULT
        assert_parent_delta_equals_independent_expectation(
            rec, 3, DT_S + 3 * PERIOD_S, check_last_sample_model=False
        )
        assert ctrl.max_observed_dt_s == DT_S + 3 * PERIOD_S

    def test_charge_done_tick_delta_transferred_before_child_dropped(self, monkeypatch):
        """TAPER_HOLD-ban a 4. résztick éri el a taper_hold_s-t (2,6 s):
        CHARGE_DONE, a szülő eldobja a gyermeket -- a tick teljes deltája
        előtte átkerül."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch, taper_hold_s=2.6)
        child = ctrl._charge_ctrl
        child._taper_timer_s = 0.0
        target_V = force_child_state(child, psu, ChargeState.TAPER_HOLD)
        readings = ScriptedChargeReadings(
            psu, dmm_v, taper_currents(), varying_voltages(target_V)
        )
        rec = run_tick(ctrl, child, readings)
        assert child.state == ChargeState.CHARGE_DONE
        assert ctrl._charge_ctrl is None
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX
        assert_parent_delta_equals_independent_expectation(rec, 4, DT_S + 3 * PERIOD_S)
        assert ctrl.max_observed_dt_s == DT_S + 3 * PERIOD_S

    def test_zero_delta_tick_still_records_dt_diagnostics(self, monkeypatch):
        """Nem integráló gyermek-tick (INIT -> PRECHECK): nulla delta, a
        dt-diagnosztika (max_observed_dt_s) ekkor is vezetődik."""
        ctrl, _psu, _dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        assert child.state == ChargeState.INIT
        ctrl.advance(7.0)
        assert child.accumulated_charge_Ah == 0.0
        assert ctrl.accumulated_charge_Ah == 0.0
        assert ctrl.accumulated_charge_Wh == 0.0
        assert ctrl.max_observed_dt_s == 7.0

    # -------------------------------------------------------------- #
    # R2-B5 (R2-session-02, DFAM-033): kivételes kilépés a gyermek
    # fast-loopjából (Ctrl+C a résztickek közötti sleep()-ben).
    @staticmethod
    def _install_interrupting_fast_loop_clock(monkeypatch, k_sleep):
        """A FastLoopClock-kal azonos kézi óra (sleep() léptet,
        perf_counter() olvas), de a k_sleep-edik fast-loop sleep()
        KeyboardInterrupt-ot dob. Csak a charge_controller modul `time`
        nevét cseréli (l. install_fast_loop_clock())."""
        from types import SimpleNamespace

        import Prog.src.charge_controller as cc_module

        clock = {"now": 0.0, "sleeps": 0}

        def perf_counter():
            return clock["now"]

        def sleep(seconds):
            clock["sleeps"] += 1
            if clock["sleeps"] == k_sleep:
                raise KeyboardInterrupt("R2-B5 scripted Ctrl+C in fast-loop sleep")
            clock["now"] += seconds

        monkeypatch.setattr(
            cc_module, "time",
            SimpleNamespace(perf_counter=perf_counter, sleep=sleep),
        )

    @pytest.mark.parametrize("k_sleep", [1, 4])
    def test_keyboard_interrupt_in_fast_loop_sleep_transfers_child_delta_once(
        self, monkeypatch, k_sleep
    ):
        """[regresszió, R2-B5 (R2-session-02), DFAM-033] Ctrl+C a gyermek
        k-adik fast-loop sleep()-jében: a gyermek által addig MÁR
        elintegrált k résztick Ah/Wh-deltája és a dt-diagnosztika
        (dt_s + (k-1)·period_s) pontosan egyszer átkerül a session-szintű
        integrátorba, az eredeti kivétel változatlanul továbbmegy. A javítás
        előtt az átadás csak az advance() normál visszatérése után állt --
        a szülő a már elintegrált töltést elveszítette."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V)
        )
        self._install_interrupting_fast_loop_clock(monkeypatch, k_sleep)
        p_ah, p_wh = ctrl.accumulated_charge_Ah, ctrl.accumulated_charge_Wh
        c_ah, c_wh = child.accumulated_charge_Ah, child.accumulated_charge_Wh
        with pytest.raises(KeyboardInterrupt):
            ctrl.advance(DT_S)
        assert len(readings.pairs) == k_sleep
        exp_ah, exp_wh = expected_delta(readings.pairs, DT_S, PERIOD_S)
        assert exp_ah > 0.0 and exp_wh > 0.0
        assert child.accumulated_charge_Ah - c_ah == pytest.approx(exp_ah, rel=1e-12, abs=1e-15)
        assert child.accumulated_charge_Wh - c_wh == pytest.approx(exp_wh, rel=1e-12, abs=1e-15)
        assert ctrl.accumulated_charge_Ah - p_ah == pytest.approx(exp_ah, rel=1e-12, abs=1e-15)
        assert ctrl.accumulated_charge_Wh - p_wh == pytest.approx(exp_wh, rel=1e-12, abs=1e-15)
        assert ctrl.max_observed_dt_s == DT_S + (k_sleep - 1) * PERIOD_S

    def test_keyboard_interrupt_runner_checkpoint_keeps_child_delta(
        self, monkeypatch, tmp_path
    ):
        """[regresszió, R2-B5 (R2-session-02), DFAM-033] Runner-lánc: Ctrl+C
        a PRECHARGE-gyermek 4. fast-loop sleep()-jében, valódi TestRunner
        alatt. A checkpoint FAULT, a charge_ah/charge_wh == a gyermek által
        elintegrált == a mock-bemenetből független várt érték, a PSU és a
        Load OFF, a KeyboardInterrupt továbbmegy. A javítás előtt a
        checkpoint charge_ah 0 volt."""
        import json
        import time as real_time
        from types import SimpleNamespace

        import Prog.src.test_runner as tr_module
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.test_runner import (
            StepKind,
            TestPlan,
            TestRunner,
            TestRunnerConfig,
            TestStep,
            TestType,
        )

        install_fast_loop_clock(monkeypatch)
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.0, charge_config=fast_loop_charge_config(),
        )
        ctrl.advance(0.0)  # INIT -> PRECHARGE (beágyazott ChargeController létrejön)
        assert ctrl.state == OcvSocState.PRECHARGE
        child = ctrl._charge_ctrl
        assert child is not None
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        psu.output_on()
        assert psu.output_commanded_on
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V)
        )
        self._install_interrupting_fast_loop_clock(monkeypatch, 4)
        # Runner-óra: minden perf_counter()-hívás DT_S-sel lép, így a
        # gyermek első résztickje pontosan DT_S-t kap.
        runner_clock = {"t": 0.0}

        def runner_perf_counter():
            runner_clock["t"] += DT_S
            return runner_clock["t"]

        monkeypatch.setattr(tr_module, "time", SimpleNamespace(
            perf_counter=runner_perf_counter,
            monotonic=real_time.monotonic,
            sleep=real_time.sleep,
        ))
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        runner = TestRunner(
            instrument_manager=InstrumentManager(
                psu=psu, load=load, dmm_voltage=dmm_v, dmm_temperature=dmm_t,
            ),
            safety=ctrl._safety,
            logger=logger,
            profile=ctrl._profile,
            config=TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="t"),
            charge_ctrl_factory=lambda: None,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
            ocv_soc_ctrl_factory=lambda: ctrl,
        )
        try:
            with pytest.raises(KeyboardInterrupt):
                runner.run(TestPlan(
                    test_type=TestType.OCV_SOC_CHARACTERIZATION,
                    steps=(TestStep(StepKind.OCV_SOC, "ocv_soc"),),
                ))
        finally:
            logger.close()
        ckpt = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
        assert len(readings.pairs) == 4
        exp_ah, exp_wh = expected_delta(readings.pairs, DT_S, PERIOD_S)
        assert exp_ah > 0.0 and exp_wh > 0.0
        assert ckpt["status"] == "FAULT"
        assert ckpt["charge_ah"] == pytest.approx(child.accumulated_charge_Ah, rel=1e-12)
        assert ckpt["charge_ah"] == pytest.approx(exp_ah, rel=1e-12)
        assert ckpt["charge_wh"] == pytest.approx(exp_wh, rel=1e-12)
        assert ctrl.accumulated_charge_Ah == pytest.approx(exp_ah, rel=1e-12)
        assert not psu.output_commanded_on
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
        """Nem keypoint SOC-on (90%) → 7200s relax.

        grid_pos=1 → a 95%-os pontot már elértük; begin_step_discharge célja
        grid[2]=90% (nem keypoint) → target_removed_Ah = 7.0*(100-90)/100 = 0.7 Ah.
        """
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._grid_pos = 1
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_step_discharge()
        # discharge_current_A = 7.0/10 = 0.7A → 0.7A * 3600s = 0.7 Ah
        ctrl.advance(3600.0)
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

    def test_impulse_prep_faults_on_reverse_polarity_voltage(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P2] az
        IMPULSE_PREP korábban nem hívott check_battery_voltage()-t — egy
        negatív/fordított-polaritású OCV-olvasás csak "SKIPPED_LOW_VOLTAGE"-
        ként lett volna elkönyvelve begin_pulse()-ban FAULT helyett
        (CLAUDE.md §10.6/SAFE-009), ha a mérőcsatlakozás a megelőző
        STEP_RELAX/PRECHARGE_RELAX alatt lazult meg."""
        ctrl, _psu, load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.IMPULSE_PREP
        dmm_v.voltage_V = -1.0

        state = ctrl.advance(0.0)

        assert state == OcvSocState.FAULT
        assert "REVERSE_POLARITY" in ctrl.fault_reason
        assert not load.input_commanded_on


# ------------------------------------------------------------------ #
# TestOcvSocImpulse                                                    #
# ------------------------------------------------------------------ #

def _run_full_impulse(ctrl, max_ticks: int = 60) -> OcvSocState:
    """IMPULSE_ON → (IMPULSE_ACQUIRE chunkok) → LOG_SOC_POINT tick-sorozat.

    A PulseMeasurementService instrumentum-időzített chunkokban mintavételez
    (alapértelmezés: pulse_sample_interval_s=0.2, pulse_chunk_sample_count=5,
    impulse_duration_s=30.0 → 30 chunk). advance(0.0) — a dt_s a chunk-alapú
    architektúrában nem használt, a mintavétel idejét a DMM SAMP:TIM adja."""
    ctrl.advance(0.0)  # IMPULSE_ON → begin_pulse() (vagy közvetlenül LOG_SOC_POINT, ha skip)
    ticks = 0
    while ctrl.state == OcvSocState.IMPULSE_ACQUIRE and ticks < max_ticks:
        ctrl.advance(0.0)
        ticks += 1
    return ctrl.state


class TestOcvSocImpulse:
    def test_rb_calculation(self):
        """Rb számítás instrumentum-időzített chunk-acquisitionnel:
        v_before=13.5V, v(t)=13.2V minden mintán, i=1.4A → rb = 0.3/1.4 ≈ 0.214 Ohm."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        assert ctrl._impulse_current_A_set == pytest.approx(1.4)

        state = _run_full_impulse(ctrl)

        i_set = ctrl._impulse_current_A_set
        assert ctrl._rb_1s == pytest.approx((13.5 - 13.2) / i_set, rel=1e-3)
        assert ctrl._rb_10s == pytest.approx((13.5 - 13.2) / i_set, rel=1e-3)
        assert ctrl._rb_30s == pytest.approx((13.5 - 13.2) / i_set, rel=1e-3)
        assert state == OcvSocState.LOG_SOC_POINT

    def test_rb_raw_du_di_baseline_wired_from_real_impulse(self):
        """[regresszió, Kör 19 Batch D3a, F-1/DFAM-021] a fenti test_rb_
        calculation() csak a HÁNYADOST (rb_1s stb.) ellenőrzi -- ez a
        teszt bizonyítja, hogy `_finish_impulse_acquisition()` a VALÓS
        PulseAcquisitionResult-ból ténylegesen kitölti a dU/dI/baseline
        session-mezőket is (nem csak kézzel injektált értékekre működik,
        l. test_on_soc_point_persists_pulse_raw_du_di_and_baseline()),
        és hogy a hányados a nyers mezőkből rekonstruálható."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON

        state = _run_full_impulse(ctrl)
        assert state == OcvSocState.LOG_SOC_POINT

        assert ctrl._pulse_v_baseline_V == pytest.approx(13.5)
        assert ctrl._rb_1s_dU_V == pytest.approx(13.5 - 13.2, rel=1e-3)
        assert ctrl._rb_10s_dU_V == pytest.approx(13.5 - 13.2, rel=1e-3)
        assert ctrl._rb_30s_dU_V == pytest.approx(13.5 - 13.2, rel=1e-3)
        assert ctrl._rb_1s_dI_A is not None and ctrl._rb_1s_dI_A > 0
        assert ctrl._rb_1s == pytest.approx(
            ctrl._rb_1s_dU_V / ctrl._rb_1s_dI_A, rel=1e-3
        )

    def test_impulse_acquire_faults_on_concurrent_psu_load(self):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] az
        IMPULSE_ACQUIRE fázis korábban nem hívott check_concurrent_psu_
        load()-ot (CLAUDE.md §10.2) — az IMPULSE_PREP egyszer ellenőrzi Load
        bekapcsolása ELŐTT (2. kör javítás), de az akár 30s+ aktív pulse
        alatt korábban egyszer sem futott újra, szemben a testvér
        _run_step_discharge()-dzsel."""
        ctrl, psu, load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.5)
        dmm_v.timed_voltage_fn = lambda t: 12.3
        ctrl._state = OcvSocState.IMPULSE_ON
        ctrl.advance(0.0)  # -> IMPULSE_ACQUIRE (begin_pulse() sikeresen elindult)
        assert ctrl.state == OcvSocState.IMPULSE_ACQUIRE
        assert load.input_commanded_on is True  # a pulse alatt a Load ténylegesen aktív

        psu.output_commanded_on = True  # szimulált hibás relé/konkurrens PSU aktivitás
        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason

    def test_impulse_on_faults_on_load_power_limit_exceeded(self):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), Session/
        Workflow P1 lelet] a STEP_DISCHARGE (C/10) már megkapta a
        check_load_power()-t (4. kör), de az impulzus-fázisok kimaradtak —
        holott az impulzusáram C/5 (a STEP_DISCHARGE DUPLÁJA), nagyobb
        teljesítménykorlát-túllépési kockázattal. A Load bekapcsolása
        ELŐTT kell ellenőrizni (begin_pulse() rögtön bekapcsolná)."""
        ctrl, _psu, load, _dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.5)
        ctrl._state = OcvSocState.IMPULSE_ON
        ctrl._u_batt = 20.0
        ctrl._impulse_current_A_set = 15.0  # 20V * 15A = 300W > 250W max

        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "LOAD_POWER_LIMIT" in ctrl.fault_reason
        assert load.input_commanded_on is False  # a Load SOSEM kapcsolt be

    def test_impulse_on_turns_off_load_after_measurement(self):
        """Az impulzus befejezése után a terhelésnek ki kell kapcsolni."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.5)
        dmm_v.timed_voltage_fn = lambda t: 12.3
        ctrl._state = OcvSocState.IMPULSE_ON

        _run_full_impulse(ctrl)

        assert not load.input_commanded_on

    def test_pulse_timing_is_instrument_timed_not_python_tick(self):
        """SUP01-ACCEPT-001/6: a pulse idozitest a PulseMeasurementService (DMM
        SAMP:TIM chunkok) adja — dt_s=0.0 mellett is helyes t1/t10/t30 adódik,
        mert nem a hívó által átadott dt_s-ből, hanem az instrumentum saját,
        deklarált mintavételi intervallumából számolódik."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON

        _run_full_impulse(ctrl)

        assert ctrl._t1_actual_s == pytest.approx(1.0)
        assert ctrl._t10_actual_s == pytest.approx(10.0)
        assert ctrl._t30_actual_s == pytest.approx(30.0)

    def test_on_soc_point_callback_called(self):
        """LOG_SOC_POINT-ban az on_soc_point callbacket meg kell hívni."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)

        # Kézzel tesszük be a LOG_SOC_POINT állapotba, grid_pos=1 → 95%-os pont
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._last_ocv_V = 12.3
        ctrl._rb_1s = 0.010
        ctrl._rb_10s = 0.012
        ctrl._rb_30s = 0.013

        ctrl._run_log_soc_point()

        assert len(collected) == 1
        point = collected[0]
        assert point["soc_percent"] == pytest.approx(95.0)
        assert point["ocv_V"] == pytest.approx(12.3)
        # SUP01-TERM-003: mOhm egység, nem "rb_1s_ohm" — 0.010 Ohm = 10.0 mOhm
        assert point["R_pulse_actual_t1_mOhm"] == pytest.approx(10.0)

    def test_on_soc_point_persists_pulse_raw_du_di_and_baseline(self):
        """[regresszió, Kör 19 Batch D3a, F-1/DFAM-021 7. előfordulás] a
        R_pulse_actual_t*_mOhm = dU/dI hányados nyers bemenetei korábban
        SOHA nem jutottak el az ocv_soc_table.csv-be -- csak a végeredmény
        (hányados) naplózódott, a mérés utólag nem volt ellenőrizhető/
        rekonstruálható. Ez a teszt bizonyítja, hogy dU/dI/baseline
        MOST MÁR jelen van az on_soc_point payload-ban, ÉS hogy a
        `pulse_v_baseline_V` SZÁMSZERŰEN ELTÉR `ocv_V`-től (nem
        helyettesíthető vele -- más mintavételi időpontból származnak,
        l. __init__ mezőik kommentje)."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)

        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._last_ocv_V = 12.30  # PRECOND/_run_impulse_prep() mintavétele
        ctrl._rb_1s = 0.010
        ctrl._rb_10s = 0.012
        ctrl._rb_30s = 0.013
        ctrl._rb_1s_dU_V = 0.050
        ctrl._rb_10s_dU_V = 0.060
        ctrl._rb_30s_dU_V = 0.065
        ctrl._rb_1s_dI_A = 5.0
        ctrl._rb_10s_dI_A = 5.0
        ctrl._rb_30s_dI_A = 5.0
        ctrl._pulse_v_baseline_V = 12.45  # begin_pulse() KÜLÖN mintavétele

        ctrl._run_log_soc_point()

        assert len(collected) == 1
        point = collected[0]
        assert point["dU_t1_V"] == pytest.approx(0.050)
        assert point["dU_t10_V"] == pytest.approx(0.060)
        assert point["dU_t30_V"] == pytest.approx(0.065)
        assert point["dI_t1_A"] == pytest.approx(5.0)
        assert point["dI_t10_A"] == pytest.approx(5.0)
        assert point["dI_t30_A"] == pytest.approx(5.0)
        assert point["pulse_v_baseline_V"] == pytest.approx(12.45)
        # A lényegi állítás: a baseline NEM egyezik ocv_V-vel -- két külön
        # mintavételi időpontból származnak, összekeverésük hibás dU-t adna.
        assert point["pulse_v_baseline_V"] != pytest.approx(point["ocv_V"])
        # A hányados önmagából rekonstruálható a nyers bemenetekből:
        assert point["R_pulse_actual_t1_mOhm"] == pytest.approx(
            point["dU_t1_V"] / point["dI_t1_A"] * 1000.0
        )

    def test_on_soc_point_pulse_raw_fields_none_when_target_not_reached(self):
        """A dU/dI/baseline mezők a `reached` kapuval konzisztensen None-ok
        maradnak, ha egy célidőpont nem lett elérve -- ugyanaz a minta,
        mint a hányados (R_pulse_actual_t*_mOhm) saját None-állapota."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)

        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._last_ocv_V = 12.30
        # _rb_1s_dU_V/_dI_A alapértéke None (nincs impulzus futtatva)

        ctrl._run_log_soc_point()

        assert len(collected) == 1
        point = collected[0]
        assert point["dU_t1_V"] is None
        assert point["dI_t1_A"] is None
        assert point["R_pulse_actual_t1_mOhm"] is None

    def test_on_soc_point_relax_duration_is_actual_elapsed_not_setpoint(self):
        """Review-kör 5, Metrológia M5-05 (P3): korábban `relax_duration_s`
        a relax CÉL-időtartamát (`_current_relax_s`, a setpoint) jelentette,
        nem a ténylegesen mért relax-időt (`_relax_elapsed_s`) — a kettő
        eltérhet (pl. tick-granularitás miatt a tényleges idő mindig
        legalább a setpoint, gyakran valamivel több)."""
        ctrl, *_ = make_ocv_soc_controller(dmm_voltage_V=12.5, capacity_Ah=7.0)
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)

        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._last_ocv_V = 12.3
        # Szándékosan ELTÉRŐ setpoint/tényleges pár, hogy a két érték
        # összekeverése ne maradhasson észrevétlen.
        ctrl._current_relax_s = 18000.0
        ctrl._relax_elapsed_s = 18042.5

        ctrl._run_log_soc_point()

        point = collected[0]
        assert point["relax_duration_s"] == pytest.approx(18042.5)

    def test_next_target_advances_after_log(self):
        """LOG_SOC_POINT után a soc_index (a legutóbb naplózott pont) VÁLTOZATLAN
        marad — a grid-pozíció csak a következő kisütési lépés SIKERES befejezése
        után lép tovább (SUP01-GRID-005: current_logged != next_target).
        A next_target_soc_percent viszont már a következő célra mutat."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1  # 95%
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._run_log_soc_point()
        assert ctrl.soc_index == pytest.approx(95.0)
        assert ctrl.next_target_soc_percent == pytest.approx(90.0)
        assert ctrl.state == OcvSocState.STEP_DISCHARGE

    def test_not_done_after_second_to_last_point_logged(self):
        """SUP01-GRID-006: az utolsó előtti pont naplózása UTÁN még NEM DONE —
        a valódi végpontot (a racs utolsó eleme, uniform módban 0%) is le kell
        még mérni, mielőtt a teszt befejeződhet (off-by-one/végpont javítás)."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = len(ctrl._grid) - 2  # utolsó előtti pont (uniform: 5.0)
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._run_log_soc_point()
        assert ctrl.state == OcvSocState.STEP_DISCHARGE
        # a grid_pos csak a kisütés SIKERES befejezésekor lép a végpontra;
        # a cél viszont már a racs utolsó (valódi 0%) eleme
        assert ctrl.next_target_soc_percent == pytest.approx(ctrl._grid[-1])

    def test_done_only_after_final_grid_point_logged(self):
        """DONE csak a racs UTOLSÓ (valódi teszt-végpont) pontjának naplózása után."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = len(ctrl._grid) - 1  # már a végponton állunk
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

    def test_temp_dmm_failure_during_step_discharge_faults(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P1] a
        _temp_dmm_fault_s számláló _read_dmm()-ben MÁR nőtt, de sosem került
        kiértékelésre advance()-ben — egy hőmérséklet-DMM kiesés a lépcsőzött
        kisütés alatt teljesen észrevétlen maradt volna (CLAUDE.md §10.4).
        Ugyanaz a minta, mint golden_gg_controller.py-ben."""
        ctrl, _psu, _load, _dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState.STEP_DISCHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(70.0)  # MONITOR_ONLY timeout=60s, egy tick alatt túllépve

        assert ctrl.state == OcvSocState.FAULT
        assert "TEMPERATURE" in ctrl.fault_reason

    def test_temp_dmm_warning_during_step_discharge_sets_last_warning_code(self):
        """[regresszió, Teljes kódbázis review 9. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] last_warning_code korábban teljesen hiányzott
        OcvSocController-en — a MONITOR_ONLY grace-ablakban (fault előtt) a
        hőmérséklet-DMM figyelmeztetés sehol nem jelent meg, test_runner.py
        warning_flags mezője mindig None maradt."""
        ctrl, _psu, _load, _dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState.STEP_DISCHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 10s < 60s timeout, WARNING, nem FAULT

        assert ctrl.state != OcvSocState.FAULT
        assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"

    def test_temp_dmm_fault_ticks_counts_failed_reads_not_seconds(self):
        """[regresszió, review-kör 3 (2026-09-09), friss Metrológia M3-02 /
        Szoftver/Architektúra F2 (P2)] Az OcvSocController korábban
        hiányzott a temp_dmm_fault_ticks property/számlálóból, amely a
        chemid/golden_gg/traditional_learning testvéreken már megvolt —
        a main_window.py temp_dmm_fault_ticks_total összege és a
        capacity_result_quality DEGRADED-gate emiatt sosem látta az
        OCV_SOC session alatti hőmérséklet-DMM-kiesést. Tick-alapú (nem
        másodperc-alapú), a chemid_controller.py azonos tesztjének
        mintáját követve."""
        ctrl, _psu, _load, _dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState.STEP_DISCHARGE
        assert ctrl.temp_dmm_fault_ticks == 0

        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 1 sikertelen tick, WARNING-küszöb alatt
        assert ctrl.temp_dmm_fault_ticks == 1

        ctrl.advance(5.0)  # 2. sikertelen tick, MÉG mindig WARNING alatt
        assert ctrl.temp_dmm_fault_ticks == 2

        dmm_t.simulate_temp_failure = False
        ctrl.advance(1.0)  # sikeres olvasás -- a tick-számláló NEM nullázódik
        assert ctrl.temp_dmm_fault_ticks == 2

    def test_temp_dmm_valid_true_after_successful_read(self):
        """[regresszió, MAX verifier (review-kör 3, a9cb161469c468fc8) P2-1]
        a temp_dmm_valid property (Metrológia M3-01, P1) egyik
        controlleren sem kapott önálló, controller-szintű regressziós
        tesztet -- ez a hiány engedte át a chemid_controller.py-beli P1-1
        blokkoló hibát 1792 zöld teszt mellett."""
        ctrl, _psu, _load, _dmm_v, _dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_after_failed_read(self):
        ctrl, _psu, _load, _dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState.STEP_DISCHARGE
        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_valid_true_delegated_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a3f0e6dcf93588360) P2-1]
        a self._dmm_valid (feszültség) delegálás a beágyazott
        ChargeControllerből mindig megtörtént PRECHARGE alatt, de a
        self._temp_dmm_valid nem -- egy ott ténylegesen hibázó
        hőmérséklet-DMM a szülő szintjén hamisan True-t jelentett volna,
        mert a saját _read_dmm() ebben az állapotban ki van kapcsolva."""
        ctrl, *_ = make_ocv_soc_controller()
        state = ctrl.advance(0.0)
        assert state == OcvSocState.PRECHARGE

        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_delegated_from_embedded_charge_controller(self):
        ctrl, _psu, _load, _dmm_v, dmm_t = make_ocv_soc_controller()
        state = ctrl.advance(0.0)
        assert state == OcvSocState.PRECHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_fault_ticks_live_add_and_carry_over_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a306e7e764e3a717b) P2-1,
        DFAM-001 további előfordulása] l. test_chemid_controller.py azonos
        tesztjének docstringje ugyanerről a hiányzó live-add + carry-over
        mintáról."""
        from Prog.src.charge_controller import ChargeState
        ctrl, _psu, _load, _dmm_v, dmm_t = make_ocv_soc_controller()
        state = ctrl.advance(0.0)
        assert state == OcvSocState.PRECHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_fault_ticks == 2

        dmm_t.simulate_temp_failure = False
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)  # PRECHARGE -> PRECHARGE_RELAX
        assert ctrl._charge_ctrl is None
        assert ctrl.temp_dmm_fault_ticks == 2


# ------------------------------------------------------------------ #
# TestOcvSocPrechargeSingleDmmRead                                     #
# Fix: dmm_feedback_lost_hibaelemzes_20260628.txt — kettős DMM-olvasás #
# egy ticken belül PRECHARGE alatt hamis DMM_FEEDBACK_LOST-ot okozott. #
# ------------------------------------------------------------------ #

class TestOcvSocPrechargeSingleDmmRead:
    def test_precharge_tick_reads_dmm_voltage_exactly_once(self):
        """PRECHARGE ticken csak a belső ChargeController olvassa a DMM-et."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl.advance(0.0)  # INIT → PRECHARGE
        assert ctrl.state == OcvSocState.PRECHARGE
        dmm_v.reset_calls()

        ctrl.advance(1.0)  # PRECHARGE tick

        assert dmm_v.calls.count("read_voltage()") == 1

    def test_precharge_survives_transient_two_reads_worth_of_jump(self):
        """Egy tick alatti nagy feszültségváltozás (korábban két olvasás közt
        detektálható lett volna) most nem okoz hamis faultot, mert csak egyszer
        olvasunk — a jump-detektor a valós driverben van, itt a MockDMM csak azt
        igazolja, hogy a state-machine nem hívja duplán a DMM-et."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(dmm_voltage_V=13.0)
        ctrl.advance(0.0)
        dmm_v.voltage_V = 15.9  # nagy ugrás — csak 1x olvasva nem eredményez emergency_stop-ot itt
        state = ctrl.advance(1.0)
        assert state != OcvSocState.FAULT

    def test_precharge_still_faults_on_real_dmm_loss(self):
        """Regresszió: valódi DMM-kiesés PRECHARGE alatt továbbra is FAULT-ot ad."""
        from Prog.src.charge_controller import ChargeState
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.PRECHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.PRECHECK
        dmm_v.set_dmm_invalid()

        ctrl.advance(1.0)

        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"

    def test_precharge_syncs_u_batt_from_charge_controller(self):
        """A saját _read_dmm kihagyása után is friss _u_batt kell (ChargeControllerből szinkronizálva)."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(dmm_voltage_V=13.2)
        ctrl.advance(0.0)
        dmm_v.voltage_V = 13.7
        ctrl.advance(1.0)
        assert ctrl._u_batt == pytest.approx(13.7)


# ------------------------------------------------------------------ #
# TestOcvSocImpulsePrepSingleDmmRead                                    #
# [regresszió, Teljes kódbázis review 7. kör (2026-08-30), Session/    #
# Workflow P2 lelet #1] _run_impulse_prep() korábban egy MÁSODIK,      #
# önálló read_voltage()-t indított, holott advance() MÁR olvasta a     #
# feszültség-DMM-et ebben a tickben (IMPULSE_PREP nincs a PRECHARGE/   #
# IMPULSE_*-kizárási listán) — ugyanaz a mintázat, mint a fenti         #
# TestOcvSocPrechargeSingleDmmRead osztály PRECHARGE-re.               #
# ------------------------------------------------------------------ #

class TestOcvSocImpulsePrepSingleDmmRead:
    def test_impulse_prep_tick_reads_dmm_voltage_exactly_once(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=13.0)
        ctrl._state = OcvSocState.IMPULSE_PREP
        dmm_v.reset_calls()

        ctrl.advance(0.0)

        assert dmm_v.calls.count("read_voltage()") == 1

    def test_impulse_prep_last_ocv_v_matches_the_single_read(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.34)
        ctrl._state = OcvSocState.IMPULSE_PREP

        ctrl.advance(0.0)

        assert ctrl._last_ocv_V == pytest.approx(12.34)

    def test_impulse_prep_still_faults_on_real_dmm_loss(self):
        """Regresszió: valódi DMM-kiesés IMPULSE_PREP alatt továbbra is
        FAULT-ot ad, most már az EGYETLEN (tick-eleji) olvasás sikertelensége
        alapján, nem egy második, redundáns olvasásén."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=13.0)
        ctrl._state = OcvSocState.IMPULSE_PREP
        dmm_v.set_dmm_invalid()

        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"


# ------------------------------------------------------------------ #
# TestOcvSocSup01GridPolicy                                            #
# SUP01-GRID-001/002: TI-aligned és uniform racs, konfigurálható.      #
# ------------------------------------------------------------------ #

class TestOcvSocSup01GridPolicy:
    def test_default_grid_mode_is_uniform_backward_compatible(self):
        """Alapértelmezés UNIFORM_SOC_STEP marad — nincs váratlan viselkedésváltás
        a meglévő projekteknél (a TI-aligned mód opt-in, l. gap-mátrix nyitott pont)."""
        cfg = OcvSocConfig()
        assert cfg.grid_mode == "UNIFORM_SOC_STEP"

    def test_ti_aligned_grid_has_15_points_in_spec_order(self):
        from Prog.src.ocv_soc_controller import TI_ALIGNED_15_POINT_GRID
        cfg = OcvSocConfig(grid_mode="TI_ALIGNED_15_POINT")
        ctrl, *_ = make_ocv_soc_controller(config=cfg)
        assert ctrl._grid == TI_ALIGNED_15_POINT_GRID
        assert len(ctrl._grid) == 15
        assert ctrl._grid[0] == pytest.approx(100.0)
        assert ctrl._grid[-1] == pytest.approx(0.2)
        assert ctrl._grid == tuple(sorted(ctrl._grid, reverse=True))

    def test_uniform_grid_10_percent_step(self):
        cfg = OcvSocConfig(grid_mode="UNIFORM_SOC_STEP", step_percent=10.0)
        ctrl, *_ = make_ocv_soc_controller(config=cfg)
        assert ctrl._grid == (100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 10.0, 0.0)

    def test_uniform_grid_5_percent_step_ends_exactly_at_zero(self):
        cfg = OcvSocConfig(grid_mode="UNIFORM_SOC_STEP", step_percent=5.0)
        ctrl, *_ = make_ocv_soc_controller(config=cfg)
        assert ctrl._grid[-1] == 0.0
        assert len(ctrl._grid) == 21

    def test_invalid_grid_mode_raises(self):
        with pytest.raises(ValueError):
            OcvSocConfig(grid_mode="BOGUS_MODE")


# ------------------------------------------------------------------ #
# TestOcvSocSup01PulseAh                                               #
# SUP01-PULSE-009/TEST-002: a pulse alatt eltávolított Ah a SOC-ba     #
# számít, különben a racs fokozatosan elcsúszna.                      #
# ------------------------------------------------------------------ #

class TestOcvSocSup01PulseAh:
    def test_pulse_ah_accumulates_during_impulse_wait(self):
        """Teljes 30s pulse, konstans 1.4A → removed_Ah = 1.4A × 30s/3600.

        R1 B8a (F-21, D-B8-8) fixture-korrekció, az elvárás VÁLTOZATLAN: a
        FakeMonotonicClock az új OFF-nyugta horgony lekérdezésére is egy
        teljes chunkidőt (1,0 s) léptetne -- nem létező terhelési idő. Az
        InstrumentLatencyClock nulla latenciával csak a DMM-fetch-ben lép
        (l. test_pulse_measurement_service.py azonos fixture-korrekcióját)."""
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.0, capacity_Ah=7.0, pulse_time_source=clock,
        )
        bind_pulse_instrument_latency(clock, dmm_v, load)
        dmm_v.timed_voltage_fn = lambda t: 12.9
        ctrl._state = OcvSocState.IMPULSE_ON
        _run_full_impulse(ctrl)
        assert ctrl._impulse_removed_Ah == pytest.approx(1.4 * 30.0 / 3600.0, rel=1e-3)

    def test_pulse_ah_folds_into_removed_ah_total_at_log(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._removed_Ah_total = 1.0
        ctrl._impulse_removed_Ah = 0.05
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()
        assert ctrl.removed_Ah_total == pytest.approx(1.05)
        # a következő lépés cél-Ah-ja már a frissített (pulse-szal növelt) összeghez
        # viszonyítva számolódik — enélkül a racs fokozatosan elcsúszna.
        assert ctrl._impulse_removed_Ah == 0.0


# ------------------------------------------------------------------ #
# TestOcvSocSup01CutoffDuringPulse                                     #
# SUP01-PULSE-010/TEST-005: cutoff pulse közben → azonnali Load OFF.  #
# ------------------------------------------------------------------ #

class TestOcvSocSup01CutoffDuringPulse:
    def test_cutoff_during_pulse_immediate_load_off(self):
        """A feszültség az első chunk (t<=1.0s) VÉGÉN a terminate voltage
        (11.4V) alá esik — azonnali Load OFF, nincs a teljes 30s-ig várakozás."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.0, capacity_Ah=7.0
        )
        _set_pulse_test_cutoff(ctrl)
        dmm_v.timed_voltage_fn = lambda t: 13.0 if t < 1.0 else 11.0
        ctrl._state = OcvSocState.IMPULSE_ON

        state = _run_full_impulse(ctrl)

        assert not load.input_commanded_on
        assert state == OcvSocState.LOG_SOC_POINT
        assert ctrl._cutoff_during_pulse is True

    def test_cutoff_during_pulse_does_not_force_full_30s(self):
        """A mérést nem szabad a teljes időtartamig kényszeríteni — az abort
        azonnal az első (cutoff-ot tartalmazó) chunk után történjen, nem az
        összes chunk lefutása után."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.0, capacity_Ah=7.0
        )
        _set_pulse_test_cutoff(ctrl)
        dmm_v.timed_voltage_fn = lambda t: 11.0  # azonnal a terminate alatt
        ctrl._state = OcvSocState.IMPULSE_ON

        ctrl.advance(0.0)   # IMPULSE_ON → IMPULSE_ACQUIRE
        ctrl.advance(0.0)   # 1. chunk → cutoff → LOG_SOC_POINT

        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        assert ctrl._t30_actual_s is None  # nincs érvényes 30s eredmény

    def test_cutoff_during_pulse_quality_flag_in_output(self):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.0, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        _set_pulse_test_cutoff(ctrl)
        dmm_v.timed_voltage_fn = lambda t: 11.0
        ctrl._state = OcvSocState.IMPULSE_ON

        _run_full_impulse(ctrl)
        ctrl.advance(0.0)  # LOG_SOC_POINT ténylegesen feldolgozva

        assert collected[-1]["measurement_quality"] == "CUTOFF_DURING_PULSE"

    def test_cutoff_during_pulse_resistance_is_none_not_zero(self):
        """Governance-v2 review-kör 1 (Metrológia reviewer, CF-23/MET-R1-06,
        DFAM-024 optional_measurement_collapsed_to_zero): egy nem-elért
        (cutoff-olt) pontnál az `or 0.0` sentinel korábban egy fizikailag
        lehetetlen, de valós mérésnek látszó 0.0 mOhm-ot írt volna a
        `R_pulse_actual_t1_mOhm` mezőbe — ugyanabban a sorban, ahol a
        testvér `t1_actual_s` helyesen `None` marad. Mostantól mindkettő
        `None`."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.0, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        _set_pulse_test_cutoff(ctrl)
        dmm_v.timed_voltage_fn = lambda t: 11.0
        ctrl._state = OcvSocState.IMPULSE_ON

        _run_full_impulse(ctrl)
        ctrl.advance(0.0)  # LOG_SOC_POINT ténylegesen feldolgozva

        point = collected[-1]
        assert point["R_pulse_actual_t1_mOhm"] is None
        assert point["R_pulse_actual_t10_mOhm"] is None
        assert point["R_pulse_actual_t30_mOhm"] is None


# ------------------------------------------------------------------ #
# TestOcvSocSup01SkippedLowVoltage                                     #
# SUP01-PULSE-010: "0% vagy 0.2% ponton a pulse SKIPPED_LOW_VOLTAGE    #
# lehet" — ha a feszültség MÁR a pulse indítása előtt a terminate      #
# alatt van, a terhelés el sem indul.                                  #
# ------------------------------------------------------------------ #

class TestOcvSocSup01SkippedLowVoltage:
    def test_pulse_skipped_when_already_below_terminate(self):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=11.0, capacity_Ah=7.0  # terminate 11.4V alatt
        )
        _set_pulse_test_cutoff(ctrl)
        ctrl._state = OcvSocState.IMPULSE_ON

        state = ctrl.advance(0.0)

        assert state == OcvSocState.LOG_SOC_POINT
        assert ctrl._skipped_low_voltage is True
        assert not load.input_commanded_on

    def test_skipped_low_voltage_quality_flag_in_output(self):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=11.0, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        _set_pulse_test_cutoff(ctrl)
        ctrl._state = OcvSocState.IMPULSE_ON
        ctrl.advance(0.0)   # → LOG_SOC_POINT (skipped)
        ctrl.advance(0.0)   # feldolgozva

        assert collected[-1]["measurement_quality"] == "SKIPPED_LOW_VOLTAGE"


# ------------------------------------------------------------------ #
# TestOcvSocSup01MeasuredCurrent                                       #
# SUP01-PULSE-007/008/TEST-004: mért DeltaI elsődleges, setpoint csak  #
# degraded fallback.                                                   #
# ------------------------------------------------------------------ #

class TestOcvSocSup01MeasuredCurrent:
    def test_pulse_resistance_uses_measured_current_not_setpoint(self):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        ctrl.advance(0.0)  # begin_pulse(): load.current_A = impulse_current_A_set = 1.4A
        assert ctrl._impulse_current_A_set == pytest.approx(1.4)
        assert ctrl.state == OcvSocState.IMPULSE_ACQUIRE
        load.current_A = 1.0  # mért ≠ setpoint (szimulált eltérés), minden chunk-nál ez olvasódik

        while ctrl.state == OcvSocState.IMPULSE_ACQUIRE:
            ctrl.advance(0.0)

        assert ctrl._rb_1s == pytest.approx((13.5 - 13.2) / 1.0, rel=1e-3)

    def test_pulse_current_degraded_on_measure_failure(self):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        ctrl.advance(0.0)
        load.simulate_current_readback_failure = True

        while ctrl.state == OcvSocState.IMPULSE_ACQUIRE:
            ctrl.advance(0.0)

        assert ctrl._pulse_current_degraded is True
        assert ctrl._rb_1s == pytest.approx(
            (13.5 - 13.2) / ctrl._impulse_current_A_set, rel=1e-3
        )

    def test_degraded_quality_reflected_in_output(self):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        ctrl.advance(0.0)
        load.simulate_current_readback_failure = True

        while ctrl.state == OcvSocState.IMPULSE_ACQUIRE:
            ctrl.advance(0.0)
        ctrl.advance(0.0)    # LOG_SOC_POINT feldolgozva

        assert collected[-1]["measurement_quality"] == "DEGRADED_SETPOINT_CURRENT"
        # [regresszió, Kör 17 MAX-effort review, R17-13, DFAM-021+DFAM-003+
        # DFAM-001] a pulse_current_degraded jelző korábban csak a fenti,
        # priority-sorrendben ELSŐNEK kiértékelt measurement_quality
        # sztringen keresztül volt látható -- saját, dedikált CSV-oszlopa
        # nem volt, ellentétben a fast_transient_current_degraded
        # testvérrel. Most már saját kulcsa is van.
        assert collected[-1]["pulse_current_degraded"] is True

    def test_pulse_current_degraded_column_survives_higher_priority_quality_masking(self):
        """[regresszió, Kör 17 MAX-effort review, R17-13] Ha egy MAGASABB
        prioritású jelző (itt: cutoff_during_pulse) is be van állítva,
        _determine_point_quality() a measurement_quality mezőben ezt adja
        vissza (nem a DEGRADED_SETPOINT_CURRENT-et) -- de a
        pulse_current_degraded SAJÁT oszlopa ettől függetlenül True marad,
        nem "nyelődik el" a magasabb prioritású minősítés mögött. Ez a
        pontos ok, amiért a jelzőnek saját, dedikált CSV-oszlopra van
        szüksége a measurement_quality-n túl."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        ctrl.advance(0.0)
        load.simulate_current_readback_failure = True

        while ctrl.state == OcvSocState.IMPULSE_ACQUIRE:
            ctrl.advance(0.0)

        assert ctrl._pulse_current_degraded is True
        ctrl._cutoff_during_pulse = True  # magasabb prioritasu jelzo, kezzel szimulalva

        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        ctrl.advance(0.0)  # LOG_SOC_POINT feldolgozva

        assert collected[-1]["measurement_quality"] == "CUTOFF_DURING_PULSE"
        assert collected[-1]["pulse_current_degraded"] is True

    def test_baseline_current_degraded_reflected_in_quality(self):
        """[regresszió, Teljes kódbázis review 20. kör (2026-09-06),
        Metrológia/Adatintegritás P2 lelet] a PulseMeasurementService
        baseline_current_degraded jelzőjét _finish_impulse_acquisition()
        korábban sosem olvasta ki — _determine_point_quality() ezt az
        ágat kihagyta, "OK" minősítést adhatott egy olyan R_1s/10s/30s
        pontra, aminek a ΔI-je feltételezett, nem mért bázisáramot
        használt. Jelenleg a pulse_baseline_window_s alapértéke 0.0
        mindenhol (GUI-n keresztül nem elérhető), ezért ez a teszt
        explicit, a hatókör hatását igazoló config-értéket állít be."""
        cfg = OcvSocConfig(
            step_percent=5.0, discharge_rate_divisor=10, relax_default_s=7200.0,
            relax_keypoint_s=18000.0, impulse_current_rate_divisor=5,
            impulse_duration_s=30.0, pulse_baseline_window_s=1.0,
        )
        ctrl, _psu, load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0, config=cfg,
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        load.simulate_current_readback_failure = True
        ctrl.advance(0.0)  # _run_impulse_on() -> begin_pulse() -> baseline capture hibázik
        load.simulate_current_readback_failure = False  # a pulzus-áram mérése ettől kezdve sikeres

        while ctrl.state == OcvSocState.IMPULSE_ACQUIRE:
            ctrl.advance(0.0)

        # Az ellenőrzés a LOG_SOC_POINT-feldolgozó tick ELŐTT történik — az
        # a tick a következő pontra készülve resetelné a jelzőket (l. a
        # per-pont reset blokk _baseline_current_degraded = False sora).
        assert ctrl._baseline_current_degraded is True
        assert ctrl._pulse_current_degraded is False

        ctrl.advance(0.0)    # LOG_SOC_POINT feldolgozva

        assert collected[-1]["measurement_quality"] == "DEGRADED_BASELINE_CURRENT_ASSUMED"
        assert collected[-1]["baseline_current_degraded"] is True


# ------------------------------------------------------------------ #
# TestOcvSocSup01CapacityBasisAndMetadata                              #
# SUP01-GRID-003/DATA-001, SUP01-TERM-002/003, SUP01-PULSE-005/006     #
# ------------------------------------------------------------------ #

class TestOcvSocSup01CapacityBasisAndMetadata:
    def test_measured_capacity_quality_flag(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0, measured_capacity_Ah=6.5)
        assert ctrl.capacity_basis_quality == "MEASURED_CAPACITY_BASED"
        assert ctrl.capacity_basis_Ah == pytest.approx(6.5)

    def test_nominal_capacity_quality_flag(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0, measured_capacity_Ah=None)
        assert ctrl.capacity_basis_quality == "NOMINAL_CAPACITY_BASED_SOC"
        assert ctrl.capacity_basis_Ah == pytest.approx(7.0)

    def test_output_includes_method_origin_and_sync_quality(self):
        """SUP01-PULSE-001/005 (WP-AK-04): a projekt-mérnöki módszer és az
        instrumentum-időzített PulseMeasurementService szinkronizációs
        minősége explicit jelölve legyen a kimeneten."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()

        point = collected[0]
        assert point["method_origin"] == "PROJECT_ENGINEERING_METHOD"
        assert point["synchronization_quality"] == "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
        assert point["capacity_basis_quality"] == "NOMINAL_CAPACITY_BASED_SOC"
        assert point["grid_mode"] == "UNIFORM_SOC_STEP"

    def test_pulse_duration_set_and_actual_s_reach_output(self):
        """Review-kör 5, Metrológia M5-03 (P2, DFAM-021 computed_but_never_
        persisted): a PulseMeasurementService MÁR kiszámolja a tényleges
        impulzus-terhelési időt (pulse_duration_actual_s) és a konfigurált
        célt (pulse_duration_set_s), de ez korábban sosem jutott el az
        on_soc_point kimenetre — a controller a PulseAcquisitionResult
        ezen két mezőjét egyszerűen nem olvasta ki."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.0, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        ctrl._state = OcvSocState.IMPULSE_ON

        _run_full_impulse(ctrl)
        ctrl.advance(0.0)  # LOG_SOC_POINT ténylegesen feldolgozva

        point = collected[-1]
        assert point["pulse_duration_set_s"] == pytest.approx(ctrl._config.impulse_duration_s)
        assert point["pulse_duration_actual_s"] is not None
        assert point["pulse_duration_actual_s"] > 0.0

    def test_output_ocv_semantic_type_is_accelerated_lab_ocv(self):
        """GAP-OLD-007: a 2h/5h relax NEM azonos a FIAMM 24 órás reference OCV-vel
        — a kimenet explicit ezt jelölje, ne csak sima 'ocv_V'-t adjon."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()

        point = collected[0]
        assert point["ocv_semantic_type"] == "ACCELERATED_LAB_OCV"

    def test_output_uses_r_pulse_actual_naming_not_rb(self):
        """SUP01-TERM-003: nem egyetlen általános 'Rb' mező — időfüggő elnevezés
        kötelező, és mOhm egységben (SUP01-SRC-008)."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._rb_1s = 0.020
        ctrl._run_log_soc_point()

        point = collected[0]
        assert "R_pulse_actual_t1_mOhm" in point
        assert "R_pulse_actual_t10_mOhm" in point
        assert "R_pulse_actual_t30_mOhm" in point
        assert point["R_pulse_actual_t1_mOhm"] == pytest.approx(20.0)
        assert "rb_1s_ohm" not in point

    def test_actual_timestamp_fields_present_after_impulse(self):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        _run_full_impulse(ctrl)
        assert ctrl._t1_actual_s == pytest.approx(1.0)
        assert ctrl._t10_actual_s == pytest.approx(10.0)
        assert ctrl._t30_actual_s == pytest.approx(30.0)

    def test_no_ti_ra_or_golden_image_export_field(self):
        """SUP01-TERM-002/DATA-004: tilos TI Ra vagy golden image mezőt/nevet
        automatikusan kiadni a labor pulse-tábla adataiból."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()

        point = collected[0]
        forbidden_keys = {"ti_ra", "ra_table", "golden_image", "Cell0_Ra", "R_a0"}
        assert not (forbidden_keys & set(point.keys()))


# ------------------------------------------------------------------ #
# TestOcvSocRbPulseSpecPassthrough                                    #
# RB Pulse Digitalizálás spec (Folyamatok/tervek/RB_Pulse_Digitalizalas_ #
# Fejlesztesi_Spec_v1_2026-08.md): a config új mezőit a                #
# PulseMeasurementService konstruktorának kell megkapnia, VÁLTOZATLAN  #
# alapértékek mellett (opt-in, backward-compatible).                  #
# ------------------------------------------------------------------ #

class TestOcvSocRbPulseSpecPassthrough:
    def test_default_config_uses_instrument_timed_trigger_mode(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        assert ctrl._pulse_service._trigger_mode == "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"

    def test_default_config_has_no_baseline_window_or_recovery(self):
        """A meglévő projekteknél a baseline/recovery capture nem aktiválódhat
        magától — csak explicit config esetén."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        assert ctrl._pulse_service._baseline_window_s == 0.0
        assert ctrl._pulse_service._recovery_duration_s == 0.0

    def test_hardware_pulse_trigger_mode_reaches_pulse_service(self):
        cfg = OcvSocConfig(
            step_percent=5.0, discharge_rate_divisor=10,
            relax_default_s=7200.0, relax_keypoint_s=18000.0,
            impulse_current_rate_divisor=5, impulse_duration_s=30.0,
            pulse_trigger_mode="HARDWARE_PULSE_SOFTWARE_TRIGGERED",
        )
        from Prog.src.pulse_measurement_service import PulseDischargeError
        with pytest.raises(PulseDischargeError, match="HARDWARE_PULSE_HIL_PENDING"):
            make_ocv_soc_controller(capacity_Ah=7.0, config=cfg)

    def test_baseline_and_recovery_config_reach_pulse_service(self):
        cfg = OcvSocConfig(
            step_percent=5.0, discharge_rate_divisor=10,
            relax_default_s=7200.0, relax_keypoint_s=18000.0,
            impulse_current_rate_divisor=5, impulse_duration_s=30.0,
            pulse_baseline_window_s=2.0, pulse_recovery_duration_s=5.0,
        )
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0, config=cfg)
        assert ctrl._pulse_service._baseline_window_s == pytest.approx(2.0)
        assert ctrl._pulse_service._recovery_duration_s == pytest.approx(5.0)

    def test_result_passthrough_fields_present_with_default_config(self):
        """Alapértelmezett configgal is jelen kell lenniük az új mezőknek a
        callback payloadban, csak semleges (None/False/üres) értékkel — a
        meglévő fogyasztók ezeket figyelmen kívül hagyhatják."""
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        _run_full_impulse(ctrl)
        ctrl.advance(0.0)  # LOG_SOC_POINT tick -> callback

        point = collected[0]
        assert point["pulse_trigger_mode"] == "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
        assert point["measured_baseline_current_A"] is None
        assert point["current_trace_incomplete"] is False
        assert point["voltage_trace_incomplete"] is False
        assert point["recovery_sample_count"] == 0
        assert point["fast_transient_R_mOhm"] == {}
        assert point["fast_transient_synchronization_quality"] is None


# ------------------------------------------------------------------ #
# TestOcvSocTraceIncompleteQuality                                    #
# RB Pulse Digitalizálás spec: HARDWARE_PULSE módban post-hoc derülhet #
# ki, hogy a fetch-elt trace nem fedte le a teljes pulse-időt.        #
# ------------------------------------------------------------------ #

class TestOcvSocTraceIncompleteQuality:
    def test_current_trace_incomplete_quality_code(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._current_trace_incomplete = True
        assert ctrl._determine_point_quality() == "CURRENT_TRACE_INCOMPLETE"

    def test_voltage_trace_incomplete_quality_code(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._voltage_trace_incomplete = True
        assert ctrl._determine_point_quality() == "VOLTAGE_TRACE_INCOMPLETE"

    def test_cutoff_takes_precedence_over_trace_incomplete(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._cutoff_during_pulse = True
        ctrl._current_trace_incomplete = True
        assert ctrl._determine_point_quality() == "CUTOFF_DURING_PULSE"

    def test_flags_reset_after_log_soc_point(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._current_trace_incomplete = True
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()
        assert ctrl._current_trace_incomplete is False


class TestPointQualityCountsProperty:
    """Kor 18, Batch C12 (METRO-P2-1, DFAM-021, R17-12 mintajat koveteve):
    a _determine_point_quality() minden ponton kiszamolodik es az
    ocv_soc_table.csv-be el is jut, DE session-szintu osszesites nelkul
    a report.json/befejezes-popup korabban SOHA nem latta."""

    def test_empty_by_default(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.point_quality_counts == {}

    def test_ok_point_increments_ok_count(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()
        assert ctrl.point_quality_counts == {"OK": 1}

    def test_degraded_point_increments_its_own_key(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._current_trace_incomplete = True
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()
        assert ctrl.point_quality_counts == {"CURRENT_TRACE_INCOMPLETE": 1}

    def test_counts_accumulate_across_multiple_points_and_do_not_reset(self):
        """A per-pont minositő flagek (l. test_flags_reset_after_log_soc_
        point) minden ponton nullazodnak, DE a session-szintu osszesito
        NEM -- ugyanaz az elv, mint relax_dmm_fault_total-nal."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()  # OK pont

        ctrl._current_trace_incomplete = True
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 2
        ctrl._run_log_soc_point()  # degradált pont

        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 3
        ctrl._run_log_soc_point()  # ismét OK

        assert ctrl.point_quality_counts == {"OK": 2, "CURRENT_TRACE_INCOMPLETE": 1}

    def test_property_returns_a_copy_not_the_internal_dict(self):
        """A property ne engedje a hivonak veletlenul mutalni a belso
        allapotot (ugyanaz a minta, mint warning_code_counts/dict(...))."""
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()
        counts = ctrl.point_quality_counts
        counts["OK"] = 999
        assert ctrl.point_quality_counts == {"OK": 1}


# ------------------------------------------------------------------ #
# TestOcvSocFastTransientLayer                                        #
# RB Pulse Digitalizálás spec §6.3/§12/§22: FAST_TRANSIENT_            #
# CHARACTERIZATION — opcionális, ms-tartományú diagnosztikai réteg a   #
# standard (1/10/30s) pulse UTÁN. Alapértelmezésben KIKAPCSOLVA.       #
# ------------------------------------------------------------------ #

def _run_until(ctrl, target_states, max_ticks: int = 80):
    """advance()-ol, amíg az állapot valamelyik target_states-be nem ér."""
    seen = []
    ticks = 0
    while ctrl.state not in target_states and ticks < max_ticks:
        seen.append(ctrl.state)
        ctrl.advance(0.0)
        ticks += 1
    return seen


class TestOcvSocFastTransientLayer:
    def _make_fast_enabled_controller(self):
        cfg = OcvSocConfig(
            step_percent=5.0, discharge_rate_divisor=10,
            relax_default_s=7200.0, relax_keypoint_s=18000.0,
            impulse_current_rate_divisor=5, impulse_duration_s=30.0,
            fast_transient_enabled=True,
            fast_transient_chunk_sample_count=1,
            fast_transient_sample_interval_s=0.6,
            fast_transient_duration_s=1.2,
            fast_transient_target_times_s=(0.6, 1.2),
            fast_transient_time_tolerance_s=0.01,
        )
        return make_ocv_soc_controller(dmm_voltage_V=13.5, capacity_Ah=7.0, config=cfg)

    def test_fast_transient_disabled_by_default(self):
        assert OcvSocConfig().fast_transient_enabled is False

    def test_default_config_never_builds_fast_pulse_service(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        assert ctrl._fast_pulse_service is None

    def test_default_config_standard_pulse_goes_straight_to_log(self):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        state = _run_full_impulse(ctrl)
        assert state == OcvSocState.LOG_SOC_POINT

    def test_fast_transient_states_run_after_standard_pulse(self):
        ctrl, psu, load, dmm_v, dmm_t = self._make_fast_enabled_controller()
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON

        seen = _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})

        assert OcvSocState.IMPULSE_FAST_ON in seen
        assert OcvSocState.IMPULSE_FAST_ACQUIRE in seen
        assert ctrl.state == OcvSocState.LOG_SOC_POINT

    def test_fast_transient_acquire_faults_on_concurrent_psu_load(self):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] l.
        TestOcvSocImpulse.test_impulse_acquire_faults_on_concurrent_psu_load
        ugyanerről a hiányzó mintáról a fast-transient variánsra."""
        ctrl, psu, load, dmm_v, _dmm_t = self._make_fast_enabled_controller()
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        _run_until(ctrl, {OcvSocState.IMPULSE_FAST_ACQUIRE, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.IMPULSE_FAST_ACQUIRE
        assert load.input_commanded_on is True

        psu.output_commanded_on = True
        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason

    def test_impulse_fast_on_faults_on_load_power_limit_exceeded(self):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), Session/
        Workflow P1 lelet] l. TestOcvSocImpulse.test_impulse_on_faults_on_
        load_power_limit_exceeded ugyanerről a hiányzó mintáról a
        fast-transient variánsra."""
        ctrl, _psu, load, _dmm_v, _dmm_t = self._make_fast_enabled_controller()
        ctrl._state = OcvSocState.IMPULSE_FAST_ON
        ctrl._u_batt = 20.0
        ctrl._impulse_current_A_set = 15.0  # 20V * 15A = 300W > 250W max

        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "LOAD_POWER_LIMIT" in ctrl.fault_reason
        assert load.input_commanded_on is False  # a Load SOSEM kapcsolt be

    def test_impulse_fast_on_faults_on_reverse_polarity(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P2 lelet #5] _run_impulse_fast_on() korábban CSAK
        check_load_power()-t hívott (5. kör) — reverse-polarity
        (check_battery_voltage()) ellenőrzés nélkül aktiválta a Load-ot,
        ellentétben a standard pulse _run_impulse_prep()-jével, ami MINDIG
        ellenőrzi ezt a Load bekapcsolása előtt (CLAUDE.md §10.6, SAFE-009).
        Az elavult PREP-ellenőrzésre hagyatkozás akár 30s+-mal korábbi
        állapotot tükrözne — friss ellenőrzés kell közvetlenül a fast-
        transient Load-aktiválás előtt is."""
        ctrl, _psu, load, _dmm_v, _dmm_t = self._make_fast_enabled_controller()
        ctrl._state = OcvSocState.IMPULSE_FAST_ON
        ctrl._u_batt = -1.0  # fordított polaritás / hibás bekötés

        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "REVERSE_POLARITY" in ctrl.fault_reason
        assert load.input_commanded_on is False  # a Load SOSEM kapcsolt be

    def test_impulse_fast_on_faults_on_concurrent_psu_load(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P2 lelet #5] l. test_impulse_fast_on_faults_on_reverse_
        polarity docstringje ugyanerről a hiányzó mintáról — a kölcsönös
        kizárás (check_concurrent_psu_load(), CLAUDE.md §10.2) ellenőrzésre."""
        ctrl, psu, load, _dmm_v, _dmm_t = self._make_fast_enabled_controller()
        ctrl._state = OcvSocState.IMPULSE_FAST_ON
        ctrl._u_batt = 13.5
        # Stale/külső egyidejű PSU+Load ON állapot szimulálása — ugyanaz a
        # minta, mint _run_impulse_prep() ellenőrzésénél: a check a Load
        # ÚJ bekapcsolása ELŐTT, egy MÁR fennálló konfliktust szűr ki.
        psu.output_commanded_on = True
        load.input_commanded_on = True

        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason

    def test_fast_transient_results_populate_R_mOhm(self):
        ctrl, psu, load, dmm_v, dmm_t = self._make_fast_enabled_controller()
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})

        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        i_set = ctrl._impulse_current_A_set
        assert 0.6 in ctrl._fast_R_mOhm
        assert ctrl._fast_R_mOhm[0.6] == pytest.approx(
            (13.5 - 13.2) / i_set * 1000.0, rel=1e-2
        )

    def test_fast_transient_payload_fields_present(self):
        ctrl, psu, load, dmm_v, dmm_t = self._make_fast_enabled_controller()
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        ctrl.advance(0.0)  # LOG_SOC_POINT tick -> callback

        assert len(collected) == 1
        point = collected[0]
        assert 0.6 in point["fast_transient_R_mOhm"]
        assert point["fast_transient_synchronization_quality"] is not None

    def test_on_soc_point_scalar_keys_all_persisted_to_ocv_soc_columns(self):
        """[regresszió, CF-21/MET-R1-19 (DFAM-012, batch 4.2/4.6, a CF-22
        MAX verifier kör azonosította)] az `on_soc_point` dict minden
        SKALÁR kulcsának szerepelnie kell a `logger.py::OCV_SOC_COLUMNS`
        listában, különben a `Logger.log_ocv_soc_point()` (ami szigorúan
        `{col: data.get(col, "") for col in OCV_SOC_COLUMNS}` szűr)
        csendben eldobja — az adat kiszámolódik a controllerben, de sosem
        kerül az ocv_soc_table.csv-be. A 3, per-célidőpont DICT-értékű
        mező (`fast_transient_R_mOhm`/`_actual_s`/`_time_error_s`) NEM
        tartozik ide — azok flatten-elése külön, dedikált tervezési
        döntést igényel (fix célidőpont-oszlopok a konfigurálható
        `fast_transient_target_times_s`-hez), l. logger.py komment."""
        from Prog.src.logger import OCV_SOC_COLUMNS

        ctrl, psu, load, dmm_v, dmm_t = self._make_fast_enabled_controller()
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        ctrl.advance(0.0)  # LOG_SOC_POINT tick -> callback

        assert len(collected) == 1
        point = collected[0]
        dict_valued_exceptions = {
            "fast_transient_R_mOhm", "fast_transient_actual_s",
            "fast_transient_time_error_s",
        }
        for value in dict_valued_exceptions:
            assert isinstance(point[value], dict), (
                f"{value} már nem dict-értékű -- a kizárás-lista elavult"
            )
        scalar_keys = set(point.keys()) - dict_valued_exceptions
        missing = scalar_keys - set(OCV_SOC_COLUMNS)
        assert not missing, (
            f"on_soc_point skalár kulcsai hiányoznak az OCV_SOC_COLUMNS-ból, "
            f"csendben elveszve az ocv_soc_table.csv-ből: {sorted(missing)}"
        )

    def test_on_soc_point_temperature_valid_false_when_temp_dmm_fails(self):
        """[regresszió, Metrológia M4-04 (review-kör-4, DFAM-001)] a
        temperature_C mező sticky (utolsó sikeres olvasásból ragadt), ha
        a hőmérséklet-DMM épp a LOG_SOC_POINT ticken hibázik -- korábban
        semmi nem jelezte ezt soronként az ocv_soc_table.csv-ben, holott
        a testvér IEC-controller (iec_dc_resistance_controller.py) ezt
        már megkapta. A mező LÉTEZÉSÉNEK értelme, hogy egy ilyen sticky
        érték NE tűnjön friss mérésnek."""
        ctrl, psu, load, dmm_v, dmm_t = self._make_fast_enabled_controller()
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        dmm_t.simulate_temp_failure = True
        collected = []
        ctrl.on_soc_point = lambda data: collected.append(data)
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        ctrl.advance(0.0)  # LOG_SOC_POINT tick -> callback

        assert len(collected) == 1
        assert collected[0]["temperature_valid"] is False

    def test_fast_transient_not_started_after_cutoff_during_standard_pulse(self):
        """Ha a standard pulse alatt cutoff volt, a fast réteg NEM indul el —
        a berendezés már le lett kapcsolva, felesleges/veszélyes lenne újra
        impulzust adni rá."""
        ctrl, psu, load, dmm_v, dmm_t = self._make_fast_enabled_controller()
        _set_pulse_test_cutoff(ctrl)
        dmm_v.timed_voltage_fn = lambda t: 13.0 if t < 1.0 else 11.0
        ctrl._state = OcvSocState.IMPULSE_ON

        seen = _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})

        assert OcvSocState.IMPULSE_FAST_ON not in seen
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        assert ctrl._cutoff_during_pulse is True

    def test_fast_transient_cutoff_does_not_corrupt_standard_pulse_quality(self):
        """Review-lelet: a fast-transient réteg alatti cutoff/degraded NEM
        írhatja felül a standard (1/10/30s) pulse minősítését — az attól
        teljesen független, KIEGÉSZÍTŐ diagnosztikai adat. Enélkül egy
        pusztán a ms-tartományú impulzus alatti cutoff hamisan azt
        jelezné, hogy a már érvényesen lezárt standard R-tábla adatok
        megbízhatatlanok."""
        ctrl, psu, load, dmm_v, dmm_t = self._make_fast_enabled_controller()
        _set_pulse_test_cutoff(ctrl)
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON

        # A standard pulse tisztán lezajlik...
        _run_until(
            ctrl,
            {OcvSocState.IMPULSE_FAST_ON, OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT},
        )
        assert ctrl.state == OcvSocState.IMPULSE_FAST_ON
        assert ctrl._cutoff_during_pulse is False
        assert ctrl._rb_1s > 0.0  # standard eredmény már véglegesült

        # ...majd KIZÁRÓLAG a fast fázis alatt cutoff-ot szimulálunk.
        dmm_v.timed_voltage_fn = lambda t: 11.0
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.LOG_SOC_POINT

        assert ctrl._fast_cutoff_during_pulse is True
        # A standard pulse minősítését NEM szabad felülírnia — a CF-22
        # fix (batch 4.2) óta a fő measurement_quality mező kizárólag a
        # saját, elkülönített fast_transient_cutoff_during_pulse mezőn
        # keresztül látszik, a _determine_point_quality() "OK" marad:
        assert ctrl._cutoff_during_pulse is False
        assert ctrl._determine_point_quality() == "OK"
        # A standard R-tábla adatai érintetlenek maradnak:
        assert ctrl._rb_1s > 0.0
        assert ctrl._rb_10s > 0.0
        assert ctrl._rb_30s > 0.0

    def test_fast_transient_invalid_resistance_reaches_own_field_and_quality(self):
        """[regresszió, Teljes kódbázis review 21. kör (2026-09-07),
        Metrológia/Adatintegritás P2 lelet] a fast-transient réteg saját
        has_invalid_resistance eredménye korábban sehol nem jutott el — sem
        külön mezőbe (mint a fast_transient_cutoff_during_pulse/
        fast_transient_current_degraded), sem az on_soc_point-ba, sem a
        _determine_point_quality()-be. Ugyanaz a "külön mező, NEM írja
        felül a standard pulse minősítését" minta, mint a cutoff/degraded
        jelzőknél."""
        from Prog.src.pulse_measurement_service import (
            PulseAcquisitionResult, PulseResultPoint,
        )
        ctrl, *_ = self._make_fast_enabled_controller()
        assert ctrl._fast_pulse_service is not None

        r = PulseResultPoint(
            target_time_s=0.6, actual_time_s=0.6, voltage_V=13.2, current_A=1.0,
            delta_voltage_V=0.0, delta_current_A=0.0, resistance_ohm=None,
            resistance_mOhm=None, time_error_s=0.0, within_tolerance=True,
            reached=True,
        )
        result = PulseAcquisitionResult(
            started=True, skipped_low_voltage=False, cutoff_during_pulse=False,
            pulse_current_degraded=False, v_baseline_V=13.2, pulse_current_set_A=1.0,
            pulse_current_measured_mean_A=1.0, pulse_duration_set_s=1.2,
            pulse_duration_actual_s=1.2, synchronization_quality="INSTRUMENT_TIMED_SOFTWARE_TRIGGERED",
            method_origin="FAST_TRANSIENT_CHARACTERIZATION", removed_Ah=0.0,
            results={0.6: r}, has_invalid_resistance=True,
        )
        ctrl._fast_pulse_service.finalize = lambda: result

        ctrl._finish_fast_impulse_acquisition()

        assert ctrl._fast_invalid_pulse_resistance is True
        # A standard pulse minősítését NEM szabad felülírnia (CF-22 fix,
        # batch 4.2 — l. test_fast_transient_cutoff... azonos indoklása):
        assert ctrl._invalid_pulse_resistance is False
        assert ctrl._determine_point_quality() == "OK"

    def test_fast_transient_trace_incomplete_reaches_own_fields_and_quality(self):
        """[regresszió, Teljes kódbázis review 22. kör (2026-09-07),
        Metrológia/Adatintegritás P2 lelet] a fast-transient réteg saját
        current_trace_incomplete/voltage_trace_incomplete eredménye —
        ugyanaz a mintázat, mint a 21. kör has_invalid_resistance
        leletében — korábban sehol nem jutott el: sem külön mezőbe (mint a
        fast_transient_cutoff_during_pulse/current_degraded/
        invalid_resistance), sem az on_soc_point-ba, sem a
        _determine_point_quality()-be."""
        from Prog.src.pulse_measurement_service import (
            PulseAcquisitionResult, PulseResultPoint,
        )
        ctrl, *_ = self._make_fast_enabled_controller()
        assert ctrl._fast_pulse_service is not None

        r = PulseResultPoint(
            target_time_s=0.6, actual_time_s=0.6, voltage_V=13.2, current_A=1.0,
            delta_voltage_V=0.1, delta_current_A=1.0, resistance_ohm=0.1,
            resistance_mOhm=100.0, time_error_s=0.0, within_tolerance=True,
            reached=True,
        )
        result = PulseAcquisitionResult(
            started=True, skipped_low_voltage=False, cutoff_during_pulse=False,
            pulse_current_degraded=False, v_baseline_V=13.2, pulse_current_set_A=1.0,
            pulse_current_measured_mean_A=1.0, pulse_duration_set_s=1.2,
            pulse_duration_actual_s=1.2, synchronization_quality="HARDWARE_SYNCHRONIZED",
            method_origin="FAST_TRANSIENT_CHARACTERIZATION", removed_Ah=0.0,
            results={0.6: r}, current_trace_incomplete=True,
        )
        ctrl._fast_pulse_service.finalize = lambda: result

        ctrl._finish_fast_impulse_acquisition()

        assert ctrl._fast_current_trace_incomplete is True
        # A standard pulse minősítését NEM szabad felülírnia (CF-22 fix,
        # batch 4.2 — l. test_fast_transient_cutoff... azonos indoklása):
        assert ctrl._current_trace_incomplete is False
        assert ctrl._determine_point_quality() == "OK"

    def test_fast_transient_skipped_low_voltage_reaches_own_field_and_quality(self):
        """[regresszió, Teljes kódbázis review 23. kör (2026-09-07),
        Metrológia/Adatintegritás P2 lelet] a fast-transient réteg saját
        (a standard pulse-tól független, friss DMM-olvasáson alapuló)
        skipped_low_voltage eredménye — a testvér cutoff/degraded/
        invalid_resistance/trace_incomplete jelzőkkel ellentétben — sosem
        jutott el: sem külön mezőbe, sem az on_soc_point-ba, sem a
        _determine_point_quality()-be. Egy elakadt fast-transient impulzus
        "sikeresen lezajlott" benyomást kelthetett."""
        from Prog.src.pulse_measurement_service import PulseAcquisitionResult
        ctrl, *_ = self._make_fast_enabled_controller()
        assert ctrl._fast_pulse_service is not None

        result = PulseAcquisitionResult(
            started=False, skipped_low_voltage=True, cutoff_during_pulse=False,
            pulse_current_degraded=False, v_baseline_V=9.0, pulse_current_set_A=1.0,
            pulse_current_measured_mean_A=0.0, pulse_duration_set_s=1.2,
            pulse_duration_actual_s=0.0, synchronization_quality="INSTRUMENT_TIMED_SOFTWARE_TRIGGERED",
            method_origin="FAST_TRANSIENT_CHARACTERIZATION", removed_Ah=0.0,
        )
        ctrl._fast_pulse_service.finalize = lambda: result

        ctrl._finish_fast_impulse_acquisition()

        assert ctrl._fast_skipped_low_voltage is True
        # A standard pulse minősítését NEM szabad felülírnia (CF-22 fix,
        # batch 4.2 — l. test_fast_transient_cutoff... azonos indoklása):
        assert ctrl._skipped_low_voltage is False
        assert ctrl._determine_point_quality() == "OK"

    def test_fast_transient_current_and_voltage_degradation_do_not_corrupt_quality(self):
        """[regresszió, CF-22 fix (batch 4.2)] a fast-transient réteg
        MARADÉK két saját minőségjelzője (pulse_current_degraded/
        voltage_trace_incomplete) — a testvér cutoff/invalid_resistance/
        current_trace_incomplete/skipped_low_voltage jelzőkkel azonos
        mintát követve — szintén NEM szabad, hogy felülírja a standard
        pulse `_determine_point_quality()` eredményét."""
        from Prog.src.pulse_measurement_service import (
            PulseAcquisitionResult, PulseResultPoint,
        )
        ctrl, *_ = self._make_fast_enabled_controller()
        assert ctrl._fast_pulse_service is not None

        r = PulseResultPoint(
            target_time_s=0.6, actual_time_s=0.6, voltage_V=13.2, current_A=1.0,
            delta_voltage_V=0.1, delta_current_A=1.0, resistance_ohm=0.1,
            resistance_mOhm=100.0, time_error_s=0.0, within_tolerance=True,
            reached=True,
        )
        result = PulseAcquisitionResult(
            started=True, skipped_low_voltage=False, cutoff_during_pulse=False,
            pulse_current_degraded=True, v_baseline_V=13.2, pulse_current_set_A=1.0,
            pulse_current_measured_mean_A=1.0, pulse_duration_set_s=1.2,
            pulse_duration_actual_s=1.2, synchronization_quality="INSTRUMENT_TIMED_SOFTWARE_TRIGGERED",
            method_origin="FAST_TRANSIENT_CHARACTERIZATION", removed_Ah=0.0,
            results={0.6: r}, voltage_trace_incomplete=True,
        )
        ctrl._fast_pulse_service.finalize = lambda: result

        ctrl._finish_fast_impulse_acquisition()

        assert ctrl._fast_pulse_current_degraded is True
        assert ctrl._fast_voltage_trace_incomplete is True
        assert ctrl._pulse_current_degraded is False
        assert ctrl._voltage_trace_incomplete is False
        assert ctrl._determine_point_quality() == "OK"

    def test_fast_transient_ah_folds_into_impulse_removed_ah(self):
        """A fast-transient impulzus is fogyaszt Ah-t — ugyanabba az
        _impulse_removed_Ah összegzőbe kell kerülnie, mint a standardnak."""
        ctrl, psu, load, dmm_v, dmm_t = self._make_fast_enabled_controller()
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        standard_only_Ah = ctrl._impulse_current_A_set * 30.0 / 3600.0
        assert ctrl._impulse_removed_Ah > standard_only_Ah


class TestReleaseAudit20260823PulseTimingToleranceQuality:
    """Release-audit 2026-08-23 (2.8): a within_tolerance jelző (a reached-
    től FÜGGETLEN, tényleges időzítési tolerancia-ellenőrzés) korábban
    sosem folyt be a minőség-döntésbe — egy R_1s, ami jóval a célidőpont
    után mintavételeződött (pl. a dokumentált, fizikailag validált SUP01
    HIL-lelet: 2.141s tényleges idő az 1.0s cél helyett), "OK" minősítést
    kapott, mert csak a `reached` (alsó korlát) számított."""

    def test_flag_false_by_default(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        assert ctrl._pulse_timing_out_of_tolerance is False
        assert ctrl._determine_point_quality() == "OK"

    def test_out_of_tolerance_flag_yields_degraded_quality(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._pulse_timing_out_of_tolerance = True
        assert ctrl._determine_point_quality() == "DEGRADED_TIMING_TOLERANCE"

    def test_cutoff_takes_precedence_over_timing_tolerance(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._cutoff_during_pulse = True
        ctrl._pulse_timing_out_of_tolerance = True
        assert ctrl._determine_point_quality() == "CUTOFF_DURING_PULSE"

    def test_timing_tolerance_takes_precedence_over_trace_incomplete(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._pulse_timing_out_of_tolerance = True
        ctrl._current_trace_incomplete = True
        assert ctrl._determine_point_quality() == "DEGRADED_TIMING_TOLERANCE"

    def test_finish_impulse_acquisition_sets_flag_from_reached_but_out_of_tolerance_point(self):
        """[a SUP01 HIL-lelet közvetlen reprodukciója] reached=True,
        within_tolerance=False -> a resistance_ohm kiszámolódik, DE a
        minőség NEM maradhat "OK"."""
        from Prog.src.pulse_measurement_service import (
            PulseAcquisitionResult, PulseResultPoint,
        )
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)

        r1 = PulseResultPoint(
            target_time_s=1.0, actual_time_s=2.141, voltage_V=12.0, current_A=1.0,
            delta_voltage_V=0.2, delta_current_A=1.0, resistance_ohm=0.2,
            resistance_mOhm=200.0, time_error_s=1.141, within_tolerance=False,
            reached=True,
        )
        result = PulseAcquisitionResult(
            started=True, skipped_low_voltage=False, cutoff_during_pulse=False,
            pulse_current_degraded=False, v_baseline_V=12.2, pulse_current_set_A=1.0,
            pulse_current_measured_mean_A=1.0, pulse_duration_set_s=1.0,
            pulse_duration_actual_s=2.141, synchronization_quality="INSTRUMENT_TIMED_SOFTWARE_TRIGGERED",
            method_origin="STANDARD_PULSE", removed_Ah=0.0,
            results={1.0: r1},
        )
        ctrl._pulse_service.finalize = lambda: result

        ctrl._finish_impulse_acquisition()

        assert ctrl._rb_1s == pytest.approx(0.2)  # a resistance_ohm-ot MÉG mindig kiszámolja
        assert ctrl._pulse_timing_out_of_tolerance is True
        assert ctrl._determine_point_quality() == "DEGRADED_TIMING_TOLERANCE"

    def test_flags_reset_after_log_soc_point(self):
        ctrl, *_ = make_ocv_soc_controller(capacity_Ah=7.0)
        ctrl._pulse_timing_out_of_tolerance = True
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._run_log_soc_point()
        assert ctrl._pulse_timing_out_of_tolerance is False


class TestLastIntegrationSourceProperty:
    """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), Szoftver/
    Architektúra P3 lelet] test_runner.py::_build_sample() sample[
    "integration_current_source"]-je enélkül mindig None maradt PRECHARGE
    alatt — a beágyazott ChargeController ténylegesen vezeti ezt, de nem
    volt proxy-property rajta."""

    def test_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.last_integration_source == "ZERO"

    def test_not_a_live_proxy_of_embedded_charge_ctrl(self):
        """R6-B3 (R6-metrology-01, DFAM-001): a property NEM a gyermek
        elo proxyja -- a gyermek forrasa csak a toltesi delta atvetelekor
        (advance()) rogzul; a pozitiv utat l. test_test_runner.py
        TestR6B3CompositeIntegrationSourceToSampleRow."""
        from types import SimpleNamespace
        ctrl, *_ = make_ocv_soc_controller()
        ctrl._charge_ctrl = SimpleNamespace(last_integration_source="PSU_READBACK")
        assert ctrl.last_integration_source == "ZERO"


class TestTaperTimerSProperty:
    """[regresszió, Teljes kódbázis review 12. kör (2026-08-31), Szoftver/
    Architektúra P3 lelet] ugyanaz a hiányzó proxy-property minta, mint
    fent a TestLastIntegrationSourceProperty-nél (11. kör)."""

    def test_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.taper_timer_s == 0.0

    def test_reflects_embedded_charge_ctrl(self):
        from types import SimpleNamespace
        ctrl, *_ = make_ocv_soc_controller()
        ctrl._charge_ctrl = SimpleNamespace(taper_timer_s=42.0)
        assert ctrl.taper_timer_s == 42.0


class TestEmergencyStopOffFailureReported:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Hardver/
    Műszervezérlés P2 lelet] l. test_charge_controller.py::
    TestEmergencyStopOffFailureReported ugyanerről a csendben elnyelt
    OFF-kísérlet-hiba mintáról."""

    def test_load_off_failure_surfaces_in_fault_reason(self):
        ctrl, _psu, load, _dmm_v, _dmm_t = make_ocv_soc_controller()

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.emergency_stop("DMM_FEEDBACK_LOST")

        assert ctrl.state == OcvSocState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert "LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason

    def test_off_success_leaves_fault_reason_unchanged(self):
        ctrl, *_ = make_ocv_soc_controller()
        ctrl.emergency_stop("DMM_FEEDBACK_LOST")
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"


class TestRelaxDmmFaultTotalProperty:
    """[regresszió, Teljes kódbázis review 14. kör (2026-09-04),
    Szoftver/Architektúra + Session/Workflow/Állapotgép P2 lelet — két
    domain-ágens is önállóan megtalálta] a testvér
    GoldenGgController/TraditionalLearningController/
    ChemIdCharacterizationController mindegyike gyűjt egy session-
    szintű, fázisváltáskor NEM nullázódó relax_dmm_fault_total
    számlálót — az OcvSocController-ről ez hiányzott, csak a per-SOC-
    pont _dmm_fault_during_relax (fázisváltáskor mindig resetelt)
    létezett, ami kizárólag a nyers soc_points.csv-be jutott el, a
    session-szintű capacity_result_quality DEGRADED-kapuba soha."""

    def test_zero_by_default(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl.relax_dmm_fault_total == 0

    def test_reflects_internal_counter(self):
        ctrl, *_ = make_ocv_soc_controller()
        ctrl._relax_dmm_fault_total = 4
        assert ctrl.relax_dmm_fault_total == 4

    def test_accumulates_during_precharge_relax_and_survives_phase_change(self):
        """Valódi DMM-kiesés PRECHARGE_RELAX alatt session-szinten
        rögzül, és a következő SOC-pont (STEP_RELAX) fázisváltása
        NEM nullázza — szemben a per-pont _dmm_fault_during_relax-szal."""
        from Prog.src.charge_controller import ChargeState
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=14.4)
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.PRECHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        state = ctrl.advance(1.0)
        assert state == OcvSocState.PRECHARGE_RELAX
        assert ctrl.relax_dmm_fault_total == 0

        dmm_v.set_dmm_invalid()
        ctrl.advance(1.0)  # DMM-kiesés PRECHARGE_RELAX alatt (nem FAULT, csak flag)

        assert ctrl.state == OcvSocState.PRECHARGE_RELAX
        assert ctrl.relax_dmm_fault_total == 1

        # Fázisváltás: a per-pont _dmm_fault_during_relax resetelődik,
        # DE a session-szintű relax_dmm_fault_total-nak MEG KELL maradnia.
        dmm_v.set_dmm_valid()
        ctrl._state = OcvSocState.STEP_RELAX
        ctrl._dmm_fault_during_relax = False
        ctrl.advance(1.0)

        assert ctrl.relax_dmm_fault_total == 1


class TestBatch42bF2OcvSocRelaxDmmFaultEventFiresOnlyOnTransition:
    """[regresszió, BATCH 4.2b F-2, 2026-09-13, DECISION_REGISTER.md
    GOV-024, "5. csoport"] az OCV_SOC_RELAX_DMM_FAULT esemény korábban
    MINDEN ticken tüzelt, amíg a DMM-kiesés tartott a relax alatt -- egy
    hosszú (2-5 órás) relax alatti tartós kiesés sok száz/ezer azonos
    eseményt generálhatott, kiürítve az EventLogWidget 500-soros
    gyűrűjéből a korábbi kontextust. A relax_dmm_fault_total számláló
    (fent) ettől függetlenül MINDEN ticken nő -- ez csak az esemény-
    emissziót gátolja, nem a számlálást."""

    def test_consecutive_relax_failures_fire_event_only_once(self):
        from Prog.src.charge_controller import ChargeState
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=14.4)
        ctrl.advance(0.0)
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX

        events: list = []
        ctrl.on_event = events.append
        dmm_v.set_dmm_invalid()
        for _ in range(5):
            ctrl.advance(1.0)

        fault_events = [
            e for e in events if e["event_code"] == "OCV_SOC_RELAX_DMM_FAULT"
        ]
        assert len(fault_events) == 1
        assert ctrl.relax_dmm_fault_total == 5

    def test_recovery_then_new_failure_fires_a_second_event(self):
        from Prog.src.charge_controller import ChargeState
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=14.4)
        ctrl.advance(0.0)
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX

        events: list = []
        ctrl.on_event = events.append
        dmm_v.set_dmm_invalid()
        ctrl.advance(1.0)
        dmm_v.set_dmm_valid()
        ctrl.advance(1.0)  # helyreáll
        dmm_v.set_dmm_invalid()
        ctrl.advance(1.0)  # új kiesés -- ez is egy ÚJ átmenet

        fault_events = [
            e for e in events if e["event_code"] == "OCV_SOC_RELAX_DMM_FAULT"
        ]
        assert len(fault_events) == 2


class TestReviewRound5S505UnhandledStateRaises:
    """[regresszió, review-kör 5, Session/Workflow S5-05 (P3, DFAM-004
    sibling) -- l. test_charge_controller.py azonos hivatkozású tesztje
    ugyanerről a hibaosztályról] advance() korábban hallgatólagosan
    (else nélkül) no-op-olt egy le nem kezelt OcvSocState-re."""

    def test_advance_raises_on_unhandled_state(self):
        ctrl, *_ = make_ocv_soc_controller()
        ctrl._state = "BOGUS_FUTURE_STATE"
        with pytest.raises(ValueError, match="BOGUS_FUTURE_STATE"):
            ctrl.advance(1.0)


class TestOcvSocFastTransientNplc:
    """2026-09-12, FAST_TRANSIENT NPLC-javítás — controller-szintű regresszió.

    A hibaosztály: a session normál üzemi DCV integrációs ideje NPLC=10
    (main_window.py `configure_dcv(range_V=100, nplc=10)` + test_runner.py
    `relax_ocv_nplc=10.0` az OCV_SOC lépésre), ami a 34465A-n 50 Hz-en 0,2 s
    apertúra mintánként (34460-70-Manual.txt 14028-14052. sor) — a
    FAST_TRANSIENT 5 ms-os `sample_interval_s`-e ezzel fizikailag
    teljesíthetetlen (uo. 9067-9072. sor: -221 "cannot meet requested
    timing; SAMP:TIM changed")."""

    def _make_ctrl(self, **cfg_overrides):
        kwargs = dict(
            step_percent=5.0, discharge_rate_divisor=10,
            relax_default_s=7200.0, relax_keypoint_s=18000.0,
            impulse_current_rate_divisor=5, impulse_duration_s=30.0,
            fast_transient_enabled=True,
            fast_transient_chunk_sample_count=2,
            fast_transient_sample_interval_s=0.005,
            fast_transient_duration_s=0.02,
            fast_transient_target_times_s=(0.02,),
            fast_transient_time_tolerance_s=0.01,
        )
        kwargs.update(cfg_overrides)
        return make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0, config=OcvSocConfig(**kwargs)
        )

    def test_default_nplc_is_the_lowest_available_on_every_34465a_variant(self):
        """0,02 PLC a 34465A MINDEN változatában elérhető (34460-70-Manual.txt
        11428-11433. sor); a 0,006/0,002/0,001 fokozatok CSAK a DIG opcióval
        léteznek, ezért a projekt nem támaszkodhat rájuk."""
        assert OcvSocConfig().fast_transient_nplc == 0.02

    def test_standard_pulse_service_gets_q6_nplc(self):
        """Q-6: a standard 1 NPLC; a fast-transient külön 0,02 NPLC marad."""
        ctrl, *_ = self._make_ctrl()
        assert ctrl._pulse_service._acquisition_nplc == 1.0
        assert ctrl._fast_pulse_service is not None
        assert ctrl._fast_pulse_service._acquisition_nplc == pytest.approx(0.02)

    def test_fast_transient_switches_and_restores_nplc(self):
        ctrl, psu, load, dmm_v, dmm_t = self._make_ctrl()
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON

        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})

        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        assert dmm_v.called("set_nplc(0.02)")
        # A session hátralévő (normál sebességű) mérései nem sérülhetnek.
        assert dmm_v.nplc == pytest.approx(10.0)
        assert ctrl._fast_timing_infeasible is False
        assert ctrl._fast_nplc_restore_failed is False

    def test_infeasible_timing_stops_the_layer_without_false_data(self):
        """Ha a műszer SAJÁT képessége (SAMP:TIM? MIN) szerint a kért köz nem
        teljesíthető, a réteg NEM mér: az impulzus el sem indul, nincs
        fast_transient_R_mOhm adat, és a minősítés explicit."""
        ctrl, psu, load, dmm_v, dmm_t = self._make_ctrl(fast_transient_nplc=10.0)
        dmm_v.timed_voltage_fn = lambda t: 13.2
        collected: list[dict] = []
        ctrl.on_soc_point = collected.append
        ctrl._state = OcvSocState.IMPULSE_ON

        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        # A jelzőket a LOG_SOC_POINT tick UTÁN a controller pontonként
        # nullázza (l. _run_log_soc_point() végét), ezért itt még olvassuk.
        assert ctrl._fast_timing_infeasible is True
        assert ctrl._fast_R_mOhm == {}
        assert load.input_commanded_on is False
        assert dmm_v.nplc == pytest.approx(10.0)

        ctrl.advance(0.0)   # LOG_SOC_POINT tick -> callback
        point = collected[0]
        assert point["fast_transient_timing_infeasible"] is True
        assert "SAMPLE_TIMER_MIN_EXCEEDS_REQUESTED_INTERVAL" in (
            point["fast_transient_timing_infeasible_reason"]
        )
        # A STANDARD (1/10/30 s) mérés érvényes marad — a diagnosztikai réteg
        # minősítése nem szennyezi a fő eredményt (rules/20-metrology.md).
        assert point["R_pulse_actual_t1_mOhm"] is not None

    def test_infeasible_timing_does_not_fault_the_session(self):
        """A FAST_TRANSIENT opcionális diagnosztika — a megvalósíthatatlan
        időzítés nem safety-esemény, nem visz FAULT-ba."""
        ctrl, psu, load, dmm_v, dmm_t = self._make_ctrl(fast_transient_nplc=10.0)
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.LOG_SOC_POINT

    def test_emergency_stop_mid_fast_pulse_restores_nplc(self):
        """A fast-transient impulzus TÖBB advance() ticken át tart; egy
        cikluson KÍVÜLI megszakítás (itt: PSU/Load kölcsönös kizárás sérülése)
        után a PulseMeasurementService soha nem kapna több hívást — az
        integrációs időt az emergency_stop cleanupjának kell visszaállítania
        (rules/10-safety.md 9.)."""
        ctrl, psu, load, dmm_v, dmm_t = self._make_ctrl(
            fast_transient_duration_s=1.0,   # sok chunk, hogy legyen mit megszakítani
            fast_transient_target_times_s=(0.02,),
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        _run_until(ctrl, {OcvSocState.IMPULSE_FAST_ACQUIRE, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.IMPULSE_FAST_ACQUIRE
        assert dmm_v.nplc == pytest.approx(0.02)

        psu.output_commanded_on = True
        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert dmm_v.nplc == pytest.approx(10.0)

    def test_emergency_stop_nplc_restore_never_blocks_safe_off(self):
        """CLAUDE.md §6: egy OFF-hiba (itt: a DMM-cleanup hibája) nem
        akadályozhatja a Load/PSU lekapcsolását — a safe-off ELŐBB fut."""
        ctrl, psu, load, dmm_v, dmm_t = self._make_ctrl(
            fast_transient_duration_s=1.0,
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        _run_until(ctrl, {OcvSocState.IMPULSE_FAST_ACQUIRE, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.IMPULSE_FAST_ACQUIRE

        dmm_v.simulate_nplc_set_failure = True
        ctrl.emergency_stop("TEST_REASON")

        assert ctrl.state == OcvSocState.FAULT
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False


def test_c08_psu_readback_proxies_delegate_to_charge_ctrl():
    """[MAX verifier P3-4, 2026-09-15, review-kör 6, C-08a/C-08b] a
    i_psu_measured_A/psu_current_readback_valid/u_psu_measured_V/psu_
    voltage_readback_valid proxy property-k (test_runner.py::
    _build_sample() ezeket olvassa a skip_psu_voltage/skip_psu_current
    döntéshez is, l. instrument_manager.py) korábban NEM voltak
    lefedve -- a MAX verifier manuálisan igazolta helyességüket
    futásidőben, de egy jövőbeli copy-paste-hiba (pl. rossz mezőnév a
    delegálásban) néma maradt volna."""
    ctrl, *_ = make_ocv_soc_controller()
    assert ctrl._charge_ctrl is None
    assert ctrl.i_psu_measured_A is None
    assert ctrl.psu_current_readback_valid is None
    assert ctrl.u_psu_measured_V is None
    assert ctrl.psu_voltage_readback_valid is None

    class _FakeChargeCtrl:
        i_psu_measured_A = 1.23
        psu_current_readback_valid = True
        u_psu_measured_V = 14.56
        psu_voltage_readback_valid = False

    ctrl._charge_ctrl = _FakeChargeCtrl()
    assert ctrl.i_psu_measured_A == 1.23
    assert ctrl.psu_current_readback_valid is True
    assert ctrl.u_psu_measured_V == 14.56
    assert ctrl.psu_voltage_readback_valid is False


class TestLoadCurrentReadbackResetOrder:
    """MAX verifier V-1 (2026-09-15, R2-06, 12. writer-kör): l.
    test_golden_gg_controller.py azonos nevű teszt-osztálya a teljes
    indoklásért -- a C-08a F-1 hibaosztály-minta replikálása ocv_soc_
    controller.py-ra (STEP_DISCHARGE fázis)."""

    def test_resets_to_none_on_dmm_feedback_lost_gate(self):
        ctrl, _psu, load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=7.0
        )
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_step_discharge()

        load.current_A = 0.6
        ctrl.advance(1.0)
        assert ctrl.load_current_readback_valid is True
        assert ctrl.i_load_measured_A == pytest.approx(0.6)

        dmm_v.simulate_timeout = True  # top-of-tick DMM_FEEDBACK_LOST kapu
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.load_current_readback_valid is None
        assert ctrl.i_load_measured_A is None


class TestEnergyIntegratorStaleGuard:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-18, 12. writer-kör):
    l. golden_gg_controller.py azonos nevű teszt-osztálya a teljes
    indoklásért -- `i_psu_measured_A` NEM tick-scoped, egy top-of-tick
    safety-kapu FAULT-ticken az ELŐZŐ, sikeres tick áram-értékén marad,
    miközben `psu_current_readback_valid` helyesen None-ra reset."""

    def _reach_charge_cc_with_baseline_current(self, dmm_voltage_V=12.0, psu_current_A=1.0):
        from Prog.src.charge_controller import ChargeState

        ctrl, psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(0.0)  # INIT -> PRECHARGE
        assert ctrl.state == OcvSocState.PRECHARGE
        ctrl._charge_ctrl._charge_current_set_A = 1.5
        psu.set_output_current(1.5)
        ctrl._charge_ctrl._state = ChargeState.CHARGE_CC
        psu.current_A = psu_current_A
        ctrl.advance(2.0)  # valódi, sikeres tick -- _i_charge/valid mindkettő friss
        assert ctrl._charge_ctrl.psu_current_readback_valid is True
        assert ctrl._charge_ctrl.i_psu_measured_A == pytest.approx(psu_current_A)
        return ctrl, dmm_v

    def test_fault_gate_tick_does_not_integrate_stale_psu_current(self):
        ctrl, dmm_v = self._reach_charge_cc_with_baseline_current()
        ah_before = ctrl.accumulated_charge_Ah

        dmm_v.simulate_timeout = True  # top-of-tick DMM_FEEDBACK_LOST kapu
        ctrl.advance(3600.0)  # nagy dt_s -- ha elévült áramot integrálna, azonnal látszana

        assert ctrl._charge_ctrl.psu_current_readback_valid is None
        assert ctrl.accumulated_charge_Ah == pytest.approx(ah_before)

    def test_dt_statistics_still_recorded_on_such_tick(self):
        """Mutációs őr: a HELYES fix `0.0`-t integrál (nem `skip`-el), hogy
        a dt-statisztikák TOVÁBBRA IS frissüljenek ezen a ticken."""
        ctrl, dmm_v = self._reach_charge_cc_with_baseline_current()

        dmm_v.simulate_timeout = True
        ctrl.advance(3600.0)

        assert ctrl.max_observed_dt_s >= 3600.0


class TestF19FactoryPulseIntervalOnHilFaithfulMock:
    """F-19/DFAM-006: a korábbi 10 NPLC/0,2 s profil elutasítása.
    Q-6 szerint a gyári OCV standard már 1 NPLC/AZ ON; a régi hibás
    profilt a teszt explicit állítja elő, és ON előtti hibát követel."""

    def _make(self, config: OcvSocConfig):
        ctrl, psu, load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0, config=config
        )
        dmm_v.simulate_hil_sample_timer_silent_rejection = True
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        return ctrl, psu, load, dmm_v

    def test_legacy_10plc_interval_rejected_before_load_on(self):
        """A régi 10 NPLC/0,2 s kérés most már az ON ELŐTT elutasítandó."""
        ctrl, psu, load, dmm_v = self._make(OcvSocConfig())
        ctrl._pulse_service._acquisition_nplc = 10.0
        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert "SAMPLE_TIMER_MIN_EXCEEDS_REQUESTED_INTERVAL" in ctrl.fault_reason
        assert not load.called("input_on(")
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert dmm_v.check_error() == []

    def test_hil_verified_interval_completes_without_fault(self):
        ctrl, _psu, load, _dmm_v = self._make(OcvSocConfig(pulse_sample_interval_s=0.41))

        state = _run_full_impulse(ctrl)

        assert state == OcvSocState.LOG_SOC_POINT
        assert load.input_commanded_on is False


# ------------------------------------------------------------------ #
# R1 B8b, F-23 (P2, DFAM-001): a PUBLIKUS dmm_valid tick-scoped       #
# érvénytelenítése IMPULSE_* tickeken (D-B8-7: érvénytelenítés,       #
# chunkminta-átvétel nélkül; a privát _dmm_valid és u_batt_V          #
# változatlan).                                                        #
# ------------------------------------------------------------------ #

_F23_IMPULSE_STATES = (
    OcvSocState.IMPULSE_ON, OcvSocState.IMPULSE_ACQUIRE,
    OcvSocState.IMPULSE_FAST_ON, OcvSocState.IMPULSE_FAST_ACQUIRE,
)


def _f23_fast_enabled_controller():
    """Mind a négy IMPULSE_* állapotot bejáró konfiguráció (l.
    TestOcvSocFastTransientLayer._make_fast_enabled_controller())."""
    cfg = OcvSocConfig(
        step_percent=5.0, discharge_rate_divisor=10,
        relax_default_s=7200.0, relax_keypoint_s=18000.0,
        impulse_current_rate_divisor=5, impulse_duration_s=30.0,
        fast_transient_enabled=True,
        fast_transient_chunk_sample_count=1,
        fast_transient_sample_interval_s=0.6,
        fast_transient_duration_s=1.2,
        fast_transient_target_times_s=(0.6, 1.2),
        fast_transient_time_tolerance_s=0.01,
    )
    ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
        dmm_voltage_V=13.5, capacity_Ah=7.0, config=cfg
    )
    dmm_v.timed_voltage_fn = lambda t: 13.2
    return ctrl, psu, load, dmm_v, dmm_t


def _f23_runner_sharing_instruments(tmp_path, psu, load, dmm_v, dmm_t):
    """Valódi TestRunner, amelynek InstrumentManager-e UGYANAZOKAT a mock
    műszereket látja, mint a controller -- így a _build_sample() esetleges
    második DMM-olvasása a közös dmm_v.calls-ban látszana."""
    from datetime import datetime, timezone

    from Prog.src.instrument_manager import InstrumentManager
    from Prog.src.logger import LogConfig, Logger
    from Prog.src.test_runner import TestRunner, TestRunnerConfig

    profile = _make_profile(7.0)
    instruments = InstrumentManager(
        psu=psu, load=load, dmm_voltage=dmm_v, dmm_temperature=dmm_t,
    )
    logger = Logger(session_dir=tmp_path, config=LogConfig())
    runner = TestRunner(
        instrument_manager=instruments,
        safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
        logger=logger,
        profile=profile,
        config=TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="t"),
        charge_ctrl_factory=lambda: None,
        discharge_ctrl_factory=lambda: None,
        relax_ctrl_factory=lambda step: None,
    )
    runner._start_time = datetime.now(timezone.utc)
    return runner, logger


class TestF23PublicDmmValidInvalidatedDuringImpulse:
    """[regresszió, R1 B8b, F-23 (P2), DFAM-001] IMPULSE_* tickeken a
    feszültség-DMM tulajdonosa a PulseMeasurementService (SUP01-ARCH-002),
    a controller NEM olvassa a DMM-et -- a publikus dmm_valid korábban a
    pulzus ELŐTTI, privát _dmm_valid-ot (True) adta vissza, így a
    test_runner.py::_build_sample() a pulzus előtti OCV-t
    dmm_voltage_valid=True-val naplózta. A fix tick-scoped mezőt vezet be;
    u_batt_V (nem None) és a privát _dmm_valid VÁLTOZATLAN."""

    def test_new_field_initialised_false_in_constructor(self):
        ctrl, *_ = make_ocv_soc_controller()
        assert ctrl._dmm_owned_by_pulse_service_this_tick is False
        assert ctrl.dmm_valid is True

    def test_dmm_valid_false_on_every_impulse_state_tick_private_unchanged(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = _f23_fast_enabled_controller()
        ctrl._state = OcvSocState.IMPULSE_PREP
        ctrl.advance(0.0)  # IMPULSE_PREP: friss, saját DMM-olvasás
        assert ctrl.state == OcvSocState.IMPULSE_ON
        assert ctrl.dmm_valid is True

        reads_per_state: dict = {}
        ticks = 0
        while ctrl.state in _F23_IMPULSE_STATES and ticks < 80:
            start_state = ctrl.state
            dmm_v.reset_calls()
            ctrl.advance(0.0)
            ticks += 1
            reads_per_state[start_state] = (
                reads_per_state.get(start_state, 0) + dmm_v.calls.count("read_voltage()")
            )
            assert ctrl.dmm_valid is False, start_state
            # A privát _dmm_valid és a belső kapuk érintetlenek.
            assert ctrl._dmm_valid is True, start_state
            # u_batt_V NEM None -- a runner skip_battery_voltage-e megmarad
            # (a pulzus előtti, ill. a befejező tickben a _finish_impulse_
            # acquisition() által beírt utolsó pulzusminta; KEEP_AS_IS).
            assert ctrl.u_batt_V is not None

        assert set(reads_per_state) == set(_F23_IMPULSE_STATES)
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        # Változatlan DMM-hívásszám: a fix ELŐTTI kódon (HEAD 39cbb10)
        # mért karakterizáció -- az egyetlen read_voltage() a pulzus-
        # szolgáltatás begin_pulse()-beli baseline-olvasása (IMPULSE_ON/
        # IMPULSE_FAST_ON); a controller maga IMPULSE_* alatt nem olvas.
        assert reads_per_state == {
            OcvSocState.IMPULSE_ON: 1,
            OcvSocState.IMPULSE_ACQUIRE: 0,
            OcvSocState.IMPULSE_FAST_ON: 1,
            OcvSocState.IMPULSE_FAST_ACQUIRE: 0,
        }

    def test_finishing_transition_tick_false_then_next_normal_tick_revalidates(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON

        state = _run_full_impulse(ctrl)

        # A befejező (IMPULSE_ACQUIRE -> LOG_SOC_POINT) átmeneti tick: a
        # tick ELEJÉN a pulzusszolgáltatás volt a tulajdonos, a controller
        # nem olvasott -- konzervatív False.
        assert state == OcvSocState.LOG_SOC_POINT
        assert ctrl.dmm_valid is False

        dmm_v.reset_calls()
        dmm_v.voltage_V = 13.3
        ctrl.advance(1.0)  # LOG_SOC_POINT: normál, saját DMM-olvasás

        assert dmm_v.calls.count("read_voltage()") == 1
        assert ctrl.dmm_valid is True
        assert ctrl.u_batt_V == pytest.approx(13.3)
        assert ctrl.state != OcvSocState.FAULT

    def test_terminal_tick_after_impulse_fault_clears_the_tick_flag(self):
        """A nullázás a terminális return ELŐTT fut (R2-06 minta): a FAULT-
        ot okozó IMPULSE-tick még False, a következő (terminális) tickben a
        pulzusszolgáltatás már nem tulajdonos."""
        ctrl, psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.5)
        dmm_v.timed_voltage_fn = lambda t: 12.3
        ctrl._state = OcvSocState.IMPULSE_ON
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.IMPULSE_ACQUIRE
        psu.output_commanded_on = True
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.dmm_valid is False

        ctrl.advance(0.0)  # terminális tick

        assert ctrl._dmm_owned_by_pulse_service_this_tick is False
        assert ctrl.dmm_valid is ctrl._dmm_valid

    def test_build_sample_marks_impulse_tick_voltage_invalid_without_extra_dmm_read(
        self, tmp_path
    ):
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        runner, logger = _f23_runner_sharing_instruments(tmp_path, psu, load, dmm_v, dmm_t)
        from Prog.src.test_runner import StepKind, TestStep
        step = TestStep(StepKind.OCV_SOC, "ocv_soc")

        ctrl._state = OcvSocState.IMPULSE_PREP
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.IMPULSE_ON
        prep_sample = runner._build_sample(step, ctrl)
        assert prep_sample["dmm_voltage_valid"] is True

        ctrl.advance(0.0)  # IMPULSE_ON -> IMPULSE_ACQUIRE
        dmm_v.reset_calls()
        ctrl.advance(0.0)  # IMPULSE_ACQUIRE chunk
        assert ctrl.state == OcvSocState.IMPULSE_ACQUIRE
        calls_before_sample = dmm_v.calls.count("read_voltage()")
        sample = runner._build_sample(step, ctrl)

        assert sample["dmm_voltage_valid"] is False
        # utolsó ismert (pulzus előtti) érték marad, nem None
        assert sample["battery_voltage_V"] == pytest.approx(13.5)
        # nincs második (runner-oldali) DMM-olvasás
        assert dmm_v.calls.count("read_voltage()") == calls_before_sample == 0
        logger.close()

    def test_after_pulse_log_point_and_discharge_continue_without_dmm_feedback_lost(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, capacity_Ah=7.0
        )
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        assert _run_full_impulse(ctrl) == OcvSocState.LOG_SOC_POINT

        states = []
        for _ in range(3):
            states.append(ctrl.advance(1.0))
            assert ctrl.dmm_valid is True

        assert OcvSocState.FAULT not in states
        assert "DMM_FEEDBACK_LOST" not in (ctrl.fault_reason or "")

    def test_precharge_and_non_impulse_samples_unchanged(self, tmp_path):
        # PRECHARGE: a beágyazott ChargeController értéke megy át.
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.34)
        runner, logger = _f23_runner_sharing_instruments(tmp_path, psu, load, dmm_v, dmm_t)
        from Prog.src.test_runner import StepKind, TestStep
        step = TestStep(StepKind.OCV_SOC, "ocv_soc")
        ctrl.advance(0.0)  # INIT -> PRECHARGE
        ctrl.advance(1.0)  # PRECHARGE tick
        assert ctrl.state == OcvSocState.PRECHARGE
        assert ctrl.dmm_valid is True
        assert runner._build_sample(step, ctrl)["dmm_voltage_valid"] is True
        logger.close()

        # Nem-impulzus (STEP_RELAX) tick: a privát _dmm_valid megy át,
        # érvényes és érvénytelen olvasásnál egyaránt.
        ctrl2, _psu2, _load2, dmm_v2, _dmm_t2 = make_ocv_soc_controller(dmm_voltage_V=12.6)
        ctrl2._state = OcvSocState.STEP_RELAX
        ctrl2.advance(1.0)
        assert ctrl2.dmm_valid is True
        dmm_v2.set_dmm_invalid()
        ctrl2.advance(1.0)
        assert ctrl2._dmm_valid is False
        assert ctrl2.dmm_valid is False


class TestR1B3bDischargeStartBatteryVoltageGate:
    """[regresszió, Claude-Codex loop R1 B3b: NS-02 (DFAM-001)]

    `_begin_step_discharge()` a B3 DMM-őr után `check_battery_voltage()` nélkül adott
    Load INPUT ON-t: egy érvényes, de a meglévő safety-limiten
    (`SafetyManager.check_battery_voltage()`: U_batt <= 0 V vagy
    > `batt_absolute_max_V`) kívüli U_batt csak a KÖVETKEZŐ tick per-tick
    kapujában (`_run_step_discharge()`) okozott FAULT-ot, az energizálás UTÁN. Elvárt
    sorrend: DMM-őr -> feszültségkapu -> concurrent-kapu -> setup (a
    `_run_rebound_check()` és az OCV IMPULSE_PREP meglévő sorrendje).
    A kapu a meglévő limitet alkalmazza, új/szigorúbb limitet nem vezet be
    (határ-karakterizáció)."""

    @staticmethod
    def _bad_voltage(ctrl, case: str) -> tuple[float, str]:
        return {
            "zero": (0.0, "REVERSE_POLARITY_OR_WIRING_FAULT"),
            "negative": (-0.5, "REVERSE_POLARITY_OR_WIRING_FAULT"),
            "above_abs_max": (ctrl._profile.batt_absolute_max_V + 0.01, "BATTERY_OVERVOLTAGE"),
        }[case]

    @staticmethod
    def _assert_faulted_without_energize(ctrl, psu, load, expected_reason: str) -> None:
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == expected_reason
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls

    @staticmethod
    def _make():
        ctrl, psu, load, *_ = make_ocv_soc_controller(dmm_voltage_V=12.5)
        ctrl._state = OcvSocState.STEP_DISCHARGE
        return ctrl, psu, load

    @pytest.mark.parametrize("case", ["zero", "negative", "above_abs_max"])
    def test_bad_voltage_faults_before_energizing(self, case):
        ctrl, psu, load = self._make()
        u, reason = self._bad_voltage(ctrl, case)
        ctrl._u_batt = u

        ctrl._begin_step_discharge()

        self._assert_faulted_without_energize(ctrl, psu, load, reason)
        load.reset_calls()
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert "input_on()" not in load.calls

    def test_dmm_guard_precedes_voltage_gate(self):
        """Sorrend a (karakterizáció): DMM-hiba + 0 V -> a B3 DMM-őr nyer."""
        ctrl, _psu, load = self._make()
        ctrl._dmm_valid = False
        ctrl._u_batt = 0.0

        ctrl._begin_step_discharge()

        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls

    def test_voltage_gate_precedes_concurrent_gate(self):
        """Sorrend b: PSU commanded ON + 0 V -> a feszültségkapu nyer, nem a concurrent-kapu."""
        ctrl, psu, load = self._make()
        psu.output_commanded_on = True
        ctrl._u_batt = 0.0

        ctrl._begin_step_discharge()

        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"
        assert "input_on()" not in load.calls

    def test_exact_abs_max_still_energizes(self):
        """Határ (karakterizáció): pontosan batt_absolute_max_V mellett még
        energizál -- nincs új, szigorúbb limit."""
        ctrl, _psu, load = self._make()
        ctrl._u_batt = ctrl._profile.batt_absolute_max_V

        ctrl._begin_step_discharge()

        assert "input_on()" in load.calls
        assert ctrl.state != OcvSocState.FAULT

    def test_chain_log_soc_point_zero_volt_faults_before_next_step_energize(self):
        """Lánc: valódi LOG_SOC_POINT tick, friss, érvényes 0 V DMM-olvasás ->
        FAULT a következő kisütési lépés energizálása ELŐTT."""
        ctrl, psu, load, dmm_v, _ = make_ocv_soc_controller(dmm_voltage_V=12.5, capacity_Ah=7.0)
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._last_ocv_V = 12.3
        load.reset_calls()
        psu.reset_calls()
        dmm_v.voltage_V = 0.0

        ctrl.advance(1.0)

        self._assert_faulted_without_energize(
            ctrl, psu, load, "REVERSE_POLARITY_OR_WIRING_FAULT"
        )


# ------------------------------------------------------------------ #
# R2-B4 (R2-metrology-01, DFAM-033): impulzusenergia inkrementális     #
# könyvelése                                                           #
# ------------------------------------------------------------------ #

class _R2B4CurrentSeq:
    """Változó mért áram: a MockLoad hívásnaplója megmarad, de a
    visszaadott érték a megadott sorozatból jön (ciklikusan)."""

    def __init__(self, load, values):
        self._real = load.measure_current
        self._values = values
        self._k = 0

    def __call__(self):
        self._real()
        value = self._values[self._k % len(self._values)]
        self._k += 1
        return value


class _R2B4FinalizeRecorder:
    """A finalize() eredményének removed_Ah/removed_Wh-ját rögzíti --
    a normál út független elvárt értéke a finalize() összege."""

    def __init__(self, service, sink):
        self._real = service.finalize
        self._sink = sink

    def __call__(self):
        result = self._real()
        self._sink.append((result.removed_Ah, result.removed_Wh))
        return result


class TestR2B4PulseEnergyIncrementalBooking:
    """[regresszió, R2-B4 (R2-metrology-01), DFAM-033 RECURRING] Az
    impulzusenergia korábban KIZÁRÓLAG a PulseMeasurementService.finalize()-
    ben keletkezett, és csak a normál _finish_*() vette át -- megszakított
    (chunk-hiba / Stop / E-stop) std vagy fast impulzusnál a már mért Ah/Wh
    kimaradt a controller-, TestResult- és checkpoint-összegből. A fix a
    mért prefixet minden sikeres step_chunk() után könyveli, a normál
    lezárás csak a maradékot (OFF-farok) adja hozzá, az emergency_stop() a
    két OFF és a FAULT UTÁN a már rögzített prefixet idempotensen átveszi
    (C1), majd egyszeri OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY eseményt ad.
    Az elvárt értékek a mock-bemenetből (áram × idő, × feszültség)
    számolódnak. Unit/mock bizonyíték -- az OFF-késés és a farok nagysága
    HIL_PENDING."""

    I_PULSE = 7.0 / 5
    V_PULSE = 12.3
    CLOCK_STEP = 0.6
    EVENT = "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"

    # -------------------------------------------------------------- #
    @staticmethod
    def _runner(ctrl, psu, load, dmm_v, dmm_t, tmp_path):
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.test_runner import TestRunner, TestRunnerConfig

        logger = Logger(session_dir=tmp_path, config=LogConfig())
        runner = TestRunner(
            instrument_manager=InstrumentManager(
                psu=psu, load=load, dmm_voltage=dmm_v, dmm_temperature=dmm_t,
            ),
            safety=ctrl._safety,
            logger=logger,
            profile=ctrl._profile,
            config=TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="t"),
            charge_ctrl_factory=lambda: None,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
            ocv_soc_ctrl_factory=lambda: ctrl,
        )
        return runner, logger

    @staticmethod
    def _run_plan(runner, logger, tmp_path):
        import json

        from Prog.src.test_runner import StepKind, TestPlan, TestStep, TestType

        try:
            result = runner.run(TestPlan(
                test_type=TestType.OCV_SOC_CHARACTERIZATION,
                steps=(TestStep(StepKind.OCV_SOC, "ocv_soc"),),
            ))
        finally:
            logger.close()
        ckpt = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
        return result, ckpt

    def _events(self, events):
        return [e for e in events if e["event_code"] == self.EVENT]

    @staticmethod
    def _watch_runner_pulse(monkeypatch, runner, ctrl, load, dmm_v, clock, *, fast=False):
        """B21b: valós worker, műszerhookok és független fetch-időbélyegek."""
        from queue import Empty
        from threading import get_ident

        import Prog.src.pulse_measurement_service as pulse_module

        acq = OcvSocState.IMPULSE_FAST_ACQUIRE if fast else OcvSocState.IMPULSE_ACQUIRE
        on_state = OcvSocState.IMPULSE_FAST_ON if fast else OcvSocState.IMPULSE_ON
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        control_thread = get_ident()
        st = {
            "base": None, "origin": None, "chunks": [], "readings": [],
            "trigger_fired": False, "runner_samples": [], "sampling": False,
            "cutoff_v": None, "accepting": False,
        }
        bind_pulse_instrument_latency(clock, dmm_v, load)
        real_on, real_fetch, real_current = (
            load.input_on, dmm_v.fetch_timed_acquisition, load.measure_current,
        )

        def input_on(*args, **kwargs):
            if ctrl.state == on_state:
                st["base"] = (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh)
                st["origin"] = clock()
            return real_on(*args, **kwargs)

        def fetch(**kwargs):
            raw = real_fetch(**kwargs)
            if ctrl.state == acq:
                st["chunks"].append((
                    clock() - st["origin"],
                    [float(v) for v in raw.split(",")], dmm_v._timed_interval_s,
                ))
            return raw

        def current():
            assert get_ident() == control_thread
            value = real_current()
            if st["accepting"] and not st["sampling"]:
                ended, values, interval = st["chunks"][-1]
                last = next(
                    (i for i, v in enumerate(values) if st["cutoff_v"] is not None and v <= st["cutoff_v"]),
                    len(values) - 1,
                )
                # A fetch-vég és a mock SAMP:TIM adja a tényleges mintabélyeget;
                # cutoffnál csak az első alacsony mintáig tart a prefix.
                stamp = ended - (len(values) - 1 - last) * interval
                st["readings"].append((stamp, value, values[last]))
            return value

        real_step = service.step_chunk

        def step(*args, **kwargs):
            st["accepting"] = True
            try:
                return real_step(*args, **kwargs)
            finally:
                st["accepting"] = False

        monkeypatch.setattr(service, "step_chunk", step)

        def bounded_sample(sample):
            st["runner_samples"].append(sample)
            if len(st["runner_samples"]) >= 24:
                runner.request_stop()

        # A virtuális idő üres Queue-várakozáskor is telik. Így egy elmaradt
        # trigger a pulse deadline-ja, majd a 24. runner-minta után ASSERTION,
        # és nem végtelen ciklus (a valós worker/kapuk változatlanok).
        worker_class = getattr(pulse_module, "_PulseDmmWorker", None)
        worker_init = worker_class.__init__ if worker_class is not None else None

        def initialize_worker(worker, pulse_service):
            assert worker_init is not None
            worker_init(worker, pulse_service)
            real_get = worker.results.get

            def get_result(*args, **kwargs):
                try:
                    return real_get(*args, **kwargs)
                except Empty:
                    clock.advance(kwargs.get("timeout", 0.0))
                    raise

            monkeypatch.setattr(worker.results, "get", get_result)

        if worker_class is not None:
            monkeypatch.setattr(worker_class, "__init__", initialize_worker)
        monkeypatch.setattr(load, "input_on", input_on)
        monkeypatch.setattr(dmm_v, "fetch_timed_acquisition", fetch)
        monkeypatch.setattr(load, "measure_current", current)
        runner.on_sample = bounded_sample
        st["service"] = service
        return st

    @staticmethod
    def _expected_runner_prefix(st):
        # Csak mock-bemenet és megfigyelt fetch-/mintabélyeg; sem finalize(),
        # sem measured_energy_so_far(), sem a prod integrátor nem orákulum.
        previous = 0.0
        ah = wh = 0.0
        for stamp, current, voltage in st["readings"]:
            assert stamp > previous
            ah += current * (stamp - previous) / 3600.0
            wh += current * voltage * (stamp - previous) / 3600.0
            previous = stamp
        return ah, wh

    @staticmethod
    def _run_pulse(ctrl):
        ctrl._enter(OcvSocState.IMPULSE_ON)
        ctrl.advance(0.0)
        while ctrl.state in (
            OcvSocState.IMPULSE_ACQUIRE,
            OcvSocState.IMPULSE_FAST_ON,
            OcvSocState.IMPULSE_FAST_ACQUIRE,
        ):
            ctrl.advance(0.0)

    def _one_chunk_into_std_pulse(self, **load_flags):
        clock = FakeMonotonicClock(step_s=self.CLOCK_STEP)
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, pulse_time_source=clock,
        )
        dmm_v.timed_voltage_fn = lambda t: self.V_PULSE
        for name, value in load_flags.items():
            setattr(load, name, value)
        ctrl._enter(OcvSocState.IMPULSE_ON)
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.IMPULSE_ACQUIRE
        return ctrl, psu, load, dmm_v, dmm_t

    # -------------------------------------------------------------- #
    # (a) runner-lánc: {std, fast} x {chunk-hiba, Stop, E-stop}
    # -------------------------------------------------------------- #
    @pytest.mark.parametrize("fast", [False, True], ids=["std", "fast"])
    @pytest.mark.parametrize("how", ["chunk_error", "stop", "estop"])
    def test_aborted_pulse_books_measured_part_once(self, monkeypatch, how, fast, tmp_path):
        cfg = OcvSocConfig(
            fast_transient_enabled=fast, impulse_duration_s=1.2 if fast else 30.0,
        )
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, config=cfg, pulse_time_source=clock,
        )
        dmm_v.timed_voltage_fn = lambda t: self.V_PULSE
        ctrl._state = OcvSocState.IMPULSE_PREP
        events: list[dict] = []
        ctrl.on_event = events.append
        runner, logger = self._runner(ctrl, psu, load, dmm_v, dmm_t, tmp_path)
        st = self._watch_runner_pulse(
            monkeypatch, runner, ctrl, load, dmm_v, clock, fast=fast,
        )
        real_fetch = dmm_v.fetch_timed_acquisition

        def fetch(**kwargs):
            if len(st["readings"]) == 1 and not st["trigger_fired"]:
                # Az első chunkot a PMS már elfogadta; a második fetch
                # belsejében még aktív terhelés mellett szakad meg a pulse.
                assert len(st["service"]._current_readings) == 1
                assert load.input_commanded_on
                st["trigger_fired"] = True
                if how == "chunk_error":
                    dmm_v.simulate_timed_acquisition_failure = True
                elif how == "stop":
                    runner.request_stop()
                else:
                    runner.request_emergency_stop("USER_EMERGENCY_STOP")
            return real_fetch(**kwargs)

        monkeypatch.setattr(dmm_v, "fetch_timed_acquisition", fetch)
        result, ckpt = self._run_plan(runner, logger, tmp_path)

        assert st["trigger_fired"], "A második fetch megszakítása elmaradt"
        assert len(st["readings"]) == 1
        base_ah, base_wh = st["base"]
        part_ah, part_wh = self._expected_runner_prefix(st)
        exp_ah, exp_wh = base_ah + part_ah, base_wh + part_wh
        assert ctrl.accumulated_discharge_Ah == pytest.approx(exp_ah, rel=1e-9)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(exp_wh, rel=1e-9)
        assert result.total_discharge_ah == pytest.approx(exp_ah, rel=1e-9)
        assert result.total_discharge_wh == pytest.approx(exp_wh, rel=1e-9)
        assert ckpt["discharge_ah"] == pytest.approx(exp_ah, rel=1e-9)
        assert ckpt["discharge_wh"] == pytest.approx(exp_wh, rel=1e-9)
        assert result.status == ("STOPPED" if how == "stop" else "FAULT")
        if how == "chunk_error":
            assert "ACQUISITION_FAILED" in result.reason
            assert "simulated timed acquisition failure" in ctrl.fault_reason
        else:
            assert result.reason == ("USER_STOP_REQUESTED" if how == "stop" else "USER_EMERGENCY_STOP")
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        n_on = 2 if fast else 1
        assert load.calls.count("input_on()") == n_on
        assert len(self._events(events)) == 1

        # Ismételt controller- és runner-abort: nincs új könyvelés, esemény, ON.
        before = (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh)
        ctrl.emergency_stop("AGAIN")
        runner._active_controller = ctrl
        runner._abort_active_controller("AGAIN2")
        assert (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh) == before
        assert len(self._events(events)) == 1
        assert load.calls.count("input_on()") == n_on

    # -------------------------------------------------------------- #
    # C1: terminális (cutoff/target-ági) OFF-hiba a leolvasás rögzítése
    # UTÁN -> a már rögzített prefix is könyvelődik
    # -------------------------------------------------------------- #
    @pytest.mark.parametrize("fast", [False, True], ids=["std", "fast"])
    @pytest.mark.parametrize("branch", ["target", "cutoff"])
    def test_terminal_off_failure_after_recorded_reading_books_full_prefix(
        self, monkeypatch, branch, fast, tmp_path,
    ):
        """C1: a terminális OFF a két már rögzített leolvasás után hibázik.

        Két 0,4 s-os chunk a változatlan 1,2 s célablakon belül; cutoffnál
        a második chunk első alacsony mintája zár. A határidős OFF az utolsó
        fetch után már nem indít új áramolvasást. A két mért prefix megmarad.
        """
        cfg = OcvSocConfig(
            impulse_duration_s=1.2,
            pulse_chunk_sample_count=2,
            fast_transient_enabled=fast,
            fast_transient_chunk_sample_count=1,
            fast_transient_sample_interval_s=0.4,
            fast_transient_duration_s=1.2,
            fast_transient_target_times_s=(0.6, 1.2),
            fast_transient_time_tolerance_s=0.01,
        )
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, config=cfg, pulse_time_source=clock,
        )
        _set_pulse_test_cutoff(ctrl)
        v_low = ctrl._profile.terminate_voltage_pack_V - 0.5
        ctrl._state = OcvSocState.IMPULSE_PREP
        events: list[dict] = []
        runner, logger = self._runner(ctrl, psu, load, dmm_v, dmm_t, tmp_path)
        st = self._watch_runner_pulse(
            monkeypatch, runner, ctrl, load, dmm_v, clock, fast=fast,
        )
        st.update({
            "armed": False, "low": False, "cutoff_v": ctrl._profile.terminate_voltage_pack_V,
            "off_failed": 0, "n_event": 0,
        })
        dmm_v.timed_voltage_fn = lambda t: v_low if st["low"] else self.V_PULSE
        real_off = load.input_off

        def input_off():
            if st["armed"]:
                st["armed"] = False
                st["off_failed"] += 1
                assert len(st["service"]._current_readings) == 2
                assert load.input_commanded_on
                raise RuntimeError("simulated terminal load off failure")
            real_off()

        load.input_off = input_off  # type: ignore[method-assign]
        ctrl.on_event = events.append
        real_fetch = dmm_v.fetch_timed_acquisition

        def fetch(**kwargs):
            if len(st["readings"]) == 1 and not st["trigger_fired"]:
                assert len(st["service"]._current_readings) == 1
                assert load.input_commanded_on
                st["trigger_fired"] = True
                st["armed"] = True
                st["low"] = branch == "cutoff"
            return real_fetch(**kwargs)

        monkeypatch.setattr(dmm_v, "fetch_timed_acquisition", fetch)
        result, ckpt = self._run_plan(runner, logger, tmp_path)

        assert st["trigger_fired"], "A második chunk OFF-hibája nem élesedett"
        assert st["off_failed"] == 1
        assert len(st["readings"]) == 2
        part_ah, part_wh = self._expected_runner_prefix(st)
        base_ah, base_wh = st["base"]
        exp_ah, exp_wh = base_ah + part_ah, base_wh + part_wh
        assert ctrl.accumulated_discharge_Ah == pytest.approx(exp_ah, rel=1e-12)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(exp_wh, rel=1e-12)
        assert result.total_discharge_ah == pytest.approx(exp_ah, rel=1e-9)
        assert result.total_discharge_wh == pytest.approx(exp_wh, rel=1e-9)
        assert ckpt["discharge_ah"] == pytest.approx(exp_ah, rel=1e-9)
        assert ckpt["discharge_wh"] == pytest.approx(exp_wh, rel=1e-9)
        assert result.status == "FAULT"
        assert ctrl.state == OcvSocState.FAULT
        assert "ACQUISITION_FAILED" in ctrl.fault_reason
        assert "simulated terminal load off failure" in ctrl.fault_reason
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert load.calls.count("input_on()") == (2 if fast else 1)
        ev = self._events(events)
        assert len(ev) == 1
        # az esemény a könyvelt prefixet közli: mindkét leolvasást
        assert "(2 áramleolvasás" in ev[0]["event_message"]
        assert f"{part_ah:.6f} Ah" in ev[0]["event_message"]

    # -------------------------------------------------------------- #
    # R2-B4-V1 (fixup): a terminális OFF BaseException-t (KeyboardInterrupt/
    # SystemExit) dob a 2. leolvasás rögzítése UTÁN
    # -------------------------------------------------------------- #
    @pytest.mark.parametrize("raising_event", [False, True], ids=["event_ok", "event_raises"])
    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("fast", [False, True], ids=["std", "fast"])
    @pytest.mark.parametrize("branch", ["target", "cutoff"])
    def test_base_exception_from_terminal_off_books_prefix_before_runner_flush(
        self, monkeypatch, branch, fast, exc_type, raising_event, tmp_path,
    ):
        """A controller acquisition-kezelője korábban csak Exception-t fogott:
        egy, a 2. leolvasás rögzítése UTÁN a terminális OFF-ból dobott
        KeyboardInterrupt/SystemExit megkerülte, a runner finally-
        _flush_totals()-a még az 1. chunk összegét vette át, és a prefix
        csak a runner későbbi külső emergency_stop()-jában került a
        controllerbe -- a checkpoint/runner-összeg egy chunkkal kevesebb
        lett. Elvárt: OFF, FAULT, a teljes prefix a controllerben, a
        runner-összegben és a checkpointban, 1 esemény, és az EREDETI
        kivételobjektum propagál (dobó on_event mellett is)."""
        from Prog.src.test_runner import StepKind, TestPlan, TestStep, TestType

        cfg = OcvSocConfig(
            impulse_duration_s=1.2,
            pulse_chunk_sample_count=2,
            fast_transient_enabled=fast,
            fast_transient_chunk_sample_count=1,
            fast_transient_sample_interval_s=0.4,
            fast_transient_duration_s=1.2,
            fast_transient_target_times_s=(0.6, 1.2),
            fast_transient_time_tolerance_s=0.01,
        )
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, config=cfg, pulse_time_source=clock,
        )
        _set_pulse_test_cutoff(ctrl)
        v_low = ctrl._profile.terminate_voltage_pack_V - 0.5
        interrupt = exc_type("simulated terminal load off interrupt")
        ctrl._state = OcvSocState.IMPULSE_PREP
        events: list[dict] = []
        runner, logger = self._runner(ctrl, psu, load, dmm_v, dmm_t, tmp_path)
        st = self._watch_runner_pulse(
            monkeypatch, runner, ctrl, load, dmm_v, clock, fast=fast,
        )
        st.update({
            "armed": False, "low": False, "cutoff_v": ctrl._profile.terminate_voltage_pack_V,
            "off_raised": 0, "n_event": 0,
        })
        dmm_v.timed_voltage_fn = lambda t: v_low if st["low"] else self.V_PULSE
        real_off = load.input_off

        def input_off():
            if st["armed"]:
                st["armed"] = False
                st["off_raised"] += 1
                assert len(st["service"]._current_readings) == 2
                assert load.input_commanded_on
                raise interrupt
            real_off()

        load.input_off = input_off  # type: ignore[method-assign]

        def on_event(event):
            events.append(event)
            if event["event_code"] == self.EVENT:
                st["n_event"] += 1
                st["at_event"] = (
                    ctrl.state, load.input_commanded_on, psu.output_commanded_on,
                )
                if raising_event:
                    raise RuntimeError("event-handler-raised")

        ctrl.on_event = on_event
        real_fetch = dmm_v.fetch_timed_acquisition

        def fetch(**kwargs):
            if len(st["readings"]) == 1 and not st["trigger_fired"]:
                assert len(st["service"]._current_readings) == 1
                assert load.input_commanded_on
                st["trigger_fired"] = True
                st["armed"] = True
                st["low"] = branch == "cutoff"
            return real_fetch(**kwargs)

        monkeypatch.setattr(dmm_v, "fetch_timed_acquisition", fetch)
        try:
            with pytest.raises(exc_type) as excinfo:
                runner.run(TestPlan(
                    test_type=TestType.OCV_SOC_CHARACTERIZATION,
                    steps=(TestStep(StepKind.OCV_SOC, "ocv_soc"),),
                ))
                assert st["trigger_fired"], "A második chunk OFF-hibája nem élesedett"
        finally:
            logger.close()
        import json

        ckpt = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))

        assert excinfo.value is interrupt
        assert st["trigger_fired"], "A második chunk OFF-hibája nem élesedett"
        assert st["off_raised"] == 1
        assert len(st["readings"]) == 2
        part_ah, part_wh = self._expected_runner_prefix(st)
        base_ah, base_wh = st["base"]
        exp_ah, exp_wh = base_ah + part_ah, base_wh + part_wh
        assert ctrl.accumulated_discharge_Ah == pytest.approx(exp_ah, rel=1e-12)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(exp_wh, rel=1e-12)
        # a runner végleges összege (TestResult-forrás) és a checkpoint
        assert runner._total_discharge_ah == pytest.approx(exp_ah, rel=1e-9)
        assert runner._total_discharge_wh == pytest.approx(exp_wh, rel=1e-9)
        assert ckpt["discharge_ah"] == pytest.approx(exp_ah, rel=1e-9)
        assert ckpt["discharge_wh"] == pytest.approx(exp_wh, rel=1e-9)
        assert ckpt["status"] == "FAULT"
        assert ckpt["reason"].startswith("UNHANDLED_EXCEPTION")
        assert ctrl.state == OcvSocState.FAULT
        assert "ACQUISITION_FAILED" in ctrl.fault_reason
        assert "simulated terminal load off interrupt" in ctrl.fault_reason
        assert st["n_event"] == 1
        assert st["at_event"] == (OcvSocState.FAULT, False, False)
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert load.calls.count("input_on()") == (2 if fast else 1)

    # -------------------------------------------------------------- #
    # C3: dobó on_event -- OFF és FAULT előtte, az energia megmarad
    # -------------------------------------------------------------- #
    def test_raising_event_handler_on_direct_emergency_stop(self):
        ctrl, psu, load, _dmm_v, _dmm_t = self._one_chunk_into_std_pulse()
        seen: dict = {}

        def on_event(event):
            if event["event_code"] == self.EVENT:
                seen["at_event"] = (
                    ctrl.state, load.input_commanded_on, psu.output_commanded_on,
                )
                raise RuntimeError("event-handler-raised")

        ctrl.on_event = on_event
        n_on = load.calls.count("input_on()")
        with pytest.raises(RuntimeError, match="event-handler-raised"):
            ctrl.emergency_stop("USER_EMERGENCY_STOP")

        assert seen["at_event"] == (OcvSocState.FAULT, False, False)
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == "USER_EMERGENCY_STOP"
        assert "all_outputs_off()" in psu.calls
        assert ctrl.accumulated_discharge_Ah == pytest.approx(
            self.I_PULSE * self.CLOCK_STEP / 3600.0, rel=1e-9,
        )
        assert load.calls.count("input_on()") == n_on

    def test_partial_energy_event_carries_explicit_warning_severity(self):
        """[regresszió, R3-B3 (R3-GUI-1, P3), DFAM-018 RECURRING] az
        OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY kódneve semleges, a negatív
        jelentés (a session-összegből hiányzó, nem becsült szakasz) a
        payloadban van -- a központi heurisztika INFO-nak minősítette. A
        producer minősít: explicit WARNING."""
        ctrl, *_ = self._one_chunk_into_std_pulse()
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl.emergency_stop("USER_EMERGENCY_STOP")
        ev = self._events(events)
        assert len(ev) == 1
        assert ev[0]["severity"] == "WARNING"

    @pytest.mark.parametrize("how", ["chunk_error", "estop"])
    def test_raising_event_handler_in_runner_chain_keeps_energy(self, monkeypatch, how, tmp_path):
        """Belső út (chunk-hiba): a callback kivétele a runner unhandled-
        exception safe-offjába fut -> checkpoint UNHANDLED_EXCEPTION, a
        controller eredeti oka a fault_reason-ben. Külső út (E-stop): a
        runner _abort_active_controller() védi, a checkpoint oka marad."""
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, pulse_time_source=clock,
        )
        dmm_v.timed_voltage_fn = lambda t: self.V_PULSE
        ctrl._state = OcvSocState.IMPULSE_PREP
        runner, logger = self._runner(ctrl, psu, load, dmm_v, dmm_t, tmp_path)
        st = self._watch_runner_pulse(monkeypatch, runner, ctrl, load, dmm_v, clock)
        st["n_event"] = 0

        def on_event(event):
            if event["event_code"] == self.EVENT:
                st["n_event"] += 1
                st["at_event"] = (
                    ctrl.state, load.input_commanded_on, psu.output_commanded_on,
                )
                raise RuntimeError("event-handler-raised")

        ctrl.on_event = on_event

        real_fetch = dmm_v.fetch_timed_acquisition

        def fetch(**kwargs):
            if len(st["readings"]) == 1 and not st["trigger_fired"]:
                assert len(st["service"]._current_readings) == 1
                assert load.input_commanded_on
                st["trigger_fired"] = True
                if how == "chunk_error":
                    dmm_v.simulate_timed_acquisition_failure = True
                else:
                    runner.request_emergency_stop("USER_EMERGENCY_STOP")
            return real_fetch(**kwargs)

        monkeypatch.setattr(dmm_v, "fetch_timed_acquisition", fetch)
        result, ckpt = self._run_plan(runner, logger, tmp_path)

        assert st["trigger_fired"], "A második fetch megszakítása elmaradt"
        assert len(st["readings"]) == 1
        base_ah, base_wh = st["base"]
        part_ah, part_wh = self._expected_runner_prefix(st)
        exp_ah, exp_wh = base_ah + part_ah, base_wh + part_wh
        assert st["n_event"] == 1
        assert st["at_event"] == (OcvSocState.FAULT, False, False)
        assert result.status == "FAULT"
        assert ckpt["status"] == "FAULT"
        if how == "chunk_error":
            assert ckpt["reason"].startswith("UNHANDLED_EXCEPTION")
            assert "event-handler-raised" in ckpt["reason"]
            assert "IMPULSE_ACQUISITION_FAILED" in ctrl.fault_reason
        else:
            assert ckpt["reason"] == "USER_EMERGENCY_STOP"
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.accumulated_discharge_Ah == pytest.approx(exp_ah, rel=1e-9)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(exp_wh, rel=1e-9)
        assert result.total_discharge_ah == pytest.approx(exp_ah, rel=1e-9)
        assert result.total_discharge_wh == pytest.approx(exp_wh, rel=1e-9)
        assert ckpt["discharge_ah"] == pytest.approx(exp_ah, rel=1e-9)
        assert ckpt["discharge_wh"] == pytest.approx(exp_wh, rel=1e-9)
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert load.calls.count("input_on()") == 1

    # -------------------------------------------------------------- #
    # (b) kétpulzusos normál út: összeg == sum finalize().removed_*
    # -------------------------------------------------------------- #
    @pytest.mark.parametrize("fast", [False, True], ids=["std", "fast"])
    def test_normal_path_equals_finalize_sum_two_pulses(self, fast):
        cfg = OcvSocConfig(fast_transient_enabled=fast)
        ctrl, _psu, load, dmm_v, _dmm_t = make_ocv_soc_controller(dmm_voltage_V=12.5, config=cfg)
        dmm_v.timed_voltage_fn = lambda t: 12.3 - 0.004 * t
        load.measure_current = _R2B4CurrentSeq(load, [1.40, 1.37, 1.43, 1.39])  # type: ignore[method-assign]
        finals: list[tuple[float, float]] = []
        ctrl._pulse_service.finalize = _R2B4FinalizeRecorder(ctrl._pulse_service, finals)  # type: ignore[method-assign]
        if fast:
            ctrl._fast_pulse_service.finalize = _R2B4FinalizeRecorder(  # type: ignore[method-assign]
                ctrl._fast_pulse_service, finals,
            )
        self._run_pulse(ctrl)
        ctrl._impulse_removed_Ah = 0.0
        self._run_pulse(ctrl)
        assert len(finals) == (4 if fast else 2)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(sum(a for a, _ in finals), rel=1e-12)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(sum(w for _, w in finals), rel=1e-12)

    @pytest.mark.parametrize("fast", [False, True], ids=["std", "fast"])
    def test_soc_grid_impulse_removed_keeps_full_finalize_value(self, fast):
        """Az _impulse_removed_Ah (SOC-rács) a TELJES finalize().removed_Ah-t
        kapja, nem a removed - booked maradékot -- std-nél `=`, a fast
        rétegnél `+=` a TELJES fast finalize()-értékkel (R2-B4 fixup, T-1)."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, config=OcvSocConfig(fast_transient_enabled=fast),
        )
        dmm_v.timed_voltage_fn = lambda t: 12.3
        finals: list[tuple[float, float]] = []
        ctrl._pulse_service.finalize = _R2B4FinalizeRecorder(ctrl._pulse_service, finals)  # type: ignore[method-assign]
        if fast:
            ctrl._fast_pulse_service.finalize = _R2B4FinalizeRecorder(  # type: ignore[method-assign]
                ctrl._fast_pulse_service, finals,
            )
        self._run_pulse(ctrl)
        assert len(finals) == (2 if fast else 1)
        full_ah = sum(a for a, _ in finals)
        assert ctrl._impulse_removed_Ah == pytest.approx(full_ah, rel=1e-12)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(full_ah, rel=1e-12)

    # -------------------------------------------------------------- #
    # (c) esemény csak ACQUIRE-ben; (d) degraded jelzés
    # -------------------------------------------------------------- #
    @pytest.mark.parametrize(
        "state",
        [OcvSocState.IMPULSE_ON, OcvSocState.IMPULSE_FAST_ON, OcvSocState.STEP_DISCHARGE],
    )
    def test_no_abort_event_outside_active_pulse_acquisition(self, state):
        ctrl, *_ = make_ocv_soc_controller(config=OcvSocConfig(fast_transient_enabled=True))
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._state = state
        before = (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh)
        ctrl.emergency_stop("X")
        assert ctrl.state == OcvSocState.FAULT
        assert not self._events(events)
        assert (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh) == before

    def test_abort_event_text_states_unbooked_remainder_not_estimated(self):
        ctrl, *_ = self._one_chunk_into_std_pulse()
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl.emergency_stop("USER_EMERGENCY_STOP")
        ev = self._events(events)
        assert len(ev) == 1
        msg = ev[0]["event_message"]
        assert "(1 áramleolvasás" in msg
        assert "utolsó könyvelt leolvasás utáni szakasz (ha volt terhelés) NINCS benne" in msg
        assert "nincs becsülve" in msg
        assert "pulse_current_degraded" not in msg

    def test_abort_event_marks_degraded_part(self):
        ctrl, *_ = self._one_chunk_into_std_pulse(simulate_current_readback_failure=True)
        # setpoint-fallback (1,4 A) a könyvelt részben
        assert ctrl.accumulated_discharge_Ah == pytest.approx(
            self.I_PULSE * self.CLOCK_STEP / 3600.0, rel=1e-9,
        )
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl.emergency_stop("USER_EMERGENCY_STOP")
        ev = self._events(events)
        assert len(ev) == 1
        assert "pulse_current_degraded" in ev[0]["event_message"]

    # -------------------------------------------------------------- #
    # (e) chunkonként növekvő összeg (controller és runner-minta)
    # -------------------------------------------------------------- #
    def test_accumulated_discharge_grows_per_chunk(self):
        clock = FakeMonotonicClock(step_s=self.CLOCK_STEP)
        ctrl, _psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, pulse_time_source=clock,
        )
        dmm_v.timed_voltage_fn = lambda t: self.V_PULSE
        ctrl._enter(OcvSocState.IMPULSE_ON)
        ctrl.advance(0.0)
        seen = []
        for _ in range(3):
            ctrl.advance(0.0)
            seen.append(ctrl.accumulated_discharge_Ah)
        assert ctrl.state == OcvSocState.IMPULSE_ACQUIRE
        assert seen == pytest.approx(
            [self.I_PULSE * self.CLOCK_STEP * k / 3600.0 for k in (1, 2, 3)], rel=1e-9,
        )

    def test_runner_sample_accumulated_discharge_grows_per_chunk(self, monkeypatch, tmp_path):
        """A valódi runner-mintaépítő a belső chunkok friss összegét látja;
        a terminális, ténylegesen naplózott minta ugyanazt a prefixet őrzi."""
        from itertools import pairwise

        from Prog.src.test_runner import StepKind, TestStep

        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=12.5, pulse_time_source=clock,
        )
        dmm_v.timed_voltage_fn = lambda t: self.V_PULSE
        ctrl._state = OcvSocState.IMPULSE_PREP
        events: list[dict] = []
        ctrl.on_event = events.append
        runner, logger = self._runner(ctrl, psu, load, dmm_v, dmm_t, tmp_path)
        st = self._watch_runner_pulse(monkeypatch, runner, ctrl, load, dmm_v, clock)
        seen: list[dict] = []
        real_book = ctrl._book_measured_pulse_energy

        def snapshot():
            # A mintaépítés saját mock read_all()-ja nem PMS-áramleolvasás.
            st["sampling"] = True
            try:
                return runner._build_sample(TestStep(StepKind.OCV_SOC, "ocv_soc"), ctrl)
            finally:
                st["sampling"] = False

        def book(service):
            observe = service is st["service"] and not st["trigger_fired"]
            if observe and not seen:
                seen.append(snapshot())
            real_book(service)
            if observe:
                seen.append(snapshot())
                if len(seen) == 4:
                    assert load.input_commanded_on
                    st["trigger_fired"] = True
                    runner.request_stop()

        monkeypatch.setattr(ctrl, "_book_measured_pulse_energy", book)
        result, ckpt = self._run_plan(runner, logger, tmp_path)
        assert st["trigger_fired"], "Három belső chunk könyvelése elmaradt"
        assert len(st["readings"]) == 3
        base_ah, base_wh = st["base"]
        expected = [
            self._expected_runner_prefix({**st, "readings": st["readings"][:k]})
            for k in range(4)
        ]
        assert [row["accumulated_discharge_Ah"] for row in seen] == pytest.approx(
            [base_ah + ah for ah, _ in expected], rel=1e-9,
        )
        assert [row["accumulated_discharge_Wh"] for row in seen] == pytest.approx(
            [base_wh + wh for _, wh in expected], rel=1e-9,
        )
        assert all(b[0] > a[0] for a, b in pairwise(expected))
        exp_ah, exp_wh = base_ah + expected[-1][0], base_wh + expected[-1][1]
        assert result.status == "STOPPED"
        assert result.reason == ckpt["reason"] == "USER_STOP_REQUESTED"
        assert result.total_discharge_ah == pytest.approx(exp_ah, rel=1e-9)
        assert result.total_discharge_wh == pytest.approx(exp_wh, rel=1e-9)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(exp_ah, rel=1e-9)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(exp_wh, rel=1e-9)
        assert ckpt["discharge_ah"] == pytest.approx(exp_ah, rel=1e-9)
        assert ckpt["discharge_wh"] == pytest.approx(exp_wh, rel=1e-9)
        assert st["runner_samples"][-1]["accumulated_discharge_Ah"] == pytest.approx(exp_ah, rel=1e-9)
        assert st["runner_samples"][-1]["accumulated_discharge_Wh"] == pytest.approx(exp_wh, rel=1e-9)
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert load.calls.count("input_on()") == 1
        assert len(self._events(events)) == 1

    def test_r3b1_empty_token_chunk_faults_and_keeps_prior_chunk_energy(self):
        """[regresszió, R3-B1 (R3-metrology-01), DFAM-001] Controller-szint:
        az üres mezős chunk (pulse_chunk_sample_count=3) FAULT-ot ad
        IMPULSE_ACQUISITION_FAILED okkal, Load és PSU OFF, nincs új
        input_on(), és a már könyvelt első chunk Ah/Wh-ja változatlan,
        egyetlen részenergia-eseménnyel."""
        ctrl, psu, load, dmm_v, _ = self._one_chunk_into_std_pulse()
        part = self.I_PULSE * self.CLOCK_STEP / 3600.0
        assert ctrl.accumulated_discharge_Ah == pytest.approx(part, rel=1e-9)
        events: list[dict] = []
        ctrl.on_event = events.append
        n_on = load.calls.count("input_on()")
        dmm_v.fetch_timed_acquisition_raw_override = "12.3,,12.3"
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.FAULT
        assert "IMPULSE_ACQUISITION_FAILED" in ctrl.fault_reason
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert ctrl.accumulated_discharge_Ah == pytest.approx(part, rel=1e-9)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(part * self.V_PULSE, rel=1e-9)
        assert len(self._events(events)) == 1
        assert load.calls.count("input_on()") == n_on


class TestR8B1OcvSocStartPowerGate:
    """R8-B1: a STEP_DISCHARGE a kisutesi setpointtal kapuz."""

    def test_over_power_faults_before_setup(self):
        ctrl, psu, load, *_ = make_ocv_soc_controller(dmm_voltage_V=12.5, capacity_Ah=300.0)
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._u_batt = 12.5
        assert ctrl._discharge_current_A == pytest.approx(30.0)

        ctrl._begin_step_discharge()

        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == "LOAD_POWER_LIMIT_EXCEEDED"
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)
        self._assert_fault_logged(ctrl, psu, load)
        load.reset_calls()
        psu.reset_calls()
        ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)

    @pytest.mark.parametrize("capacity_Ah", [199.0, 200.0])
    def test_at_or_below_limit_starts(self, capacity_Ah):
        ctrl, _psu, load, *_ = make_ocv_soc_controller(
            dmm_voltage_V=12.5, capacity_Ah=capacity_Ah,
        )
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._u_batt = 12.5

        ctrl._begin_step_discharge()

        assert ctrl.state == OcvSocState.STEP_DISCHARGE
        assert load.input_commanded_on is True
        assert f"set_current({capacity_Ah / 10.0})" in load.calls

    @staticmethod
    def _assert_fault_logged(ctrl, psu, load):
        import csv
        import io
        from unittest.mock import Mock

        from Prog.src.logger import CSV_COLUMNS, LogConfig, Logger
        from Prog.src.test_runner import (
            StepKind,
            TestRunner,
            TestRunnerConfig,
            TestStep,
        )

        output = io.StringIO()
        logger = Logger.__new__(Logger)
        logger._csv_writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
        logger._csv_writer.writeheader()
        logger._pending_rows = []
        logger._last_commit_wall_t = 0.0
        logger._config = LogConfig(sqlite_commit_interval_s=float("inf"))
        instruments = Mock(psu=psu, load=load)
        instruments.read_all.return_value = {}
        runner = TestRunner(
            instruments, ctrl._safety, logger, ctrl._profile, TestRunnerConfig(),
            lambda: None, lambda: None, lambda step: None,
        )
        logger.log_sample(runner._build_sample(TestStep(StepKind.OCV_SOC, "R8-B1"), ctrl))
        row = next(csv.DictReader(io.StringIO(output.getvalue())))
        assert row["fault_flags"] == "LOAD_POWER_LIMIT_EXCEEDED"
        assert row["load_input_commanded_on"] == "False"


class TestR8B2OcvSocPulseVoltageGate:
    """R8-B2: PREP -> standard/fast szolgaltatas -> FAULT es mintanaplo."""

    @staticmethod
    def _make(fast):
        cfg = OcvSocConfig(
            pulse_chunk_sample_count=3, pulse_sample_interval_s=0.6,
            impulse_duration_s=3.6,
            fast_transient_enabled=fast,
            fast_transient_chunk_sample_count=3,
            fast_transient_sample_interval_s=0.005,
            fast_transient_duration_s=0.03,
        )
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5, config=cfg, pulse_time_source=clock,
        )
        bind_pulse_instrument_latency(clock, dmm_v, load)
        ctrl._state = OcvSocState.IMPULSE_PREP
        assert ctrl.advance(0.0) == OcvSocState.IMPULSE_ON
        dmm_v.timed_voltage_fn = lambda t: 13.2
        if fast:
            _run_until(ctrl, {OcvSocState.IMPULSE_FAST_ON, OcvSocState.FAULT})
            assert ctrl.state == OcvSocState.IMPULSE_FAST_ON
        load.reset_calls()
        psu.reset_calls()
        dmm_v.reset_calls()
        return ctrl, psu, load, dmm_v, dmm_t

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("source", ["baseline", "chunk"])
    def test_transient_overvoltage_faults_without_ok_soc_point(self, fast, source, monkeypatch):
        ctrl, psu, load, dmm_v, _ = self._make(fast)
        points, events, off_order = [], [], []
        ctrl.on_soc_point = points.append
        ctrl.on_event = events.append
        real_load_off, real_psu_off = load.input_off, psu.all_outputs_off

        def load_off():
            off_order.append("load")
            real_load_off()

        def psu_off():
            off_order.append("psu")
            real_psu_off()

        monkeypatch.setattr(load, "input_off", load_off)
        monkeypatch.setattr(psu, "all_outputs_off", psu_off)
        if source == "baseline":
            dmm_v.voltage_V = 14.56
        else:
            ctrl.advance(0.0)
            assert ctrl.state == (
                OcvSocState.IMPULSE_FAST_ACQUIRE if fast else OcvSocState.IMPULSE_ACQUIRE
            )
            ctrl.advance(0.0)  # Egy ervenyes chunk energiaja mar konyvelve.
            assert ctrl.accumulated_discharge_Ah > 0
            dmm_v.fetch_timed_acquisition_raw_override = "13.2,14.56,13.2"
        energy_before = ctrl.accumulated_discharge_Ah

        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == "BATTERY_OVERVOLTAGE: 14.56 V > 14.55 V"
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert off_order[-2:] == ["load", "psu"]
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        assert ctrl.accumulated_discharge_Ah == pytest.approx(energy_before)
        assert dmm_v.nplc == 10.0
        assert dmm_v.calls.count("read_voltage()") == 1
        if source == "baseline":
            assert not any(
                c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls
            )
        else:
            assert any(e["event_code"] == "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY" for e in events)
        self._assert_fault_logged(ctrl, psu, load)

        # Az atmeneti tuske megszunik; a FAULT utan sincs hamis OK pont vagy uj ON.
        dmm_v.voltage_V = 13.5
        dmm_v.fetch_timed_acquisition_raw_override = None
        load.reset_calls()
        psu.reset_calls()
        for _ in range(12):
            ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert points == []
        assert ctrl.point_quality_counts.get("OK", 0) == 0
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)

    @pytest.mark.parametrize("fast", [False, True])
    def test_chunk_off_failure_keeps_overvoltage_fault(self, fast, monkeypatch):
        ctrl, psu, load, dmm_v, _ = self._make(fast)
        ctrl.advance(0.0)
        dmm_v.fetch_timed_acquisition_raw_override = "13.2,14.56,13.2"
        real_off = load.input_off
        attempts = []

        def fail_first_off():
            attempts.append("off")
            if len(attempts) == 1:
                raise RuntimeError("first OFF failed")
            real_off()

        monkeypatch.setattr(load, "input_off", fail_first_off)

        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason == (
            "BATTERY_OVERVOLTAGE: 14.56 V > 14.55 V"
            " | IMPULSE_LOAD_OFF_FAILED after BATTERY_OVERVOLTAGE: first OFF failed"
        )
        assert attempts == ["off", "off"]
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert "all_outputs_off()" in psu.calls
        self._assert_fault_logged(ctrl, psu, load)

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("voltage", [13.5, 14.55])
    def test_normal_and_exact_limit_pulses_complete(self, fast, voltage):
        ctrl, _psu, load, dmm_v, _ = self._make(fast)
        dmm_v.voltage_V = voltage
        dmm_v.fetch_timed_acquisition_raw_override = str(voltage)
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})

        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        assert ctrl.fault_reason == ""
        assert load.input_commanded_on is False
        assert dmm_v.nplc == 10.0
        points = []
        ctrl.on_soc_point = points.append
        ctrl.advance(0.0)
        assert len(points) == 1
        assert ctrl._cutoff_during_pulse is False

    @pytest.mark.parametrize("fast", [False, True])
    def test_normal_cutoff_remains_nonfault(self, fast):
        ctrl, _psu, load, dmm_v, _ = self._make(fast)
        _set_pulse_test_cutoff(ctrl)
        ctrl.advance(0.0)
        dmm_v.fetch_timed_acquisition_raw_override = "13.2,11.0,13.2"

        assert ctrl.advance(0.0) == OcvSocState.LOG_SOC_POINT

        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        assert service is not None
        assert service.finalize().cutoff_during_pulse is True
        assert ctrl.fault_reason == ""
        assert load.input_commanded_on is False
        assert dmm_v.nplc == 10.0

    @staticmethod
    def _assert_fault_logged(ctrl, psu, load):
        import csv
        import io
        from unittest.mock import Mock

        from Prog.src.logger import CSV_COLUMNS, LogConfig, Logger
        from Prog.src.test_runner import (
            StepKind,
            TestRunner,
            TestRunnerConfig,
            TestStep,
        )

        output = io.StringIO()
        logger = Logger.__new__(Logger)
        logger._csv_writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
        logger._csv_writer.writeheader()
        logger._pending_rows = []
        logger._last_commit_wall_t = 0.0
        logger._config = LogConfig(sqlite_commit_interval_s=float("inf"))
        instruments = Mock(psu=psu, load=load)
        instruments.read_all.return_value = {}
        runner = TestRunner(
            instruments, ctrl._safety, logger, ctrl._profile, TestRunnerConfig(),
            lambda: None, lambda: None, lambda step: None,
        )
        logger.log_sample(runner._build_sample(TestStep(StepKind.OCV_SOC, "R8-B2"), ctrl))
        row = next(csv.DictReader(io.StringIO(output.getvalue())))
        assert row["fault_flags"] == ctrl.fault_reason
        assert row["load_input_commanded_on"] == "False"


class TestR9B2OcvSocPulsePowerGate:
    """R9-B2: PREP -> szolgaltatas -> FAULT -> memoriabeli mintanaplo."""

    @staticmethod
    def _make(fast):
        cfg = OcvSocConfig(
            pulse_chunk_sample_count=3, pulse_sample_interval_s=0.6,
            impulse_duration_s=3.6, fast_transient_enabled=fast,
            fast_transient_chunk_sample_count=3,
            fast_transient_sample_interval_s=0.005, fast_transient_duration_s=0.03,
        )
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm, _ = make_ocv_soc_controller(
            dmm_voltage_V=12.49, capacity_Ah=100.0, config=cfg, pulse_time_source=clock,
        )
        bind_pulse_instrument_latency(clock, dmm, load)
        dmm.timed_voltage_fn = lambda t: 12.49
        ctrl._state = OcvSocState.IMPULSE_PREP
        assert ctrl.advance(0.0) == OcvSocState.IMPULSE_ON
        if fast:
            _run_until(ctrl, {OcvSocState.IMPULSE_FAST_ON, OcvSocState.FAULT})
            assert ctrl.state == OcvSocState.IMPULSE_FAST_ON
        load.reset_calls()
        psu.reset_calls()
        dmm.reset_calls()
        return ctrl, psu, load, dmm

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("source", ["baseline", "chunk", "current"])
    @pytest.mark.parametrize("off_fails", [False, True])
    def test_power_fault_preserves_reason_cleanup_and_booked_prefix(
        self, fast, source, off_fails, monkeypatch,
    ):
        ctrl, psu, load, dmm = self._make(fast)
        points, events, order = [], [], []
        ctrl.on_soc_point = points.append
        ctrl.on_event = events.append
        if source == "baseline":
            dmm.voltage_V = 12.51
        else:
            ctrl.advance(0.0)
            ctrl.advance(0.0)
            assert ctrl.accumulated_discharge_Ah > 0
            if source == "chunk":
                dmm.fetch_timed_acquisition_raw_override = "12.49,12.51,12.49"
            else:
                load.current_A = 20.1
        before = (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh)
        real_off, real_psu_off = load.input_off, psu.all_outputs_off

        def load_off():
            order.append("load")
            if off_fails and order.count("load") == 1:
                raise RuntimeError("first OFF failed")
            real_off()

        def psu_off():
            order.append("psu")
            real_psu_off()

        monkeypatch.setattr(load, "input_off", load_off)
        monkeypatch.setattr(psu, "all_outputs_off", psu_off)
        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        reason = ctrl.fault_reason
        assert reason.startswith("LOAD_POWER_LIMIT_EXCEEDED:")
        voltage, current = (12.49, 20.1) if source == "current" else (12.51, 20.0)
        for fragment in (
            f"U={voltage:.6g} V", f"I={current:.6g} A",
            f"P={voltage * current:.6g} W", "limit=250 W",
        ):
            assert fragment in reason
        assert psu.output_commanded_on is False
        assert order[-1] == "psu"
        assert "all_outputs_off()" in psu.calls
        if off_fails:
            assert " | " in reason
            assert "first OFF failed" in reason
            if source != "baseline":
                assert "IMPULSE_LOAD_OFF_FAILED after LOAD_POWER_LIMIT_EXCEEDED" in reason
                assert order == ["load", "load", "psu"]
        else:
            assert order[-2:] == ["load", "psu"]
        # Baseline-nal a sikertelen OFF elott sem volt ON.
        assert load.input_commanded_on is False
        assert (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh) == pytest.approx(before)
        assert dmm.nplc == 10.0
        assert dmm.calls.count("read_voltage()") == 1
        if source == "baseline":
            assert not load.called("input_on()")
        else:
            assert any(e["event_code"] == "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY" for e in events)
        TestR8B2OcvSocPulseVoltageGate._assert_fault_logged(ctrl, psu, load)

        dmm.voltage_V = 12.49
        dmm.fetch_timed_acquisition_raw_override = None
        load.current_A = 20.0
        load.reset_calls()
        psu.reset_calls()
        for _ in range(12):
            ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert points == []
        assert ctrl.point_quality_counts.get("OK", 0) == 0
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)
        assert (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh) == pytest.approx(before)

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("voltage", [12.49, 12.50])
    def test_at_or_below_power_limit_completes(self, fast, voltage):
        ctrl, psu, load, dmm = self._make(fast)
        dmm.voltage_V = voltage
        dmm.fetch_timed_acquisition_raw_override = str(voltage)
        _run_until(ctrl, {OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT})
        assert ctrl.state == OcvSocState.LOG_SOC_POINT
        assert ctrl.fault_reason == ""
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert dmm.nplc == 10.0
        points = []
        ctrl.on_soc_point = points.append
        ctrl.advance(0.0)
        assert len(points) == 1


    @pytest.mark.parametrize("fast", [False, True])
    def test_controller_forwards_existing_safety_limit(self, fast):
        profile = _make_profile(100.0)
        safety = SafetyManager(
            profile, PsuMode.INDEPENDENT, max_load_power_W=249.9,
        )
        psu, load = MockPSU(), MockLoad()
        dmm = MockDMM(voltage_V=12.50)
        ctrl = OcvSocController(
            psu, load, dmm, MockDMM(temperature_C=22.0), profile, safety,
            OcvSocConfig(fast_transient_enabled=fast),
            pulse_time_source=InstrumentLatencyClock(),
        )
        ctrl._u_batt = 12.49
        ctrl._state = OcvSocState.IMPULSE_FAST_ON if fast else OcvSocState.IMPULSE_ON
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason.startswith("LOAD_POWER_LIMIT_EXCEEDED:")
        assert "limit=249.9 W" in ctrl.fault_reason
        assert not load.called("input_on()")
        assert psu.output_commanded_on is False
        assert dmm.nplc == 10.0


class TestR9B3OcvSocPulseFaultReason:
    """R9-B3: a pulzus kivalto ertekei a runner-csatornakon is megmaradnak."""

    @staticmethod
    def _fault_case(fast, source, off_fails, monkeypatch):
        ctrl, psu, load, dmm, _ = TestR8B2OcvSocPulseVoltageGate._make(fast)
        points, order = [], []
        ctrl.on_soc_point = points.append
        if source == "baseline":
            dmm.voltage_V = 14.56
        else:
            ctrl.advance(0.0)
            ctrl.advance(0.0)
            assert ctrl.accumulated_discharge_Ah > 0
            dmm.fetch_timed_acquisition_raw_override = "13.2,14.56,13.2"
        before = (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh)
        real_off, real_psu_off = load.input_off, psu.all_outputs_off

        def load_off():
            order.append("load")
            if off_fails and order.count("load") == 1:
                raise RuntimeError("first OFF failed")
            real_off()

        def psu_off():
            order.append("psu")
            real_psu_off()

        monkeypatch.setattr(load, "input_off", load_off)
        monkeypatch.setattr(psu, "all_outputs_off", psu_off)
        ctrl.advance(0.0)

        assert ctrl.state == OcvSocState.FAULT
        assert order == (["load", "load", "psu"] if source == "chunk" else ["load", "psu"])
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh) == pytest.approx(before)
        assert dmm.nplc == 10.0
        if source == "baseline":
            assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        load.reset_calls()
        psu.reset_calls()
        for _ in range(3):
            ctrl.advance(1.0)
        assert ctrl.state == OcvSocState.FAULT
        assert points == []
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)
        return ctrl, psu, load

    @staticmethod
    def _runner_fault(ctrl, psu, load):
        import csv
        import io
        from unittest.mock import Mock

        from Prog.src.logger import CSV_COLUMNS, LogConfig, Logger
        from Prog.src.test_runner import (
            StepKind,
            TestRunner,
            TestRunnerConfig,
            TestStep,
        )

        samples, event_output = io.StringIO(), io.StringIO()
        logger = Logger.__new__(Logger)
        logger._csv_writer = csv.DictWriter(samples, fieldnames=CSV_COLUMNS)
        logger._csv_writer.writeheader()
        logger._pending_rows = []
        logger._last_commit_wall_t = 0.0
        logger._config = LogConfig(sqlite_commit_interval_s=float("inf"))
        logger._event_writer = csv.DictWriter(
            event_output,
            fieldnames=("timestamp_iso", "event_code", "event_message", "is_critical"),
        )
        logger._event_writer.writeheader()
        logger.flush_all = Mock()
        logger.close = Mock()
        logger.write_checkpoint = Mock()
        instruments = Mock(psu=psu, load=load)
        instruments.read_all.return_value = {}
        instruments.safe_all_off.return_value = {"PSU": True, "Load": True}
        instruments.poll_errors.return_value = []
        runner = TestRunner(
            instruments, ctrl._safety, logger, ctrl._profile, TestRunnerConfig(),
            lambda: None, lambda: None, lambda step: None,
        )
        events = []
        runner.on_event = events.append
        logger.log_sample(runner._build_sample(TestStep(StepKind.OCV_SOC, "R9-B3"), ctrl))
        result = runner._emergency_stop(ctrl.fault_reason)
        row = next(csv.DictReader(io.StringIO(samples.getvalue())))
        event = next(
            e for e in csv.DictReader(io.StringIO(event_output.getvalue()))
            if e["event_code"] == "EMERGENCY_STOP"
        )
        assert row["fault_flags"] == ctrl.fault_reason
        assert row["load_input_commanded_on"] == "False"
        assert result.status == "FAULT"
        assert result.reason == ctrl.fault_reason
        assert event["event_message"] == ctrl.fault_reason
        assert event["is_critical"] == "True"
        assert events[-1] == {
            "event_code": "EMERGENCY_STOP", "event_message": ctrl.fault_reason,
        }
        assert logger.write_checkpoint.call_args.args[0]["reason"] == ctrl.fault_reason
        return result

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("source", ["baseline", "chunk"])
    @pytest.mark.parametrize("off_fails", [False, True])
    def test_voltage_reason_survives_cleanup_and_runner(self, fast, source, off_fails, monkeypatch):
        ctrl, psu, load = self._fault_case(fast, source, off_fails, monkeypatch)
        reason = ctrl.fault_reason
        expected = "BATTERY_OVERVOLTAGE: 14.56 V > 14.55 V"
        if off_fails:
            if source == "chunk":
                expected += " | IMPULSE_LOAD_OFF_FAILED after BATTERY_OVERVOLTAGE: first OFF failed"
            else:
                expected += " | LOAD_OFF_FAILED:first OFF failed"
        assert reason == expected
        self._runner_fault(ctrl, psu, load)

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("source", ["baseline", "chunk"])
    def test_generic_reason_unchanged(self, fast, source, monkeypatch):
        ctrl, psu, load, _, _ = TestR8B2OcvSocPulseVoltageGate._make(fast)
        if source == "chunk":
            ctrl.advance(0.0)
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        injected = RuntimeError("CUSTOM / override failure")

        def fail(*args, **kwargs):
            raise injected

        monkeypatch.setattr(service, "begin_pulse" if source == "baseline" else "step_chunk", fail)
        ctrl.advance(0.0)
        prefix = "FAST_TRANSIENT" if fast else "IMPULSE"
        phase = "START" if source == "baseline" else "ACQUISITION"
        assert ctrl.fault_reason == f"{prefix}_{phase}_FAILED: CUSTOM / override failure"
        assert ctrl.state == OcvSocState.FAULT
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        self._runner_fault(ctrl, psu, load)

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("event_fails", [False, True])
    def test_chunk_base_exception_keeps_original_object(
        self, fast, error_type, event_fails, monkeypatch,
    ):
        ctrl, psu, load, _, _ = TestR8B2OcvSocPulseVoltageGate._make(fast)
        ctrl.advance(0.0)
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        injected = error_type("original interrupt")

        def fail():
            raise injected

        def on_event(event):
            if event_fails:
                raise RuntimeError("event callback failed")

        ctrl.on_event = on_event
        monkeypatch.setattr(service, "step_chunk", fail)
        with pytest.raises(error_type) as caught:
            ctrl.advance(0.0)
        assert caught.value is injected
        prefix = "FAST_TRANSIENT" if fast else "IMPULSE"
        assert ctrl.fault_reason == f"{prefix}_ACQUISITION_FAILED: original interrupt"
        assert ctrl.state == OcvSocState.FAULT
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False


class TestR12B1OcvSocPulseTemperatureRange:
    """R12-B1: friss pulzushomerseklet, egyszeri jelzes es runner-aggregacio."""

    PHASES = (
        "IMPULSE_ON", "IMPULSE_ACQUIRE",
        "IMPULSE_FAST_ON", "IMPULSE_FAST_ACQUIRE",
    )

    @pytest.mark.parametrize("phase", PHASES)
    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("temp,outside", [
        (14.9, True), (15.0, False), (22.0, False), (30.0, False),
        (30.1, True), (40.0, True), (59.9, True),
    ])
    def test_pulse_range_once_per_tick(self, monkeypatch, phase, mode, temp, outside):
        from unittest.mock import Mock

        ctrl, _, _, dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState[phase]
        ctrl._safety.temp_comp_mode = mode
        dmm_t.temperature_C = temp
        handler = Mock()
        read_t = Mock(wraps=dmm_t.read_temperature)
        read_v = Mock(wraps=dmm_v.read_voltage)
        monkeypatch.setattr(ctrl, "_run_" + phase.lower(), handler)
        monkeypatch.setattr(dmm_t, "read_temperature", read_t)
        monkeypatch.setattr(dmm_v, "read_voltage", read_v)

        for tick in (1, 2):
            ctrl.advance(1.0)
            assert ctrl.temperature_out_of_range_count == tick * int(outside)
            expected = "TEMPERATURE_OUT_OF_RANGE" if outside else ""
            assert ctrl.last_warning_code == expected
            assert ctrl.temp_dmm_valid is True
            assert ctrl.battery_temperature_C == temp
            assert read_t.call_count == tick
            assert handler.call_count == tick
            read_v.assert_not_called()

    @pytest.mark.parametrize("phase", PHASES)
    def test_dropout_warning_has_priority_with_valid_range_warning(self, monkeypatch, phase):
        from unittest.mock import Mock

        from Prog.src.safety import SafetyResult, WarningCode

        ctrl, _, _, _, dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState[phase]
        dmm_t.temperature_C = 40.0
        monkeypatch.setattr(ctrl, "_run_" + phase.lower(), Mock())
        monkeypatch.setattr(ctrl._safety, "check_temperature_dmm_fault", Mock(
            return_value=SafetyResult(warning=WarningCode.TEMPERATURE_DMM_LOST),
        ))
        ctrl.advance(1.0)
        assert ctrl.temperature_out_of_range_count == 1
        assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"

    @pytest.mark.parametrize("phase", PHASES)
    @pytest.mark.parametrize("mode", list(TempCompMode))
    def test_invalid_temperature_does_not_reclassify_stale_value(self, monkeypatch, phase, mode):
        from unittest.mock import Mock

        ctrl, _, _, dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState[phase]
        ctrl._safety.temp_comp_mode = mode
        ctrl._battery_temperature_C = 85.0
        dmm_t.simulate_temp_failure = True
        handler = Mock()
        read_t = Mock(wraps=dmm_t.read_temperature)
        read_v = Mock(wraps=dmm_v.read_voltage)
        monkeypatch.setattr(ctrl, "_run_" + phase.lower(), handler)
        monkeypatch.setattr(dmm_t, "read_temperature", read_t)
        monkeypatch.setattr(dmm_v, "read_voltage", read_v)

        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False
        assert ctrl.temperature_out_of_range_count == 0
        read_t.assert_called_once()
        read_v.assert_not_called()
        if mode is TempCompMode.ENABLED:
            assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
            handler.assert_not_called()
        else:
            assert ctrl.fault_reason == ""
            handler.assert_called_once()
            expected = "TEMPERATURE_DMM_LOST" if mode is TempCompMode.MONITOR_ONLY else ""
            assert ctrl.last_warning_code == expected
            if mode is TempCompMode.MONITOR_ONLY:
                ctrl.advance(ctrl._safety.temperature_dmm_fault_timeout_s)
                assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
                handler.assert_called_once()
                assert ctrl.temperature_out_of_range_count == 0

    @pytest.mark.parametrize("phase", PHASES)
    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("temp", [60.0, 85.0])
    def test_overtemperature_stops_before_dispatch(self, monkeypatch, phase, mode, temp):
        from unittest.mock import Mock

        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller()
        ctrl._state = OcvSocState[phase]
        ctrl._safety.temp_comp_mode = mode
        dmm_t.temperature_C = temp
        off_order = []
        for device, method, label in (
            (load, "input_off", "load"), (psu, "all_outputs_off", "psu"),
        ):
            original = getattr(device, method)

            def off(original=original, label=label):
                off_order.append(label)
                original()

            monkeypatch.setattr(device, method, off)
        handler = Mock()
        read_t = Mock(wraps=dmm_t.read_temperature)
        read_v = Mock(wraps=dmm_v.read_voltage)
        monkeypatch.setattr(ctrl, "_run_" + phase.lower(), handler)
        monkeypatch.setattr(dmm_t, "read_temperature", read_t)
        monkeypatch.setattr(dmm_v, "read_voltage", read_v)

        assert ctrl.advance(1.0) is OcvSocState.FAULT
        assert ctrl.fault_reason == "BATTERY_OVERTEMPERATURE"
        assert ctrl.temperature_out_of_range_count == 0
        assert off_order == ["load", "psu"]
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        calls = load.calls + psu.calls
        assert not any(call.startswith((
            "input_on", "output_on", "set_current", "set_mode_", "set_output_",
        )) for call in calls)
        ctrl.advance(1.0)
        assert load.calls + psu.calls == calls
        read_t.assert_called_once()
        read_v.assert_not_called()
        handler.assert_not_called()

    @pytest.mark.parametrize("source,temp", [
        ("pulse", 40.0), ("prep", 40.0), ("pulse", 22.0), ("pulse", 60.0),
    ])
    def test_full_standard_pulse_runner(self, monkeypatch, source, temp):
        from unittest.mock import Mock

        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger
        from Prog.src.test_runner import (
            StepKind,
            TestPlan,
            TestRunner,
            TestRunnerConfig,
            TestStep,
            TestType,
        )

        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm_v, dmm_t = make_ocv_soc_controller(
            dmm_voltage_V=13.5,
            config=OcvSocConfig(pulse_sample_interval_s=0.6),
            pulse_time_source=clock,
        )
        bind_pulse_instrument_latency(clock, dmm_v, load)
        dmm_v.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_PREP
        ctrl._grid_pos = len(ctrl._grid) - 1
        ticks = []
        events = []
        ctrl.on_event = events.append

        def temperature():
            phase = ctrl.state.name
            hot = phase in self.PHASES if source == "pulse" else phase == "IMPULSE_PREP"
            value = temp if hot else 22.0
            ticks.append((phase, value))
            return value

        temperature_observations = []
        check_temperature = ctrl._check_pulse_temperature

        def checked_temperature(dt_s):
            assert len(temperature_observations) < 200, "B24 temperature chain exceeded its bound"
            phase = ctrl.state
            value = check_temperature(dt_s)
            if phase != OcvSocState.IMPULSE_ACQUIRE:
                return value
            temperature_observations.append(
                f"T={ctrl.battery_temperature_C} C; valid={ctrl.temp_dmm_valid}; "
                f"warning={ctrl.last_warning_code}; fault={ctrl.fault_reason}"
            )
            return value

        monkeypatch.setattr(ctrl, "_check_pulse_temperature", checked_temperature)
        read_t = Mock(side_effect=temperature)
        monkeypatch.setattr(dmm_t, "read_temperature", read_t)
        logger = Mock(spec=Logger)
        runner = TestRunner(
            instrument_manager=InstrumentManager(psu, load, dmm_v, dmm_t),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: None,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
            ocv_soc_ctrl_factory=lambda: ctrl,
        )
        result = runner.run(TestPlan(
            test_type=TestType.OCV_SOC_CHARACTERIZATION,
            steps=(TestStep(StepKind.OCV_SOC, "R12-B1"),),
        ))
        samples = [call.args[0] for call in logger.log_sample.call_args_list]
        # B21b: a saját felügyelet külső mintasor nélkül is olvas T-t.
        assert read_t.call_count == len(ticks)
        assert samples and len(samples) <= len(ticks)
        assert ticks[0] == ("IMPULSE_PREP", temp if source == "prep" else 22.0)
        pulse_ticks = [(phase, value) for phase, value in ticks if phase in self.PHASES]
        assert pulse_ticks
        # Minden belső T-olvasás minősítését továbbra is külön ellenőrizzük.
        # A napló csak az állapotváltásokat adja ki (B24).
        internal_ticks = [(phase, value) for phase, value in ticks if phase == "IMPULSE_ACQUIRE"]
        temperature_events = [e for e in events if e["event_code"] == "OCV_SOC_PULSE_TEMPERATURE"]
        if temp < 60.0:
            assert internal_ticks
        expected_temperature_messages = [
            f"T={value} C; valid=True; "
            f"warning={'TEMPERATURE_OUT_OF_RANGE' if value == 40.0 else ''}; "
            f"fault={'BATTERY_OVERTEMPERATURE' if value >= 60.0 else ''}"
            for _, value in internal_ticks
        ]
        assert temperature_observations == expected_temperature_messages
        assert [e["event_message"] for e in temperature_events] == list(dict.fromkeys(expected_temperature_messages))
        outer_ticks = [(phase, value) for phase, value in ticks if phase != "IMPULSE_ACQUIRE"]
        assert [row["battery_temperature_C"] for row in samples] == [value for _, value in outer_ticks]
        for row, (_, value) in zip(samples, outer_ticks):
            expected = "TEMPERATURE_OUT_OF_RANGE" if value == 40.0 else None
            assert row["warning_flags"] == expected
        if temp >= 60.0:
            assert result.status == "FAULT"
            assert result.reason == "BATTERY_OVERTEMPERATURE"
            assert result.temperature_out_of_range_count == 0
            assert load.input_commanded_on is False
            assert psu.output_commanded_on is False
            logger.log_event.assert_any_call(
                "EMERGENCY_STOP", "BATTERY_OVERTEMPERATURE", is_critical=True,
            )
            assert samples[-1]["fault_flags"] == "BATTERY_OVERTEMPERATURE"
        else:
            assert result.status == "DONE"
            assert ctrl.state is OcvSocState.DONE
            assert ticks[-1] == ("LOG_SOC_POINT", 22.0)
            expected_count = sum(value == 40.0 for _, value in ticks)
            if temp == 40.0:
                assert expected_count > 0
            assert ctrl.temperature_out_of_range_count == expected_count
            assert result.temperature_out_of_range_count == expected_count
            expected_counts = {"TEMPERATURE_OUT_OF_RANGE": expected_count} if expected_count else {}
            assert result.warning_code_counts == expected_counts
            for sample in samples:
                value = sample["battery_temperature_C"]
                assert value in [v for _, v in ticks]
                expected = "TEMPERATURE_OUT_OF_RANGE" if value == 40.0 else None
                assert sample["warning_flags"] == expected


class TestR16B15StandardConfiguration:
    """Q-6 regresszió: kizárólag mockolt műszerek, memóriabeli óra."""

    @staticmethod
    def _make(**latencies):
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm, temp = make_ocv_soc_controller(
            dmm_voltage_V=13.5, pulse_time_source=clock,
        )
        bind_pulse_instrument_latency(clock, dmm, load, **latencies)
        dmm.nplc = 10.0
        dmm.autozero_on = False
        # Q-6 rev4, DMM_V 1 NPLC/AZ ON: független HIL-orákulum.
        dmm.sample_timer_min_raw_override = "0.04045"
        dmm.timed_voltage_fn = lambda t: 13.2
        ctrl._state = OcvSocState.IMPULSE_ON
        return ctrl, psu, load, dmm, temp, clock

    def test_default_profile_before_baseline_and_on_then_restore(self, monkeypatch):
        ctrl, psu, load, dmm, _temp, _clock = self._make()
        baseline = dmm.read_voltage
        observed = []

        def read():
            observed.append((dmm.nplc, dmm.autozero_on))
            return baseline()

        monkeypatch.setattr(dmm, "read_voltage", read)
        assert OcvSocConfig().pulse_sample_interval_s == 0.2
        ctrl.advance(0.0)
        assert observed == [(1.0, True)]
        assert load.input_commanded_on
        assert dmm.calls.index("enable_autozero()") < dmm.calls.index(
            "query_sample_timer_capability()"
        ) < dmm.calls.index("read_voltage()")
        assert _run_full_impulse(ctrl) == OcvSocState.LOG_SOC_POINT
        result = ctrl._pulse_service.finalize()
        assert len(result.samples) == 150
        assert result.pulse_duration_actual_s == pytest.approx(30.0)
        for target in (1.0, 10.0, 30.0):
            point = result.results[target]
            assert point.actual_time_s == pytest.approx(target)
            assert point.time_error_s == pytest.approx(0.0, abs=1e-10)
            assert point.within_tolerance
        expected_ah = 1.4 * 30.0 / 3600.0
        assert result.removed_Ah == pytest.approx(expected_ah)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(expected_ah)
        assert dmm.nplc == 10.0
        assert dmm.autozero_on
        assert not load.input_commanded_on
        assert not psu.output_commanded_on

    @pytest.mark.parametrize("minimum", ["0.200001", "NaN", "inf", "-1", "bad"])
    def test_invalid_minimum_faults_without_clamp_or_on(self, minimum):
        ctrl, psu, load, dmm, _temp, _clock = self._make()
        dmm.sample_timer_min_raw_override = minimum
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.FAULT
        assert "IMPULSE_CONFIG_REJECTED" in ctrl.fault_reason
        assert not load.called("input_on(")
        assert not dmm.called("configure_timed_acquisition(")
        assert not load.input_commanded_on
        assert not psu.output_commanded_on
        assert dmm.nplc == 10.0
        assert ctrl.accumulated_discharge_Ah == 0.0

    @pytest.mark.parametrize("failure", ["nplc", "autozero", "minimum", "errors"])
    def test_configuration_failure_faults_before_on(self, monkeypatch, failure):
        ctrl, psu, load, dmm, _temp, _clock = self._make()
        setter = dmm.set_nplc
        if failure == "nplc":
            def ignore_one(nplc):
                if nplc != 1.0:
                    setter(nplc)
            monkeypatch.setattr(dmm, "set_nplc", ignore_one)
        elif failure == "autozero":
            def reject():
                raise RuntimeError("AZ readback OFF")
            monkeypatch.setattr(dmm, "enable_autozero", reject)
        elif failure == "minimum":
            dmm.simulate_sample_timer_query_failure = True
        else:
            monkeypatch.setattr(dmm, "check_error", lambda: ["-221,config"])
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.FAULT
        assert "IMPULSE_CONFIG_REJECTED" in ctrl.fault_reason
        assert not load.called("input_on(")
        assert not load.input_commanded_on
        assert not psu.output_commanded_on
        assert dmm.nplc == 10.0

    def test_timer_readback_still_rejects_silent_clamp(self):
        ctrl, psu, load, dmm, _temp, _clock = self._make()
        dmm.actual_sample_timer_override_s = 0.41
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.FAULT
        assert "DMM_TIMED_ACQUISITION_CONFIG_REJECTED" in ctrl.fault_reason
        assert not dmm.called("arm_timed_acquisition()")
        assert not load.input_commanded_on
        assert not psu.output_commanded_on
        assert dmm.nplc == 10.0

    def test_restore_failure_keeps_energy_and_faults(self, monkeypatch):
        ctrl, psu, load, dmm, _temp, _clock = self._make(
            reset_s=0.01, input_off_s=0.07,
        )
        events = []
        ctrl.on_event = events.append
        setter = dmm.set_nplc

        def fail_restore(nplc):
            if nplc == 10.0:
                raise RuntimeError("restore failed")
            setter(nplc)

        monkeypatch.setattr(dmm, "set_nplc", fail_restore)
        assert _run_full_impulse(ctrl) == OcvSocState.FAULT
        assert "ACQUISITION_NPLC_RESTORE_FAILED" in ctrl.fault_reason
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.4 * 30.57 / 3600.0)
        assert events == []
        assert not load.input_commanded_on
        assert not psu.output_commanded_on

    @pytest.mark.parametrize("restore_fails", [False, True])
    def test_emergency_stop_restores_standard_only_after_both_off(
        self, monkeypatch, restore_fails,
    ):
        ctrl, psu, load, dmm, _temp, _clock = self._make()
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        assert dmm.nplc == 1.0
        setter = dmm.set_nplc
        off_states = []

        def restore(nplc):
            off_states.append((load.input_commanded_on, psu.output_commanded_on))
            if restore_fails:
                raise RuntimeError("restore failed")
            setter(nplc)

        monkeypatch.setattr(dmm, "set_nplc", restore)
        ctrl.emergency_stop("TEST_STOP")
        assert ctrl.state == OcvSocState.FAULT
        assert off_states == [(False, False)]
        assert dmm.nplc == (1.0 if restore_fails else 10.0)
        assert ("STANDARD_PULSE_NPLC_RESTORE_FAILED" in ctrl.fault_reason) == restore_fails
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.4 * 0.6 / 3600.0)

    @pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
    def test_restore_interrupt_keeps_fault_and_measured_prefix(
        self, monkeypatch, exception,
    ):
        ctrl, psu, load, dmm, _temp, _clock = self._make()
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        assert dmm.nplc == 1.0

        def interrupt(nplc):
            raise exception("cleanup interrupted")

        monkeypatch.setattr(dmm, "set_nplc", interrupt)
        with pytest.raises(exception, match="cleanup interrupted"):
            ctrl.emergency_stop("TEST_STOP")
        assert ctrl.state == OcvSocState.FAULT
        assert not load.input_commanded_on
        assert not psu.output_commanded_on
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.4 * 0.6 / 3600.0)


class TestR16B15PacingContract:
    """Q-36: saját terhelési ablak, a külső runner ütemétől függetlenül."""

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("tick", [0.0, 2.0, 5.0, 30.0])
    @pytest.mark.parametrize("temp_s", [0.0, 0.45])
    def test_runner_reports_real_window_points_and_consistent_ah(
        self, monkeypatch, fast, tick, temp_s,
    ):
        from unittest.mock import Mock

        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger
        from Prog.src.test_runner import (
            StepKind,
            TestPlan,
            TestRunner,
            TestRunnerConfig,
            TestStep,
            TestType,
        )

        ctrl, psu, load, dmm, temp, clock = (
            TestR16B15F2Cleanup._make(fast=True)
            if fast else TestR16B15StandardConfiguration._make()
        )
        ctrl._grid_pos = len(ctrl._grid) - 1
        temperature = temp.read_temperature

        def read_temperature():
            clock.advance(temp_s)
            return temperature()

        monkeypatch.setattr(temp, "read_temperature", read_temperature)
        monkeypatch.setattr("Prog.src.test_runner.time.perf_counter", clock)
        monkeypatch.setattr("Prog.src.test_runner.time.sleep", clock.advance)
        points = []
        ctrl.on_soc_point = points.append
        logger = Mock(spec=Logger)
        runner = TestRunner(
            instrument_manager=InstrumentManager(psu, load, dmm, temp),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=TestRunnerConfig(runner_tick_s=tick, sleep_enabled=True),
            charge_ctrl_factory=lambda: None,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
            ocv_soc_ctrl_factory=lambda: ctrl,
        )
        session = runner.run(TestPlan(
            test_type=TestType.OCV_SOC_CHARACTERIZATION,
            steps=(TestStep(StepKind.OCV_SOC, "R16-B15b"),),
        ))
        assert session.status == "DONE"
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        result = service.finalize()
        target_s = 0.6 if fast else 30.0
        # A lefedettség csökkenhet; mintaszám nem nyújthatja a Load ON-t.
        assert result.samples
        # Legfeljebb az éppen futó, nem megszakítható T-I/O nyúlhat túl.
        assert target_s - 1e-8 <= result.pulse_duration_actual_s <= target_s + temp_s + 1e-8
        assert target_s - 1e-8 <= result.load_command_duration_s <= target_s + temp_s + 1e-8
        assert result.pulse_duration_set_s == target_s
        expected_ah = 1.4 * result.pulse_duration_actual_s / 3600.0
        assert result.removed_Ah == pytest.approx(expected_ah)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(expected_ah)
        assert session.total_discharge_ah == pytest.approx(expected_ah)
        assert points[0]["removed_Ah_total"] == pytest.approx(expected_ah)
        for target, point in result.results.items():
            if point.reached:
                assert point.time_error_s == pytest.approx(point.actual_time_s - target)
                assert point.within_tolerance == (abs(point.time_error_s) <= service._time_tolerance_s)
        assert result.load_command_time_error_s == pytest.approx(result.load_command_duration_s - target_s)
        if temp_s == 0.0:
            assert result.load_command_time_error_s == pytest.approx(0.0, abs=1e-8)
        assert not load.input_commanded_on
        assert not psu.output_commanded_on


class TestR16B15F2Cleanup:
    """DFAM-016: valódi driver, fake VISA, memóriabeli óra és napló."""

    @staticmethod
    def _make(fast=False):
        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm, temp = make_ocv_soc_controller(
            dmm_voltage_V=13.5, pulse_time_source=clock,
            config=OcvSocConfig(fast_transient_enabled=fast),
        )
        bind_pulse_instrument_latency(clock, dmm, load)
        dmm.sample_timer_min_raw_override = "0.001"
        dmm.timed_voltage_fn = lambda t: 13.2
        ctrl._u_batt = 13.5
        ctrl._dmm_valid = True
        ctrl._state = OcvSocState.IMPULSE_FAST_ON if fast else OcvSocState.IMPULSE_ON
        return ctrl, psu, load, dmm, temp, clock

    @staticmethod
    def _driver(monkeypatch, dmm, fast, failure, events, clock):
        from unittest.mock import Mock

        from Prog.drivers.device_dmm import Keysight34465ADMM

        driver = Keysight34465ADMM()
        state = {"nplc": 10.0, "az": 1.0, "restoring": False, "acquired": False}
        acquisition = 0.02 if fast else 1.0

        def query(command):
            events.append(command)
            if command.startswith("VOLT:DC:NPLC "):
                value = float(command.split()[1].split(";")[0])
                if value == 10.0:
                    state["restoring"] = state["acquired"]
                    if state["restoring"]:
                        clock.advance(20.0)
                    if not state["restoring"] or failure != "ignored_restore":
                        state["nplc"] = value
                else:
                    state["acquired"] = True
                    state["nplc"] = value
                    if failure == "partial_setter":
                        raise TimeoutError("NPLC applied, OPC lost")
                return "1"
            if command == "VOLT:DC:NPLC?":
                if state["restoring"] and failure == "nplc_query":
                    raise TimeoutError("restore NPLC? failed")
                return str(state["nplc"])
            if command == "VOLT:DC:ZERO:AUTO ON;*OPC?":
                state["az"] = 1.0
                return "1"
            if command == "VOLT:DC:ZERO:AUTO?":
                if state["restoring"] and failure == "az_query":
                    raise TimeoutError("restore AZ? failed")
                return "0" if state["restoring"] and failure == "az_off" else str(state["az"])
            raise AssertionError(command)

        driver._resource = Mock()
        driver._resource.query.side_effect = query
        driver._connected = True
        for name in ("set_nplc", "get_nplc", "enable_autozero"):
            monkeypatch.setattr(dmm, name, getattr(driver, name))
        return state, acquisition

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("terminal", [
        "complete", "cutoff", "stop", "baseline_error", "on_error",
        "acquisition_error", "off_error", "skipped", "minimum", "psu_error",
    ])
    def test_terminal_restore_follows_both_off(self, monkeypatch, fast, terminal):
        ctrl, psu, load, dmm, _temp, clock = self._make(fast)
        _set_pulse_test_cutoff(ctrl)
        events = []
        self._driver(monkeypatch, dmm, fast, "", events, clock)
        off_times = []
        load_off, psu_off = load.input_off, psu.all_outputs_off

        def off():
            events.append("LOAD_OFF")
            off_times.append(clock())
            if terminal == "off_error" and len(off_times) == 1:
                raise RuntimeError("terminal OFF failed")
            load_off()

        def psu_safe():
            events.append("PSU_OFF")
            if terminal == "psu_error":
                raise RuntimeError("PSU OFF failed")
            psu_off()

        monkeypatch.setattr(load, "input_off", off)
        monkeypatch.setattr(psu, "all_outputs_off", psu_safe)
        if terminal == "skipped":
            dmm.voltage_V = 11.0
        if terminal == "minimum":
            dmm.sample_timer_min_raw_override = "0.201"
        if terminal == "baseline_error":
            monkeypatch.setattr(dmm, "read_voltage", lambda: (_ for _ in ()).throw(
                RuntimeError("baseline failed")
            ))
        if terminal == "on_error":
            monkeypatch.setattr(load, "set_mode_cc", lambda: (_ for _ in ()).throw(
                RuntimeError("ON setup failed")
            ))
        ctrl.advance(0.0)
        if terminal == "stop":
            ctrl.emergency_stop("TEST_STOP")
        elif terminal == "acquisition_error":
            dmm.simulate_timed_acquisition_failure = True
        elif terminal == "cutoff":
            dmm.timed_voltage_fn = lambda t: 11.0
        active = (OcvSocState.IMPULSE_ACQUIRE, OcvSocState.IMPULSE_FAST_ACQUIRE)
        for _ in range(60):
            if ctrl.state not in active:
                break
            ctrl.advance(0.0)
        assert ctrl.state in (OcvSocState.LOG_SOC_POINT, OcvSocState.FAULT)
        restore = events.index("VOLT:DC:NPLC 10;*OPC?")
        assert events.index("LOAD_OFF") < events.index("PSU_OFF") < restore
        assert events[restore + 1:restore + 4] == [
            "VOLT:DC:ZERO:AUTO ON;*OPC?", "VOLT:DC:ZERO:AUTO?", "VOLT:DC:NPLC?",
        ]
        if terminal == "off_error":
            assert events[:restore].count("LOAD_OFF") >= 2
            # A 20 s-os restore egyik szükséges OFF-kísérletet sem késlelteti.
            assert off_times[1] == off_times[0]
            assert ctrl.state == OcvSocState.FAULT
            assert "terminal OFF failed" in ctrl.fault_reason
            assert ctrl.accumulated_discharge_Ah > 0.0
        if terminal == "psu_error":
            assert ctrl.state == OcvSocState.FAULT
            assert "PSU_OFF_FAILED" in ctrl.fault_reason
        assert not load.input_commanded_on
        assert not psu.output_commanded_on

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("failure", [
        "partial_setter", "ignored_restore", "nplc_query", "az_query", "az_off",
    ])
    def test_runner_transaction_and_readback(self, monkeypatch, fast, failure):
        from unittest.mock import Mock

        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger
        from Prog.src.test_runner import (
            StepKind,
            TestPlan,
            TestRunner,
            TestRunnerConfig,
            TestStep,
            TestType,
        )

        ctrl, psu, load, dmm, temp, clock = self._make(fast)
        ctrl._grid_pos = len(ctrl._grid) - 1
        points = []
        ctrl.on_soc_point = points.append
        events = []
        state, acquisition = self._driver(
            monkeypatch, dmm, fast, failure, events, clock,
        )
        monkeypatch.setattr("Prog.src.test_runner.time.perf_counter", clock)
        monkeypatch.setattr("Prog.src.test_runner.time.sleep", clock.advance)
        logger = Mock(spec=Logger)
        runner = TestRunner(
            instrument_manager=InstrumentManager(psu, load, dmm, temp),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None, ocv_soc_ctrl_factory=lambda: ctrl,
        )
        runner._last_configured_nplc = 10.0
        result = runner.run(TestPlan(
            test_type=TestType.OCV_SOC_CHARACTERIZATION,
            steps=(TestStep(StepKind.OCV_SOC, "R16-B15F2"),),
        ))
        assert "VOLT:DC:NPLC 10;*OPC?" in events
        restore = max(i for i, event in enumerate(events) if event == "VOLT:DC:NPLC 10;*OPC?")
        if failure not in ("az_query", "az_off"):
            assert "VOLT:DC:NPLC?" in events[restore + 1:]
        assert "VOLT:DC:ZERO:AUTO?" in events[restore + 1:]
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        assert service is not None
        if failure == "partial_setter":
            assert events.count(f"VOLT:DC:NPLC {acquisition:.6g};*OPC?") == 3
            assert state["nplc"] == 10.0
            assert not service.nplc_restore_failed
            assert not load.called("input_on(")
            assert "NPLC_SET_FAILED" in (
                points[0]["fast_transient_timing_infeasible_reason"] if fast else ctrl.fault_reason
            )
            # A fast konfiguráció elutasítása opcionális diagnosztika marad.
            assert result.status == ("DONE" if fast else "FAULT")
        else:
            assert result.status == "FAULT"
            assert "NPLC_RESTORE_FAILED" in result.reason
            assert service.nplc_restore_failed
            assert ctrl.accumulated_discharge_Ah > 0.0
            assert state["nplc"] == (acquisition if failure == "ignored_restore" else 10.0)
        assert runner._last_configured_nplc is None
        assert not load.input_commanded_on
        assert not psu.output_commanded_on
        if result.status == "FAULT":
            assert logger.log_event.called


class TestR16B15F4RestoreSignal:
    """DFAM-016 × DFAM-018: restore-hiba és jelzés a teljes runner-láncon."""

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("failure", ["ignored_restore", "nplc_query", "az_query"])
    @pytest.mark.parametrize("signal", ["stop", "abort", "emergency", "stop_abort", "stop_emergency"])
    def test_restore_fault_precedes_signal(self, monkeypatch, fast, failure, signal):
        from unittest.mock import Mock

        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger
        from Prog.src.test_runner import (
            StepKind,
            TestPlan,
            TestRunner,
            TestRunnerConfig,
            TestStep,
            TestType,
        )

        ctrl, psu, load, dmm, temp, clock = TestR16B15F2Cleanup._make(fast)
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        assert service is not None
        commands, pulse_events, terminal_events, at_signal, later_active = [], [], [], [], []
        ctrl.on_event = pulse_events.append
        state, acquisition = TestR16B15F2Cleanup._driver(
            monkeypatch, dmm, fast, failure, commands, clock,
        )
        monkeypatch.setattr("Prog.src.test_runner.time.perf_counter", clock)
        monkeypatch.setattr("Prog.src.test_runner.time.sleep", clock.advance)
        logger = Mock(spec=Logger)
        runner = TestRunner(
            instrument_manager=InstrumentManager(psu, load, dmm, temp),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None, ocv_soc_ctrl_factory=lambda: ctrl,
        )
        runner.on_event = terminal_events.append
        original_set = dmm.set_nplc

        def set_nplc(value):
            # A kezdeti runner-konfiguráció átmegy; csak a valódi restore jelez.
            if value == 10.0 and state["acquired"] and not at_signal:
                assert not load.input_commanded_on and not psu.output_commanded_on
                at_signal.append((service.measured_energy_so_far(), recovery.call_count))
                if signal.startswith("stop"):
                    runner.request_stop()
                if "abort" in signal:
                    runner.request_emergency_stop("ABORT_DURING_RESTORE")
                elif "emergency" in signal:
                    runner.request_emergency_stop("USER_EMERGENCY_STOP")
            return original_set(value)

        monkeypatch.setattr(dmm, "set_nplc", set_nplc)
        finalize = Mock(side_effect=AssertionError("Jelzés után nincs finalize"))
        recovery = Mock(wraps=service._capture_recovery_if_configured)
        monkeypatch.setattr(service, "finalize", finalize)
        monkeypatch.setattr(service, "_capture_recovery_if_configured", recovery)
        for device, methods in (
            (load, ("input_on", "fire_transient_pulse")),
            (dmm, ("configure_timed_acquisition", "arm_timed_acquisition", "fetch_timed_acquisition")),
        ):
            for method in methods:
                original = getattr(device, method)

                def recorded(*args, original=original, method=method, **kwargs):
                    if at_signal:
                        later_active.append(method)
                    return original(*args, **kwargs)

                monkeypatch.setattr(device, method, recorded)

        result = runner.run(TestPlan(
            test_type=TestType.OCV_SOC_CHARACTERIZATION,
            steps=(TestStep(StepKind.OCV_SOC, "R16-B15F4"),),
        ))
        signal_reason = (
            "ABORT_DURING_RESTORE" if "abort" in signal
            else "USER_EMERGENCY_STOP" if "emergency" in signal
            else "USER_STOP_REQUESTED"
        )
        assert len(at_signal) == 1
        assert result.status == "FAULT"
        assert result.reason.startswith("PULSE_NPLC_RESTORE_FAILED"), result.reason
        assert result.reason.endswith(" | " + signal_reason), result.reason
        assert ctrl.state == OcvSocState.FAULT
        assert "NPLC_RESTORE_FAILED" in ctrl.fault_reason
        assert service.nplc_restore_failed
        assert state["nplc"] == (acquisition if failure == "ignored_restore" else 10.0)
        restore = commands.index("VOLT:DC:NPLC 10;*OPC?", commands.index(
            f"VOLT:DC:NPLC {acquisition:.6g};*OPC?"
        ))
        assert "VOLT:DC:ZERO:AUTO?" in commands[restore + 1:]
        if failure != "az_query":
            assert "VOLT:DC:NPLC?" in commands[restore + 1:]
        finalize.assert_not_called()
        assert recovery.call_count == at_signal[0][1]
        assert not later_active
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert result.safe_off_attempted
        assert result.safe_off_result["Load"] and result.safe_off_result["PSU"]
        checkpoint = logger.write_checkpoint.call_args.args[0]
        assert (checkpoint["status"], checkpoint["reason"]) == ("FAULT", result.reason)
        terminal = [
            e for e in terminal_events
            if e["event_code"] in ("GRACEFUL_STOP", "EMERGENCY_STOP", "TEST_DONE")
        ]
        assert len(terminal) == 1
        assert terminal[0]["event_code"] == "EMERGENCY_STOP"
        assert result.reason in terminal[0]["event_message"]
        logger.log_event.assert_any_call("EMERGENCY_STOP", result.reason, is_critical=True)
        measured = service.measured_energy_so_far()
        assert measured == at_signal[0][0]
        assert measured.reading_count > 0 and measured.removed_Ah > 0.0
        assert ctrl._pulse_energy_booked == measured
        assert ctrl.accumulated_discharge_Ah == pytest.approx(measured.removed_Ah)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(measured.removed_Wh)
        assert result.total_discharge_ah == pytest.approx(measured.removed_Ah)
        assert result.total_discharge_wh == pytest.approx(measured.removed_Wh)
        assert checkpoint["discharge_ah"] == pytest.approx(measured.removed_Ah)
        assert checkpoint["discharge_wh"] == pytest.approx(measured.removed_Wh)
        assert sum(
            e["event_code"] == "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"
            for e in pulse_events
        ) == 1
        ctrl._book_measured_pulse_energy(service)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(measured.removed_Ah)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(measured.removed_Wh)


class TestR16B15F5ConfigurationAndInterrupts:
    """DFAM-024/001/017/016: mock műszerek, RAM-napló, valós runner-lánc."""

    @staticmethod
    def _runner(ctrl, psu, load, dmm, temp):
        from unittest.mock import Mock

        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger
        from Prog.src.test_runner import TestRunner, TestRunnerConfig

        ctrl._grid_pos = len(ctrl._grid) - 1
        logger = Mock(spec=Logger)
        runner = TestRunner(
            instrument_manager=InstrumentManager(psu, load, dmm, temp),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None, ocv_soc_ctrl_factory=lambda: ctrl,
        )
        return runner, logger

    @staticmethod
    def _run(runner):
        from Prog.src.test_runner import StepKind, TestPlan, TestStep, TestType

        return runner.run(TestPlan(
            test_type=TestType.OCV_SOC_CHARACTERIZATION,
            steps=(TestStep(StepKind.OCV_SOC, "R16-B15F5"),),
        ))

    @pytest.mark.parametrize("failure", [
        "minimum", "autozero", "nplc_readback", "errors", "setter",
    ])
    def test_rejected_configuration_keeps_last_real_voltage(self, monkeypatch, failure):
        ctrl, psu, load, dmm, temp, _clock = TestR16B15F2Cleanup._make()
        setter = dmm.set_nplc
        if failure == "minimum":
            dmm.sample_timer_min_raw_override = "0.21"
        elif failure == "autozero":
            def reject_az():
                raise RuntimeError("AZ readback OFF")
            monkeypatch.setattr(dmm, "enable_autozero", reject_az)
        elif failure in ("nplc_readback", "setter"):
            def rejected_set(value):
                if value == 1.0:
                    if failure == "setter":
                        setter(value)
                        raise RuntimeError("NPLC applied, OPC lost")
                    return
                setter(value)
            monkeypatch.setattr(dmm, "set_nplc", rejected_set)
        else:
            monkeypatch.setattr(
                dmm, "check_error",
                lambda: ["-221,config"] if dmm.nplc == 1.0 else [],
            )
        runner, logger = self._runner(ctrl, psu, load, dmm, temp)
        live = []
        runner.on_sample = live.append

        result = self._run(runner)

        assert result.status == "FAULT"
        assert "IMPULSE_CONFIG_REJECTED" in result.reason
        assert ctrl.u_batt_V == 13.5
        assert ctrl._pulse_v_baseline_V is None
        samples = [call.args[0] for call in logger.log_sample.call_args_list]
        assert samples and live
        assert all(s["battery_voltage_V"] == 13.5 for s in samples + live)
        assert ctrl._pulse_service.finalize().samples == []
        assert not load.called("input_on(")
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert ctrl.accumulated_discharge_Ah == 0.0

    @pytest.mark.parametrize("fast", [False, True])
    def test_runner_defers_failed_nplc_retry_until_pulse_restore(self, monkeypatch, fast):
        ctrl, psu, load, dmm, temp, _clock = TestR16B15F2Cleanup._make(fast)
        runner, _logger = self._runner(ctrl, psu, load, dmm, temp)
        setter, reconfigure = dmm.set_nplc, runner._reconfigure_dmm_nplc_for_relax_substate
        configure, read = dmm.configure_timed_acquisition, dmm.read_voltage
        writes, apertures, baselines = [], [], []
        in_retry = False

        def set_nplc(value):
            writes.append((in_retry, ctrl.state.name, value, load.input_commanded_on))
            if len(writes) == 1:
                raise RuntimeError("step-start NPLC write lost")
            setter(value)

        def retry(step, controller):
            nonlocal in_retry
            in_retry = True
            try:
                reconfigure(step, controller)
            finally:
                in_retry = False

        def acquire(*args, **kwargs):
            apertures.append(dmm.nplc)
            return configure(*args, **kwargs)

        def baseline(*args, **kwargs):
            if ctrl.state in (OcvSocState.IMPULSE_ON, OcvSocState.IMPULSE_FAST_ON):
                baselines.append(dmm.nplc)
            return read(*args, **kwargs)

        monkeypatch.setattr(dmm, "set_nplc", set_nplc)
        monkeypatch.setattr(runner, "_reconfigure_dmm_nplc_for_relax_substate", retry)
        monkeypatch.setattr(dmm, "configure_timed_acquisition", acquire)
        monkeypatch.setattr(dmm, "read_voltage", baseline)

        result = self._run(runner)

        assert result.status == "DONE", result.reason
        acquisition = 0.02 if fast else 1.0
        assert baselines == [acquisition]
        assert apertures and all(nplc == acquisition for nplc in apertures)
        assert not any(retry and (state.startswith("IMPULSE_") or on)
                       for retry, state, _value, on in writes), writes
        assert any(retry and state == "LOG_SOC_POINT" and value == 10.0 and not on
                   for retry, state, value, on in writes), writes
        assert dmm.nplc == 10.0
        assert runner._last_configured_nplc is None
        assert not load.input_commanded_on and not psu.output_commanded_on

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
    def test_unbooked_reading_survives_off_error_and_restore_interrupt(
        self, monkeypatch, fast, exception,
    ):
        ctrl, psu, load, dmm, _temp, _clock = TestR16B15F2Cleanup._make(fast)
        _set_pulse_test_cutoff(ctrl)
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        assert service is not None
        events, at_restore = [], []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        assert load.input_commanded_on
        assert ctrl.accumulated_discharge_Ah == 0.0
        # Cutoff: a service a leolvasást az OFF előtt rögzíti, a controller még nem.
        dmm.timed_voltage_fn = lambda t: 11.0
        off = load.input_off
        off_calls = 0
        interrupted = exception("F5 restore interrupted")

        def failed_off():
            nonlocal off_calls
            off_calls += 1
            off()
            if off_calls == 1:
                raise RuntimeError("F5 terminal OFF failed")

        def interrupt(value):
            assert value == 10.0
            at_restore.append((service.measured_energy_so_far(), ctrl._pulse_energy_booked))
            assert ctrl.state == OcvSocState.FAULT
            assert not load.input_commanded_on and not psu.output_commanded_on
            raise interrupted

        monkeypatch.setattr(load, "input_off", failed_off)
        monkeypatch.setattr(dmm, "set_nplc", interrupt)
        with pytest.raises(exception) as caught:
            ctrl.advance(0.0)

        assert caught.value is interrupted
        assert off_calls >= 2
        assert len(at_restore) == 1
        measured, booked = at_restore[0]
        assert measured.reading_count == 1 and measured.removed_Ah > 0.0
        assert booked.reading_count == 0 and booked.removed_Ah == 0.0
        assert ctrl._pulse_energy_booked == measured
        assert ctrl.accumulated_discharge_Ah == pytest.approx(measured.removed_Ah)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(measured.removed_Wh)
        assert ctrl.state == OcvSocState.FAULT
        assert "F5 terminal OFF failed" in ctrl.fault_reason
        assert sum(e["event_code"] == "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"
                   for e in events) == 1
        ctrl.emergency_stop("SECOND_STOP")
        assert ctrl.accumulated_discharge_Ah == pytest.approx(measured.removed_Ah)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(measured.removed_Wh)
        assert sum(e["event_code"] == "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"
                   for e in events) == 1

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
    def test_normal_finish_restore_interrupt_faults_before_propagating(
        self, monkeypatch, fast, exception,
    ):
        from unittest.mock import Mock

        ctrl, psu, load, dmm, _temp, _clock = TestR16B15F2Cleanup._make(fast)
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        assert service is not None
        ctrl.advance(0.0)
        interrupted = exception("F5 finish interrupted")

        def interrupt(value):
            assert value == 10.0
            assert not load.input_commanded_on and not psu.output_commanded_on
            raise interrupted

        finalizer = Mock(wraps=service.finalize)
        monkeypatch.setattr(service, "finalize", finalizer)
        monkeypatch.setattr(dmm, "set_nplc", interrupt)
        with pytest.raises(exception) as caught:
            for _ in range(1000):
                ctrl.advance(0.0)

        assert caught.value is interrupted
        assert ctrl.state == OcvSocState.FAULT
        assert "PULSE_NPLC_RESTORE_INTERRUPTED" in ctrl.fault_reason
        finalizer.assert_not_called()
        assert not load.input_commanded_on and not psu.output_commanded_on
        measured = service.measured_energy_so_far()
        assert measured.reading_count > 0 and measured.removed_Ah > 0.0
        assert ctrl.accumulated_discharge_Ah == pytest.approx(measured.removed_Ah)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(measured.removed_Wh)


class TestR16B16aNormalFloorSibling:
    @pytest.mark.parametrize("phase", ["setup", "sample", "config"])
    @pytest.mark.parametrize("failed_off", ["none", "load", "psu"])
    def test_common_floor_fault_cleanup_event_and_no_restart(self, phase, failed_off):
        parts = make_ocv_soc_controller()
        ctrl, psu, load, dmm = parts[:4]
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._step_discharge_started = True
        ctrl._dmm_valid = True
        ctrl._u_batt = dmm.voltage_V = 10.79 if phase != "config" else 12.5
        if phase == "config":
            ctrl._profile.terminate_voltage_per_cell_V = 1.60
        if phase == "sample":
            # Az első cutoff-mintát a meglévő terminate-ág rendesen lezárja.
            load.input_commanded_on = True
            ctrl._u_batt = dmm.voltage_V = 10.8
            ctrl.advance(1.0)
            assert ctrl.state == OcvSocState.STEP_RELAX
            ctrl._u_batt = dmm.voltage_V = 10.79
        calls, events = [], []
        def load_off():
            calls.append("load_off")
            if failed_off == "load":
                raise RuntimeError("load OFF error")
            load.input_commanded_on = False
        def psu_off():
            calls.append("psu_off")
            if failed_off == "psu":
                raise RuntimeError("psu OFF error")
            psu.output_commanded_on = False
        load.input_off, psu.all_outputs_off = load_off, psu_off
        ctrl.on_event = events.append
        if phase == "sample":
            load.input_commanded_on = True
            ctrl.advance(1.0)
        else:
            ctrl._begin_step_discharge()
        assert ctrl.state.value == "FAULT"
        reason = "DISCHARGE_CONFIG_INVALID" if phase == "config" else "BATTERY_UNDERVOLTAGE"
        assert ctrl.fault_reason.startswith(reason)
        assert calls[:2] == ["load_off", "psu_off"]
        assert any(e["event_code"] == reason for e in events)
        ctrl.advance(1.0)
        assert not load.called("input_on")


class TestR16B16bOcvPulseFloor:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("setup", [False, True])
    @pytest.mark.parametrize("failed_off", ["none", "load", "psu"])
    def test_pms_fault_reaches_parent_with_both_off_and_event(self, fast, setup, failed_off):
        cfg = OcvSocConfig(fast_transient_enabled=True)
        ctrl, psu, load, dmm, _temp = make_ocv_soc_controller(config=cfg)
        ctrl._profile.terminate_voltage_per_cell_V = 1.90
        ctrl._pulse_service._terminate_voltage_V = 11.4
        ctrl._fast_pulse_service._terminate_voltage_V = 11.4
        on = OcvSocState.IMPULSE_FAST_ON if fast else OcvSocState.IMPULSE_ON
        acquire = OcvSocState.IMPULSE_FAST_ACQUIRE if fast else OcvSocState.IMPULSE_ACQUIRE
        ctrl._state = on
        ctrl._u_batt = 13.0
        if not setup:
            ctrl.advance(0.0)
            assert ctrl.state == acquire
        calls, events = [], []
        def load_off():
            calls.append("load_off")
            if failed_off == "load":
                raise RuntimeError("load OFF error")
            load.input_commanded_on = False
        def psu_off():
            calls.append("psu_off")
            if failed_off == "psu":
                raise RuntimeError("psu OFF error")
            psu.output_commanded_on = False
        load.input_off, psu.all_outputs_off = load_off, psu_off
        ctrl.on_event = events.append
        dmm.voltage_V = 10.79 if setup else 13.0
        dmm.fetch_timed_acquisition_raw_override = "12.0,11.4,10.79"
        ctrl.advance(0.0)
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason.startswith("BATTERY_UNDERVOLTAGE")
        # A PMS előzetes Load OFF-ja után is mindkét szülői OFF megtörténik.
        assert "load_off" in calls and "psu_off" in calls
        assert calls.index("load_off") < calls.index("psu_off")
        assert any(e["event_code"] == "BATTERY_UNDERVOLTAGE" and e["severity"] == "CRITICAL"
                   for e in events)
        before = list(load.calls)
        ctrl.advance(1.0)
        assert load.calls == before

    def test_controller_rejects_hardware_pulse(self):
        from Prog.src.pulse_measurement_service import PulseDischargeError
        with pytest.raises(PulseDischargeError, match="HARDWARE_PULSE_HIL_PENDING"):
            make_ocv_soc_controller(config=OcvSocConfig(
                pulse_trigger_mode="HARDWARE_PULSE_SOFTWARE_TRIGGERED",
            ))


def _set_pulse_test_cutoff(ctrl):
    # A rendes cutoff-unitok az adatlapi minimum FELETT zárnak; a tiltott
    # tartományt külön, az éles kapuval a B16b regressziók vizsgálják.
    ctrl._profile.terminate_voltage_per_cell_V = 1.90
    ctrl._pulse_service._terminate_voltage_V = 11.4
    if ctrl._fast_pulse_service is not None:
        ctrl._fast_pulse_service._terminate_voltage_V = 11.4


class TestR16B16bFloorEventCleanup:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("setup", [False, True])
    @pytest.mark.parametrize("error_type", [RuntimeError, SystemExit])
    def test_raising_fault_event_runs_all_restore_attempts(self, monkeypatch, fast, setup, error_type):
        ctrl, psu, load, dmm, _temp = make_ocv_soc_controller(
            config=OcvSocConfig(fast_transient_enabled=True),
        )
        ctrl._u_batt = 13.0
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        ctrl._state = OcvSocState.IMPULSE_FAST_ON if fast else OcvSocState.IMPULSE_ON
        if not setup:
            ctrl.advance(0.0)
            ctrl.advance(0.0)  # Egy jó chunk prefixe már könyvelt.
        before_Ah = ctrl.accumulated_discharge_Ah
        restores, events = [], []
        for candidate, label in [(ctrl._pulse_service, "standard"),
                                 (ctrl._fast_pulse_service, "fast")]:
            restore = candidate.restore_acquisition_nplc
            def tracked_restore(restore=restore, label=label):
                restores.append(label)
                return restore()
            monkeypatch.setattr(candidate, "restore_acquisition_nplc", tracked_restore)
        def event(record):
            events.append(record)
            if record["event_code"] == "BATTERY_UNDERVOLTAGE":
                raise error_type("fault event failed")
        ctrl.on_event = event
        if setup:
            dmm.voltage_V = 10.79
        else:
            dmm.fetch_timed_acquisition_raw_override = "13.0,10.79,13.0"
        with pytest.raises(error_type, match="fault event failed"):
            ctrl.advance(0.0)
        assert restores == ["standard", "fast"]
        assert ctrl.state == OcvSocState.FAULT
        assert ctrl.fault_reason.startswith("BATTERY_UNDERVOLTAGE")
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert dmm.nplc == 10.0
        assert ctrl.accumulated_discharge_Ah == pytest.approx(before_Ah)
        if not setup:
            assert service.measured_energy_so_far().removed_Ah > 0.0
            assert sum(e["event_code"] == "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"
                       for e in events) == 1
        ctrl.emergency_stop("SECOND_STOP")
        assert ctrl.accumulated_discharge_Ah == pytest.approx(before_Ah)


class TestR16B16bNormalEventSibling:
    def test_normal_discharge_floor_keeps_one_critical_event(self):
        ctrl, psu, load, dmm, _temp = make_ocv_soc_controller()
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._dmm_valid = True
        ctrl._u_batt = dmm.voltage_V = 10.79
        events = []
        ctrl.on_event = events.append
        ctrl._begin_step_discharge()
        assert ctrl.state == OcvSocState.FAULT
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert sum(e["event_code"] == "BATTERY_UNDERVOLTAGE" for e in events) == 1


class TestR16B21bWindowMatrix:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("tick", [0.0, 2.0, 5.0, 30.0])
    @pytest.mark.parametrize("latency", [
        {}, {"temp_s": 0.45}, {"config_s": 0.07},
        {"fetch_s": 0.11}, {"measure_current_s": 0.03, "input_off_s": 0.07},
    ])
    def test_owned_window_targets_and_totals(self, monkeypatch, fast, tick, latency):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False, **latency)
        _B21bRig.runner(monkeypatch, rig, tick)
        session = rig.runner.run(rig.plan)
        assert session.status == "DONE"
        result = rig.service.finalize()
        target = 0.6 if fast else 30.0
        max_io = max(latency.values(), default=0.0)
        assert target - 1e-8 <= result.load_command_duration_s <= target + max_io + 1e-8
        assert result.load_command_time_error_s == pytest.approx(result.load_command_duration_s - target)
        assert result.load_on_return_monotonic_s >= result.load_on_command_monotonic_s
        assert result.load_off_return_monotonic_s >= result.load_off_command_monotonic_s
        if not latency:
            assert result.load_command_duration_s == pytest.approx(target)
            assert all(p.within_tolerance and p.current_A == pytest.approx(1.4) for p in result.results.values())
        if not result.current_trace_incomplete:
            expected = 1.4 * result.pulse_duration_actual_s / 3600.0
            assert result.removed_Ah == pytest.approx(expected)
            assert result.removed_Wh == pytest.approx(expected * 13.2)
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(result.removed_Ah)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(result.removed_Wh)
        assert session.total_discharge_ah == pytest.approx(result.removed_Ah)
        assert session.total_discharge_wh == pytest.approx(result.removed_Wh)
        assert rig.points[0]["removed_Ah_total"] == pytest.approx(result.removed_Ah)
        assert len(result.samples) <= round(target / rig.service._sample_interval_s)
        assert sum(c.startswith("input_on(") for c in rig.load.calls) == 1
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert rig.dmm.nplc == 10.0 and rig.dmm._measurement_owner is None
        windows = [e for e in rig.events if e["event_code"] == "OCV_SOC_PULSE_WINDOW"]
        assert windows and "HIL_PENDING" in windows[0]["event_message"]
        if fast:
            assert rig.points[0]["fast_transient_voltage_trace_incomplete"] == result.voltage_trace_incomplete
        else:
            assert rig.points[0]["voltage_trace_incomplete"] == result.voltage_trace_incomplete

    @pytest.mark.parametrize("fast", [False, True])
    def test_variable_measured_current_real_stamps_and_once_only_tail(self, monkeypatch, fast):
        from threading import get_ident

        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(
            monkeypatch, fast, start=False, config_s=0.007,
            fetch_s=0.011, measure_current_s=0.003, input_off_s=0.07,
        )
        _B21bRig.runner(monkeypatch, rig, 30.0)
        events = _B21bRig.trace(monkeypatch, rig)
        original_fetch = rig.dmm.fetch_timed_acquisition
        original_current = rig.load.measure_current
        fetch_ends, measured = [], []
        control_thread = get_ident()

        def fetch(**kwargs):
            raw = original_fetch(**kwargs)
            fetch_ends.append(rig.clock())
            return raw

        def current():
            assert get_ident() == control_thread
            baseline = original_current()
            if not rig.load.input_commanded_on:
                return baseline
            value = 0.41 + 0.07 * (len(measured) % 5)
            measured.append((fetch_ends[-1], value))
            return value

        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", fetch)
        monkeypatch.setattr(rig.load, "measure_current", current)
        session = rig.runner.run(rig.plan)
        assert session.status == "DONE" and measured
        result = rig.service.finalize()
        prev, expected_ah = result.load_on_command_monotonic_s, 0.0
        for at, value in measured:
            expected_ah += value * (at - prev) / 3600.0
            prev = at
        expected_ah += measured[-1][1] * (result.load_off_return_monotonic_s - prev) / 3600.0
        assert result.removed_Ah == pytest.approx(expected_ah)
        assert result.removed_Wh == pytest.approx(expected_ah * 13.2)
        assert session.total_discharge_ah == pytest.approx(expected_ah)
        assert rig.points[0]["removed_Ah_total"] == pytest.approx(expected_ah)
        assert result.samples[0].time_from_pulse_start_s > rig.service._sample_interval_s
        worker_threads = {tid for label, name, tid in events if label == "dmm" and name == "fetch_timed_acquisition"}
        assert len(worker_threads) == 1 and control_thread not in worker_threads
        assert all(tid == control_thread for label, _, tid in events if label in {"load", "psu"})
        before = (rig.ctrl.accumulated_discharge_Ah, rig.ctrl.accumulated_discharge_Wh)
        finish = rig.ctrl._finish_fast_impulse_acquisition if fast else rig.ctrl._finish_impulse_acquisition
        for _ in range(3):
            rig.ctrl._book_measured_pulse_energy(rig.service)
            finish()
            assert rig.service.finalize().removed_Ah == result.removed_Ah
        assert (rig.ctrl.accumulated_discharge_Ah, rig.ctrl.accumulated_discharge_Wh) == before
        assert rig.dmm._measurement_owner is None

    def test_standard_then_fast_both_workers_and_soc_share_single_deltas(self, monkeypatch):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, True, start=False)
        rig.ctrl._state = OcvSocState.IMPULSE_ON
        _B21bRig.runner(monkeypatch, rig, 30.0)
        session = rig.runner.run(rig.plan)
        assert session.status == "DONE"
        standard, fast = rig.ctrl._pulse_service, rig.ctrl._fast_pulse_service
        a, b = standard.finalize(), fast.finalize()
        assert a.load_command_duration_s == pytest.approx(30.0)
        assert b.load_command_duration_s == pytest.approx(0.6)
        assert b.load_on_command_monotonic_s >= a.load_off_return_monotonic_s
        expected = a.removed_Ah + b.removed_Ah
        assert session.total_discharge_ah == pytest.approx(expected)
        assert rig.points[0]["removed_Ah_total"] == pytest.approx(expected)
        assert sum(c.startswith("input_on(") for c in rig.load.calls) == 2
        before = rig.ctrl.accumulated_discharge_Ah
        rig.ctrl._book_measured_pulse_energy(standard)
        rig.ctrl._book_measured_pulse_energy(fast)
        assert rig.ctrl.accumulated_discharge_Ah == before

    def test_recovery_after_release_excluded_and_no_new_current_io(self, monkeypatch):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, start=False, input_off_s=0.07)
        rig.service._recovery_duration_s = 1.0
        _B21bRig.runner(monkeypatch, rig)
        saw_recovery = []
        original = rig.dmm.configure_timed_acquisition

        def configure(*args, **kwargs):
            if kwargs.get("owner") is None and rig.service._started:
                saw_recovery.append(rig.clock())
                assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
                assert rig.dmm._measurement_owner is None
            return original(*args, **kwargs)

        monkeypatch.setattr(rig.dmm, "configure_timed_acquisition", configure)
        session = rig.runner.run(rig.plan)
        assert session.status == "DONE"
        result = rig.service.finalize()
        assert len(saw_recovery) == 1 and len(result.recovery_samples) == 5
        expected = 1.4 * (30.0 + 0.07) / 3600.0
        assert result.removed_Ah == pytest.approx(expected)
        assert session.total_discharge_ah == pytest.approx(expected)
        assert result.recovery_samples[-1].time_from_pulse_start_s > result.pulse_duration_actual_s


class TestR16B21bPollBoundaries:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("arrival", ["stop", "deadline", "fault"])
    @pytest.mark.parametrize("target", [
        "psu.check_error", "load.check_error", "temp.check_error",
        "psu.read_output_state", "load.read_input_state", "load.read_questionable_status",
    ])
    def test_no_new_poll_io_before_off(self, monkeypatch, fast, arrival, target):
        from threading import Event, get_ident

        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _B21bRig.runner(monkeypatch, rig, 30.0)
        events = _B21bRig.trace(monkeypatch, rig)
        entered, release = Event(), Event()
        fetch, off = rig.dmm.fetch_timed_acquisition, rig.psu.all_outputs_off
        injected = []

        def blocked_fetch(**kwargs):
            entered.set()
            assert release.wait(3)
            return fetch(**kwargs)

        def psu_off():
            value = off()
            if entered.is_set():
                release.set()
            return value

        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", blocked_fetch)
        monkeypatch.setattr(rig.psu, "all_outputs_off", psu_off)
        for label, name in [s.split(".") for s in [
            "psu.check_error", "load.check_error", "temp.check_error",
            "psu.read_output_state", "load.read_input_state", "load.read_questionable_status",
        ]]:
            device = getattr(rig, label)
            original = getattr(device, name)
            key = label + "." + name

            def poll(*args, _fn=original, _key=key, **kwargs):
                events.append(("poll", _key, get_ident()))
                if _key == target and rig.load.input_commanded_on and not injected:
                    assert entered.wait(2)
                    injected.append(len(events))
                    if arrival == "stop":
                        rig.runner.request_stop()
                    elif arrival == "deadline":
                        rig.clock.advance(30.0 if not fast else 0.6)
                    else:
                        raise RuntimeError("B21b polling failed")
                return _fn(*args, **kwargs)

            monkeypatch.setattr(device, name, poll)
        try:
            session = rig.runner.run(rig.plan)
        finally:
            release.set()
        assert injected
        assert events[injected[0]][:2] == ("load", "input_off")
        expected = {"stop": "STOPPED", "deadline": "DONE", "fault": "FAULT"}[arrival]
        assert session.status == expected, session.reason
        if arrival == "fault":
            assert "B21b polling failed" in session.reason
        assert session.total_discharge_ah == 0.0
        assert rig.dmm._measurement_owner is None and rig.dmm.nplc == 10.0
        assert sum(c.startswith("input_on(") for c in rig.load.calls) == 1

    @pytest.mark.parametrize("fast", [False, True])
    def test_timing_quality_continues_to_next_soc_discharge(self, monkeypatch, fast):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, config_s=0.2)
        rig.ctrl._grid_pos = 0
        _B21bRig.run(rig)
        assert rig.ctrl.state == OcvSocState.LOG_SOC_POINT
        result = rig.service.finalize()
        assert result.voltage_trace_incomplete
        # Továbblépés a hosszú folyamatban; a hiányzó célpont nem FAULT.
        rig.ctrl.advance(0.0)
        assert rig.ctrl.state == OcvSocState.STEP_DISCHARGE
        assert not rig.ctrl.fault_reason


class TestR16B21bTemperatureClock:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("setup_s", [0.0, 2.0, 7.0])
    def test_next_failed_temperature_read_does_not_recount_observed_setup(
        self, monkeypatch, fast, setup_s,
    ):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _B21bRig.runner(monkeypatch, rig, 0.0)
        nplc = rig.dmm.set_nplc
        read = rig.temp.read_temperature
        valid_starts, failed_starts = [], []
        original_record = rig.ctrl._safety.record_temperature_dmm_read
        failed_dts = []

        def prepare(value):
            result = nplc(value)
            if value != 10.0 and not rig.service._started:
                rig.clock.advance(setup_s)
            return result

        def temperature():
            now = rig.clock()
            if rig.ctrl.state == OcvSocState.LOG_SOC_POINT:
                failed_starts.append(now)
                raise RuntimeError("B21b next outer temperature failure")
            value = read()
            valid_starts.append(now)
            return value

        def record(*, valid, dt_s):
            if not valid:
                failed_dts.append(dt_s)
            return original_record(valid=valid, dt_s=dt_s)

        monkeypatch.setattr(rig.dmm, "set_nplc", prepare)
        monkeypatch.setattr(rig.temp, "read_temperature", temperature)
        monkeypatch.setattr(rig.ctrl._safety, "record_temperature_dmm_read", record)
        session = rig.runner.run(rig.plan)
        assert session.status == "DONE", session.reason
        assert failed_starts and failed_dts and valid_starts
        assert failed_dts[0] == pytest.approx(failed_starts[0] - valid_starts[-1])
        assert 0.0 <= failed_dts[0] < 1.0
        assert rig.ctrl.last_pulse_observed_extra_s == 0.0

    @pytest.mark.parametrize("fast", [False, True])
    def test_critical_poll_first_off_failure_remains_in_reason(self, monkeypatch, fast):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _B21bRig.runner(monkeypatch, rig)
        old_error, off = rig.load.check_error, rig.load.input_off
        injected, off_failed = [], []

        def error():
            if rig.load.input_commanded_on and not injected:
                injected.append(1)
                return ['-222,"B21b critical poll"']
            return old_error()

        def turn_off():
            if injected and not off_failed:
                off_failed.append(1)
                raise RuntimeError("B21b first OFF failed")
            return off()

        monkeypatch.setattr(rig.load, "check_error", error)
        monkeypatch.setattr(rig.load, "input_off", turn_off)
        session = rig.runner.run(rig.plan)
        assert session.status == "FAULT"
        assert "SCPI_CRITICAL_ERROR[Load]" in session.reason
        assert "B21b critical poll" in session.reason
        assert "LOAD_OFF_FAILED:B21b first OFF failed" in session.reason
        assert off_failed == [1]
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on


class TestR16B21bFaultCallback:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("event_code", ["OCV_SOC_PULSE_WINDOW", "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"])
    @pytest.mark.parametrize("interrupt", [False, True])
    def test_already_fault_callback_is_secondary_and_interrupt_object_survives(
        self, monkeypatch, fast, event_code, interrupt,
    ):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast)
        error = KeyboardInterrupt("B21b callback interrupted") if interrupt else OSError("B21b event sink failed")
        emitted = []

        def event(payload):
            emitted.append(payload)
            if payload["event_code"] == event_code:
                raise error

        rig.ctrl.on_event = event

        def poll(force, before_io):
            rig.ctrl.emergency_stop("SCPI_CRITICAL_ERROR[Load]: B21b original")
            return rig.ctrl.fault_reason

        if interrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                _B21bRig.run(rig, poll)
            assert caught.value is error
        else:
            _B21bRig.run(rig, poll)
        assert rig.ctrl.state == OcvSocState.FAULT
        assert rig.ctrl.fault_reason.startswith("SCPI_CRITICAL_ERROR[Load]: B21b original")
        assert "PULSE_SECONDARY_ERROR" in rig.ctrl.fault_reason
        assert str(error) in rig.ctrl.fault_reason
        assert any(e["event_code"] == "OCV_SOC_PULSE_WINDOW" for e in emitted)
        assert rig.dmm._measurement_owner is None and rig.dmm.nplc == 10.0
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on


class TestR16B21bExpiredAckAtOff:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("where", ["config", "fetch", "temperature", "off_return"])
    def test_expired_pending_survives_off_and_off_return_does_not_age_it(
        self, monkeypatch, fast, where,
    ):
        from types import SimpleNamespace

        import Prog.src.instrument_manager as manager_module
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        latency = {
            "config": {"config_s": 1.2}, "fetch": {"fetch_s": 1.2},
            "temperature": {"temp_s": 1.2}, "off_return": {"input_off_s": 1.2},
        }[where]
        rig = _B21bRig.make(monkeypatch, fast, start=False, **latency)
        _B21bRig.runner(monkeypatch, rig, 30.0)
        monkeypatch.setattr(manager_module, "time", SimpleNamespace(monotonic=rig.clock))
        turn_on, turn_off = rig.load.input_on, rig.load.input_off
        sends = []

        def on(*args, **kwargs):
            value = turn_on(*args, **kwargs)
            rig.load.input_on_commanded_at = rig.clock()
            return value

        def off():
            if rig.load.input_commanded_on:
                sends.append(rig.clock() - rig.load.input_on_commanded_at)
            return turn_off()

        monkeypatch.setattr(rig.load, "input_on", on)
        monkeypatch.setattr(rig.load, "input_off", off)
        monkeypatch.setattr(rig.load, "read_input_state", lambda: False)
        session = rig.runner.run(rig.plan)
        assert sends
        if fast and where == "off_return":
            assert sends[0] == pytest.approx(0.6)
            assert session.status == "DONE", session.reason
            assert rig.service.activation_ack_diagnostic is None
        else:
            assert session.status == "FAULT", session.reason
            assert sends[0] >= 1.0
            assert "INPUT_STATE_" in session.reason
            if fast:
                assert "ON-nyugta" in session.reason
                assert "OFF-prioritás" in session.reason
                assert "nem az OFF eredménye" in session.reason
        assert rig.load.input_on_commanded_at is None
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert rig.dmm._measurement_owner is None and rig.dmm.nplc == 10.0
        assert session.safe_off_attempted


class TestR16B21bPreparedOnTerminal:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("delay_s", [0.6, 1.2])
    @pytest.mark.parametrize("terminal", ["stop", "write_error"])
    def test_on_terminal_before_worker_keeps_audit_and_expired_ack(
        self, monkeypatch, fast, delay_s, terminal,
    ):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _B21bRig.runner(monkeypatch, rig, 30.0)
        events = _B21bRig.trace(monkeypatch, rig)
        turn_on = rig.load.input_on

        def on(*args, **kwargs):
            value = turn_on(*args, **kwargs)
            rig.load.input_on_commanded_at = rig.clock()
            rig.clock.advance(delay_s)
            if terminal == "write_error":
                raise RuntimeError("B21b ON write return lost")
            rig.runner.request_stop()
            return value

        monkeypatch.setattr(rig.load, "input_on", on)
        session = rig.runner.run(rig.plan)
        expected = "STOPPED" if terminal == "stop" and delay_s < 1.0 else "FAULT"
        assert session.status == expected, session.reason
        if terminal == "write_error":
            assert "ON write return lost" in session.reason
        if delay_s >= 1.0:
            assert "ON-nyugta" in session.reason
        windows = [e for e in rig.events if e["event_code"] == "OCV_SOC_PULSE_WINDOW"]
        assert len(windows) == 1
        assert "OFF-küldés=" in windows[0]["event_message"]
        assert ("ON-visszatérés=None" in windows[0]["event_message"]) == (terminal == "write_error")
        assert not any(name == "acquire_measurement" for _, name, _ in events)
        assert rig.service._worker is None and rig.dmm._measurement_owner is None
        assert rig.dmm.nplc == 10.0
        assert session.total_discharge_ah == 0.0
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on


class TestR16B21bAckCleanupCallback:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("event_code", ["OCV_SOC_PULSE_WINDOW", "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"])
    @pytest.mark.parametrize("interrupt", [False, True])
    def test_expired_ack_remains_primary_when_cleanup_callback_raises(
        self, monkeypatch, fast, event_code, interrupt,
    ):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, config_s=(0.6 if fast else 30.0) + 1.2)
        rig.load.input_on_commanded_at = rig.clock()
        error = KeyboardInterrupt("B21b ACK callback interrupted") if interrupt else OSError("B21b ACK sink failed")
        emitted = []

        def event(payload):
            emitted.append(payload)
            if payload["event_code"] == event_code:
                raise error

        rig.ctrl.on_event = event
        if interrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                _B21bRig.run(rig)
            assert caught.value is error
        else:
            _B21bRig.run(rig)
        assert rig.ctrl.state == OcvSocState.FAULT
        assert rig.ctrl.fault_reason.startswith("SCPI_CRITICAL_ERROR[Load]: INPUT_STATE_POLL_FAILED: ON-nyugta")
        assert "PULSE_SECONDARY_ERROR" in rig.ctrl.fault_reason
        assert str(error) in rig.ctrl.fault_reason
        assert any(e["event_code"] == "OCV_SOC_PULSE_WINDOW" for e in emitted)
        assert rig.dmm._measurement_owner is None and rig.dmm.nplc == 10.0
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on


def _b24_bound(monkeypatch, rig):
    """Bound every new runner/block chain even if a trigger or clock regresses."""
    from time import monotonic

    deadline = monotonic() + 10.0
    clock, advance = rig.service._time_source, rig.ctrl.advance
    ticks = []

    def bounded_clock():
        assert monotonic() < deadline, "B24 pulse chain exceeded its wall-time bound"
        return clock()

    def bounded_advance(dt_s):
        assert len(ticks) < 12, "B24 runner trigger did not terminate the chain"
        ticks.append(rig.ctrl.state)
        return advance(dt_s)

    monkeypatch.setattr(rig.service, "_time_source", bounded_clock)
    monkeypatch.setattr(rig.ctrl, "advance", bounded_advance)
    return ticks


class TestR16B24Ocv:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("owned", [False, True])
    def test_q40_active_command_window_and_csv(self, monkeypatch, fast, owned):
        import csv
        import io
        from types import SimpleNamespace

        from Prog.src.logger import CSV_COLUMNS, Logger
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _b24_bound(monkeypatch, rig)
        _B21bRig.runner(monkeypatch, rig)
        step = rig.plan.steps[0]
        samples, commands = [], []
        set_current = rig.load.set_current

        def setpoint(value):
            set_current(value)
            commands.append(value)
            assert rig.ctrl.i_load_set_A == 0.0

        monkeypatch.setattr(rig.load, "set_current", setpoint)
        samples.append(rig.runner._build_sample(step, rig.ctrl))
        if owned:
            rig.ctrl.prepare_pulse_block()
        rig.ctrl.advance(0.0)
        assert commands == [pytest.approx(1.4)]
        assert rig.service._on_return_at is not None
        samples.append(rig.runner._build_sample(step, rig.ctrl))
        rig.load.input_off()
        rig.service.note_load_off_return()
        # Keep ACQUIRE state: OFF, rather than a state transition, must clear it.
        samples.append(rig.runner._build_sample(step, rig.ctrl))
        assert [s["load_set_current_A"] for s in samples] == [0.0, pytest.approx(1.4), 0.0]
        assert [s["load_input_commanded_on"] for s in samples] == [False, True, False]
        if not owned:
            # Legacy normal OFF already recorded its integration endpoint.
            rig.service._off_return_at = None
            known_off_s = rig.service._load_off_real_s
            rig.clock.advance(7.0)
            rig.ctrl.emergency_stop("B24_LATER_CLEANUP")
            assert rig.service._load_off_real_s == known_off_s
        rig.ctrl._state = OcvSocState.IMPULSE_FAST_ON if fast else OcvSocState.IMPULSE_ON
        assert rig.ctrl.i_load_set_A == 0.0  # previous pulse's _started remains True
        rig.service._on_return_at = None
        rig.service._off_return_at = None
        rig.service._load_off_real_s = None
        assert rig.service._started and rig.ctrl.i_load_set_A == 0.0

        sink = io.StringIO(newline="")
        logger = object.__new__(Logger)
        logger._csv_writer = csv.DictWriter(sink, fieldnames=CSV_COLUMNS)
        logger._csv_writer.writeheader()
        logger._pending_rows = []
        logger._last_commit_wall_t = float("inf")
        logger._config = SimpleNamespace(sqlite_commit_interval_s=1.0)
        for sample in samples:
            logger.log_sample(sample)
        rows = list(csv.DictReader(io.StringIO(sink.getvalue())))
        assert [float(row["load_set_current_A"]) for row in rows] == [0.0, pytest.approx(1.4), 0.0]
        rig.ctrl.emergency_stop("B24_TEST_CLEANUP")
        assert rig.dmm.nplc == 10.0

    @pytest.mark.parametrize("fast", [False, True])
    def test_q40_failed_on_is_not_active_even_with_conservative_command_flag(self, monkeypatch, fast):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False, input_off_s=0.25)
        _b24_bound(monkeypatch, rig)
        entered, events, off_returns = [], [], []
        rig.ctrl.on_event = events.append
        input_off = rig.load.input_off

        def off():
            input_off()
            off_returns.append(rig.clock())

        def on(**kwargs):
            entered.append(1)
            rig.load.input_commanded_on = True
            assert rig.ctrl.i_load_set_A == 0.0
            rig.clock.advance(0.5)
            raise OSError("B24 ON return lost")

        monkeypatch.setattr(rig.load, "input_on", on)
        monkeypatch.setattr(rig.load, "input_off", off)
        rig.ctrl.prepare_pulse_block()
        rig.ctrl.advance(0.0)
        assert entered == [1]
        assert rig.ctrl.state is OcvSocState.FAULT
        assert "B24 ON return lost" in rig.ctrl.fault_reason
        assert rig.service._on_return_at is None
        assert rig.ctrl.i_load_set_A == 0.0
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert off_returns == pytest.approx([100.75, 101.0])
        assert rig.service._off_return_at is not None
        assert rig.service._off_return_at == pytest.approx(off_returns[0])
        windows = [e["event_message"] for e in events if e["event_code"] == "OCV_SOC_PULSE_WINDOW"]
        assert len(windows) == 1
        off_text = windows[0].split("OFF-visszatérés=", 1)[1].split(";", 1)[0]
        assert float(off_text) == pytest.approx(off_returns[0])
        known_off_s = rig.service._load_off_real_s
        assert known_off_s is not None
        rig.clock.advance(7.0)
        rig.ctrl.emergency_stop("B24_LATER_CLEANUP")
        assert rig.service._off_return_at == pytest.approx(off_returns[0])
        assert rig.service._load_off_real_s == known_off_s
        assert rig.ctrl.i_load_set_A == 0.0
        assert rig.dmm.nplc == 10.0

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("kind", ["reset", "floor", "floor_reset"])
    def test_emergency_drain_restores_nplc_and_preserves_floor_event(self, monkeypatch, fast, kind):
        from queue import Queue
        from threading import Event
        from types import SimpleNamespace

        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast)
        _b24_bound(monkeypatch, rig)
        events, order = [], []
        rig.ctrl.on_event = events.append
        for device, name in ((rig.load, "input_off"), (rig.psu, "all_outputs_off")):
            original = getattr(device, name)

            def off(_original=original, _name=name):
                order.append(_name)
                return _original()

            monkeypatch.setattr(device, name, off)
        results = Queue()
        if "floor" in kind:
            results.put(("13.2,10.0,13.2", 0.6, None))

        def close():
            order.append("worker_close")
            if "reset" in kind:
                raise RuntimeError("PULSE_DMM_CLEANUP_FAILED:B24 reset failed")

        rig.service._worker = SimpleNamespace(
            results=results, cancel=Event(), close_after_safe_off=close,
            owner_release_confirmed=True, _thread=SimpleNamespace(is_alive=lambda: False),
        )
        rig.ctrl.emergency_stop("B24_SUPERVISORY_FAULT")
        assert order[:3] == ["input_off", "all_outputs_off", "worker_close"]
        assert rig.ctrl.fault_reason.startswith("B24_SUPERVISORY_FAULT")
        expected_label = "PULSE_DRAIN_FAILED" if "floor" in kind else "DMM_CLEANUP_FAILED"
        assert expected_label in rig.ctrl.fault_reason
        assert "NPLC_RESTORE_FAILED" not in rig.ctrl.fault_reason
        assert rig.service._worker is None and rig.dmm.nplc == 10.0
        assert not rig.service.nplc_restore_failed
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        floor_events = [e for e in events if e["event_code"] == "BATTERY_UNDERVOLTAGE"]
        assert len(floor_events) == (1 if "floor" in kind else 0)
        if floor_events:
            assert floor_events[0]["severity"] == "CRITICAL"
            assert "10" in floor_events[0]["event_message"]
            assert rig.service._discharge_fault is not None

    @pytest.mark.parametrize("fast", [False, True])
    def test_temperature_events_follow_status_transitions_without_losing_reads(self, monkeypatch, fast):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _B21bRig.runner(monkeypatch, rig)
        ticks = _b24_bound(monkeypatch, rig)
        reads = []
        read = rig.temp.read_temperature

        def temperature():
            value = read()
            reads.append(value)
            return value

        monkeypatch.setattr(rig.temp, "read_temperature", temperature)
        result = rig.runner.run(rig.plan)
        temperature_events = [e for e in rig.events if e["event_code"] == "OCV_SOC_PULSE_TEMPERATURE"]
        assert result.status == "DONE" and ticks
        assert len(reads) > 2
        assert len(temperature_events) == 1
        assert "valid=True" in temperature_events[0]["event_message"]
        assert result.warning_code_counts == {}
        assert rig.service.finalize().load_command_duration_s == pytest.approx(0.6 if fast else 30.0)

    def test_supervise_concurrent_gate_runs_before_another_chunk(self, monkeypatch):
        from queue import Empty, Queue
        from threading import Event
        from types import SimpleNamespace

        import Prog.src.pulse_measurement_service as module
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch)
        _b24_bound(monkeypatch, rig)
        results, waits, polls = Queue(), [], []

        get = results.get

        def wait(**kwargs):
            if kwargs.get("block") is False:
                return get(**kwargs)
            assert not waits, "B24 supervise did not stop concurrent PSU and Load"
            waits.append(1)
            rig.psu.output_commanded_on = True
            rig.clock.advance(0.02)
            raise Empty

        monkeypatch.setattr(results, "get", wait)
        worker = SimpleNamespace(
            results=results, cancel=Event(), request=lambda count: None,
            close_after_safe_off=lambda: None, owner_release_confirmed=True,
            _thread=SimpleNamespace(is_alive=lambda: False),
        )
        monkeypatch.setattr(module, "_PulseDmmWorker", lambda service: worker)

        def poll(force, before_io):
            polls.append(1)
            assert len(polls) <= 1, "B24 concurrency gate failed to stop the block"

        rig.ctrl.run_pulse_block(poll)
        assert waits == polls == [1]
        assert rig.ctrl.state is OcvSocState.FAULT
        assert rig.ctrl.fault_reason == "CONCURRENT_PSU_LOAD_ON"
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert rig.service._worker is None and rig.dmm.nplc == 10.0

    def test_trace_comment_describes_deadline_limited_voltage_capture(self):
        import Prog.src.ocv_soc_controller as module

        def detector(source):
            start = source.index("    def _determine_point_quality(")
            block = source[start:source.index("    def ", start + 5)]
            return (
                "standard/fast INSTRUMENT_TIMED" in block
                and "VOLTAGE_TRACE_INCOMPLETE megmarad" in block
                and "HIL_PENDING" in block
                and "standard INSTRUMENT_TIMED m\u00f3dban sosem" not in block
            )

        # Loader source is supplied directly to the detector in memory-overlay runs.
        assert detector(module.__loader__.get_source(module.__name__))


class TestR16B24Fix1FloorCleanup:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("stage", ["drain", "restore"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    def test_supervisory_cleanup_keeps_latched_floor_and_interrupt(
        self, monkeypatch, fast, stage, error_type,
    ):
        from queue import Queue
        from threading import Event
        from types import SimpleNamespace

        from Prog.src.pulse_measurement_service import PulseDischargeError
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast)
        _b24_bound(monkeypatch, rig)
        with pytest.raises(PulseDischargeError, match="BATTERY_UNDERVOLTAGE"):
            rig.service._check_discharge_floor([13.2, 10.0, 13.2])
        primary = error_type("B24 fix1 supervisory cleanup interrupted")
        order, triggered, events = [], [], []
        rig.ctrl.on_event = events.append
        for device, name in ((rig.load, "input_off"), (rig.psu, "all_outputs_off")):
            original = getattr(device, name)

            def off(_original=original, _name=name):
                order.append(_name)
                return _original()

            monkeypatch.setattr(device, name, off)

        def close():
            order.append("close")
            if stage == "drain":
                triggered.append(stage)
                raise primary

        set_nplc = rig.dmm.set_nplc

        def restore(value):
            set_nplc(value)
            if stage == "restore":
                triggered.append(stage)
                raise primary

        rig.service._worker = SimpleNamespace(
            results=Queue(), cancel=Event(), close_after_safe_off=close,
            owner_release_confirmed=True, _thread=SimpleNamespace(is_alive=lambda: False),
        )
        monkeypatch.setattr(rig.dmm, "set_nplc", restore)
        if error_type is RuntimeError:
            rig.ctrl.emergency_stop("B24_FIX1_SUPERVISORY_FAULT")
        else:
            with pytest.raises(error_type) as caught:
                rig.ctrl.emergency_stop("B24_FIX1_SUPERVISORY_FAULT")
            assert caught.value is primary
        assert triggered == [stage]
        assert order == ["input_off", "all_outputs_off", "close"]
        assert rig.ctrl.fault_reason.startswith("B24_FIX1_SUPERVISORY_FAULT")
        assert "10.000000 V < 10.800000 V" in rig.ctrl.fault_reason
        floor = [e for e in events if e["event_code"] == "BATTERY_UNDERVOLTAGE"]
        assert len(floor) == 1 and floor[0]["severity"] == "CRITICAL"
        assert "10.000000 V < 10.800000 V" in floor[0]["event_message"]
        assert rig.service._worker is None and rig.dmm.nplc == 10.0
        assert rig.service.nplc_restore_failed is (stage == "restore")
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on


class TestR16B24Fix2OnReturnStop:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("kind", ["stop", "emergency"])
    def test_done_on_return_leaves_terminal_off_to_runner(self, monkeypatch, fast, kind):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _B21bRig.runner(monkeypatch, rig)
        ticks = _b24_bound(monkeypatch, rig)
        entered, block_entries, block_offs = [], [], []
        inside_block = False
        input_on, run_block = rig.load.input_on, rig.ctrl.run_pulse_block
        if kind == "stop":
            rig.temp.temperature_C = 45.0

        def on(**kwargs):
            input_on(**kwargs)
            entered.append(kind)
            if kind == "stop":
                rig.runner.request_stop()
            else:
                rig.runner.request_emergency_stop("B24_FIX2_ON_RETURN")

        def block(poll):
            nonlocal inside_block
            block_entries.append((rig.service._started, rig.service._done))
            inside_block = True
            try:
                return run_block(poll)
            finally:
                inside_block = False

        for device, name in ((rig.load, "input_off"), (rig.psu, "all_outputs_off")):
            original = getattr(device, name)

            def off(_original=original, _name=name):
                if inside_block:
                    block_offs.append(_name)
                return _original()

            monkeypatch.setattr(device, name, off)
        monkeypatch.setattr(rig.load, "input_on", on)
        monkeypatch.setattr(rig.ctrl, "run_pulse_block", block)
        result = rig.runner.run(rig.plan)
        assert entered == [kind] and ticks
        assert block_entries == [(True, True)]
        assert block_offs == []
        assert result.status == ("STOPPED" if kind == "stop" else "FAULT")
        assert result.total_discharge_ah == result.total_discharge_wh == 0.0
        assert result.warning_code_counts == ({"TEMPERATURE_OUT_OF_RANGE": 1} if kind == "stop" else {})
        assert rig.logger.log_sample.call_count == (1 if kind == "stop" else 0)
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert rig.service._worker is None and rig.dmm._measurement_owner is None
        assert rig.dmm.nplc == 10.0


class TestR16B26bPulseConfigReason:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("source", ["primary", "drain"])
    def test_reason_survives_pulse_cleanup(self, monkeypatch, fast, source):
        from Prog.src.pulse_measurement_service import PulseDischargeError

        ctrl, psu, load, _dmm, _temp, _clock = TestR16B15F2Cleanup._make(fast)
        ctrl.advance(0.0)
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        assert service is not None and load.input_commanded_on, "Q46 pulse activation trigger"
        trace, events = [], []
        load_off, psu_off = load.input_off, psu.all_outputs_off
        restore = service.restore_acquisition_nplc
        reason = "DISCHARGE_CONFIG_INVALID: GOV-029 config"

        def stop_load():
            trace.append("LOAD")
            assert len(trace) <= 12, "Q46 cleanup bound"
            load_off()

        def stop_psu():
            trace.append("PSU")
            assert len(trace) <= 12, "Q46 cleanup bound"
            psu_off()

        def restore_and_mark():
            trace.append("RESTORE")
            restore()
            if source == "drain":
                service._discharge_fault = PulseDischargeError(reason)

        def event(data):
            trace.append("EVENT")
            assert not load.input_commanded_on and not psu.output_commanded_on
            events.append(data)

        monkeypatch.setattr(load, "input_off", stop_load)
        monkeypatch.setattr(psu, "all_outputs_off", stop_psu)
        monkeypatch.setattr(service, "restore_acquisition_nplc", restore_and_mark)
        ctrl.on_event = event
        ctrl.emergency_stop(reason if source == "primary" else "TEST_PRIMARY")
        assert trace[:3] == ["LOAD", "PSU", "RESTORE"]
        assert reason in ctrl.fault_reason
        fault_events = [e for e in events if e["event_code"] == "DISCHARGE_CONFIG_INVALID"]
        assert len(fault_events) == 1, "Q46 config-fault event trigger"
        event = fault_events[0]
        assert event["severity"] == "CRITICAL" and reason in event["event_message"]
        assert ctrl.state == OcvSocState.FAULT
        before = (list(load.calls), list(psu.calls))
        ctrl.advance(0.1)
        assert (load.calls, psu.calls) == before
