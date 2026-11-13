"""
ChemIdCharacterizationController tesztek — SUP03-TEST-001/009.
Sorrend: PRECHECK → ROOM_TEMPERATURE_CONDITIONING (2h) → FULL_CHARGE →
CHARGE_TERMINATION_VERIFIED → RELAX_5H_FULL → C20_DISCHARGE → RELAX_5H_EMPTY →
EXPORT_GPCCHEM_PACKAGE → RECHARGE_REQUIRED.
"""
from __future__ import annotations

import pytest

from Prog.src.battery_profile import BatteryProfile
from Prog.src.charge_controller import ChargeState
from Prog.src.chemid_controller import (
    ChemIdConfig,
    ChemIdCharacterizationController,
    ChemIdState,
)
from Prog.src.gpcchem_export import GpcChemConfig
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode
from Prog.tests.mock_drivers.mock_dmm import MockDMM
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
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_psu import MockPSU


def _make_profile(capacity_Ah: float = 7.0, C10_capacity_Ah: float | None = None) -> BatteryProfile:
    return BatteryProfile(
        battery_name="TEST", manufacturer="TEST", model="TST7AH",
        nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=capacity_Ah,
        C10_capacity_Ah=C10_capacity_Ah,
    )


def make_controller(
    dmm_voltage_V: float = 13.0,
    capacity_Ah: float = 7.0,
    config: ChemIdConfig | None = None,
    export_output_dir=None,
    export_config: GpcChemConfig | None = None,
    source_session_reference: str = "",
    missing_identifier_fields: list[str] | None = None,
    charge_config=None,
    C10_capacity_Ah: float | None = None,
    temp_comp_mode: TempCompMode = TempCompMode.MONITOR_ONLY,
):
    psu = MockPSU()
    load = MockLoad(voltage_V=dmm_voltage_V)
    dmm_v = MockDMM(voltage_V=dmm_voltage_V)
    dmm_t = MockDMM(temperature_C=22.0)
    profile = _make_profile(capacity_Ah, C10_capacity_Ah=C10_capacity_Ah)
    safety = SafetyManager(
        profile=profile, psu_mode=PsuMode.INDEPENDENT, temp_comp_mode=temp_comp_mode,
    )
    cfg = config or ChemIdConfig(
        conditioning_required=True, conditioning_min_s=7200.0,
        relax_full_min_s=18000.0, relax_empty_min_s=18000.0, sample_interval_s=30.0,
    )
    ctrl = ChemIdCharacterizationController(
        psu, load, dmm_v, dmm_t, profile, safety, cfg,
        export_output_dir=export_output_dir,
        export_config=export_config,
        source_session_reference=source_session_reference,
        missing_identifier_fields=missing_identifier_fields,
        charge_config=charge_config,
    )
    return ctrl, psu, load, dmm_v, dmm_t


class TestSup03ChemIdStateSequence:
    """SUP03-TEST-001: a teljes állapotsorrend igazolása."""

    def test_starts_in_init_then_precheck(self):
        ctrl, *_ = make_controller()
        assert ctrl.state == ChemIdState.INIT
        ctrl.advance(0.0)
        assert ctrl.state == ChemIdState.PRECHECK

    def test_precheck_to_conditioning_when_required(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        assert ctrl.state == ChemIdState.ROOM_TEMPERATURE_CONDITIONING

    def test_u_batt_v_reflects_dmm_reading_after_full_charge_tick(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P1 lelet] u_batt_V property hiánya miatt test_runner.py::
        _build_sample() getattr(controller, "u_batt_V", None)-je mindig
        None-t adott vissza, ezért InstrumentManager.read_all() MÁSODSZOR
        is lekérdezte ugyanazt a fizikai feszültség-DMM-et minden ticken
        (CLAUDE.md §9 "egy DMM-et egy tickben egy komponens olvasson")."""
        cfg = ChemIdConfig(conditioning_required=False)
        ctrl, *_ = make_controller(config=cfg, dmm_voltage_V=12.34)
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> FULL_CHARGE (belső ChargeController létrejön)
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        ctrl.advance(1.0)  # belső ChargeController._advance_single() olvassa a DMM-et
        assert ctrl.u_batt_V == pytest.approx(12.34)

    def test_battery_temperature_c_reflects_dmm_reading_after_full_charge_tick(self):
        """L. test_u_batt_v_reflects_dmm_reading_after_full_charge_tick
        docstringje ugyanerről a hiányzó mintáról — a hőmérséklet-DMM-re."""
        cfg = ChemIdConfig(conditioning_required=False)
        ctrl, *_ = make_controller(config=cfg, dmm_voltage_V=12.34)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.battery_temperature_C == pytest.approx(22.0)

    def test_dmm_valid_property_reflects_actual_dmm_read_result(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P2 lelet #4] a ChargeController már exponálja a dmm_valid
        property-t, ezt test_runner.py::_build_sample() a dmm_voltage_valid
        mezőhöz olvassa — a ChemIdCharacterizationController hiányzott,
        emiatt getattr(controller, "dmm_valid", None) mindig None-t adott,
        és a fallback (`u_batt is not None`) gyakorlatilag MINDIG True-t
        írt, akkor is, ha a DMM-olvasás ténylegesen hibázott."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_controller(dmm_voltage_V=12.34)
        ctrl.advance(0.0)  # INIT -> PRECHECK, DMM-olvasás
        assert ctrl.dmm_valid is True

        dmm_v.dmm_valid = False
        ctrl.advance(1.0)  # a következő DMM-olvasás hibázik
        assert ctrl.dmm_valid is False

    def test_precheck_skips_conditioning_when_not_required(self):
        cfg = ChemIdConfig(conditioning_required=False)
        ctrl, *_ = make_controller(config=cfg)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD

    def test_full_charge_uses_injected_charge_config_not_bare_default(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P1 lelet] A beágyazott ChargeController korábban mindig
        bare ChargeConfig()-ot kapott FULL_CHARGE_MANUFACTURER_METHOD alatt,
        figyelmen kívül hagyva a hívó (GUI/session) által átadott
        charge_config-ot — ellentétben a TraditionalLearningController/
        GoldenGgController-rel."""
        from Prog.src.charge_controller import ChargeConfig

        injected_cfg = ChargeConfig(fast_loop_enabled=True, charge_current_A_override=1.23)
        cfg = ChemIdConfig(conditioning_required=False)
        ctrl, *_ = make_controller(config=cfg, charge_config=injected_cfg)

        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> FULL_CHARGE_MANUFACTURER_METHOD

        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        assert ctrl._charge_ctrl is not None
        assert ctrl._charge_ctrl._config is injected_cfg
        assert ctrl._charge_ctrl._config.fast_loop_enabled is True

    def test_temp_dmm_valid_true_delegated_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a3f0e6dcf93588360) P2-1]
        a self._dmm_valid (feszültség) delegálás a beágyazott
        ChargeControllerből mindig megtörtént FULL_CHARGE_MANUFACTURER_
        METHOD alatt, de a self._temp_dmm_valid nem -- egy ott ténylegesen
        hibázó hőmérséklet-DMM a szülő szintjén hamisan True-t jelentett
        volna, mert a saját _read_dmm() ebben az állapotban ki van
        kapcsolva."""
        cfg = ChemIdConfig(conditioning_required=False)
        ctrl, *_ = make_controller(config=cfg)
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> FULL_CHARGE_MANUFACTURER_METHOD
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD

        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_delegated_from_embedded_charge_controller(self):
        cfg = ChemIdConfig(conditioning_required=False)
        ctrl, _psu, _load, _dmm_v, dmm_t = make_controller(config=cfg)
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> FULL_CHARGE_MANUFACTURER_METHOD
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_fault_ticks_live_add_and_carry_over_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a306e7e764e3a717b) P2-1,
        DFAM-001 további előfordulása] a temp_dmm_fault_ticks property
        korábban csak a saját szintű számlálót adta vissza -- a beágyazott
        ChargeController FULL_CHARGE_MANUFACTURER_METHOD alatti hibás
        tickjei nyomtalanul elvesztek volna, ellentétben a testvér
        temperature_out_of_range_count property már bevált live-add +
        carry-over mintájával."""
        cfg = ChemIdConfig(conditioning_required=False)
        ctrl, _psu, _load, _dmm_v, dmm_t = make_controller(config=cfg)
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> FULL_CHARGE_MANUFACTURER_METHOD
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_fault_ticks == 2  # élő beágyazott számláló

        from Prog.src.charge_controller import ChargeState
        dmm_t.simulate_temp_failure = False
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)  # FULL_CHARGE_MANUFACTURER_METHOD -> CHARGE_TERMINATION_VERIFIED
        assert ctrl._charge_ctrl is None
        assert ctrl.temp_dmm_fault_ticks == 2  # carry-over, a gyerek eldobása után is megmarad

    def test_charge_config_property_reflects_injected_config(self):
        """Teljes kódbázis review 16. kör (2026-09-04), Session/Workflow
        P2 lelet: a testvér GoldenGgController/TraditionalLearningController
        charge_config property-jével azonos, korábban hiányzó public API."""
        from Prog.src.charge_controller import ChargeConfig

        injected_cfg = ChargeConfig(charge_current_A_override=0.8)
        ctrl, *_ = make_controller(charge_config=injected_cfg)
        assert ctrl.charge_config is injected_cfg
        assert ctrl.charge_config.charge_current_A_override == pytest.approx(0.8)

    def test_charge_config_property_reflects_default_when_not_injected(self):
        ctrl, *_ = make_controller()
        assert ctrl.charge_config.charge_current_A_override == 0.0

    def test_conditioning_2h_transitions_to_full_charge(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(7200.0)  # 2h
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD

    def test_full_charge_done_transitions_to_termination_verified(self):
        ctrl, psu, load, dmm_v, dmm_t = make_controller(dmm_voltage_V=14.4)
        ctrl.advance(0.0)      # INIT -> PRECHECK
        ctrl.advance(0.0)      # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(7200.1)   # conditioning kész -> FULL_CHARGE
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD

        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.CHARGE_TERMINATION_VERIFIED

    def test_charge_ctrl_reference_cleared_after_charge_done(self):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31),
        Metrológia/Adatintegritás P2 lelet] a beágyazott ChargeController
        referenciáját CHARGE_DONE után NEM dobta el a controller (ellentétben
        golden_gg_controller.py/traditional_learning_controller.py mintájával)
        — a proxy-property-k (u_psu_set_V/taper_timer_s/last_integration_
        source) emiatt a PRECHARGE utolsó tickjén befagyott, elévült értéket
        adtak volna vissza a session hátralévő részében (RELAX_5H_FULL/
        C20_DISCHARGE/RELAX_5H_EMPTY alatt), holott a PSU ténylegesen ki
        van kapcsolva."""
        ctrl, *_ = make_controller(dmm_voltage_V=14.4)
        ctrl.advance(0.0)      # INIT -> PRECHECK
        ctrl.advance(0.0)      # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(7200.1)   # conditioning kész -> FULL_CHARGE
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD

        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)

        assert ctrl._charge_ctrl is None
        assert ctrl.u_psu_set_V == 0.0
        assert ctrl.i_psu_set_A == 0.0
        assert ctrl.taper_timer_s == 0.0
        assert ctrl.last_integration_source == "ZERO"

    def _advance_to_relax_full(self, dmm_voltage_V: float = 13.0, C10_capacity_Ah: float | None = None):
        ctrl, psu, load, dmm_v, dmm_t = make_controller(
            dmm_voltage_V=dmm_voltage_V, C10_capacity_Ah=C10_capacity_Ah,
        )
        ctrl.advance(0.0)      # INIT -> PRECHECK
        ctrl.advance(0.0)      # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(7200.1)   # conditioning kész -> FULL_CHARGE
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)  # -> CHARGE_TERMINATION_VERIFIED
        ctrl.advance(1.0)  # -> RELAX_5H_FULL
        return ctrl, psu, load, dmm_v, dmm_t

    def test_charge_termination_verified_transitions_to_relax_full(self):
        ctrl, *_ = self._advance_to_relax_full()
        assert ctrl.state == ChemIdState.RELAX_5H_FULL

    def test_relax_5h_full_transitions_to_c20_discharge(self):
        ctrl, psu, load, *_ = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # 5h
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        assert load.input_commanded_on

    def test_relax_extreme_dt_s_reflected_in_session_property(self):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31),
        Metrológia/Adatintegritás P2 lelet] a relax-tick-ek dt_s-e korábban
        sosem jutott el a session-szintű extreme_dt_s_count/max_observed_
        dt_s property-ig — egy extrém dt_s-tüske RELAX_5H_FULL/RELAX_5H_
        EMPTY alatt (a leghosszabb fázisok egyike) csendben elveszett
        volna a capacity_result_quality kapu elől."""
        ctrl, *_ = self._advance_to_relax_full()
        assert ctrl.extreme_dt_s_count == 0

        ctrl.advance(301.0)  # extrém dt_s (>300s küszöb) relax alatt

        assert ctrl.extreme_dt_s_count == 1
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)

    def test_c20_discharge_current_is_capacity_over_20(self):
        ctrl, *_ = self._advance_to_relax_full()
        ctrl.advance(18000.0)
        assert ctrl.discharge_current_A == pytest.approx(7.0 / 20)

    def test_accumulated_charge_ah_nonzero_during_full_charge(self):
        """Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: az
        accumulated_charge_Ah property hiánya miatt a TestRunner.
        _flush_totals() mindig 0 deltát látott a FULL_CHARGE fázis alatt.

        [frissítve, 5. kör (2026-08-29), Metrológia/Adatintegritás P1 lelet]
        a beágyazott ChargeController mostantól a SAJÁT, fault-kezelt
        i_psu_measured_A property-jét adja a session-integrátornak — ez
        csak akkor nem-nulla, ha a beágyazott ChargeController TÉNYLEGESEN
        eljut CHARGE_CC-be (INIT→PRECHECK→PSU_PRESET→CHARGE_CC, 3 saját
        advance()-tick, l. test_charge_controller.py::_advance_to_cc())."""
        ctrl, psu, _load, _dmm_v, _dmm_t = make_controller(dmm_voltage_V=13.0)
        ctrl.advance(0.0)      # INIT -> PRECHECK
        ctrl.advance(0.0)      # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(7200.1)   # conditioning kész -> FULL_CHARGE (belső ChargeController létrejön, INIT)
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        psu.current_A = 1.75
        ctrl.advance(1.0)  # belső: INIT → PRECHECK
        ctrl.advance(1.0)  # belső: PRECHECK → PSU_PRESET
        ctrl.advance(1.0)  # belső: PSU_PRESET → CHARGE_CC
        ctrl.advance(1.0)  # belső: CHARGE_CC tick — ITT olvasódik a PSU-áram
        assert ctrl.accumulated_charge_Ah > 0.0
        assert ctrl.accumulated_discharge_Ah == 0.0

    def test_accumulated_discharge_ah_nonzero_during_c20_discharge(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE (állapotváltás)
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        ctrl.advance(1.0)  # ez a tick futtatja ténylegesen _run_c20_discharge()-t
        assert ctrl.accumulated_discharge_Ah > 0.0
        assert ctrl.accumulated_charge_Ah == 0.0

    def test_relaxed_full_voltage_v_set_at_relax_5h_full_end(self):
        """[regresszió, Kör 17 MAX-effort review, R17-24a, DFAM-021+
        DFAM-026] a RELAX_5H_FULL fázis végi feszültsége korábban
        kiszámolódott (self._relaxed_full_voltage_V), de sosem lett
        kiolvasva -- most a relaxed_full_voltage_V/relaxed_full_voltage_
        valid property teszi elérhetővé."""
        ctrl, _psu, _load, dmm_v, _dmm_t = self._advance_to_relax_full(dmm_voltage_V=13.0)
        assert ctrl.relaxed_full_voltage_V is None
        assert ctrl.relaxed_full_voltage_valid is False

        dmm_v.voltage_V = 12.85
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        assert ctrl.relaxed_full_voltage_V == pytest.approx(12.85)
        assert ctrl.relaxed_full_voltage_valid is True

    def test_relaxed_full_voltage_valid_false_on_dmm_failure_at_relax_end_and_faults_before_c20_energize(self):
        """[regresszió, Kör 17 MAX-effort review, R17-24a] ha a feszültség-
        DMM éppen a RELAX_5H_FULL vég tickjén hibázik, a relaxed_full_
        voltage_V a KORÁBBI (sticky-last-good) értéket hordozza tovább
        -- NEM egy fabrikált 0.0-t (ugyanaz az elv, mint R17-11a) --, DE
        a valid flag ezt False-ra jelzi, hogy a fogyasztó ne tekintse
        friss mérésnek.

        [elvárás-változás, Claude-Codex loop R1 B3, F-02, DFAM-001] a
        korábbi elvárás (C20_DISCHARGE állapot) ismerten hibás DMM-
        visszacsatolás mellett Load INPUT ON-t fogadott el. Safety-
        szigorítás (CLAUDE.md §6, .claude/rules/10-safety.md 4.): a
        `_begin_c20_discharge()` DMM-őre ilyenkor FAULT-ot ad energizálás
        nélkül. A sticky feszültség és a valid=False ellenőrzése megmarad."""
        ctrl, psu, load, dmm_v, _dmm_t = self._advance_to_relax_full(dmm_voltage_V=13.0)
        dmm_v.voltage_V = 12.9
        ctrl.advance(1.0)  # egy korabbi, sikeres relax-tick -- self._u_batt frissul
        last_good = ctrl.u_batt_V
        assert last_good == pytest.approx(12.9)

        load.reset_calls()
        psu.reset_calls()
        dmm_v.simulate_timeout = True
        ctrl.advance(17999.0)  # RELAX_5H_FULL vege, DE a DMM EZEN a tickken hibazik
        assert ctrl.state == ChemIdState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert load.input_commanded_on is False
        assert "input_on()" not in load.calls
        assert "all_outputs_off()" in psu.calls
        assert ctrl.relaxed_full_voltage_V == pytest.approx(last_good)
        assert ctrl.relaxed_full_voltage_valid is False

        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FAULT
        assert "input_on()" not in load.calls

    def test_accumulated_ah_zero_before_any_advance(self):
        ctrl, *_ = make_controller()
        assert ctrl.accumulated_charge_Ah == 0.0
        assert ctrl.accumulated_discharge_Ah == 0.0

    def test_accumulated_charge_wh_nonzero_during_full_charge(self):
        """[frissítve, Teljes kódbázis review 5. kör (2026-08-29), Metrológia/
        Adatintegritás P1 lelet] l. test_accumulated_charge_ah_nonzero_
        during_full_charge docstringje ugyanerről a mintáról — a beágyazott
        ChargeController-nek TÉNYLEGESEN CHARGE_CC-be kell jutnia."""
        ctrl, psu, _load, _dmm_v, _dmm_t = make_controller(dmm_voltage_V=13.0)
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        ctrl.advance(7200.1)
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        psu.current_A = 1.75
        ctrl.advance(1.0)  # belső: INIT → PRECHECK
        ctrl.advance(1.0)  # belső: PRECHECK → PSU_PRESET
        ctrl.advance(1.0)  # belső: PSU_PRESET → CHARGE_CC
        ctrl.advance(1.0)  # belső: CHARGE_CC tick — ITT olvasódik a PSU-áram
        assert ctrl.accumulated_charge_Wh > 0.0
        assert ctrl.accumulated_discharge_Wh == 0.0

    def test_accumulated_discharge_wh_nonzero_during_c20_discharge(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        ctrl.advance(1.0)
        assert ctrl.accumulated_discharge_Wh > 0.0
        assert ctrl.accumulated_charge_Wh == 0.0

    def test_u_psu_set_v_and_i_load_set_a_reflect_active_phase(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P2] a
        ténylegesen parancsolt PSU-feszültség/Load-áram setpoint korábban
        nem volt elérhető ChemIdCharacterizationController-en."""
        ctrl, _psu, _load, _dmm_v, _dmm_t = make_controller(dmm_voltage_V=13.0)
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        ctrl.advance(7200.1)
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        for _ in range(5):
            ctrl.advance(1.0)
            if ctrl.u_psu_set_V > 0.0:
                break
        assert ctrl.u_psu_set_V > 0.0
        assert ctrl.i_load_set_A == 0.0  # nem kisütés alatt vagyunk

    def test_reverse_polarity_during_c20_discharge_faults_not_relax(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív C20
        kisütés alatt egy fizikailag ésszerűtlen (<=0V) DMM-olvasás korábban
        csendben "elértük a végfeszültséget"-ként RELAX_5H_EMPTY-t okozott
        volna, NEM FAULT-ot (CLAUDE.md §10.6/SAFE-009)."""
        ctrl, _psu, _load, dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE (állapotváltás)
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        dmm_v.voltage_V = 0.0
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FAULT
        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"

    def test_concurrent_psu_during_c20_discharge_faults(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív C20
        kisütés alatt egy PSU ON hardveres konfliktust korábban a beágyazott
        ág sosem ellenőrizte (csak a fázis indításakor volt PSU OFF-ra
        kényszerítve) — CLAUDE.md §10.2 defense-in-depth hiány."""
        ctrl, psu, _load, _dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE (állapotváltás)
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        psu.output_commanded_on = True
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason

    def test_c20_discharge_faults_on_load_power_limit_exceeded(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P1 lelet] A standalone DischargeController._run_cc() minden
        ticken ellenőrzi a Keithley 2380 250W hardver-teljesítménykorlátot —
        a beágyazott _run_c20_discharge() korábban nem."""
        ctrl, _psu, load, _dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE (állapotváltás)
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        load.current_A = 25.0  # 13V * 25A = 325W > 250W max
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FAULT
        assert "LOAD_POWER_LIMIT" in ctrl.fault_reason

    def test_c20_discharge_current_far_below_setpoint_gives_warning_not_fault(self):
        """[regresszió, Hardver/Műszervezérlés HW4-02 (review-kör 4, P1,
        DFAM-001)] l. discharge_controller.py testvér tesztjének
        docstringje ugyanerről a hiányzó setpoint-vs-mért kapuról."""
        ctrl, _psu, load, dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE (állapotváltás)
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        dmm_v.voltage_V = 13.0
        load.current_A = ctrl._discharge_current_A * 0.1  # a setpoint töredéke -- "ragadt"
        ctrl.advance(1.0)
        assert ctrl.state != ChemIdState.FAULT
        assert ctrl.last_warning_code == "LOAD_CURRENT_SETPOINT_NOT_REACHED"

    def test_c20_discharge_setpoint_warning_tick_counter_survives_higher_priority_warning(self):
        """[regresszió, MAX verifier (review-kör 4, HW4-02, 329c1a3) N1
        lelete] l. discharge_controller.py testvér tesztjének docstringje
        ugyanerről a hiányzó, a _last_warning_code slot-tól független
        tick-számlálóról."""
        ctrl, _psu, load, dmm_v, dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        dmm_v.voltage_V = 13.0
        dmm_t.temperature_C = 40.0  # profil névleges tartományán (15-30C) kívül -- MINDEN tickben aktív
        load.current_A = ctrl._discharge_current_A * 0.1  # a setpoint töredéke -- "ragadt"
        assert ctrl.load_current_setpoint_warning_ticks == 0
        ctrl.advance(1.0)
        assert ctrl.last_warning_code == "TEMPERATURE_OUT_OF_RANGE"
        assert ctrl.load_current_setpoint_warning_ticks == 1

    def test_c20_discharge_faults_on_max_discharge_time_reached(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P2 lelet] a beágyazott C20_DISCHARGE fázis eddig
        KIZÁRÓLAG a feszültség-alapú leállási feltételre támaszkodott —
        egy sosem-csökkenő (pl. hibás/rögzült) DMM-olvasás mellett a
        kisütés időben korlátlanul futhatott volna."""
        ctrl, _psu, _load, dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE (állapotváltás)
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        dmm_v.voltage_V = 13.0  # sosem éri el a terminate_voltage_pack_V-t
        ctrl.advance(86400.1)
        assert ctrl.state == ChemIdState.FAULT
        assert "MAX_DISCHARGE_TIME_REACHED" in ctrl.fault_reason

    def test_c20_discharge_faults_on_max_discharge_ah_reached(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P2 lelet] ugyanaz a hiányzó backstop az Ah-oldalon."""
        ctrl, _psu, load, dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # -> C20_DISCHARGE
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        dmm_v.voltage_V = 13.0
        load.current_A = 10.0  # nagy áram -> gyorsan túllépi a névleges kapacitás 110%-át
        ctrl.advance(3600.0)
        assert ctrl.state == ChemIdState.FAULT
        assert "MAX_DISCHARGE_AH_REACHED" in ctrl.fault_reason

    def test_discharge_current_a_uses_c10_capacity_when_explicitly_set(self):
        """[regresszió, Governance-v2 review-kör 1 (CF-16/MET-R1-04+16+18,
        DFAM-001 6. előfordulás + DFAM-017, batch 4.4)] a kezdeti
        _discharge_current_A setpoint korábban nyers nominal_capacity_Ah-t
        osztott (_CHEMID_DISCHARGE_RATE_DIVISOR=20), NEM a kanonikus
        _c10_base_Ah-t — ugyanaz a hibaosztály, mint az ocv_soc_controller.py
        testvér property-jénél (8. kör)."""
        ctrl, *_ = make_controller(capacity_Ah=100.0, C10_capacity_Ah=80.0)
        assert ctrl._discharge_current_A == pytest.approx(4.0)  # 80/20, NEM 100/20=5.0

    def test_max_discharge_ah_reached_uses_c10_capacity_threshold(self):
        """[regresszió, Governance-v2 review-kör 1 (CF-16/MET-R1-04+16+18,
        DFAM-001 6. előfordulás + DFAM-017, batch 4.4)] ugyanaz a hibaosztály,
        amit a 13. kör (`682d97e`) már javított — a
        védelmi plafon nyers nominal_capacity_Ah-t (FG-sorozatnál C20-alapú)
        használt, ellentétes bázissal, mint a testvér discharge_controller.py/
        golden_gg_controller.py/traditional_learning_controller.py. Egy
        explicit, a nominal_capacity_Ah-nál ALACSONYABB C10_capacity_Ah
        esetén a fixnek KORÁBBAN kell megállítania a kisütést (a szigorúbb
        C10-alapú plafonnál), mint amit egy nominal-alapú plafon tenne."""
        ctrl, _psu, load, dmm_v, _dmm_t = self._advance_to_relax_full(
            C10_capacity_Ah=1.0,  # jóval a 7.0 Ah nominal alatt
        )
        ctrl.advance(18000.0)  # -> C20_DISCHARGE
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        dmm_v.voltage_V = 13.0  # sosem éri el a terminate_voltage_pack_V-t
        # C10-alapú plafon (max_discharge_Ah_factor=1.10 alapérték): 1.0*1.10=1.1 Ah.
        # nominal-alapú (HIBÁS) plafon lenne: 7.0*1.10=7.7 Ah. 2.0 Ah felhalmozás
        # a C10-alapút túllépi, a nominal-alapút NEM -- ez a diszkrimináló eset.
        load.current_A = 2.0
        ctrl.advance(3600.0)
        assert ctrl.state == ChemIdState.FAULT
        assert "MAX_DISCHARGE_AH_REACHED" in ctrl.fault_reason

    def test_c20_discharge_max_time_respects_safety_manager_override(self):
        """[regresszió, Teljes kódbázis review 8. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] max_discharge_time_s korábban egy privát,
        hardkódolt modul-konstansként élt itt — egy SafetyManager-en
        beállított egyedi érték NEM ért volna el ide. Mostantól
        self._safety.max_discharge_time_s a kanonikus forrás, a standalone
        DischargeController mintáját követve (5-8. kör konszolidáció)."""
        ctrl, _psu, _load, dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE (állapotváltás)
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        dmm_v.voltage_V = 13.0  # sosem éri el a terminate_voltage_pack_V-t
        ctrl._safety.max_discharge_time_s = 5.0  # << a 86400s alapértéktől

        ctrl.advance(10.0)  # a szűkített limit fölött, de az alapérték alatt

        assert ctrl.state == ChemIdState.FAULT
        assert "MAX_DISCHARGE_TIME_REACHED" in ctrl.fault_reason

    def test_discharge_to_manufacturer_minimum_transitions_to_relax_empty(self):
        ctrl, psu, load, dmm_v, dmm_t = self._advance_to_relax_full(dmm_voltage_V=13.0)
        ctrl.advance(18000.0)  # -> C20_DISCHARGE
        dmm_v.voltage_V = 10.5  # terminate_voltage_pack_V = 6*1.8 = 10.8V alatt
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.RELAX_5H_EMPTY
        assert not load.input_commanded_on

    def test_relax_5h_empty_transitions_to_export(self):
        ctrl, psu, load, dmm_v, dmm_t = self._advance_to_relax_full(dmm_voltage_V=13.0)
        ctrl.advance(18000.0)
        dmm_v.voltage_V = 10.5
        ctrl.advance(1.0)  # -> RELAX_5H_EMPTY
        ctrl.advance(18000.0)  # 5h
        assert ctrl.state == ChemIdState.EXPORT_GPCCHEM_PACKAGE

    def test_export_transitions_to_recharge_required(self):
        ctrl, psu, load, dmm_v, dmm_t = self._advance_to_relax_full(dmm_voltage_V=13.0)
        ctrl.advance(18000.0)
        dmm_v.voltage_V = 10.5
        ctrl.advance(1.0)
        ctrl.advance(18000.0)
        ctrl.advance(0.0)  # EXPORT -> RECHARGE_REQUIRED
        assert ctrl.state == ChemIdState.RECHARGE_REQUIRED
        assert ctrl.recharge_required is True

    def test_recharge_required_is_terminal(self):
        ctrl, *_ = make_controller()
        ctrl._state = ChemIdState.RECHARGE_REQUIRED
        state = ctrl.advance(1.0)
        assert state == ChemIdState.RECHARGE_REQUIRED


# ------------------------------------------------------------------ #
# TestChemIdPhaseElapsedS                                              #
# [CF-19, governance-v2 kör 2, DFAM-023] l. test_ocv_soc_controller.py #
# azonos hivatkozású teszt-osztálya.                                   #
# ------------------------------------------------------------------ #

class TestChemIdPhaseElapsedS:
    def test_starts_at_zero(self):
        ctrl, *_ = make_controller()
        assert ctrl.phase_elapsed_s == 0.0

    def test_grows_while_state_unchanged(self):
        # PRECHECK egyetlen tick alatt továbblép (nincs idő-alapú kapuja),
        # ezért a "változatlan állapotban nő" esetet a rá következő
        # ROOM_TEMPERATURE_CONDITIONING (conditioning_min_s=7200s alapból)
        # fázison keresztül teszteljük -- az bőven kitart 8 másodpercig.
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        assert ctrl.state == ChemIdState.ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(5.0)
        ctrl.advance(3.0)
        assert ctrl.state == ChemIdState.ROOM_TEMPERATURE_CONDITIONING
        assert ctrl.phase_elapsed_s == pytest.approx(8.0)

    def test_resets_on_state_transition(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(100.0)
        assert ctrl.phase_elapsed_s == pytest.approx(100.0)
        ctrl.advance(7200.0)  # elér/túllépi a conditioning_min_s-t
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        assert ctrl.phase_elapsed_s == pytest.approx(0.0)


class TestSup03ChemIdSampleCollection:
    def test_no_samples_before_relax_full(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.samples == []

    def test_samples_collected_during_relax_and_discharge(self):
        ctrl, psu, load, dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        ctrl.advance(30.0)
        assert len(ctrl.samples) >= 1

    def test_discharge_current_negative_in_samples(self):
        """SUP03-CHEMID-019: kisütési áram előjele NEGATÍV."""
        ctrl, psu, load, dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        ctrl.advance(18000.0)  # -> C20_DISCHARGE
        load.current_A = 0.35
        ctrl.advance(1.0)
        assert ctrl.samples[-1].current_A < 0

    def test_relax_current_is_zero_in_samples(self):
        ctrl, psu, load, dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        ctrl.advance(30.0)
        assert ctrl.samples[-1].current_A == pytest.approx(0.0)

    def test_log_elapsed_continuous_across_phases(self):
        """A napló egyetlen folytonos idővonal RELAX_5H_FULL kezdetétől."""
        ctrl, psu, load, dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        ctrl.advance(100.0)
        elapsed_before_discharge = ctrl.samples[-1].elapsed_s
        ctrl.advance(17900.0)  # relax vége (18000 összesen)
        ctrl.advance(1.0)  # C20 discharge tick
        assert ctrl.samples[-1].elapsed_s > elapsed_before_discharge


class TestSup03ChemIdSafety:
    def test_precheck_faults_on_dmm_invalid(self):
        ctrl, psu, load, dmm_v, dmm_t = make_controller()
        dmm_v.set_dmm_invalid()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"

    def test_begin_c20_discharge_faults_on_concurrent_psu_load_before_energizing(self):
        """[Kör 20 Batch E13a, F20-S3, DFAM-001] `_begin_c20_discharge()`
        korábban a `try:` blokk BELSEJÉBEN, feltétel nélkül futtatta
        `psu.all_outputs_off()`-t, majd `load.input_on()`-t -- egy stale
        `psu.output_commanded_on=True` flaget az `all_outputs_off()`
        CSENDBEN letörölt volna, MIELŐTT bármi észlelhetné a konfliktust.
        Ez a teszt a fix ELŐTTI kódon bukna: `ctrl.state` NEM lenne FAULT,
        és `load.input_commanded_on` True lenne."""
        ctrl, psu, load, _dmm_v, _dmm_t = make_controller()
        psu.output_commanded_on = True

        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_c20_discharge()

        assert ctrl.state == ChemIdState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason
        assert load.input_commanded_on is False

    def test_begin_c20_discharge_dmm_invalid_faults_before_energizing(self):
        """[regresszió, Claude-Codex loop R1 B3, F-02, DFAM-001] a
        `_begin_c20_discharge()` korábban ismerten hibás DMM-olvasás
        mellett is Load INPUT ON-t adott -- a per-tick DMM-kapu csak a
        KÖVETKEZŐ ticken futott."""
        ctrl, psu, load, _dmm_v, _dmm_t = make_controller()
        ctrl._dmm_valid = False

        ctrl._begin_c20_discharge()

        assert ctrl.state == ChemIdState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls

    def test_c20_discharge_faults_on_load_comm_lost(self):
        ctrl, psu, load, dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        ctrl.advance(18000.0)
        load.simulate_current_readback_failure = True
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FAULT
        assert ctrl.fault_reason == "LOAD_COMM_LOST"

    def test_emergency_stop_turns_off_load_and_psu(self):
        ctrl, psu, load, *_ = make_controller()
        load.input_on()
        ctrl.emergency_stop("TEST")
        assert not load.input_commanded_on
        assert not psu.output_commanded_on
        assert ctrl.state == ChemIdState.FAULT

    def test_dmm_dropout_during_relax_flags_quality(self):
        ctrl, psu, load, dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        dmm_v.set_dmm_invalid()
        ctrl.advance(30.0)
        assert ctrl._dmm_fault_during_relax is True

    def test_temp_dmm_failure_during_relax_faults(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P1] a
        _temp_dmm_fault_s számláló _read_dmm()-ben MÁR nőtt, de sosem került
        kiértékelésre advance()-ben — egy hőmérséklet-DMM kiesés a több órás
        relax/kisütés alatt teljesen észrevétlen maradt volna (CLAUDE.md
        §10.4). Ugyanaz a minta, mint golden_gg_controller.py-ben."""
        ctrl, _psu, _load, _dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        dmm_t.simulate_temp_failure = True
        ctrl.advance(70.0)  # MONITOR_ONLY timeout=60s, egy tick alatt túllépve

        assert ctrl.state == ChemIdState.FAULT
        assert "TEMPERATURE" in ctrl.fault_reason

    def test_temp_dmm_warning_during_relax_sets_last_warning_code(self):
        """[regresszió, Teljes kódbázis review 9. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] last_warning_code korábban teljesen hiányzott
        ChemIdCharacterizationController-en — a MONITOR_ONLY grace-ablakban
        (fault előtt) a hőmérséklet-DMM figyelmeztetés sehol nem jelent meg,
        test_runner.py warning_flags mezője mindig None maradt."""
        ctrl, _psu, _load, _dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 10s < 60s timeout, WARNING, nem FAULT

        assert ctrl.state != ChemIdState.FAULT
        assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"

    def test_temp_dmm_fault_ticks_counts_failed_reads_not_seconds(self):
        """[regresszió, review-kör 2 (2026-09-09), friss Metrológia/
        Adatintegritás NEW-2 (DFAM-024 2. előfordulás + DFAM-021)] A
        _battery_temperature_C egy sikertelen hőmérséklet-DMM-olvasásnál
        CSENDBEN a korábbi értéken marad, és ez quality=OK-ként kerül
        exportra (roomtemp_rel_dis_rel.csv) -- a temp_dmm_fault_ticks
        számláló az EGYETLEN jelző, ami a session_meta-n át eljut a
        report.json-ig, hogy ilyen kiesés történt. Tick-alapú (nem
        másodperc-alapú, szemben a _temp_dmm_fault_s-sel) -- minden
        sikertelen olvasási TICK egyet számol, függetlenül a dt_s
        hosszától."""
        ctrl, _psu, _load, _dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        assert ctrl.temp_dmm_fault_ticks == 0

        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 1 sikertelen tick, WARNING-küszöb alatt
        assert ctrl.temp_dmm_fault_ticks == 1

        ctrl.advance(5.0)  # 2. sikertelen tick, MÉG mindig WARNING alatt
        assert ctrl.temp_dmm_fault_ticks == 2

        dmm_t.simulate_temp_failure = False
        ctrl.advance(1.0)  # sikeres olvasás -- a tick-szamlalo NEM nullazodik
        assert ctrl.temp_dmm_fault_ticks == 2

    def test_temp_dmm_valid_true_after_successful_read(self):
        """[regresszió, MAX verifier (review-kör 3, a9cb161469c468fc8) P1-1,
        BLOCKING] a temp_dmm_valid property (Metrológia M3-01, P1) mezőt a
        commit hozzáadta, de a _read_dmm()-be sosem lett bekötve --
        test_runner.py mostantól ezt a property-t preferálja a
        dmm_temperature_valid mezőhöz, ezért egy CHEMID_CHARACTERIZATION_PBA
        session samples.csv-je egy TELJESEN halott hőmérséklet-DMM mellett
        is True-t jelentett volna. Az egyetlen ok, amiért ez 1792 zöld
        teszt mellett észrevétlen maradt: egyik controlleren sem volt
        önálló, controller-szintű regressziós teszt erre a property-re
        (csak egy kézzel írt test_runner.py stub fedte a mechanizmust,
        nem a valódi wiring-et) -- ez a teszt-osztály pótolja ezt
        mind a 8 testvér-controlleren."""
        ctrl, _psu, _load, _dmm_v, _dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_after_failed_read(self):
        ctrl, _psu, _load, _dmm_v, dmm_t = TestSup03ChemIdStateSequence()._advance_to_relax_full(
            dmm_voltage_V=13.0
        )
        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)
        assert ctrl.temp_dmm_valid is False


# ------------------------------------------------------------------ #
# TestWpAk05GpcChemExportWiring                                       #
# SUP03-CHEMID-019 "Kotelezo export": ha a hívó megadja az export     #
# célkönyvtárat/konfigurációt, a csomag automatikusan létrejön.       #
# ------------------------------------------------------------------ #

def _advance_to_export_ready(
    export_output_dir=None, export_config: GpcChemConfig | None = None,
    source_session_reference: str = "",
    missing_identifier_fields: list[str] | None = None,
    temp_comp_mode: TempCompMode = TempCompMode.MONITOR_ONLY,
    dmm_t_simulate_temp_failure: bool = False,
):
    """INIT -> ... -> EXPORT_GPCCHEM_PACKAGE (a legutolsó advance() előtt)."""
    ctrl, psu, load, dmm_v, dmm_t = make_controller(
        dmm_voltage_V=13.0,
        export_output_dir=export_output_dir,
        export_config=export_config,
        source_session_reference=source_session_reference,
        missing_identifier_fields=missing_identifier_fields,
        temp_comp_mode=temp_comp_mode,
    )
    dmm_t.simulate_temp_failure = dmm_t_simulate_temp_failure
    ctrl.advance(0.0)       # INIT -> PRECHECK
    ctrl.advance(0.0)       # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
    ctrl.advance(7200.1)    # conditioning kész -> FULL_CHARGE
    from Prog.src.charge_controller import ChargeState
    assert ctrl._charge_ctrl is not None
    ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
    ctrl.advance(1.0)       # -> CHARGE_TERMINATION_VERIFIED
    ctrl.advance(1.0)       # -> RELAX_5H_FULL
    ctrl.advance(18000.0)   # -> C20_DISCHARGE
    dmm_v.voltage_V = 10.5  # terminate_voltage_pack_V alatt
    ctrl.advance(1.0)       # -> RELAX_5H_EMPTY
    ctrl.advance(18000.0)   # -> EXPORT_GPCCHEM_PACKAGE
    assert ctrl.state == ChemIdState.EXPORT_GPCCHEM_PACKAGE
    return ctrl, psu, load, dmm_v, dmm_t


class TestRelaxDmmFaultTotalProperty:
    """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
    Adatintegritás P2 lelet] a relax-fázisok (RELAX_5H_FULL/RELAX_5H_EMPTY)
    alatti feszültség-DMM-olvasási hibák session-szintű összege korábban
    csak _run_export()-ban (a GPCCHEM export-zip belsejében, csak sikeres
    exportnál) jelent meg — nem volt publikus property rajta,
    main_window.py::_write_report() nem tudta elérni. FAULT-tal (export
    előtt) végződő session esetén a szám nyomtalanul elveszett."""

    def test_zero_by_default(self):
        ctrl, *_ = make_controller()
        assert ctrl.relax_dmm_fault_total == 0

    def test_reflects_internal_counter(self):
        ctrl, *_ = make_controller()
        ctrl._relax_dmm_fault_total = 4
        assert ctrl.relax_dmm_fault_total == 4


class TestChemIdRelaxDmmFaultEvent:
    """Kor 18, Batch C7 R17-14 (DFAM-001+DFAM-011): a HAROM relax-
    implementacio (RelaxController, OcvSocController, ChemIdController)
    kozul ketto mar elo operatori jelzest ad egy DMM-kiesesre relaxacio
    kozben -- a ChemID inline relaxja EDDIG NEM. Atmenet-only emites
    (28ba01e mintaja), a _relax_dmm_fault_total szamlalo valtozatlanul
    minden ticken no."""

    def _advance_to_relax_full(self, **kwargs):
        ctrl, psu, load, dmm_v, dmm_t = make_controller(**kwargs)
        ctrl.advance(0.0)      # INIT -> PRECHECK
        ctrl.advance(0.0)      # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(7200.1)   # conditioning kész -> FULL_CHARGE
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)  # -> CHARGE_TERMINATION_VERIFIED
        ctrl.advance(1.0)  # -> RELAX_5H_FULL
        return ctrl, psu, load, dmm_v, dmm_t

    def test_three_consecutive_dmm_faults_emit_exactly_one_event(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = self._advance_to_relax_full()
        assert ctrl.state == ChemIdState.RELAX_5H_FULL
        captured: list[dict] = []
        ctrl.on_event = captured.append

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        ctrl.advance(1.0)

        fault_events = [e for e in captured if e["event_code"] == "CHEMID_RELAX_DMM_FAULT"]
        assert len(fault_events) == 1
        assert ctrl.relax_dmm_fault_total == 3
        assert "RELAX_5H_FULL" in fault_events[0]["event_message"]

    def test_recovery_then_second_fault_emits_second_event(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = self._advance_to_relax_full()
        captured: list[dict] = []
        ctrl.on_event = captured.append

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)  # 1. hiba -> esemény
        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)  # helyreállás
        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)  # 2. hiba -> ÚJ esemény (2. átmenet)

        fault_events = [e for e in captured if e["event_code"] == "CHEMID_RELAX_DMM_FAULT"]
        assert len(fault_events) == 2

    def test_no_event_while_dmm_healthy(self):
        ctrl, *_ = self._advance_to_relax_full()
        captured: list[dict] = []
        ctrl.on_event = captured.append
        for _ in range(3):
            ctrl.advance(1.0)
        assert captured == []

    def test_event_message_names_the_empty_relax_phase(self):
        """RELAX_5H_EMPTY-re is átmenet-only és a helyes fázisnevet
        hordozza (nem a FULL feliratot copy-paste-elve)."""
        ctrl, _psu, _load, dmm_v, _dmm_t = self._advance_to_relax_full()
        ctrl.advance(18000.0)  # RELAX_5H_FULL -> C20_DISCHARGE
        dmm_v.voltage_V = 10.5
        ctrl.advance(1.0)  # -> RELAX_5H_EMPTY
        assert ctrl.state == ChemIdState.RELAX_5H_EMPTY

        captured: list[dict] = []
        ctrl.on_event = captured.append
        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)

        fault_events = [e for e in captured if e["event_code"] == "CHEMID_RELAX_DMM_FAULT"]
        assert len(fault_events) == 1
        assert "RELAX_5H_EMPTY" in fault_events[0]["event_message"]


class TestLastIntegrationSourceProperty:
    """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), Szoftver/
    Architektúra P3 lelet] test_runner.py::_build_sample() sample[
    "integration_current_source"]-je enélkül mindig None maradt FULL_
    CHARGE_MANUFACTURER_METHOD alatt — a beágyazott ChargeController
    ténylegesen vezeti ezt, de nem volt proxy-property rajta."""

    def test_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_controller()
        assert ctrl.last_integration_source == "ZERO"

    def test_not_a_live_proxy_of_embedded_charge_ctrl(self):
        """R6-B3 (R6-metrology-01, DFAM-001): a property NEM a gyermek
        elo proxyja -- a gyermek forrasa csak a toltesi delta atvetelekor
        (advance()) rogzul; a pozitiv utat l. test_test_runner.py
        TestR6B3CompositeIntegrationSourceToSampleRow."""
        from types import SimpleNamespace
        ctrl, *_ = make_controller()
        ctrl._charge_ctrl = SimpleNamespace(last_integration_source="PSU_READBACK")
        assert ctrl.last_integration_source == "ZERO"


class TestTaperTimerSProperty:
    """[regresszió, Teljes kódbázis review 12. kör (2026-08-31), Szoftver/
    Architektúra P3 lelet] ugyanaz a hiányzó proxy-property minta, mint
    fent a TestLastIntegrationSourceProperty-nél (11. kör)."""

    def test_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_controller()
        assert ctrl.taper_timer_s == 0.0

    def test_reflects_embedded_charge_ctrl(self):
        from types import SimpleNamespace
        ctrl, *_ = make_controller()
        ctrl._charge_ctrl = SimpleNamespace(taper_timer_s=42.0)
        assert ctrl.taper_timer_s == 42.0


class TestWpAk05GpcChemExportWiring:
    def test_no_export_config_still_reaches_recharge_required(self):
        """Backward kompatibilitás: export_output_dir/export_config nélkül
        (pl. a régi tesztek) az állapotátmenet változatlanul működik,
        export nélkül."""
        ctrl, *_ = _advance_to_export_ready()
        ctrl.advance(0.0)
        assert ctrl.state == ChemIdState.RECHARGE_REQUIRED
        assert ctrl.export_dir is None
        assert ctrl.export_zip_path is None

    def test_export_creates_zip_and_reaches_recharge_required(self, tmp_path):
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, *_ = _advance_to_export_ready(
            export_output_dir=tmp_path, export_config=export_config,
            source_session_reference="session_ref_test",
        )

        ctrl.advance(0.0)  # EXPORT_GPCCHEM_PACKAGE -> RECHARGE_REQUIRED

        assert ctrl.state == ChemIdState.RECHARGE_REQUIRED
        assert ctrl.recharge_required is True
        assert (tmp_path / "gpcchem_submission.zip").exists()
        assert (tmp_path / "config.txt").exists()
        assert (tmp_path / "roomtemp_rel_dis_rel.csv").exists()
        assert (tmp_path / "measurement_quality.json").exists()
        assert ctrl.export_zip_path == str(tmp_path / "gpcchem_submission.zip")

    def test_export_flags_relax_dmm_fault_in_measurement_quality(self, tmp_path):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] a
        relax alatti DMM-kiesés korábban nyomon volt tartva
        (_dmm_fault_during_relax), de sosem jutott el a TI-nek exportált
        csomag measurement_quality.json-jába."""
        import json
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, *_ = _advance_to_export_ready(
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl._relax_dmm_fault_total = 3

        ctrl.advance(0.0)  # EXPORT_GPCCHEM_PACKAGE -> RECHARGE_REQUIRED

        quality = json.loads((tmp_path / "measurement_quality.json").read_text(encoding="utf-8"))
        assert any("Feszültség-DMM kiesés" in limitation for limitation in quality["limitations"])
        # [Kör 19 Batch D3b, F-2 MAX verifier lelet] a régi, összemosott
        # "Hőmérséklet-/feszültség-DMM" szöveg NEM adhat hamis lefedettségi
        # igényt egy tisztán feszültség-DMM hibánál (temp_dmm_fault_ticks=0).
        assert not any("Hőmérséklet-DMM kiesés" in f for f in quality["limitations"])

    def test_export_flags_temperature_dmm_fault_in_measurement_quality(self, tmp_path):
        """[regresszió, Kör 19 Batch D3b, F-2] a `temp_dmm_fault_ticks`
        korábban NEM jutott el a TI export limitáció-listájába -- csak a
        report.json-ba/DEGRADED-kapuba -- holott a gpcchem_export.py
        `cell_surface_temperature_C` oszlopa pontosan ettől a hibától
        lehet stale, jelzés nélkül."""
        import json
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, *_ = _advance_to_export_ready(
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl._temp_dmm_fault_ticks = 9

        ctrl.advance(0.0)  # EXPORT_GPCCHEM_PACKAGE -> RECHARGE_REQUIRED

        quality = json.loads((tmp_path / "measurement_quality.json").read_text(encoding="utf-8"))
        assert any(
            "Hőmérséklet-DMM kiesés (9 " in f and "cell_surface_temperature_C" in f
            for f in quality["limitations"]
        ), "hianyzik a hőmérséklet-DMM limitáció (vagy hibás a szám), jóllehet temp_dmm_fault_ticks=9"
        assert not any("Feszültség-DMM kiesés" in f for f in quality["limitations"])

    def test_export_flags_both_voltage_and_temperature_dmm_faults_independently(
        self, tmp_path
    ):
        """[MAX verifier P3-1 lelete, Kör 19 Batch D3b] a két limitáció
        FÜGGETLEN if-ágból ered -- ez a teszt bizonyítja, hogy EGYÜTTES
        előfordulásnál MINDKETTŐ megjelenik, nem csak az egyik."""
        import json
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, *_ = _advance_to_export_ready(
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl._relax_dmm_fault_total = 3
        ctrl._temp_dmm_fault_ticks = 9

        ctrl.advance(0.0)  # EXPORT_GPCCHEM_PACKAGE -> RECHARGE_REQUIRED

        quality = json.loads((tmp_path / "measurement_quality.json").read_text(encoding="utf-8"))
        assert any(
            "Feszültség-DMM kiesés" in f and "(3 érintett tick)" in f
            for f in quality["limitations"]
        )
        assert any("Hőmérséklet-DMM kiesés (9 " in f for f in quality["limitations"])

    def test_export_flags_temperature_never_measured_under_temp_comp_off(self, tmp_path):
        """[regresszió, Kör 19 Batch D3c, F-3 rész (a)] TempCompMode.OFF
        mellett safety.py::check_temperature_dmm_fault() FELTÉTEL NÉLKÜL
        üres SafetyResult-ot ad -- egy session-kezdettől halott hőmérséklet-
        DMM-nél SOHA nem sikerül egy olvasás sem. L. a golden_gg_
        controller.py testvér-tesztjének docstringje a teljes
        indoklásért."""
        import json
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, *_ = _advance_to_export_ready(
            export_output_dir=tmp_path, export_config=export_config,
            temp_comp_mode=TempCompMode.OFF, dmm_t_simulate_temp_failure=True,
        )
        assert ctrl._temp_dmm_ever_read_ok is False, (
            "a temp DMM sosem olvasott sikeresen a teszt teljes futása alatt"
        )

        ctrl.advance(0.0)  # EXPORT_GPCCHEM_PACKAGE -> RECHARGE_REQUIRED

        quality = json.loads((tmp_path / "measurement_quality.json").read_text(encoding="utf-8"))
        assert any(
            "SOHA nem olvasott sikeresen" in f and "cell_surface_temperature_C" in f
            for f in quality["limitations"]
        ), "hianyzik a 'sosem mert homerseklet' limitacio TempCompMode.OFF alatt"
        # [MAX verifier F-3 lelete, Kör 19 D3c] a D3b tick-alapú
        # limitációnak IS jelen kell lennie ugyanekkor, nem csak a D3c-nek
        # -- l. golden_gg_controller.py testvér-tesztjének kommentje.
        assert any(
            "Hőmérséklet-DMM kiesés (" in f for f in quality["limitations"]
        ), "a D3b tick-alapú limitációnak IS jelen kell lennie, nem csak a D3c-nek"

    def test_export_flags_temperature_ever_read_ok_survives_full_charge_carry_over(
        self, tmp_path
    ):
        """[MAX verifier F-1 lelete, Kör 19 Batch D3c] l. golden_gg_
        controller.py azonos nevű tesztjének teljes docstringje a
        csapda-indoklásért: advance() MINDEN nem-FULL_CHARGE_MANUFACTURER_
        METHOD állapotban (PRECHECK/ROOM_TEMPERATURE_CONDITIONING-ot is
        beleértve, l. :568) a SZÜLŐ saját _read_dmm()-jét hívja, a
        FULL_CHARGE_MANUFACTURER_METHOD állapotba lépés PEDIG csak
        KONSTRUÁLJA a gyermek ChargeController-t, nem tickeli -- ezért a
        hőmérséklet-DMM itt a konstrukció-fázis alatt SZÁNDÉKOSAN halott,
        csak a gyermek ELSŐ valódi tick-jénél áll helyre, majd a session
        VÉGÉIG ismét halott."""
        import json

        from Prog.src.charge_controller import ChargeState
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, _psu, _load, dmm_v, dmm_t = make_controller(
            dmm_voltage_V=13.0, export_output_dir=tmp_path, export_config=export_config,
            temp_comp_mode=TempCompMode.OFF,
        )
        dmm_t.simulate_temp_failure = True

        ctrl.advance(0.0)       # INIT -> PRECHECK (szülő saját olvasása hibázik)
        ctrl.advance(0.0)       # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING (hibázik)
        ctrl.advance(7200.1)    # conditioning kész -> FULL_CHARGE_MANUFACTURER_
        # METHOD (a szülő saját olvasása ITT IS hibázik; a gyermek MOST
        # csak KONSTRUÁLÓDIK, még nem tickelt)
        assert ctrl._charge_ctrl is not None
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        assert ctrl._temp_dmm_ever_read_ok is False, (
            "meta-ellenőrzés: itt MÉG False-nak kell lennie, különben a "
            "teszt szekvenciája hibás"
        )

        dmm_t.simulate_temp_failure = False  # a DMM most helyreáll
        ctrl.advance(1.0)  # a gyermek ELSŐ, VALÓDI tick-je
        assert ctrl._temp_dmm_ever_read_ok is True, (
            "a beágyazott ChargeController valós, sikeres tick-olvasásának "
            "át kellett volna öröklődnie a szülőre"
        )

        # Mostantól a session VÉGÉIG a hőmérséklet-DMM halott -- a flagnek
        # MEGMARAD True-nak (monoton), NEM esik vissza False-ra.
        dmm_t.simulate_temp_failure = True
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)       # -> CHARGE_TERMINATION_VERIFIED
        ctrl.advance(1.0)       # -> RELAX_5H_FULL
        ctrl.advance(18000.0)   # -> C20_DISCHARGE
        dmm_v.voltage_V = 10.5
        ctrl.advance(1.0)       # -> RELAX_5H_EMPTY
        ctrl.advance(18000.0)   # -> EXPORT_GPCCHEM_PACKAGE
        assert ctrl.state == ChemIdState.EXPORT_GPCCHEM_PACKAGE
        assert ctrl._temp_dmm_ever_read_ok is True, (
            "a flag NEM eshet vissza False-ra egy KÉSŐBBI olvasási hibánál"
        )

        ctrl.advance(0.0)  # EXPORT_GPCCHEM_PACKAGE -> RECHARGE_REQUIRED

        quality = json.loads((tmp_path / "measurement_quality.json").read_text(encoding="utf-8"))
        assert not any(
            "SOHA nem olvasott sikeresen" in f for f in quality["limitations"]
        ), "a 'sosem mert' limitacio HAMISAN jelent meg, holott a FULL_CHARGE alatt VALODI olvasas tortent"

    def test_export_uses_measured_ambient_temperature(self, tmp_path):
        """SUP03-CHEMID-012: a project_metadata.json (D-Extra, 2026-09-13
        óta KÜLÖN sidecar a config.txt-től, l. gpcchem_export.py) a
        ténylegesen mért (konditálás alatt rögzített) szobahőmérsékletet
        tartalmazza, nem a konstrukciókori alapértéket."""
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            room_temperature_C=20.0,
        )
        ctrl, psu, load, dmm_v, dmm_t = make_controller(
            dmm_voltage_V=13.0, export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        dmm_t.temperature_C = 24.5  # a "mért" szobahőmérséklet a konditálás alatt
        ctrl.advance(7200.1)
        from Prog.src.charge_controller import ChargeState
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        ctrl.advance(18000.0)
        dmm_v.voltage_V = 10.5
        ctrl.advance(1.0)
        ctrl.advance(18000.0)
        ctrl.advance(0.0)

        import json as _json
        project_metadata = _json.loads(
            (tmp_path / "project_metadata.json").read_text(encoding="utf-8")
        )
        assert project_metadata["room_temperature_C"] == 24.5

        # CF-31 MAX verifier P2-2 lelete (2026-09-08, l. ugyanez golden_gg_
        # controller.py::_run_export()-ben): a beírt érték az akku-felületi
        # hőmérséklet-DMM méréséből ered, nem önálló ambiens szenzorból --
        # ezt explicit limitációként kell rögzíteni, nem jelöletlen "mért
        # szobahőmérséklet"-ként exportálni.
        import json
        quality = json.loads(
            (tmp_path / "measurement_quality.json").read_text(encoding="utf-8")
        )
        assert any(
            "akku-felületi" in f and "NEM önálló szoba-" in f
            for f in quality["limitations"]
        )

    def test_export_flags_unmeasured_room_temperature_with_production_default_config(
        self, tmp_path
    ):
        """CF-31 MAX verifier P2-1/P2-2 lelete (2026-09-08, l. ugyanez
        golden_gg_controller.py::_run_export()-ben): ha a conditioning
        fázis nem futott le (`conditioning_required=False`), a config.txt
        csendben a `GpcChemConfig` konstrukciókori alapértékét exportálná
        -- mostantól ez explicit limitációként jelenik meg."""
        import json

        from Prog.src.charge_controller import ChargeState
        cfg = ChemIdConfig(conditioning_required=False)
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, _psu, _load, dmm_v, _dmm_t = make_controller(
            dmm_voltage_V=13.0, config=cfg,
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> FULL_CHARGE_MANUFACTURER_METHOD (NINCS conditioning)
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        assert ctrl._ambient_temperature_C is None
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        ctrl.advance(18000.0)
        dmm_v.voltage_V = 10.5
        ctrl.advance(1.0)
        ctrl.advance(18000.0)
        ctrl.advance(0.0)

        quality = json.loads(
            (tmp_path / "measurement_quality.json").read_text(encoding="utf-8")
        )
        assert any(
            "NEM mért érték" in f and "RoomTemperatureC" in f
            for f in quality["limitations"]
        )

    def test_missing_identifier_fields_flagged_as_limitation(self, tmp_path):
        """Review-lelet: SUP03-DATA-001 kötelező azonosítói (AppVersion/
        GitCommit/Operator), ha a hívó nem tudja kitölteni, explicit
        limitációként jelenjenek meg a measurement_quality.json-ban — nem
        hallgatólagosan üres config.txt mezőként tűnjenek el."""
        export_config = GpcChemConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, *_ = _advance_to_export_ready(
            export_output_dir=tmp_path, export_config=export_config,
            missing_identifier_fields=["AppVersion", "GitCommit", "Operator"],
        )

        ctrl.advance(0.0)

        assert ctrl.state == ChemIdState.RECHARGE_REQUIRED
        import json
        quality = json.loads((tmp_path / "measurement_quality.json").read_text(encoding="utf-8"))
        limitation_text = " ".join(quality["limitations"])
        assert "AppVersion" in limitation_text
        assert "GitCommit" in limitation_text
        assert "Operator" in limitation_text

    def test_export_num_cell_series_mismatch_faults(self, tmp_path):
        """A profil (6 cella) és az export-config NumCellSeries eltérése
        blokkolja az exportot — a hiba explicit FAULT, nem hallgatólagos
        skip (SUP03-CHEMID-019 kötelező export)."""
        export_config = GpcChemConfig(
            num_cell_series=12,  # eltér a profil 6 cellájától
            battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, *_ = _advance_to_export_ready(
            export_output_dir=tmp_path, export_config=export_config,
        )

        ctrl.advance(0.0)

        assert ctrl.state == ChemIdState.FAULT
        assert "GPCCHEM_EXPORT_FAILED" in ctrl.fault_reason
        assert not (tmp_path / "gpcchem_submission.zip").exists()

    def test_export_failure_turns_off_load_and_psu(self, tmp_path):
        """A load/PSU a normál C20_DISCHARGE_TO_MANUFACTURER_MINIMUM→
        RELAX_5H_EMPTY átmenetkor MÁR kikapcsol — ez a teszt azt igazolja,
        hogy egy export-hiba (emergency_stop()) UTÁN is változatlanul off
        marad (defense-in-depth, nem egy valós on→off átmenetet mér)."""
        export_config = GpcChemConfig(
            num_cell_series=12, battery_model="TST7AH", battery_sample_id="S1",
        )
        ctrl, psu, load, dmm_v, dmm_t = _advance_to_export_ready(
            export_output_dir=tmp_path, export_config=export_config,
        )

        ctrl.advance(0.0)

        assert not load.input_commanded_on
        assert not psu.output_commanded_on


class TestEmergencyStopOffFailureReported:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Hardver/
    Műszervezérlés P2 lelet] l. test_charge_controller.py::
    TestEmergencyStopOffFailureReported ugyanerről a csendben elnyelt
    OFF-kísérlet-hiba mintáról."""

    def test_load_off_failure_surfaces_in_fault_reason(self):
        ctrl, _psu, load, _dmm_v, _dmm_t = make_controller()

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.emergency_stop("DMM_FEEDBACK_LOST")

        assert ctrl.state == ChemIdState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert "LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason

    def test_off_success_leaves_fault_reason_unchanged(self):
        ctrl, *_ = make_controller()
        ctrl.emergency_stop("DMM_FEEDBACK_LOST")
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"


class TestReviewRound5S505UnhandledStateRaises:
    """[regresszió, review-kör 5, Session/Workflow S5-05 (P3, DFAM-004
    sibling) -- l. test_charge_controller.py azonos hivatkozású tesztje
    ugyanerről a hibaosztályról] advance() korábban hallgatólagosan
    (else nélkül) no-op-olt egy le nem kezelt ChemIdState-re."""

    def test_advance_raises_on_unhandled_state(self):
        ctrl, *_ = make_controller()
        ctrl._state = "BOGUS_FUTURE_STATE"
        with pytest.raises(ValueError, match="BOGUS_FUTURE_STATE"):
            ctrl.advance(1.0)


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
        ctrl, psu, _load, dmm_v, _dmm_t = make_controller(
            config=ChemIdConfig(conditioning_required=False),
            dmm_voltage_V=12.0, charge_config=cfg,
        )
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> FULL_CHARGE (beágyazott ChargeController létrejön)
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
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
        assert ctrl.state == ChemIdState.FAULT
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
        assert ctrl.state == ChemIdState.CHARGE_TERMINATION_VERIFIED
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


def test_c08_psu_readback_proxies_delegate_to_charge_ctrl():
    """[MAX verifier P3-4, 2026-09-15, review-kör 6, C-08a/C-08b] l.
    test_ocv_soc_controller.py azonos nevű tesztje a teljes indoklásért."""
    ctrl, *_ = make_controller()
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
    indoklásért -- a C-08a F-1 hibaosztály-minta replikálása chemid_
    controller.py-ra."""

    def test_resets_to_none_on_dmm_feedback_lost_gate(self):
        from Prog.src.charge_controller import ChargeState

        ctrl, _psu, load, dmm_v, _dmm_t = make_controller(dmm_voltage_V=13.0)
        ctrl.advance(0.0)      # INIT -> PRECHECK
        ctrl.advance(0.0)      # PRECHECK -> ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(7200.1)   # conditioning kész -> FULL_CHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)  # -> CHARGE_TERMINATION_VERIFIED
        ctrl.advance(1.0)  # -> RELAX_5H_FULL
        ctrl.advance(18000.0)  # 5h -> C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM

        load.current_A = 0.35
        ctrl.advance(1.0)
        assert ctrl.load_current_readback_valid is True
        assert ctrl.i_load_measured_A == pytest.approx(0.35)

        dmm_v.simulate_timeout = True  # top-of-tick DMM_FEEDBACK_LOST kapu
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FAULT
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

        cfg = ChemIdConfig(conditioning_required=False)
        ctrl, psu, _load, dmm_v, _dmm_t = make_controller(config=cfg, dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(0.0)  # INIT -> PRECHECK
        ctrl.advance(1.0)  # PRECHECK -> FULL_CHARGE_MANUFACTURER_METHOD
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
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


class TestR1B3bDischargeStartBatteryVoltageGate:
    """[regresszió, Claude-Codex loop R1 B3b: NS-02 (DFAM-001)]

    `_begin_c20_discharge()` a B3 DMM-őr után `check_battery_voltage()` nélkül adott
    Load INPUT ON-t: egy érvényes, de a meglévő safety-limiten
    (`SafetyManager.check_battery_voltage()`: U_batt <= 0 V vagy
    > `batt_absolute_max_V`) kívüli U_batt csak a KÖVETKEZŐ tick per-tick
    kapujában (`_run_c20_discharge()`) okozott FAULT-ot, az energizálás UTÁN. Elvárt
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
        assert ctrl.state == ChemIdState.FAULT
        assert ctrl.fault_reason == expected_reason
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls

    @staticmethod
    def _make():
        ctrl, psu, load, *_ = make_controller()
        return ctrl, psu, load

    @pytest.mark.parametrize("case", ["zero", "negative", "above_abs_max"])
    def test_bad_voltage_faults_before_energizing(self, case):
        ctrl, psu, load = self._make()
        u, reason = self._bad_voltage(ctrl, case)
        ctrl._u_batt = u

        ctrl._begin_c20_discharge()

        self._assert_faulted_without_energize(ctrl, psu, load, reason)
        load.reset_calls()
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.FAULT
        assert "input_on()" not in load.calls

    def test_dmm_guard_precedes_voltage_gate(self):
        """Sorrend a (karakterizáció): DMM-hiba + 0 V -> a B3 DMM-őr nyer."""
        ctrl, _psu, load = self._make()
        ctrl._dmm_valid = False
        ctrl._u_batt = 0.0

        ctrl._begin_c20_discharge()

        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls

    def test_voltage_gate_precedes_concurrent_gate(self):
        """Sorrend b: PSU commanded ON + 0 V -> a feszültségkapu nyer, nem a concurrent-kapu."""
        ctrl, psu, load = self._make()
        psu.output_commanded_on = True
        ctrl._u_batt = 0.0

        ctrl._begin_c20_discharge()

        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"
        assert "input_on()" not in load.calls

    def test_exact_abs_max_still_energizes(self):
        """Határ (karakterizáció): pontosan batt_absolute_max_V mellett még
        energizál -- nincs új, szigorúbb limit."""
        ctrl, _psu, load = self._make()
        ctrl._u_batt = ctrl._profile.batt_absolute_max_V

        ctrl._begin_c20_discharge()

        assert "input_on()" in load.calls
        assert ctrl.state != ChemIdState.FAULT

    def test_chain_relax_5h_full_end_zero_volt_faults_before_c20_energize(self):
        """Lánc: valódi tick, friss, érvényes 0 V DMM-olvasás a RELAX_5H_FULL
        végén -> FAULT a C20-energizálás ELŐTT."""
        ctrl, psu, load, dmm_v, _ = make_controller(dmm_voltage_V=13.0)
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        ctrl.advance(7200.1)
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.state == ChemIdState.RELAX_5H_FULL
        load.reset_calls()
        psu.reset_calls()
        dmm_v.voltage_V = 0.0

        ctrl.advance(18000.0)

        self._assert_faulted_without_energize(
            ctrl, psu, load, "REVERSE_POLARITY_OR_WIRING_FAULT"
        )


class TestR8B1ChemIdStartPowerGate:
    """R8-B1: a C/20 setpoint teljesitmenye mar inditas elott ellenorzott."""

    def test_over_power_faults_before_setup(self):
        ctrl, psu, load, *_ = make_controller(dmm_voltage_V=12.5, capacity_Ah=600.0)
        ctrl._u_batt = 12.5
        assert ctrl._discharge_current_A == pytest.approx(30.0)

        ctrl._begin_c20_discharge()

        assert ctrl.state == ChemIdState.FAULT
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
        assert ctrl.state == ChemIdState.FAULT
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)

    @pytest.mark.parametrize("capacity_Ah", [398.0, 400.0])
    def test_at_or_below_limit_starts(self, capacity_Ah):
        ctrl, _psu, load, *_ = make_controller(dmm_voltage_V=12.5, capacity_Ah=capacity_Ah)
        ctrl._u_batt = 12.5

        ctrl._begin_c20_discharge()

        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        assert load.input_commanded_on is True
        assert f"set_current({capacity_Ah / 20.0})" in load.calls

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
        logger.log_sample(runner._build_sample(TestStep(StepKind.CHEMID, "R8-B1"), ctrl))
        row = next(csv.DictReader(io.StringIO(output.getvalue())))
        assert row["fault_flags"] == "LOAD_POWER_LIMIT_EXCEEDED"
        assert row["load_input_commanded_on"] == "False"


class TestR11B1TemperatureHandoff:
    """R11-B1: a kiesesi ido a meresi tulajdonos valtasakor is folytonos."""

    @staticmethod
    def _make_parent(kind, charge_config=None):
        from importlib import import_module

        factories = {
            "chemid": "make_controller",
            "ocv_soc": "make_ocv_soc_controller",
            "iec_dc_resistance": "make_iec_controller",
            "golden_gg": "make_controller",
            "traditional_learning": "make_controller",
        }
        module = import_module(f"Prog.tests.test_{kind}_controller")
        return getattr(module, factories[kind])(charge_config=charge_config)

    @staticmethod
    def _start_charge(ctrl, kind):
        if kind in ("chemid", "golden_gg"):
            ctrl._enter_full_charge()
        elif kind in ("ocv_soc", "iec_dc_resistance"):
            ctrl._enter_precharge()
        else:
            from Prog.src.traditional_learning_controller import LearningState

            ctrl._enter(LearningState.RECHARGE)

    @staticmethod
    def _watch_off(monkeypatch, psu, load, off_failure=""):
        order = []
        load_off = load.input_off
        psu_off = psu.all_outputs_off

        def input_off():
            order.append("LOAD_OFF")
            if off_failure == "load":
                raise RuntimeError("R11 load OFF failure")
            load_off()

        def all_outputs_off():
            order.append("PSU_OFF")
            if off_failure == "psu":
                raise RuntimeError("R11 PSU OFF failure")
            psu_off()

        monkeypatch.setattr(load, "input_off", input_off)
        monkeypatch.setattr(psu, "all_outputs_off", all_outputs_off)
        return order

    @staticmethod
    def _assert_logged_fault(ctrl, psu, load, dmm_v, dmm_t):
        from unittest.mock import Mock

        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.test_runner import TestRunner, TestRunnerConfig

        logger = Mock()
        runner = TestRunner(
            InstrumentManager(psu, load, dmm_v, dmm_t),
            ctrl._safety, logger, ctrl._profile,
            TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: None,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
        )
        events = []
        runner.on_event = events.append
        result = runner._emergency_stop(ctrl.fault_reason)
        assert result.status == "FAULT"
        assert "TEMPERATURE_MONITOR_LOST_CRITICAL" in result.reason
        logger.log_event.assert_any_call(
            "EMERGENCY_STOP", ctrl.fault_reason, is_critical=True,
        )
        assert any(e["event_code"] == "EMERGENCY_STOP" for e in events)
        logger.close.assert_called_once()

    @pytest.mark.parametrize("off_failure", ["", "load", "psu"])
    def test_conditioning_58s_then_charge_faults_at_60s(self, monkeypatch, off_failure):
        from Prog.src.charge_controller import ChargeConfig

        ctrl, psu, load, dmm_v, dmm_t = make_controller(
            charge_config=ChargeConfig(charge_current_A_override=0.5),
        )
        ctrl.advance(0.0)
        ctrl.advance(2.0)
        assert ctrl.state == ChemIdState.ROOM_TEMPERATURE_CONDITIONING
        ctrl.advance(7200.0 - 58.0)
        dmm_t.simulate_temp_failure = True
        for _ in range(29):
            ctrl.advance(2.0)
        assert ctrl.state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD

        order = self._watch_off(monkeypatch, psu, load, off_failure)
        ctrl.advance(2.0)
        assert ctrl.state == ChemIdState.FAULT
        assert ctrl.fault_reason.startswith("TEMPERATURE_MONITOR_LOST_CRITICAL")
        assert order and order == ["LOAD_OFF", "PSU_OFF"] * (len(order) // 2)
        assert not psu.output_commanded_on
        assert not load.input_commanded_on
        assert "output_on()" not in psu.calls
        assert "input_on()" not in load.calls
        if off_failure:
            marker = "LOAD_OFF_FAILED" if off_failure == "load" else "PSU_OFF_FAILED"
            assert marker in ctrl.fault_reason

        calls = (list(psu.calls), list(load.calls), list(dmm_t.calls))
        for _ in range(3):
            ctrl.advance(2.0)
        assert (psu.calls, load.calls, dmm_t.calls) == calls
        self._assert_logged_fault(ctrl, psu, load, dmm_v, dmm_t)

    @pytest.mark.parametrize(
        "kind", ["chemid", "ocv_soc", "iec_dc_resistance", "golden_gg", "traditional_learning"],
    )
    @pytest.mark.parametrize("next_child", [False, True])
    def test_child_return_and_next_child_keep_history(self, kind, next_child):
        ctrl, psu, load, _dmm_v, dmm_t, *_ = self._make_parent(kind)
        ctrl._safety.temperature_dmm_fault_timeout_s = 10.5
        dmm_t.simulate_temp_failure = True
        ctrl._read_dmm(4.0)
        self._start_charge(ctrl, kind)
        dmm_t.calls.clear()
        ctrl.advance(2.0)
        child = ctrl._charge_ctrl
        assert child is not None
        assert child._safety is ctrl._safety
        assert ctrl._safety.temperature_dmm_fault_s == 6.0
        assert dmm_t.calls == ["read_temperature()"]
        assert ctrl.temp_dmm_fault_ticks == 2

        child._state = ChargeState.CHARGE_DONE
        ctrl.advance(2.0)
        assert ctrl._charge_ctrl is None
        assert ctrl._safety.temperature_dmm_fault_s == 8.0
        assert ctrl.temp_dmm_fault_ticks == 3
        if next_child:
            self._start_charge(ctrl, kind)
        dmm_t.calls.clear()
        ctrl.advance(2.5)
        assert ctrl.state.name == "FAULT"
        assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
        assert ctrl._safety.temperature_dmm_fault_s == 10.5
        assert dmm_t.calls == ["read_temperature()"]
        assert ctrl.temp_dmm_fault_ticks == 4
        assert "output_on()" not in psu.calls
        assert "input_on()" not in load.calls

    @pytest.mark.parametrize(
        "kind", ["chemid", "ocv_soc", "iec_dc_resistance", "golden_gg", "traditional_learning"],
    )
    def test_fresh_child_and_parent_read_reset_history(self, kind):
        ctrl, _psu, _load, _dmm_v, dmm_t, *_ = self._make_parent(kind)
        dmm_t.simulate_temp_failure = True
        ctrl._read_dmm(58.0)
        self._start_charge(ctrl, kind)
        dmm_t.simulate_temp_failure = False
        ctrl.advance(2.0)
        assert ctrl._safety.temperature_dmm_fault_s == 0.0
        assert ctrl.temp_dmm_valid
        dmm_t.simulate_temp_failure = True
        ctrl.advance(2.0)
        assert ctrl._safety.temperature_dmm_fault_s == 2.0
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(2.0)
        assert ctrl._charge_ctrl is None
        assert ctrl._safety.temperature_dmm_fault_s == 4.0
        dmm_t.simulate_temp_failure = False
        ctrl.advance(0.0)
        assert ctrl._safety.temperature_dmm_fault_s == 0.0
        assert ctrl.state.name != "FAULT"
        assert ctrl.temp_dmm_fault_ticks == 3

    @pytest.mark.parametrize("kind", ["golden_gg", "traditional_learning"])
    def test_temperature_only_reader_hands_history_to_charge(self, kind):
        ctrl, psu, _load, _dmm_v, dmm_t, *_ = self._make_parent(kind)
        dmm_t.simulate_temp_failure = True
        ctrl._read_temperature_only(58.0)
        self._start_charge(ctrl, kind)
        ctrl.advance(2.0)
        assert ctrl.state.name == "FAULT"
        assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
        assert "output_on()" not in psu.calls

    @pytest.mark.parametrize(
        "kind", ["chemid", "ocv_soc", "iec_dc_resistance", "golden_gg", "traditional_learning"],
    )
    def test_fast_loop_counts_only_child_subticks(self, monkeypatch, kind):
        cfg = fast_loop_charge_config(charge_current_A_override=0.5)
        ctrl, psu, _load, dmm_v, dmm_t, *_ = self._make_parent(kind, cfg)
        dmm_t.simulate_temp_failure = True
        ctrl._read_dmm(10.0)
        self._start_charge(ctrl, kind)
        ctrl.advance(0.0)
        child = ctrl._charge_ctrl
        assert child is not None
        target = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        dmm_v.voltage_V = target
        psu.current_A = 0.5
        install_fast_loop_clock(monkeypatch)
        dmm_t.calls.clear()
        ticks_before = ctrl.temp_dmm_fault_ticks
        ctrl.advance(DT_S)
        assert ctrl.state.name != "FAULT"
        assert ctrl._safety.temperature_dmm_fault_s == pytest.approx(10.0 + DT_S + EXTRA_S)
        assert len(dmm_t.calls) == SUBSTEPS_PER_TICK
        assert ctrl.temp_dmm_fault_ticks - ticks_before == SUBSTEPS_PER_TICK


class TestR16B16aNormalFloorSibling:
    @pytest.mark.parametrize("phase", ["setup", "sample", "config"])
    @pytest.mark.parametrize("failed_off", ["none", "load", "psu"])
    def test_common_floor_fault_cleanup_event_and_no_restart(self, phase, failed_off):
        parts = make_controller()
        ctrl, psu, load, dmm = parts[:4]
        ctrl._state = ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        ctrl._dmm_valid = True
        ctrl._u_batt = dmm.voltage_V = 10.79 if phase != "config" else 12.5
        if phase == "config":
            ctrl._profile.terminate_voltage_per_cell_V = 1.60
        if phase == "sample":
            # Az első cutoff-mintát a meglévő terminate-ág rendesen lezárja.
            load.input_commanded_on = True
            ctrl._u_batt = dmm.voltage_V = 10.8
            ctrl.advance(1.0)
            assert ctrl.state == ChemIdState.RELAX_5H_EMPTY
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
            ctrl._begin_c20_discharge()
        assert ctrl.state.value == "FAULT"
        reason = "DISCHARGE_CONFIG_INVALID" if phase == "config" else "BATTERY_UNDERVOLTAGE"
        assert ctrl.fault_reason.startswith(reason)
        assert calls[:2] == ["load_off", "psu_off"]
        assert any(e["event_code"] == reason for e in events)
        ctrl.advance(1.0)
        assert not load.called("input_on")
