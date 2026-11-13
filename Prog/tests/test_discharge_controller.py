"""
DischargeController unit tesztek.
"""
import pytest

from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import SafetyManager, PsuMode
from Prog.src.discharge_controller import DischargeController, DischargeState, DischargeConfig
from Prog.tests.mock_drivers.fake_clock import TickClock, bind_tick_clock
from Prog.tests.mock_drivers.mock_psu import MockPSU
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_dmm import MockDMM


def make_profile():
    return BatteryProfile(
        battery_name="Teszt", manufacturer="FIAMM", model="FG20721",
        nominal_capacity_Ah=7.0, cell_count=6, nominal_voltage_V=12.0,
    )


def make_discharge_controller(dmm_voltage_V=12.5, load_power_W=None, clock=None):
    """METRO-R20-1 (Kör 20 Batch E1): a controller mostantól
    `_load_on_elapsed_s`-t egy injektált monoton `time_source`-ból
    származtatja (l. discharge_controller.py). A `TickClock` +
    `bind_tick_clock()` alapértelmezésben ε=0-t szimulál -- azaz az
    `advance(dt_s)` hívások pontosan úgy léptetik az órát, ahogy a régi,
    dt_s-akkumuláló kód tette -- így ez a gyár és a MEGLÉVŐ tesztek
    VÁLTOZATLANOK maradnak. ε>0-t (a setup-tick input_on() ELŐTTI valós
    SCPI-időfogyasztást) a burn_before_input_on() explicit injektálja."""
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
    tick_clock = clock if clock is not None else TickClock()
    ctrl = DischargeController(
        psu, load, dmm_v, dmm_t, profile, safety, cfg, time_source=tick_clock,
    )
    bind_tick_clock(ctrl)
    return ctrl, psu, load, dmm_v


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

    def test_reverse_polarity_during_active_discharge_faults_not_done(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív
        kisütés alatt egy fizikailag ésszerűtlen (<=0V) DMM-olvasás korábban
        csendben "elértük a végfeszültséget"-ként DISCHARGE_DONE-t okozott
        volna, NEM FAULT-ot (CLAUDE.md §10.6/SAFE-009)."""
        ctrl, _psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        dmm.voltage_V = 0.0
        load.voltage_V = 0.0
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"


class TestReview3rdRoundUBattVAndTemperatureProxy:
    """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] a
    DischargeController korábban nem exponálta a u_batt_V/battery_
    temperature_C property-ket, ellentétben a testvér ChargeController/
    TraditionalLearningController/GoldenGgController-rel — emiatt
    test_runner.py::_build_sample() getattr()-je mindig None-t adott, és
    InstrumentManager.read_all() minden ticken MÁSODSZOR is lekérdezte
    ugyanazt a fizikai DMM-et (CLAUDE.md §9)."""

    def test_u_batt_v_reflects_dmm_reading_after_tick(self):
        ctrl, *_ = make_discharge_controller(dmm_voltage_V=12.34)
        ctrl.advance(dt_s=1.0)
        assert ctrl.u_batt_V == pytest.approx(12.34)

    def test_battery_temperature_c_reflects_dmm_reading_after_tick(self):
        ctrl, *_ = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)
        assert ctrl.battery_temperature_C == pytest.approx(22.0)


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
        """[frissítve, Teljes kódbázis review 8. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] max_discharge_time_s a DischargeConfig-ból a
        SafetyManager-re konszolidálva — a felülbírálás mostantól ott
        történik, ugyanaz a minta, mint max_load_power_W-nél (5. kör)."""
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl._safety.max_discharge_time_s = 5.0
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=10.0)  # exceeds max_discharge_time
        assert ctrl.state == DischargeState.FAULT


class TestDischargeMaxAh:
    """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), Szoftver/
    Architektúra P2 lelet] a MAX_DISCHARGE_AH_REACHED backstopnak eddig NEM
    volt önálló regressziós tesztje ebben a fájlban."""

    def test_fault_on_max_ah_exceeded(self):
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK -> DISCHARGE_CC_SETUP -> DISCHARGE_CC_RUN
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

        load.current_A = 10.0  # nagy áram -> gyorsan túllépi a névleges kapacitás 110%-át
        ctrl.advance(dt_s=3600.0)

        assert ctrl.state == DischargeState.FAULT
        assert "MAX_DISCHARGE_AH_REACHED" in ctrl.fault_reason

    def test_fault_on_max_ah_exceeded_uses_c10_not_c20_base(self):
        """A védendő kisütési áram (profile.C5_discharge_current_A,
        C10-alapú, l. 11. kör 5b62b0f) mellett a plafonnak is a kanonikus
        C10-bázisból (_c10_base_Ah) kell számolnia, NEM a nyers
        (FG-sorozatnál C20-alapú) nominal_capacity_Ah-ból."""
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK -> DISCHARGE_CC_SETUP -> DISCHARGE_CC_RUN
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        # FG-sorozat szimulációja: nominal_capacity_Ah=C20=20.0, C10=10.0 —
        # az alap max_discharge_Ah_factor=1.10 mellett a C10-plafon 11.0 Ah,
        # a C20-plafon 22.0 Ah — a lenti 15.0 Ah a kettő KÖZÖTT van.
        ctrl._profile = BatteryProfile(
            battery_name="TEST_FG", manufacturer="TEST", model="FG20AH",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=20.0,
            C10_capacity_Ah=10.0,
        )

        load.current_A = 15.0
        ctrl.advance(dt_s=3600.0)  # 15.0 Ah kisütve

        assert ctrl.state == DischargeState.FAULT
        assert "MAX_DISCHARGE_AH_REACHED" in ctrl.fault_reason


class TestDischargePrecheckDmmGuard:
    """V4: DischargeController PRECHECK DMM hiba → DMM_FEEDBACK_LOST."""

    def test_dmm_failure_at_precheck_gives_dmm_fault(self):
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        dmm.dmm_valid = False
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → fault
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"

    def test_dmm_valid_property_reflects_actual_dmm_read_result(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P2 lelet #4] a ChargeController már exponálja a dmm_valid
        property-t, ezt test_runner.py::_build_sample() a dmm_voltage_valid
        mezőhöz olvassa — a DischargeController hiányzott, emiatt
        getattr(controller, "dmm_valid", None) mindig None-t adott, és a
        fallback (`u_batt is not None`) gyakorlatilag MINDIG True-t írt,
        akkor is, ha a DMM-olvasás ténylegesen hibázott (u_batt csak az
        utolsó sikeres mérésen ragad, sosem esik None-ra)."""
        ctrl, _psu, _load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK, DMM-olvasás
        assert ctrl.dmm_valid is True

        dmm.dmm_valid = False
        ctrl.advance(dt_s=1.0)  # a következő DMM-olvasás hibázik
        assert ctrl.dmm_valid is False


class TestLoadCommLost:
    """P0-5: Load current readback hiba → LOAD_COMM_LOST fault, nem 0.0A integráció."""

    def _advance_to_cc_run(self):
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        return ctrl, load

    def test_load_comm_failure_causes_fault(self):
        ctrl, load = self._advance_to_cc_run()
        load.simulate_current_readback_failure = True
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.FAULT
        assert "LOAD_COMM_LOST" in ctrl.fault_reason

    def test_load_comm_failure_not_silently_integrated(self):
        """Kommunikáció hiba után nem 0.0A-t integrál — azonnal faultol."""
        ctrl, load = self._advance_to_cc_run()
        ah_before = ctrl.accumulated_discharge_Ah
        load.simulate_current_readback_failure = True
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.FAULT
        # Az Ah nem nőtt (0A integráció nélkül)
        assert ctrl.accumulated_discharge_Ah == ah_before

    def test_load_comm_failure_triggers_safe_off(self):
        ctrl, load = self._advance_to_cc_run()
        load.simulate_current_readback_failure = True
        ctrl.advance(dt_s=1.0)
        assert load.called("input_off")


class TestLoadCurrentReadbackValidProperty:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
    `i_load_measured_A`/`load_current_readback_valid` -- a
    `charge_controller.py::i_psu_measured_A`/`psu_current_readback_valid`
    Load-oldali tükre, tick-scoped validity-flag mintával."""

    def _advance_to_cc_run(self):
        ctrl, _psu, load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        return ctrl, load

    def test_valid_true_and_value_matches_integrated_sample(self):
        """A property EBBEN a tickben ténylegesen mért, VALÓDI mintát ad,
        ugyanazt, amit az integrálás is felhasznált -- nem egy MÁSODIK,
        önálló olvasást."""
        ctrl, load = self._advance_to_cc_run()
        load.current_A = 0.62
        ah_before = ctrl.accumulated_discharge_Ah
        ctrl.advance(dt_s=1.0)
        assert ctrl.load_current_readback_valid is True
        assert ctrl.i_load_measured_A == pytest.approx(0.62)
        assert ctrl.accumulated_discharge_Ah > ah_before

    def test_valid_false_on_comm_loss(self):
        ctrl, load = self._advance_to_cc_run()
        load.simulate_current_readback_failure = True
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.load_current_readback_valid is False

    def test_resets_to_none_on_top_of_tick_fault_gate(self):
        """Mutáció, amin bukik: a nullázás elhagyása/áthelyezése a kapu
        UTÁNRA -- ha a DMM_FEEDBACK_LOST kapu a Load-olvasás ELŐTT
        FAULT-ba viszi a controllert, a property NEM maradhat az előző,
        sikeres tick értékén (l. feedback_new_tick_scoped_validity_flag_
        needs_explicit_reset.md, C-08a F-1 megismétlésének elkerülése)."""
        ctrl, load = self._advance_to_cc_run()
        load.current_A = 0.62
        ctrl.advance(dt_s=1.0)
        assert ctrl.load_current_readback_valid is True  # sikeres tick, baseline

        ctrl2, _psu2, load2, dmm2 = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl2.advance(dt_s=1.0)
        assert ctrl2.state == DischargeState.DISCHARGE_CC_RUN
        load2.current_A = 0.62
        ctrl2.advance(dt_s=1.0)
        assert ctrl2.load_current_readback_valid is True

        dmm2.simulate_timeout = True
        ctrl2.advance(dt_s=1.0)
        assert ctrl2.state == DischargeState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl2.fault_reason
        assert ctrl2.load_current_readback_valid is None
        assert ctrl2.i_load_measured_A is None

    def test_is_none_on_terminal_state_tick(self):
        """A `DischargeState.FAULT` early-return-je (advance() legelső
        érdemi ága) UTÁN hívott advance() sem hagyhatja a property-t az
        utolsó, terminális-állapot-ELŐTTI tick értékén."""
        ctrl, load = self._advance_to_cc_run()
        load.current_A = 0.62
        ctrl.advance(dt_s=1.0)
        assert ctrl.load_current_readback_valid is True

        load.simulate_current_readback_failure = True
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.FAULT

        load.simulate_current_readback_failure = False
        ctrl.advance(dt_s=1.0)  # terminális tick, korai return
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.load_current_readback_valid is None
        assert ctrl.i_load_measured_A is None


class TestConcurrentPsuLoadDischarge:
    """Kisütés közben PSU ON → CONCURRENT_PSU_LOAD_ON fault."""

    def test_concurrent_psu_during_discharge_triggers_fault(self):
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        # INIT → PRECHECK → DISCHARGE_CC_SETUP → DISCHARGE_CC_RUN
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

        # Hardware conflict: PSU is also commanded on during discharging
        psu.output_commanded_on = True
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == DischargeState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason

    def test_no_fault_when_only_load_on(self):
        """Normál kisütés: Load ON, PSU OFF → nincs concurrent fault."""
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(4):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        assert load.input_commanded_on is True
        assert psu.output_commanded_on is False

    def test_concurrent_psu_stale_flag_blocks_cc_setup_input_on(self):
        """Teljes kódbázis review 15. kör (2026-09-04), Session/Workflow +
        Hardver/Műszervezérlés P1 lelet: a kölcsönös kizárás ellenőrzés
        korábban KIZÁRÓLAG a Load-ot MÁR bekapcsolt állapotban
        (DISCHARGE_CC_RUN) fogta el a PSU-oldali konfliktust — ha a PSU
        input_commanded_on flag-je egy korábbi (pl. beágyazott töltési)
        fázis write-hibája miatt stale True maradt PONTOSAN a
        DISCHARGE_CC_SETUP ticken (amikor a load.input_on() ténylegesen
        meghívódna), a Load minden ellenőrzés nélkül azonnal ON-ba
        kerülhetett volna — a 14. kör ChargeController._run_psu_preset()
        fixének tükörképe a Load-oldalon."""
        ctrl, psu, load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK -> DISCHARGE_CC_SETUP
        assert ctrl.state == DischargeState.DISCHARGE_CC_SETUP
        assert load.input_commanded_on is False

        # Stale PSU-flag PONT a Load bekapcsolása előtti ticken.
        psu.output_commanded_on = True
        ctrl.advance(dt_s=1.0)  # DISCHARGE_CC_SETUP tick -- itt hívódna input_on()

        assert ctrl.state == DischargeState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason
        # A lényeg: a Load SOSEM kapcsolt be -- a concurrent-check a
        # load.input_on() hívás ELŐTT fogta el a konfliktust.
        assert load.input_commanded_on is False


class TestDischargeTerminateOverride:
    def test_uses_override_voltage_instead_of_profile(self):
        """terminate_voltage_V_override=11.5V esetén 11.5V-nál áll meg, nem 10.8V-nál."""
        profile = make_profile()  # terminate = 6 × 1.80 = 10.80V
        psu = MockPSU(voltage_V=0.0, current_A=0.0)
        load = MockLoad(voltage_V=12.0)
        dmm_v = MockDMM(voltage_V=12.0)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = DischargeConfig(terminate_voltage_V_override=11.5)
        ctrl = DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)

        # 11.4V < override 11.5V → le kell állni
        dmm_v.voltage_V = 11.4
        load.voltage_V = 11.4
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_DONE

    def test_zero_override_uses_profile_default(self):
        """terminate_voltage_V_override=0 esetén a profil 1.80V/cella értéke érvényes."""
        profile = make_profile()  # 6 × 1.80 = 10.80V
        psu = MockPSU(voltage_V=0.0, current_A=0.0)
        load = MockLoad(voltage_V=12.0)
        dmm_v = MockDMM(voltage_V=12.0)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = DischargeConfig(terminate_voltage_V_override=0.0)
        ctrl = DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)

        # 11.4V > profile 10.80V → ne álljon meg
        dmm_v.voltage_V = 11.4
        load.voltage_V = 11.4
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

        # 10.75V < profile 10.80V → álljon meg
        dmm_v.voltage_V = 10.75
        load.voltage_V = 10.75
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_DONE


class TestPowerLimitFault:
    """[M-03] LOAD teljesítmény túllépés → FAULT."""

    def test_power_limit_triggers_fault_when_exceeded(self):
        """DMM 12.5V × terhelés 1.4A (C/5) = 17.5W > 15W max → FAULT.
        R8-B1: tick 3-ban, a CC_SETUP setpointirasa elott mar FAULT."""
        profile = make_profile()
        psu = MockPSU(voltage_V=0.0, current_A=0.0)
        load = MockLoad(voltage_V=12.5)
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0)
        # Teljes kódbázis review 5. kör (2026-08-29), Szoftver/Architektúra
        # + Hardver/Műszervezérlés P2 lelet (dedup): a limit forrása
        # DischargeConfig-ból SafetyManager-be költözött — l.
        # discharge_controller.py::DischargeConfig kommentje.
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT, max_load_power_W=15.0)
        cfg = DischargeConfig()
        ctrl = DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)

        assert ctrl.state == DischargeState.FAULT
        assert "POWER_LIMIT" in ctrl.fault_reason
        assert "input_on()" not in load.calls

    def test_power_limit_not_triggered_when_below_limit(self):
        """Normál kisütésnél (17.5W < 60W default) nem triggel."""
        ctrl, *_ = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN


class TestReleaseAudit20260823RbMeasuredCurrentAndTiming:
    """Release-audit 2026-08-23 (2.3): Rb 1s/10s/30s korábban a LOAD_SETPOINT-
    et (self._i_discharge_set) használta, NEM a ténylegesen mért terhelés-
    áramot (CLAUDE.md #11.5 megsértése), és nem jelezte, ha a tényleges
    mintavételi idő számottevően eltért a névleges célidőponttól (1s/10s/
    30s). Zéró tesztlefedettség volt ezen a területen a javítás előtt."""

    def _advance_to_discharge_run(self, ctrl):
        import pytest
        ctrl.advance(dt_s=0.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=0.0)  # PRECHECK -> DISCHARGE_CC_SETUP
        ctrl.advance(dt_s=0.0)  # DISCHARGE_CC_SETUP -> DISCHARGE_CC_RUN (load.input_on())
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

    def test_rb_1s_uses_measured_current_not_setpoint(self):
        import pytest
        ctrl, psu, load, dmm_v = make_discharge_controller(dmm_voltage_V=12.5)
        self._advance_to_discharge_run(ctrl)
        # DischargeConfig() alapértelmezett discharge_current_A=0.0 -> auto
        # C/5 = 7.0/5 = 1.4A -> load.set_current(1.4) hívva a setup-ban.
        assert ctrl.i_load_set_A == pytest.approx(1.4)
        # CC szabályozási eltérést szimulálunk: a MÉRT áram nem pontosan a
        # setpoint (1.4A), hanem 1.5A.
        load.current_A = 1.5
        dmm_v.voltage_V = 12.3  # 0.2V esés a terhelés bekapcsolása után

        ctrl.advance(dt_s=1.0)

        expected_from_measured = (12.5 - 12.3) / 1.5 * 1000.0
        expected_from_setpoint = (12.5 - 12.3) / 1.4 * 1000.0
        assert ctrl.rb_1s_mohm == pytest.approx(expected_from_measured)
        assert ctrl.rb_1s_mohm != pytest.approx(expected_from_setpoint)

    def test_rb_non_positive_delta_voltage_stays_none_not_negative(self):
        """[regresszió, review-kör 4 (2026-09-09), Metrológia M4-03 (P2,
        DFAM-001 + DFAM-018)] kisütő impulzusnál a terhelés alatti
        feszültség fizikailag NEM lehet magasabb/egyenlő a terhelés előtti
        (baseline) feszültségnél -- korábban ez negatív/nulla rb_1s_mohm-ot
        adott quality-jelzés nélkül. l. pulse_measurement_service.py azonos
        testvér-fixje ugyanerről a mintáról."""
        ctrl, psu, load, dmm_v = make_discharge_controller(dmm_voltage_V=12.5)
        self._advance_to_discharge_run(ctrl)
        load.current_A = 1.4
        # A terhelés alatti "mért" feszültség a baseline (12.5V) FÖLÉ esik
        # -- fizikailag lehetetlen, csak mérési hibából jöhet.
        dmm_v.voltage_V = 12.6

        ctrl.advance(dt_s=1.0)

        assert ctrl.rb_1s_mohm is None

    def test_rb_1s_timing_within_tolerance_not_degraded(self):
        import pytest
        ctrl, psu, load, dmm_v = make_discharge_controller(dmm_voltage_V=12.5)
        self._advance_to_discharge_run(ctrl)
        dmm_v.voltage_V = 12.3

        ctrl.advance(dt_s=1.0)  # t=1.0s, pontosan a célnál

        assert ctrl.rb_1s_actual_s == pytest.approx(1.0)
        assert ctrl.rb_timing_degraded is False

    def test_rb_1s_timing_out_of_tolerance_sets_degraded_flag(self):
        """[a metrológiai audit által talált, korábban 0 lefedettségű rés] a
        chunkonkénti holtidő miatt a tényleges mintaidő jóval a névleges
        célidőpont után lehet (dokumentált SUP01 HIL-lelet: 2.141s az 1.0s
        helyett) — ennek jeleznie kell magát."""
        import pytest
        ctrl, psu, load, dmm_v = make_discharge_controller(dmm_voltage_V=12.5)
        self._advance_to_discharge_run(ctrl)
        dmm_v.voltage_V = 12.3

        ctrl.advance(dt_s=2.0)  # t=2.0s, 1.0s-nál 1.0s késés (> 0.5s tolerancia)

        assert ctrl.rb_1s_actual_s == pytest.approx(2.0)
        assert ctrl.rb_timing_degraded is True

    def test_rb_timing_degraded_flag_reset_on_new_discharge_setup(self):
        ctrl, psu, load, dmm_v = make_discharge_controller(dmm_voltage_V=12.5)
        self._advance_to_discharge_run(ctrl)
        dmm_v.voltage_V = 12.3
        ctrl.advance(dt_s=2.0)
        assert ctrl.rb_timing_degraded is True

        ctrl._run_cc_setup()  # új kisütési (al)ciklus szimulálása

        assert ctrl.rb_timing_degraded is False
        assert ctrl.rb_1s_actual_s is None

    def test_method_origin_marks_rb_mohm_as_project_engineering_method(self):
        """[regresszió, Governance-v2 review-kör 1 (CF-24/DFAM-003 7.
        előfordulás + BAT-RI-001 sértés, batch 4.6)] a rb_1s/10s/30s_mohm
        mezőknek korábban NEM volt method_origin-jelölése a samples.csv-
        ben — a FIAMM adatlap Ri-jével összehasonlíthatónak TŰNT, holott
        IEC 60896-21/22 (adatlap Ri) vs pulse-ellenállás külön mérési
        módszer. Ugyanazt az értéket kell adnia, mint az OcvSocController
        saját R_pulse_actual_t*_mOhm mezőjének (mindkettő "nem TI Ra, nem
        FIAMM adatlap-érték")."""
        ctrl, *_ = make_discharge_controller()
        assert ctrl.method_origin == "PROJECT_ENGINEERING_METHOD"

    def test_synchronization_quality_marks_software_tick_timed_regardless_of_tick_length(self):
        """[regresszió, MAX verifier P2-1 lelete (CF-24 utóellenőrzés,
        batch 4.6)] a method_origin ÖNMAGÁBAN nem jelzi, hogy a mérés
        software-tick-timed (fő runner-tick alapú) vagy instrumentum-
        időzített — a rb_timing_degraded flag EZT NEM helyettesíti, mert
        kis (pl. 0.5-1.0s) runner_tick_s mellett False maradhat, miközben
        a mérés még mindig software-tick-timed. A synchronization_quality
        property-nek MINDIG a tényleges mintavételi módot kell jeleznie,
        függetlenül a konkrét tick hosszától/toleranciájától."""
        from Prog.src.pulse_measurement_service import SYNC_QUALITY_SOFTWARE_TICK_TIMED
        ctrl, *_ = make_discharge_controller()
        assert ctrl.synchronization_quality == SYNC_QUALITY_SOFTWARE_TICK_TIMED
        assert ctrl.synchronization_quality == "SOFTWARE_TICK_TIMED"


class TestMetroR20_1DischargeLoadOnTimeOrigin:
    """Kör 20 Batch E1 (METRO-R20-1, P2): a `_load_on_elapsed_s`-nek a
    TÉNYLEGES `input_on()` pillanatától kell számolnia, nem a hívó
    `dt_s`-einek tick-határtól vett összegétől. A fenti
    `TestReleaseAudit20260823RbMeasuredCurrentAndTiming` osztály
    időzítés-tesztjei az ε=0 IDEALIZÁCIÓT rögzítik -- ez az osztály a
    VALÓS, ε>0 esetet fedi le a `burn_before_input_on()` segítségével."""

    def _advance_to_run(self, ctrl):
        ctrl.advance(dt_s=0.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=0.0)  # PRECHECK -> DISCHARGE_CC_SETUP
        ctrl.advance(dt_s=0.0)  # DISCHARGE_CC_SETUP -> DISCHARGE_CC_RUN (input_on())
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

    def test_rb_elapsed_excludes_setup_tick_prefix(self):
        """tick=1.0s, ε=0.4s: az 1. mérő ticknél a valós load-on idő
        1*1-0.4=0.6s (< 1.0s cél) -- MÉG NINCS minta, szemben a fix ELŐTTI
        kóddal, ami itt már mintázna (dt_s-akkumuláció, az ε-előtagot
        figyelmen kívül hagyva 1.0-t adna). A 2. mérő ticknél 1.6s ->
        ekkor mintázik, rb_timing_degraded=True (|1.6-1.0|=0.6 > 0.5)."""
        from Prog.tests.mock_drivers.fake_clock import burn_before_input_on

        clock = TickClock()
        ctrl, _psu, load, dmm_v = make_discharge_controller(dmm_voltage_V=12.5, clock=clock)
        burn_before_input_on(load, clock, seconds=0.4)
        self._advance_to_run(ctrl)
        dmm_v.voltage_V = 12.3
        tick = 1.0
        ctrl.advance(dt_s=tick)                # 1. mérő tick: elapsed=0.6 < 1.0
        assert ctrl.rb_1s_mohm is None
        ctrl.advance(dt_s=tick)                # 2. mérő tick: elapsed=1.6 >= 1.0
        assert ctrl.rb_1s_actual_s == pytest.approx(1.6)
        assert ctrl.rb_timing_degraded is True

    def test_rb_timing_not_degraded_when_true_elapsed_within_tolerance(self):
        """tick=1.7s, ε=0.3s -> valós elapsed 1.4s (0.4s < 0.5s tolerancia
        -> NEM degraded). A fix ELŐTTI kód 1.7-et állított volna (0.7s
        eltérés -> degraded=True) -- ez a teszt a flag-FLIP-et
        diszkriminálja."""
        from Prog.tests.mock_drivers.fake_clock import burn_before_input_on

        clock = TickClock()
        ctrl, _psu, load, dmm_v = make_discharge_controller(dmm_voltage_V=12.5, clock=clock)
        burn_before_input_on(load, clock, seconds=0.3)
        self._advance_to_run(ctrl)
        dmm_v.voltage_V = 12.3
        ctrl.advance(dt_s=1.7)                 # 1. mérő tick: elapsed=1.7-0.3=1.4
        assert ctrl.rb_1s_actual_s == pytest.approx(1.4)
        assert ctrl.rb_timing_degraded is False

    def test_rb_timing_degraded_when_true_elapsed_outside_tolerance(self):
        """tick=1.7s, ε=0.1s -> valós elapsed 1.6s (0.6s > 0.5s tolerancia
        -> degraded=True). A fix ELŐTTI kód 1.7-et állított volna (azonos
        degraded=True, de ELTÉRŐ érték) -- ez a teszt az ÉRTÉK-regressziót
        diszkriminálja."""
        from Prog.tests.mock_drivers.fake_clock import burn_before_input_on

        clock = TickClock()
        ctrl, _psu, load, dmm_v = make_discharge_controller(dmm_voltage_V=12.5, clock=clock)
        burn_before_input_on(load, clock, seconds=0.1)
        self._advance_to_run(ctrl)
        dmm_v.voltage_V = 12.3
        ctrl.advance(dt_s=1.7)                 # 1. mérő tick: elapsed=1.7-0.1=1.6
        assert ctrl.rb_1s_actual_s == pytest.approx(1.6)
        assert ctrl.rb_timing_degraded is True


class TestEmergencyStopOffFailureReported:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Hardver/
    Műszervezérlés P2 lelet] l. test_charge_controller.py::
    TestEmergencyStopOffFailureReported ugyanerről a csendben elnyelt
    OFF-kísérlet-hiba mintáról."""

    def test_load_off_failure_surfaces_in_fault_reason(self):
        ctrl, _psu, load, _dmm = make_discharge_controller()

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.emergency_stop("DMM_FEEDBACK_LOST")

        assert ctrl.state == DischargeState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert "LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason

    def test_off_success_leaves_fault_reason_unchanged(self):
        ctrl, *_ = make_discharge_controller()
        ctrl.emergency_stop("DMM_FEEDBACK_LOST")
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"


class TestPrecheckPsuOffFailureReported:
    """[regresszió, Teljes kódbázis review 18. kör (2026-09-05), Hardver/
    Műszervezérlés P1 lelet] a _run_precheck() psu.all_outputs_off() hívása
    védtelen maradt, szemben a 17. körben (`d108c9f`) ugyanebben a
    controllerben javított _run_cc_setup()/_run_cc() hívásokkal — kivétel
    esetén korábban a controller sosem állt FAULT-ra, az állapot
    PRECHECK-ben ragadt."""

    def test_all_outputs_off_failure_escalates_to_fault(self):
        ctrl, psu, _load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK (state csak beáll)
        assert ctrl.state == DischargeState.PRECHECK

        def _raise_on_off():
            raise RuntimeError("simulated psu off failure")
        psu.all_outputs_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.advance(dt_s=1.0)  # PRECHECK tick -- itt fut le _run_precheck()

        assert ctrl.state == DischargeState.FAULT
        assert "DISCHARGE_PRECHECK_PSU_OFF_FAILED" in ctrl.fault_reason
        assert "simulated psu off failure" in ctrl.fault_reason


class TestCcSetupFailureReported:
    """[regresszió, Teljes kódbázis review 17. kör (2026-09-05), Hardver/
    Műszervezérlés P1 lelet] a testvér GoldenGgController/
    ChemIdCharacterizationController/TraditionalLearningController/
    OcvSocController mindegyike explicit try/except-be csomagolja a saját
    kisütés-indítási szekvenciáját — a DischargeController (az EREDETI,
    önálló minta) maga védtelen maradt: egy load.set_current()/input_on()
    kivétel korábban a TestRunner legkülső, generikus UNHANDLED_EXCEPTION
    ágáig szállt volna, a specifikus diagnosztika elvesztésével."""

    def test_set_current_failure_escalates_to_fault(self):
        ctrl, _psu, load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK -> DISCHARGE_CC_SETUP (state csak beáll)
        assert ctrl.state == DischargeState.DISCHARGE_CC_SETUP

        def _raise_on_set_current(_value):
            raise RuntimeError("simulated set_current failure")
        load.set_current = _raise_on_set_current  # type: ignore[method-assign]

        ctrl.advance(dt_s=1.0)  # DISCHARGE_CC_SETUP tick -- itt fut le _run_cc_setup()

        assert ctrl.state == DischargeState.FAULT
        assert "DISCHARGE_CC_SETUP_FAILED" in ctrl.fault_reason
        assert "simulated set_current failure" in ctrl.fault_reason
        assert load.input_commanded_on is False

    def test_input_on_failure_escalates_to_fault(self):
        ctrl, _psu, load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK -> DISCHARGE_CC_SETUP (state csak beáll)
        assert ctrl.state == DischargeState.DISCHARGE_CC_SETUP

        def _raise_on_input_on(**kwargs):
            raise RuntimeError("simulated input_on failure")
        load.input_on = _raise_on_input_on  # type: ignore[method-assign]

        ctrl.advance(dt_s=1.0)  # DISCHARGE_CC_SETUP tick -- itt fut le _run_cc_setup()

        assert ctrl.state == DischargeState.FAULT
        assert "DISCHARGE_CC_SETUP_FAILED" in ctrl.fault_reason
        assert "simulated input_on failure" in ctrl.fault_reason

    def test_input_on_lost_ack_with_real_driver_faults_after_single_send(self):
        """[regresszió, R1 B2 F-03, 2026-10-25] a teljes lánc VALÓS
        Keithley2380Load driverrel (szkriptelt VISA-resource): az első
        "INP ON" write elvész (a következő kísérlet sikerülne) -> a driver
        EGYSZER küldi, kivételt dob -> _run_cc_setup() try/except ->
        emergency_stop() -> Load OFF ("INP OFF;*OPC?") + PSU ALL OFF. A
        régi kódon a vak retry DISCHARGE_CC_RUN-ba vitte a controllert."""
        from unittest.mock import MagicMock

        from Prog.drivers.device_load import Keithley2380Load

        ctrl, psu, _mock_load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK -> DISCHARGE_CC_SETUP (state csak beáll)
        assert ctrl.state == DischargeState.DISCHARGE_CC_SETUP

        real_load = Keithley2380Load()
        res = MagicMock()
        res.query.return_value = "1"
        real_load._resource = res
        real_load._connected = True
        real_load._input_cmd = "INP"
        state = {"failed": False}

        def _write_side_effect(cmd: str) -> None:
            if cmd == "INP ON" and not state["failed"]:
                state["failed"] = True
                raise TimeoutError("simulated lost write")

        res.write.side_effect = _write_side_effect
        ctrl._load = real_load

        ctrl.advance(dt_s=1.0)  # DISCHARGE_CC_SETUP tick -- input_on() dob

        written = [c.args[0] for c in res.write.call_args_list]
        queried = [c.args[0] for c in res.query.call_args_list]
        assert ctrl.state == DischargeState.FAULT
        assert "DISCHARGE_CC_SETUP_FAILED" in ctrl.fault_reason
        assert written.count("INP ON") == 1
        assert "INP OFF;*OPC?" in queried
        assert "all_outputs_off()" in psu.calls
        assert real_load.input_commanded_on is False

        # Nincs további aktív parancs a FAULT után.
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.FAULT
        assert [c.args[0] for c in res.write.call_args_list].count("INP ON") == 1


class TestDischargeDoneLoadOffFailureReported:
    """[regresszió, Teljes kódbázis review 17. kör (2026-09-05), Session/
    Workflow + Hardver/Műszervezérlés P1 lelet] l. TestCcSetupFailureReported
    docstringje ugyanerről a hiányzó testvér-mintáról — itt a kisütés-
    lezáráskori (végfeszültség elérve) load.input_off()-ra vonatkozóan."""

    def test_terminate_voltage_load_off_failure_escalates_to_fault(self):
        ctrl, _psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK -> DISCHARGE_CC_SETUP -> DISCHARGE_CC_RUN
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        dmm.voltage_V = 10.75  # < 10.80V terminate
        load.voltage_V = 10.75
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == DischargeState.FAULT
        assert "DISCHARGE_LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason


class TestTempDmmValidProperty:
    """[regresszió, review-kör 3 (2026-09-09), MAX verifier (a9cb161469c468fc8)
    P2-1] a temp_dmm_valid property (Metrológia M3-01, P1) egyik
    controlleren sem kapott önálló, controller-szintű regressziós tesztet
    -- csak egy kézzel írt test_runner.py stub fedte. Ez a hiány engedte
    át a chemid_controller.py-beli P1-1 blokkoló hibát (a property sosem
    íródott False-ra) 1792 zöld teszt mellett. Ez a teszt-osztály a
    DischargeController saját, működő wiring-jét fedi le közvetlenül."""

    def test_true_after_successful_temperature_read(self):
        ctrl, *_ = make_discharge_controller()
        ctrl.advance(dt_s=1.0)
        assert ctrl.temp_dmm_valid is True

    def test_false_after_failed_temperature_read(self):
        ctrl, *_ = make_discharge_controller()
        ctrl._dmm_t.simulate_temp_failure = True
        ctrl.advance(dt_s=1.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_fault_ticks_counts_failed_reads_not_seconds(self):
        """[regresszió, MAX verifier (review-kör 3, a4d9e82debe0bdfcb) P2,
        DFAM-001 4. előfordulása] a DischargeController-nek korábban nem
        volt tick-alapú hőmérséklet-DMM-hiba-számlálója -- egy önálló
        StepKind.DISCHARGE lépés hőmérséklet-DMM-kiesését emiatt sem a
        main_window.py temp_dmm_fault_ticks_total (5 összetett controller-
        only), sem korábban a test_runner.py generikus aggregációja nem
        látta."""
        ctrl, *_ = make_discharge_controller()
        assert ctrl.temp_dmm_fault_ticks == 0

        ctrl._dmm_t.simulate_temp_failure = True
        ctrl.advance(dt_s=1.0)
        assert ctrl.temp_dmm_fault_ticks == 1

        ctrl.advance(dt_s=1.0)
        assert ctrl.temp_dmm_fault_ticks == 2

        ctrl._dmm_t.simulate_temp_failure = False
        ctrl.advance(dt_s=1.0)  # sikeres olvasás -- a tick-számláló NEM nullázódik
        assert ctrl.temp_dmm_fault_ticks == 2


class TestLoadCurrentSetpointWarning:
    """[regresszió, Hardver/Műszervezérlés HW4-02 (review-kör 4, P1,
    DFAM-001)] a beállított és a ténylegesen mért terhelőáram közti
    eltérés korábban sehol nem került ellenőrzésre -- egy elmaradó áram
    (védelmi latch, CC-tartomány-elutasítás, csendes SCPI-elutasítás)
    fizikailag hihető, de HAMIS adatfolyamként jelent volna meg."""

    def test_measured_current_matching_setpoint_no_warning(self):
        profile = make_profile()
        psu = MockPSU(voltage_V=0.0, current_A=0.0)
        load = MockLoad(voltage_V=12.5)
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = DischargeConfig(discharge_current_A=2.0)
        ctrl = DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK -> DISCHARGE_CC_SETUP -> DISCHARGE_CC_RUN
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

        load.current_A = 2.0
        ctrl.advance(dt_s=1.0)
        assert ctrl.last_warning_code == ""

    def test_current_stuck_far_below_setpoint_gives_warning(self):
        """A HW4-02 failure scenario szó szerinti alakzata: a Load
        elutasítja a setpointot (pl. CC-tartomány/latch), az áram az
        előző szinten ragad -- a mért áram fizikailag hihető, mégis
        HAMIS a kért terhelési feltétel szempontjából."""
        profile = make_profile()
        psu = MockPSU(voltage_V=0.0, current_A=0.0)
        load = MockLoad(voltage_V=12.5)
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = DischargeConfig(discharge_current_A=10.0)
        ctrl = DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

        load.current_A = 0.5  # a setpoint (10.0A) töredéke -- a load "ragadt"
        ctrl.advance(dt_s=1.0)
        assert ctrl.state != DischargeState.FAULT  # csak figyelmeztetés, nem fault
        assert ctrl.last_warning_code == "LOAD_CURRENT_SETPOINT_NOT_REACHED"

    def test_setpoint_warning_tick_counter_survives_higher_priority_warning(self):
        """[regresszió, MAX verifier (review-kör 4, HW4-02, 329c1a3) N1
        lelete] a `last_warning_code` egyetlen, tickenkénti slot -- ha egy
        magasabb prioritású (hőmérséklet-) figyelmeztetés MINDEN tickben
        aktív a session teljes ideje alatt, a LOAD_CURRENT_SETPOINT_NOT_
        REACHED sosem jutna a slotba, pontosan azt a hibaosztályt
        reprodukálva, amit a HW4-02 fix maga célzott (egy elmaradó
        terhelőáram csendben, nyomtalanul veszne el). A load_current_
        setpoint_warning_ticks számláló FÜGGETLEN a slot-tól."""
        profile = make_profile()
        psu = MockPSU(voltage_V=0.0, current_A=0.0)
        load = MockLoad(voltage_V=12.5)
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = DischargeConfig(discharge_current_A=10.0)
        ctrl = DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

        dmm_t.temperature_C = 40.0  # profil névleges tartományán (15-30C) kívül -- MINDEN tickben aktív
        load.current_A = 0.5  # a setpoint (10.0A) töredéke -- "ragadt"
        assert ctrl.load_current_setpoint_warning_ticks == 0
        ctrl.advance(dt_s=1.0)
        assert ctrl.last_warning_code == "TEMPERATURE_OUT_OF_RANGE"  # a slot-ot a hőmérséklet nyeri
        assert ctrl.load_current_setpoint_warning_ticks == 1  # de a tick MÉGIS számolva van
        ctrl.advance(dt_s=1.0)
        assert ctrl.load_current_setpoint_warning_ticks == 2


class TestReviewRound5S505UnhandledStateRaises:
    """[regresszió, review-kör 5, Session/Workflow S5-05 (P3, DFAM-004
    sibling) -- l. test_charge_controller.py azonos hivatkozású tesztje
    ugyanerről a hibaosztályról] advance() korábban hallgatólagosan
    (else nélkül) no-op-olt egy le nem kezelt DischargeState-re."""

    def test_advance_raises_on_unhandled_state(self):
        ctrl, *_ = make_discharge_controller()
        ctrl._state = "BOGUS_FUTURE_STATE"
        with pytest.raises(ValueError, match="BOGUS_FUTURE_STATE"):
            ctrl.advance(1.0)


def _f27_runner_sharing_instruments(tmp_path, ctrl):
    """R1 B8b, F-27: valódi TestRunner a controller SAJÁT mock műszereivel
    (a _build_sample() rb_delta_v képlete a controller u_batt_pre_load_V-jét
    és a minta battery_voltage_V-jét veti össze)."""
    from datetime import datetime, timezone

    from Prog.src.instrument_manager import InstrumentManager
    from Prog.src.logger import LogConfig, Logger
    from Prog.src.test_runner import TestRunner, TestRunnerConfig

    profile = make_profile()
    instruments = InstrumentManager(
        psu=ctrl._psu, load=ctrl._load,
        dmm_voltage=ctrl._dmm_v, dmm_temperature=ctrl._dmm_t,
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


def _f27_advance_through_setup(ctrl):
    ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
    ctrl.advance(dt_s=1.0)  # PRECHECK -> DISCHARGE_CC_SETUP
    ctrl.advance(dt_s=1.0)  # SETUP-tick: pre-load minta, input_on() -> DISCHARGE_CC_RUN
    assert ctrl.state == DischargeState.DISCHARGE_CC_RUN


class TestF27PreLoadVoltageOnlyAfterFirstLoadedSample:
    """[regresszió, R1 B8b, F-27 (P3), DFAM-008] a SETUP-tickben a
    `_u_batt_pre_load` UGYANABBÓL a terhelés előtti DMM-mintából jön, mint a
    minta battery_voltage_V-je -- a test_runner.py::_build_sample()
    rb_delta_v = u_pre - u_batt ezért hamis 0.000-t írt (a tick végén az
    állapot már DISCHARGE_CC_RUN, állapotőr nem segít). Fix: az
    u_batt_pre_load_V property None, amíg az első TERHELT minta (_run_cc()
    eleje) nem áll rendelkezésre. A belső Rb-képlet/időbélyeg változatlan."""

    def test_flag_false_in_constructor_and_property_none(self):
        ctrl, *_ = make_discharge_controller()
        assert ctrl._loaded_voltage_sample_available is False
        assert ctrl.u_batt_pre_load_V is None

    def test_setup_tick_property_none_and_sample_rb_delta_v_none(self, tmp_path):
        ctrl, _psu, _load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        runner, logger = _f27_runner_sharing_instruments(tmp_path, ctrl)
        from Prog.src.test_runner import StepKind, TestStep
        step = TestStep(StepKind.DISCHARGE, "discharge")

        _f27_advance_through_setup(ctrl)

        # A belső referencia változatlanul rögzül (Rb-képlet bemenete) ...
        assert ctrl._u_batt_pre_load == pytest.approx(12.5)
        # ... de a publikált érték még nincs terhelt mintához párosítva.
        assert ctrl.u_batt_pre_load_V is None
        sample = runner._build_sample(step, ctrl)
        assert sample["battery_voltage_V"] == pytest.approx(12.5)
        assert sample["rb_delta_v"] is None
        logger.close()

    def test_first_loaded_tick_delta_from_loaded_sample_rb_unchanged(self, tmp_path):
        ctrl, _psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        runner, logger = _f27_runner_sharing_instruments(tmp_path, ctrl)
        from Prog.src.test_runner import StepKind, TestStep
        step = TestStep(StepKind.DISCHARGE, "discharge")
        _f27_advance_through_setup(ctrl)

        dmm.voltage_V = 12.3  # terhelt feszültség
        load.voltage_V = 12.3
        ctrl.advance(dt_s=1.0)  # első DISCHARGE_CC_RUN tick (t = 1,0 s)

        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        assert ctrl.u_batt_pre_load_V == pytest.approx(12.5)
        sample = runner._build_sample(step, ctrl)
        assert sample["battery_voltage_V"] == pytest.approx(12.3)
        assert sample["rb_delta_v"] == pytest.approx(0.2)
        # Belső Rb (privát _u_batt_pre_load, mért áram, időbélyeg) változatlan.
        i_load = ctrl.i_load_measured_A
        assert i_load is not None and i_load > 0.01
        assert ctrl.rb_1s_mohm == pytest.approx((12.5 - 12.3) / i_load * 1000.0)
        assert ctrl.rb_1s_actual_s == pytest.approx(1.0)
        assert sample["rb_1s_mohm"] == pytest.approx(ctrl.rb_1s_mohm)
        logger.close()

    def test_dmm_failure_before_first_loaded_read_keeps_property_none(self, tmp_path):
        ctrl, _psu, _load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        runner, logger = _f27_runner_sharing_instruments(tmp_path, ctrl)
        from Prog.src.test_runner import StepKind, TestStep
        step = TestStep(StepKind.DISCHARGE, "discharge")
        _f27_advance_through_setup(ctrl)

        dmm.set_dmm_invalid()
        ctrl.advance(dt_s=1.0)  # az első terhelt olvasás hibázik

        assert ctrl.state == DischargeState.FAULT
        assert ctrl.fault_reason.startswith("DMM_FEEDBACK_LOST")
        assert ctrl._loaded_voltage_sample_available is False
        assert ctrl.u_batt_pre_load_V is None
        assert runner._build_sample(step, ctrl)["rb_delta_v"] is None
        logger.close()

    def test_new_setup_invalidates_previous_loaded_sample_flag(self):
        """Fehérdobozos: a produkciós állapotgépben nincs visszalépés
        DISCHARGE_CC_SETUP-ba, de egy (újra)setup a korábbi terhelt-minta
        jelzőt nem hagyhatja érvényben -- a _run_cc_setup() nullázza."""
        ctrl, _psu, _load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        _f27_advance_through_setup(ctrl)
        dmm.voltage_V = 12.3
        ctrl.advance(dt_s=1.0)
        assert ctrl.u_batt_pre_load_V == pytest.approx(12.5)

        ctrl._load.input_off()
        ctrl._state = DischargeState.DISCHARGE_CC_SETUP
        dmm.voltage_V = 12.4
        ctrl.advance(dt_s=1.0)  # új SETUP-tick

        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        assert ctrl._u_batt_pre_load == pytest.approx(12.4)
        assert ctrl.u_batt_pre_load_V is None


def _f25_controller(config: DischargeConfig, dmm_voltage_V: float = 12.5):
    profile = make_profile()
    psu = MockPSU(voltage_V=0.0, current_A=0.0)
    load = MockLoad(voltage_V=dmm_voltage_V)
    dmm_v = MockDMM(voltage_V=dmm_voltage_V)
    dmm_t = MockDMM(temperature_C=22.0)
    safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
    ctrl = DischargeController(
        psu, load, dmm_v, dmm_t, profile, safety, config, time_source=TickClock(),
    )
    bind_tick_clock(ctrl)
    return ctrl, profile, psu, load, dmm_v, dmm_t


_F25_CONFIGS = {
    "zero_sentinel": DischargeConfig(),
    "explicit_override": DischargeConfig(
        discharge_current_A=0.36, terminate_voltage_V_override=11.2,
    ),
}


class TestF25EffectiveSetpointAndCutoffSnapshot:
    """[regresszió, R1 B8c, F-25 (P2), DFAM-021, D-B8-6] a report.json nem
    tartalmazta a DischargeController ténylegesen alkalmazott kisütési
    áram-SETPOINTját és cutoffját (nem alapértelmezett policy -- pl. 1,75 V/
    cella vagy CUSTOM -- esetén a profile_snapshot 1,80 V/cellát mutatott).
    Az új I/O-mentes property-k a meglévő inline képletek (_run_cc_setup()/
    _run_cc()) tükrei; az inline safety-sorok érintetlenek -- ez a
    karakterizáló teszt őrzi, hogy ne sodródjanak el egymástól."""

    def test_zero_sentinel_resolves_to_profile_values(self):
        ctrl, profile, *_ = _f25_controller(DischargeConfig())
        assert ctrl.effective_discharge_current_A == profile.C5_discharge_current_A
        assert ctrl.effective_terminate_voltage_V == profile.terminate_voltage_pack_V

    def test_explicit_override_wins(self):
        ctrl, *_ = _f25_controller(_F25_CONFIGS["explicit_override"])
        assert ctrl.effective_discharge_current_A == 0.36
        assert ctrl.effective_terminate_voltage_V == 11.2

    def test_config_snapshot_contents_and_no_instrument_io(self):
        ctrl, profile, psu, load, dmm_v, dmm_t = _f25_controller(
            _F25_CONFIGS["explicit_override"],
        )
        calls_before = (list(psu.calls), list(load.calls), list(dmm_v.calls), list(dmm_t.calls))

        snapshot = ctrl.config_snapshot()

        assert (list(psu.calls), list(load.calls), list(dmm_v.calls), list(dmm_t.calls)) == calls_before
        assert snapshot == {
            "discharge_current_A": 0.36,
            "terminate_voltage_V_override": 11.2,
            "rate_origin": {"rate_policy": "UNKNOWN", "capacity_basis": "UNKNOWN", "capacity_Ah": 0.0},
            "effective_discharge_current_set_A": 0.36,
            "effective_terminate_voltage_V": 11.2,
        }
        zero = _f25_controller(DischargeConfig())[0].config_snapshot()
        assert zero["discharge_current_A"] == 0.0
        assert zero["terminate_voltage_V_override"] == 0.0
        assert zero["effective_discharge_current_set_A"] == profile.C5_discharge_current_A
        assert zero["effective_terminate_voltage_V"] == profile.terminate_voltage_pack_V

    @pytest.mark.parametrize("case", sorted(_F25_CONFIGS))
    def test_set_current_argument_equals_effective_current(self, case):
        ctrl, _profile, _psu, load, _dmm_v, _dmm_t = _f25_controller(_F25_CONFIGS[case])
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK -> DISCHARGE_CC_SETUP
        ctrl.advance(dt_s=1.0)  # SETUP: set_mode_cc/set_current/input_on
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        set_current_calls = [c for c in load.calls if c.startswith("set_current(")]
        assert set_current_calls == [f"set_current({ctrl.effective_discharge_current_A})"]
        assert ctrl.i_load_set_A == ctrl.effective_discharge_current_A

    @pytest.mark.parametrize("case", sorted(_F25_CONFIGS))
    def test_cutoff_exactly_at_effective_terminate_voltage(self, case):
        ctrl, _profile, _psu, load, dmm_v, _dmm_t = _f25_controller(_F25_CONFIGS[case])
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        eff = ctrl.effective_terminate_voltage_V

        dmm_v.voltage_V = eff + 0.01  # a rámpa felső oldala: még fut
        load.voltage_V = eff + 0.01
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN

        dmm_v.voltage_V = eff  # pontosan az effektív cutoffnál: leáll
        load.voltage_V = eff
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_DONE
        assert load.called("input_off")


class TestR8B1DischargeStartPowerGate:
    """R8-B1: a CC-setup a kert arammal, meg setpointiras elott kapuz."""

    @pytest.mark.parametrize("current_A", [0.0, 30.0], ids=["auto", "explicit"])
    def test_over_power_faults_before_setup(self, current_A):
        ctrl, psu, load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl._profile.nominal_capacity_Ah = 150.0
        ctrl._config.discharge_current_A = current_A
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_SETUP
        load.reset_calls()
        psu.reset_calls()

        ctrl.advance(1.0)

        assert ctrl.state == DischargeState.FAULT
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
        assert ctrl.state == DischargeState.FAULT
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)

    @pytest.mark.parametrize("current_A", [19.9, 20.0])
    def test_at_or_below_limit_starts(self, current_A):
        ctrl, _psu, load, _dmm = make_discharge_controller(dmm_voltage_V=12.5)
        ctrl._config.discharge_current_A = current_A
        for _ in range(3):
            ctrl.advance(1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        assert load.input_commanded_on is True
        assert f"set_current({current_A})" in load.calls

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
        logger.log_sample(runner._build_sample(TestStep(StepKind.DISCHARGE, "R8-B1"), ctrl))
        row = next(csv.DictReader(io.StringIO(output.getvalue())))
        assert row["fault_flags"] == "LOAD_POWER_LIMIT_EXCEEDED"
        assert row["load_input_commanded_on"] == "False"


class TestR11B1RelaxDischargeHistory:
    """R11-B1: az onallo RELAX sem ad uj turelmi idot a kisutesnek."""

    @pytest.mark.parametrize(
        ("mode", "reset", "fault"),
        [
            ("MONITOR_ONLY", "", True),
            ("MONITOR_ONLY", "fresh_read", False),
            ("MONITOR_ONLY", "new_session", False),
            ("OFF", "", False),
            ("ENABLED", "", True),
        ],
    )
    def test_7200s_relax_then_new_discharge(self, monkeypatch, mode, reset, fault):
        from Prog.src.relax_controller import RelaxConfig, RelaxController
        from Prog.src.safety import TempCompMode
        from Prog.tests.test_chemid_controller import TestR11B1TemperatureHandoff

        profile = make_profile()
        safety = SafetyManager(
            profile, PsuMode.INDEPENDENT, temp_comp_mode=TempCompMode[mode],
        )
        psu, load = MockPSU(), MockLoad(voltage_V=12.5)
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(simulate_temp_failure=True)
        relax = RelaxController(
            dmm_v, RelaxConfig(min_relax_s=7200.0),
            dmm_temperature=dmm_t, safety=safety,
        )
        events = []
        relax.on_event = events.append
        relax.advance(0.0)
        relax.advance(7200.0)
        if reset == "fresh_read":
            dmm_t.simulate_temp_failure = False
            relax.advance(0.0)
            dmm_t.simulate_temp_failure = True
        elif reset == "new_session":
            safety = SafetyManager(profile, PsuMode.INDEPENDENT)

        ctrl = DischargeController(
            psu, load, dmm_v, dmm_t, profile, safety, DischargeConfig(),
        )
        order = TestR11B1TemperatureHandoff._watch_off(monkeypatch, psu, load)
        ctrl.advance(2.0)
        if fault:
            assert ctrl.state == DischargeState.FAULT
            assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
            assert order == ["LOAD_OFF", "PSU_OFF"]
            assert not psu.output_commanded_on
            assert not load.input_commanded_on
            assert "input_on()" not in load.calls
            assert "output_on()" not in psu.calls
            assert any(e["event_code"] == "RELAX_TEMPERATURE_DMM_FAULT" for e in events)
            calls = (list(load.calls), list(psu.calls), list(dmm_t.calls))
            ctrl.advance(2.0)
            assert (load.calls, psu.calls, dmm_t.calls) == calls
            TestR11B1TemperatureHandoff._assert_logged_fault(
                ctrl, psu, load, dmm_v, dmm_t,
            )
        else:
            ctrl.advance(2.0)
            ctrl.advance(2.0)
            assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
            assert load.input_commanded_on
            if reset:
                assert safety.temperature_dmm_fault_s == 6.0


class TestR16B16aNormalDischargeGate:
    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("cutoff_per_cell", [1.60, 1.75])
    def test_override_is_rejected_without_clamp(self, cells, cutoff_per_cell):
        ctrl, psu, load, _dmm = make_discharge_controller()
        ctrl._profile.cell_count = cells
        _dmm.voltage_V = cells * 2.1
        ctrl._config.terminate_voltage_V_override = cells * cutoff_per_cell
        for _ in range(4):
            ctrl.advance(1.0)
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.fault_reason == "DISCHARGE_CONFIG_INVALID"
        assert load.called("input_off") and psu.called("all_outputs_off")
        assert not load.called("input_on")
        assert ctrl._config.terminate_voltage_V_override == cells * cutoff_per_cell

    @pytest.mark.parametrize("which", ["setup", "sample"])
    @pytest.mark.parametrize("failed_off", ["none", "load", "psu"])
    def test_gate_fault_off_order_event_and_no_restart(self, which, failed_off):
        ctrl, psu, load, dmm = make_discharge_controller()
        for _ in range(2 if which == "setup" else 3):
            ctrl.advance(1.0)
        if which == "sample":
            # Első cutoff-minta: rendes vég; utána modellezzük a fennmaradt terhelést.
            dmm.voltage_V = 10.8
            ctrl.advance(1.0)
            assert ctrl.state == DischargeState.DISCHARGE_DONE
            load.input_commanded_on = True
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
        on_before = sum("input_on" in c for c in load.calls)
        dmm.voltage_V = 10.79
        ctrl.advance(1.0)
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.fault_reason.startswith("BATTERY_UNDERVOLTAGE")
        assert calls[:2] == ["load_off", "psu_off"]
        assert any(e["event_code"] == "BATTERY_UNDERVOLTAGE" for e in events)
        for _ in range(3):
            ctrl.advance(1.0)
        assert sum("input_on" in c for c in load.calls) == on_before

    def test_exact_floor_is_normal_completion(self):
        ctrl, _psu, load, dmm = make_discharge_controller()
        for _ in range(3):
            ctrl.advance(1.0)
        dmm.voltage_V = 10.8
        ctrl.advance(1.0)
        assert ctrl.state == DischargeState.DISCHARGE_DONE
        assert not load.input_commanded_on

    @pytest.mark.parametrize("bad", [1.0, 10.5, float("nan"), float("inf")])
    def test_mutated_override_is_still_gated_before_on(self, bad):
        ctrl, psu, load, _dmm = make_discharge_controller()
        ctrl._config.terminate_voltage_V_override = bad
        for _ in range(4):
            ctrl.advance(1.0)
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.fault_reason == "DISCHARGE_CONFIG_INVALID"
        assert load.called("input_off") and psu.called("all_outputs_off")
        assert not load.called("input_on")

    @pytest.mark.parametrize("cells", [6, 12])
    def test_proven_c20_is_allowed_but_raw_sample_floor_remains(self, cells):
        from Prog.src.battery_profile import DischargeRateOrigin

        ctrl, _psu, load, dmm = make_discharge_controller()
        ctrl._profile.cell_count = cells
        ctrl._profile.nominal_capacity_Ah = 7.2
        ctrl._config.discharge_current_A = 0.36
        ctrl._config.rate_origin = DischargeRateOrigin("C/20", "NOMINAL_C10_CAPACITY", 7.2)
        ctrl._config.terminate_voltage_V_override = 1.75 * cells
        dmm.voltage_V = 2.1 * cells
        for _ in range(3):
            ctrl.advance(1.0)
        assert ctrl.state == DischargeState.DISCHARGE_CC_RUN
        assert load.input_commanded_on
        events = []
        ctrl.on_event = events.append
        dmm.voltage_V = 1.749 * cells
        ctrl.advance(1.0)
        assert ctrl.state == DischargeState.DISCHARGE_DONE
        assert not load.input_commanded_on
        assert any(e["event_code"] == "NORMAL_DISCHARGE_CUTOFF_OVERSHOOT" for e in events)
        load.input_commanded_on = True
        ctrl.advance(1.0)
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.fault_reason == "BATTERY_UNDERVOLTAGE"


class TestR16B16aF2SingleSampleSiblings:
    @pytest.mark.parametrize(
        "mode", ["DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL", "CHEMID", "OCV_SOC"],
    )
    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("reverse", [False, True])
    def test_later_good_tick_cannot_hide_loaded_undershoot(self, monkeypatch, mode, cells, reverse):
        if mode == "CHEMID":
            from Prog.src.chemid_controller import ChemIdState
            from Prog.tests.test_chemid_controller import make_controller

            ctrl, psu, load, dmm, *_ = make_controller()
            ctrl._state = ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
            expected_end = "RELAX_5H_EMPTY"
        elif mode == "OCV_SOC":
            from Prog.src.ocv_soc_controller import OcvSocState
            from Prog.tests.test_ocv_soc_controller import make_ocv_soc_controller

            ctrl, psu, load, dmm, *_ = make_ocv_soc_controller()
            ctrl._state = OcvSocState.STEP_DISCHARGE
            ctrl._step_discharge_started = True
            expected_end = "STEP_RELAX"
        else:
            from Prog.src.test_runner import StepKind, TestPlan

            plan = {
                "DISCHARGE_ONLY": TestPlan.discharge_only,
                "CHARACTERIZATION": TestPlan.characterization,
                "BQ_LEARNING_PHYSICAL": TestPlan.bq_learning_physical,
            }[mode]()
            assert any(step.kind == StepKind.DISCHARGE for step in plan.steps)
            ctrl, psu, load, dmm = make_discharge_controller()
            ctrl._state = DischargeState.DISCHARGE_CC_RUN
            expected_end = "DISCHARGE_DONE"
        ctrl._profile.cell_count = cells
        ctrl._profile.nominal_voltage_V = 2.0 * cells
        minimum = 1.8 * cells
        dmm.voltage_V = minimum
        load.input_commanded_on = True
        ctrl.advance(1.0)
        assert ctrl.state.name == expected_end
        assert not load.input_commanded_on
        load.input_commanded_on = True
        values = [minimum - 0.001, minimum + 0.001]
        if reverse:
            values.reverse()
        calls, events = [], []
        off_load, off_psu = load.input_off, psu.all_outputs_off
        def load_off():
            calls.append("load")
            off_load()
        def psu_off():
            calls.append("psu")
            off_psu()
        monkeypatch.setattr(load, "input_off", load_off)
        monkeypatch.setattr(psu, "all_outputs_off", psu_off)
        ctrl.on_event = events.append
        on_before = sum("input_on" in call for call in load.calls)
        for voltage in values:
            dmm.voltage_V = voltage
            ctrl.advance(1.0)
        assert ctrl.state.name == "FAULT"
        assert ctrl.fault_reason == "BATTERY_UNDERVOLTAGE"
        assert calls[:2] == ["load", "psu"]
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert any(e["event_code"] == "BATTERY_UNDERVOLTAGE" for e in events)
        ctrl.advance(1.0)
        assert ctrl.state.name == "FAULT"
        assert sum("input_on" in call for call in load.calls) == on_before
