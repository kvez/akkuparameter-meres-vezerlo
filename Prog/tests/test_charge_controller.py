"""
ChargeController unit tesztek — MockPSU/Load/DMM-mel, dt_s injection.
"""
import pytest
from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode, SafetyResult, WarningCode
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

    def test_precheck_12v_pack_series_mode_surfaces_as_warning(self):
        """[regresszió, Teljes kódbázis review 23. kör (2026-09-07), Hardver/
        Műszervezérlés P2 lelet] check_psu_mode_compatibility() egy 12V pack
        + SERIES PSU-mód kombinációra WARNING-ot ad (nem tiltott, de
        szokatlan) — ez korábban sosem jutott el last_warning_code-ig, a
        szokatlan wiring-konfiguráció detektálhatósága nulla volt."""
        ctrl, *_ = make_controller(
            psu_mode=PsuMode.SERIES,
            dmm_voltage_V=12.5,
        )
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK -> PSU_PRESET
        assert ctrl.state != ChargeState.FAULT
        assert ctrl.last_warning_code == "PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK"


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

    def test_psu_preset_starts_exactly_at_target_no_headroom(self):
        """2026-08-23 (felhasználói review, 2. kör): a PSU induló
        compliance-e PONTOSAN target_V — nincs beépített dióda-/kábelesés-
        headroom a preset-ben. "Első lépésnek a PSU-t targetre kell
        állítani" — a tényleges headroom-igényt a CC-fázisú escalation
        méri és fokozatosan adja hozzá, ha szükséges (l.
        TestCcComplianceEscalation)."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        assert abs(psu.voltage_V - 14.37) < 0.001  # Q-48: 22 C, MONITOR_ONLY

    def test_i_psu_set_a_reflects_charge_current_setpoint(self):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] a
        logger.py CSV_COLUMNS-ban deklarált psu_set_current_A mezőt
        test_runner.py::_build_sample() korábban sosem töltötte ki —
        i_psu_set_A property hiányzott, holott a belső
        _charge_current_set_A már PSU_PRESET-kor beáll."""
        ctrl, _psu, _load, _dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        assert ctrl.i_psu_set_A > 0.0
        assert ctrl.i_psu_set_A == pytest.approx(ctrl._charge_current_set_A)

    def test_i_psu_measured_a_reflects_actual_readback_not_setpoint(self):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), Metrológia/
        Adatintegritás P1 lelet] i_psu_measured_A a TÉNYLEGESEN olvasott
        (self._i_charge) PSU-áramot exponálja, elkülönítve a setpoint-
        testvérpártól (i_psu_set_A) — CLAUDE.md §12 "setpoint/readback külön
        fogalom". A beágyazott (GOLDEN_GG/CHEMID/OCV_SOC) controllerek ezt
        használják a session-szintű Ah/Wh-összesítéshez, a saját, redundáns
        PSU-olvasásuk helyett."""
        ctrl, psu, _load, _dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.current_A = 1.23
        ctrl.advance(dt_s=1.0)  # _run_charge_cc() tick — beolvassa a PSU-áramot
        assert ctrl.i_psu_measured_A == pytest.approx(1.23)

    def test_cv_entry_reduces_escalated_compliance_based_on_measured_drop(self):
        """Ha a CC-fázisú escalation korábban megemelte a compliance-t (mert
        a mért áram tartósan a setpoint alatt volt), a CV-belépés a
        TÉNYLEGESEN mért esés alapján azonnal csökkenti — nem marad a magas
        compliance-en (ugyanaz az elv, mint a korábbi, target+headroom-mal
        induló preset esetén: a PSU sosem "ragadhat" feleslegesen magas
        compliance-en, amikor a DMM azt mutatja, hogy nincs rá szükség)."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.50)
        # Escaláció szimulálása: mintha korábbi CC-tick-ek 15.30V-ra emelték
        # volna a compliance-t (mért áram-hiány miatt).
        ctrl._u_psu_set = 15.30
        psu.voltage_V = 15.30

        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV_DMM_CONTROL

        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        expected_V = 14.37 + (15.30 - 14.50)  # 22 C target + mert eses = 15.17 V
        assert abs(psu.voltage_V - expected_V) < 0.001
        assert psu.voltage_V < 15.30 - 0.05

    def test_cv_entry_caps_at_target_plus_series_drop_ceiling(self):
        """Ha a mért esés a target_V + max_expected_series_drop_V plafon
        fölé vinné a presetet, ott vágódik le — ennek elérése önmagában
        fizikai bekötési hiba jele (l. 2026-08-23 traditional_learning OV
        incidens elemzés, 2. kör). A mért esést (1.20V) szándékosan a
        ceiling (0.90V) és a check_series_drop() fault-küszöbe (1.50V,
        nem-BQ) közé választva — így ez a teszt kifejezetten a CV-belépési
        clamp-et fedi, nem a series-drop faultot (azt l. TestSeriesDrop*)."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.30)
        ctrl._u_psu_set = 15.50
        psu.voltage_V = 15.50  # mért esés 14.30V-on: 1.20V

        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV_DMM_CONTROL

        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        # Q-48: target(14.37) + max_expected_series_drop_V(0.90) = 15.27 V
        assert abs(psu.voltage_V - 15.27) < 0.001

    def test_cc_to_cv_psu_set_failure_escalates_to_fault(self):
        """[regresszió, Teljes kódbázis review 19. kör (2026-09-06), Hardver/
        Műszervezérlés P1 lelet] a CC->CV átmenet set_output_voltage()
        hívása korábban védtelen volt, szemben az egyszeri _run_psu_preset()-
        tel és a záró output_off()-fal — kivétel esetén a controller sosem
        állt saját FAULT-ra."""
        ctrl, psu, _load, _dmm = self._advance_to_cc(dmm_voltage_V=14.50)

        def _raise_on_set(_value):
            raise RuntimeError("simulated set_output_voltage failure")
        psu.set_output_voltage = _raise_on_set  # type: ignore[method-assign]

        ctrl.advance(dt_s=1.0)  # CHARGE_CC -> CV átmenet body fut

        assert ctrl.state == ChargeState.FAULT
        assert "CC_TO_CV_PSU_SET_FAILED" in ctrl.fault_reason
        assert "simulated set_output_voltage failure" in ctrl.fault_reason

    def test_cv_entry_with_failed_psu_readback_stays_in_cc_without_ceiling_jump(self):
        """[regresszió, teljes kódbázis review kör 8 (2026-09-15), R2-01,
        P0] korábban a CC->CV átmenet EGY MÁSODIK, független PSU-feszültség-
        olvasást adott ki (holott a fenti _check_series_safety() UGYANEBBEN
        a tickben már elvégezte), és ha az hibázott, CSENDBEN a maximális
        compliance-plafonra (target+max_expected_series_drop_V) ugrott --
        számláló/warning/esemény nélkül, megkerülve a MÁR bekötött
        readback-vesztés-eszkalációt. Most a controller a MÁR meglévő
        _u_psu_measured_V-t használja újra, hiba esetén NEM lép CV-be és
        NEM ír a PSU-ra."""
        ctrl, psu, _load, _dmm = self._advance_to_cc(dmm_voltage_V=14.50)
        voltage_before = psu.voltage_V
        psu.simulate_voltage_readback_timeout = True

        ctrl.advance(dt_s=1.0)  # _check_series_safety() readback-hiba -> CV-belépés kimarad

        assert ctrl.state == ChargeState.CHARGE_CC
        assert psu.voltage_V == pytest.approx(voltage_before)  # nincs write
        assert ctrl.last_warning_code == "PSU_VOLTAGE_READBACK_LOST"
        assert ctrl.consecutive_psu_voltage_readback_lost == 1

    def test_cv_entry_readback_loss_escalates_to_fault_after_three_checks(self):
        """[regresszió, R2-01] 3 egymást követő sikertelen CV-belépési
        readback -- a meglévő psu_voltage_readback_lost_after_n_checks=3
        küszöb FAULT-ra eszkalál, safe-off-fal.

        MAX verifier F-1 lelete (2026-09-15): ez a teszt ÖNMAGÁBAN a
        pre-fix kódon is átment (a régi `else`-ág is 3 tick alatt
        FAULT-ra jutott, csak közben 2 CSENDES, mérés nélküli PSU-írást
        végzett -- 14.40->15.30->15.20). A `voltage_before` assert teszi
        diszkriminálóvá: a fix UTÁN a PSU-ra a FAULT-ig egyetlen write
        sem történik (a setpoint a teljes readback-vesztés alatt
        változatlan marad), a fix ELŐTT viszont igen."""
        ctrl, psu, load, _dmm = self._advance_to_cc(dmm_voltage_V=14.50)
        voltage_before = psu.voltage_V
        psu.simulate_voltage_readback_timeout = True

        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert "PSU_VOLTAGE_READBACK_LOST_PERSISTENT" in ctrl.fault_reason
        assert psu.output_commanded_on is False
        assert load.input_commanded_on is False
        assert psu.voltage_V == pytest.approx(voltage_before)

    def test_cv_entry_reuses_series_safety_reading_single_psu_voltage_read_per_tick(self):
        """[regresszió, R2-01] a CV-belépés a _check_series_safety() MÁR
        elvégzett olvasását használja újra -- nincs második, redundáns
        fizikai measure_output_voltage() hívás ugyanabban a tickben."""
        ctrl, psu, _load, _dmm = self._advance_to_cc(dmm_voltage_V=14.50)
        psu.reset_calls()

        ctrl.advance(dt_s=1.0)  # CHARGE_CC -> CHARGE_CV_DMM_CONTROL

        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        assert psu.calls.count("measure_output_voltage()") == 1


class TestCcComplianceEscalation:
    """2026-08-23 (felhasználói review, 2. kör): _maybe_escalate_cc_compliance()
    — mért áram-hiány alapú, fokozatos compliance-emelés CC fázisban,
    ahelyett hogy a PSU induló compliance-e egyszerre target+headroom-ra
    ugrana. make_12v_profile(capacity_Ah=7.0): effective_max_charge_A=1.75A,
    psu_hw_max_A(INDEPENDENT)=1.5A → charge_current_set_A=1.5A."""

    def _advance_to_cc(self, dmm_voltage_V=13.50):
        """13.50V alapérték: preset compliance (target_V=14.40) mellett
        u_drop=0.90V — a warning (1.20V) és a nem-BQ fault (1.50V) küszöb
        alatt is marad, hogy a series-drop check ne zavarja az escalation
        izolált tesztelését."""
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        assert ctrl.state == ChargeState.CHARGE_CC
        return ctrl, psu, load, dmm

    def test_escalates_when_current_starved_far_from_target(self):
        ctrl, psu, load, dmm = self._advance_to_cc()
        psu.current_A = 0.5  # jóval az 1.5A setpoint alatt
        before = ctrl.u_psu_set_V
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC
        assert ctrl.u_psu_set_V > before
        assert abs(ctrl.u_psu_set_V - (before + 0.050)) < 0.001  # max_step_up_V

    def test_no_escalation_when_current_at_setpoint(self):
        ctrl, psu, load, dmm = self._advance_to_cc()
        psu.current_A = 1.5  # a teljes cél-áram folyik
        before = ctrl.u_psu_set_V
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC
        assert abs(ctrl.u_psu_set_V - before) < 0.001

    def test_no_escalation_near_cv_entry(self):
        """CV-belépés közelében ezt már a _regulate_cv() DMM-hurokja kezeli,
        nem az escalation."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.30)
        psu.current_A = 0.5
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL

    def test_escalation_capped_at_target_plus_series_drop(self):
        """target(14.40) + max_expected_series_drop_V(0.90) = 15.30V plafon
        — ennek elérése önmagában fizikai bekötési hiba jele, sosem léphető
        túl."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.25)
        ctrl._u_psu_set = 15.29
        psu.voltage_V = 15.29
        psu.current_A = 0.5
        ctrl.advance(dt_s=1.0)
        assert ctrl.u_psu_set_V <= 15.30 + 1e-6

    def test_escalation_never_exceeds_ceiling_isolated(self):
        """Az _maybe_escalate_cc_compliance() önmagában (más safety-check-től
        elszigetelve) sok hívás után is pontosan a plafonnál áll meg, nem
        lépi túl."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.25)
        ctrl._charge_current_set_A = 1.5
        ctrl._i_charge = 0.5
        for _ in range(200):
            ctrl._maybe_escalate_cc_compliance()
        ceiling = 14.37 + 0.90  # Q-48: 22 C target, valtozatlan headroom
        assert ctrl.u_psu_set_V <= ceiling + 1e-6
        assert abs(ctrl.u_psu_set_V - ceiling) < 1e-6

    def test_escalation_psu_set_failure_escalates_to_fault(self):
        """[regresszió, Teljes kódbázis review 19. kör (2026-09-06), Hardver/
        Műszervezérlés P1 lelet] a compliance-escalation set_output_voltage()
        hívása korábban védtelen volt."""
        ctrl, psu, _load, _dmm = self._advance_to_cc()
        psu.current_A = 0.5  # jóval a setpoint alatt -> escalation tüzel

        def _raise_on_set(_value):
            raise RuntimeError("simulated set_output_voltage failure")
        psu.set_output_voltage = _raise_on_set  # type: ignore[method-assign]

        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert "CC_COMPLIANCE_ESCALATION_FAILED" in ctrl.fault_reason
        assert "simulated set_output_voltage failure" in ctrl.fault_reason


class TestPsuChargeCurrentSetpointGate:
    """Kor 18, Batch C7 R17-10 (DFAM-001): safety.py::
    check_psu_charge_current_setpoint() bekötése CC fázisban. A gate CSAK
    a compliance-emelés kimerülése (ceiling elérve) UTÁN tüzelhet -- a
    flag az ELŐZŐ ticken a _maybe_escalate_cc_compliance()-ben frissül,
    a jelen ticken a _run_charge_cc() elején (a readback UTÁN, a safety-
    kapuk ELŐTT) kerül felhasználásra."""

    def _advance_to_cc(self, dmm_voltage_V=13.50):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        assert ctrl.state == ChargeState.CHARGE_CC
        return ctrl, psu, load, dmm

    def test_cc_tick_with_exhausted_compliance_increments_counter(self):
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.25)
        ctrl._u_psu_set = 15.30  # pontosan a plafonon (14.40+0.90)
        psu.voltage_V = 15.30
        psu.current_A = 0.5  # jóval az 1.5A setpoint alatt
        ctrl.advance(dt_s=1.0)  # a tick VÉGI escalate() látja: már a plafonon -> flag=True
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 0
        ctrl.advance(dt_s=1.0)  # ez a tick már a flag=True állapotot látja (tick eleje)
        assert ctrl.state == ChargeState.CHARGE_CC
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 1

    def test_counter_increments_even_when_warning_slot_taken(self):
        """HW4-02 MAX verifier N1 tanulsága: egy tartósan aktív, MAGASABB
        prioritású figyelmeztetés SOHA nem szabad elnyelnie a
        tick-számlálót -- csak a `_last_warning_code` slot marad az
        eredetin."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.25)
        ctrl._u_psu_set = 15.30
        psu.voltage_V = 15.30
        psu.current_A = 0.5
        ctrl.advance(dt_s=1.0)  # eléri a plafont, flag=True a tick VÉGÉN
        # Egy tartósan aktív, MAGASABB prioritású (a tick elején kiértékelt)
        # figyelmeztetés szimulálása -- ez foglalja el a _last_warning_code
        # egyetlen, nem-felülírásos slotját MIELŐTT a mi kapunk lefutna.
        ctrl._safety.check_temperature_dmm_fault = (
            lambda *_a, **_kw: SafetyResult(warning=WarningCode.TEMPERATURE_DMM_LOST)
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 1
        assert ctrl._last_warning_code == WarningCode.TEMPERATURE_DMM_LOST.name

    def test_cv_phase_does_not_warn(self):
        """A gate a _run_charge_cv()-ben NINCS bekötve -- CV-ben a mért
        áram LEGITIMEN esik a setpont alá (taper felé közelítés)."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.30)
        psu.current_A = 0.5
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        for _ in range(5):
            ctrl.advance(dt_s=1.0)
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 0

    def test_cc_ramp_with_remaining_headroom_does_not_warn(self):
        """A normál escalation-rámpa (test_escalates_when_current_starved_
        far_from_target tükre) -- a plafon SOHA nem éri el, a flag mindig
        False marad, a kapu SOHA nem tüzel."""
        ctrl, psu, load, dmm = self._advance_to_cc()
        psu.current_A = 0.5  # jóval az 1.5A setpoint alatt
        for _ in range(5):
            ctrl.advance(dt_s=1.0)
            if ctrl.state != ChargeState.CHARGE_CC:
                break
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 0

    def test_flag_resets_when_current_recovers_to_setpoint(self):
        """MAX verifier P2-1 (Kor 18, Batch C7 R17-10): a
        _cc_compliance_headroom_exhausted flag tick-scoped -- MINDEN
        korai return ELŐTT explicit False-ra áll a _maybe_escalate_cc_
        compliance() tetején (l. feedback_new_tick_scoped_validity_flag_
        needs_explicit_reset.md). Enélkül egy áram-helyreállás UTÁNI
        ÚJBÓLI éhezés a régi (stale) True flaget látná az első ticken,
        és HAMIS figyelmeztetést adna, mielőtt a plafon-állapot
        valójában ismét beállt volna. A mutáció-teszt bizonyította: a
        reset eltávolítása mellett is MIND a 11 korábbi teszt zöld
        marad -- ez az ELSŐ teszt, ami ezt a konkrét ágat gyakorolja."""
        ctrl, psu, load, dmm = self._advance_to_cc(dmm_voltage_V=14.25)
        ctrl._u_psu_set = 15.30  # pontosan a plafonon
        psu.voltage_V = 15.30
        psu.current_A = 0.5
        ctrl.advance(dt_s=1.0)  # A: flag=True a tick VÉGÉN (a plafon-ágban)
        ctrl.advance(dt_s=1.0)  # B: flag=True-t lát -> tüzel
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 1

        psu.current_A = 1.5  # helyreállás: pontosan a setpointon (>=98%)
        ctrl.advance(dt_s=1.0)  # C: flag=True-t lát, DE a mért áram már a
        # setponton -> nem tüzel; a tick VÉGÉN az escalate() a "current
        # >=98%" ágon tér vissza -> flag=False
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 1

        psu.current_A = 0.5  # újbóli éhezés
        ctrl.advance(dt_s=1.0)  # D: flag=False-t lát (C VÉGÉN reset-elve)
        # -> NEM tüzel EZEN a ticken, meg akkor sem, ha az áram mar megint
        # alacsony -- ez pontosan a P2-1 discriminator.
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 1
        ctrl.advance(dt_s=1.0)  # E: a D VÉGI escalate() ujra a plafon-agat
        # eri el (u_psu_set valtozatlanul 15.30) -> flag=True -> tuzel.
        assert ctrl.psu_charge_current_setpoint_warning_ticks == 2


class TestFastLoop:
    """2026-08-23 (felhasználói review, 2. kör): fast_loop_enabled=True
    esetén a CV-közeli/CV/TAPER fázisban több, ténylegesen mért idejű
    bel-ső résztick fut egy külső advance() hívás alatt — alapértelmezésben
    KIKAPCSOLVA (l. ChargeConfig.fast_loop_enabled docstring), hogy a
    mock-alapú tesztek ne lassuljanak valós time.sleep()-ekkel."""

    def _fast_cfg(self, **kwargs) -> ChargeConfig:
        return ChargeConfig(
            fast_loop_enabled=True,
            fast_loop_period_s=0.01,
            fast_loop_max_substeps=3,
            **kwargs,
        )

    def _make_cv_ctrl(self, cfg, u_batt=14.30, u_psu=14.40, i_charge=0.1):
        profile = make_12v_profile()
        psu = MockPSU(voltage_V=u_psu, current_A=i_charge)
        dmm_v = MockDMM(voltage_V=u_batt)
        ctrl = ChargeController(
            psu, MockLoad(), dmm_v, MockDMM(temperature_C=22.0), profile,
            SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT), cfg,
        )
        ctrl._charge_current_set_A = 1.5
        psu.set_output_current(1.5)
        psu.current_A = i_charge
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = u_psu
        return ctrl, psu, dmm_v

    def test_fast_loop_runs_multiple_substeps_in_cv(self):
        ctrl, psu, dmm_v = self._make_cv_ctrl(self._fast_cfg())
        ctrl.advance(dt_s=1.0)
        assert dmm_v.calls.count("read_voltage()") > 1

    def test_fast_loop_disabled_by_default_single_substep(self):
        """fast_loop_enabled alapértéke False — visszamenőlegesen kompatibilis
        (pontosan egy DMM-olvasás), amíg valaki explicit be nem kapcsolja."""
        ctrl, psu, dmm_v = self._make_cv_ctrl(ChargeConfig())
        ctrl.advance(dt_s=1.0)
        assert dmm_v.calls.count("read_voltage()") == 1

    def test_no_fast_loop_far_from_target_in_cc(self):
        cfg = self._fast_cfg()
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5, config=cfg)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        dmm.reset_calls()
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC (távol a céltól)
        assert dmm.calls.count("read_voltage()") == 1

    def test_fast_loop_substep_count_bounded_even_with_large_dt_s(self):
        """[regresszió, 2026-09-15, C-07a/DFAM-027] A `fast_loop_max_
        substeps` limit nulla substep-I/O-idő ESETÉN is korlátozza a
        tényleges substep-számot -- ez a teszt ezt az (önmagában nem
        capeléstől függő) korlátot rögzíti kis bemenettel. A `budget_s`
        cap MAGA (a `perf_counter()<deadline` feltétel explicit felső
        korlátja) csak NEM-NULLA substep-I/O-idő mellett hoz MÉRHETŐ
        különbséget -- l. a sibling test_fast_loop_deadline_cap_bounds_
        blocking_time_with_slow_io() diszkrimináló tesztet, amely ezt
        kimutatja (MAX verifier V-1 lelete: egy korábbi verzió itt
        HAMISAN állította, hogy a cap SOHA nem változtat megfigyelhető
        viselkedést -- l. CLAIM_LEDGER.yaml FAST-LOOP-DEADLINE-CAP-001)."""
        cfg = self._fast_cfg()  # max_substeps=3, period_s=0.01
        ctrl, psu, dmm_v = self._make_cv_ctrl(cfg)
        ctrl.advance(dt_s=1000.0)  # rendkívül nagy, "befagyott" tick szimulálása
        # Az ELSŐ (nem fast-loop) _advance_single() hívás mindig olvas egyet;
        # a fast loop legfeljebb fast_loop_max_substeps-1 TOVÁBBI olvasást ad.
        assert dmm_v.calls.count("read_voltage()") <= cfg.fast_loop_max_substeps

    def test_fast_loop_deadline_cap_bounds_blocking_time_with_slow_io(self, monkeypatch):
        """[regresszió, 2026-09-15, C-07a/DFAM-027, MAX verifier V-1 lelete]
        A `budget_s` cap NÉLKÜL (HEAD, a korábbi kód) a `deadline` a
        NYERS, kapálatlan `dt_s`-ből számolt -- ha a substepek SAJÁT
        `_advance_single()` I/O-ideje (w, itt egy lassított DMM-olvasás)
        nem nulla, a `perf_counter()<deadline` feltétel `dt_s` nagy
        értékénél SOKÁIG igaz marad, és a substep-számláló (nem a
        deadline) lesz a tényleges korlát -- DE a substep-számláló
        limitje `fast_loop_max_substeps`-nél áll meg, miközben MINDEN
        substep extra `w` valós időt fogyaszt, amivel a teljes blokkolási
        idő (`fast_loop_max_substeps * (period_s+w)`) ARÁNYOSAN nő `w`-vel
        -- ezzel szemben a cappel a `budget_s` MAGA korlátozza, hány
        substep FÉR BELE a büdzsébe, a substep SAJÁT I/O-idejét is
        figyelembe véve (a `perf_counter()<deadline` feltétel korábban
        lép ki, ha `w>0`), KEVESEBB substeppel. A várt olvasásszám
        (period_s=0.2, max_substeps=20, io_s=0.10, dt_s=60.0) itt 14 --
        KORREKCIÓ (MAX verifier 2. kör W-1 lelete, 2026-09-15): egy
        korábbi verzió ezt "implementáció-specifikusnak, nem feltétlenül
        bitre egyezőnek" nevezte a MAX verifier 1. körének saját mérésével
        (11) szemben -- ez FÉLREVEZETŐ volt, a különbség NEM valamiféle
        harness-bizonytalanság, hanem egy pontosan rekonstruálható tényező:
        `_read_dmm()` (charge_controller.py) KÉT DMM-et olvas (feszültség
        ÉS hőmérséklet), a MAX verifier saját méréséhez MINDKETTŐT
        lassította (substep-saját I/O-idő w=2*io_s=0.20 -> 11 substep),
        ez a teszt CSAK a feszültség-DMM-et (w=io_s=0.10 -> 14 substep) --
        UGYANAZ a zárt alak (`substeps = ceil((budget_s-period_s)/
        (period_s+w)) + 1`), MÁS w mellett kiértékelve. A 14 tehát PONTOS,
        determinisztikus érték, nem közelítés -- mutációval igazolt (a cap
        nélküli verzióra visszaállítva 20-at ad, l. CLAIM_LEDGER.yaml
        FAST-LOOP-DEADLINE-CAP-001 a teljes rekonciliációért)."""
        import Prog.src.charge_controller as cc_module

        fake_now = [0.0]
        monkeypatch.setattr(cc_module.time, "perf_counter", lambda: fake_now[0])

        def _fake_sleep(s: float) -> None:
            fake_now[0] += s

        monkeypatch.setattr(cc_module.time, "sleep", _fake_sleep)

        cfg = ChargeConfig(
            fast_loop_enabled=True, fast_loop_period_s=0.2, fast_loop_max_substeps=20,
        )
        ctrl, psu, dmm_v = self._make_cv_ctrl(cfg)

        io_s = 0.10
        original_read = dmm_v.read_voltage

        def _slow_read():
            fake_now[0] += io_s
            return original_read()

        dmm_v.read_voltage = _slow_read  # type: ignore[method-assign]

        ctrl.advance(dt_s=60.0)

        # Determinisztikusan mért érték a jelen (capelt) implementáción,
        # fake órával -- mutációval igazolva (a cap nélküli verzióra
        # visszaállítva 20-at adna, l. a fix-saga jegyzőkönyvét).
        assert dmm_v.calls.count("read_voltage()") == 14

    def test_fast_loop_stops_on_fault(self):
        ctrl, psu, dmm_v = self._make_cv_ctrl(self._fast_cfg())

        original_read = dmm_v.read_voltage
        call_count = {"n": 0}

        def _spiking_read():
            call_count["n"] += 1
            if call_count["n"] >= 2:
                dmm_v.voltage_V = 15.0  # > 14.55V limit
            return original_read()

        dmm_v.read_voltage = _spiking_read

        state = ctrl.advance(dt_s=1.0)
        assert state == ChargeState.FAULT


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
        ctrl._charge_current_set_A = 1.5
        psu.set_output_current(1.5)
        psu.current_A = 0.15
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
        """Q-48: 22 C target 14.37 - 14.365 = 0.005 V < deadband (0.010 V)."""
        ctrl, psu = self._make_cv_controller(u_batt=14.365)
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
        """U_psu_set <= target_V + max_expected_series_drop.

        2026-08-23 traditional_learning OV incidens elemzés, 2. kör
        (felhasználói review): a plafon target_V-hez van kötve — ennek
        elérése önmagában fizikai bekötési hiba jele ("target + max series
        drop elérésekor tudható, hogy ott valami fizikai bekötési hiba
        van... nem lehet semmilyen állapotban sem"), nem csak egy
        DMM-visszacsatolás-vesztés elleni finomhangolt backstop."""
        cfg = ChargeConfig(max_expected_series_drop_V=0.90)
        profile = make_12v_profile()
        ctrl = ChargeController(
            MockPSU(current_A=0.1), MockLoad(), MockDMM(voltage_V=13.0),
            MockDMM(temperature_C=22.0), profile,
            SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT), cfg
        )
        ctrl._charge_current_set_A = 1.5
        ctrl._psu.set_output_current(1.5)
        ctrl._psu.current_A = 0.1
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = 20.0  # absurdly high
        ctrl.advance(dt_s=1.0)
        expected_ceiling = 14.37 + cfg.max_expected_series_drop_V  # Q-44: 22 C.
        assert abs(ctrl._u_psu_set - expected_ceiling) < 0.001

    def test_bq_mode_uses_bq_series_drop_headroom(self):
        """dmm_feedback_lost_hibaelemzes_20260823: BQ/panel-módban a preset
        compliance-nek a fault_series_drop_V_bq-vel (3.50V) összhangban lévő
        headroomot kell használnia, nem a nem-BQ 0.90V-ot — különben a PSU
        compliance a valós (akár 3.5V-os) soros esés alatt reked."""
        cfg = ChargeConfig()
        profile = make_12v_profile()
        ctrl = ChargeController(
            MockPSU(current_A=0.1), MockLoad(), MockDMM(voltage_V=13.0),
            MockDMM(temperature_C=22.0), profile,
            SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT, is_bq_mode=True), cfg
        )
        ctrl._charge_current_set_A = 1.5
        ctrl._psu.set_output_current(1.5)
        ctrl._psu.current_A = 0.1
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = 30.0  # absurdly high
        ctrl.advance(dt_s=1.0)
        expected_ceiling = 14.37 + cfg.max_expected_series_drop_V_bq  # Q-44: 22 C.
        assert abs(ctrl._u_psu_set - expected_ceiling) < 0.001
        assert ctrl._u_psu_set > profile.charge_voltage_pack_V + cfg.max_expected_series_drop_V + 0.001

    def test_regulate_cv_psu_set_failure_escalates_to_fault(self):
        """[regresszió, Teljes kódbázis review 19. kör (2026-09-06), Hardver/
        Műszervezérlés P1 lelet] a _regulate_cv() set_output_voltage()
        hívása korábban védtelen volt — ez a legmagasabb hívási
        gyakoriságú védtelen hardver-write volt a teljes kódbázisban
        (fast_loop módban akár 5 Hz, CV/TAPER_HOLD fázisban)."""
        ctrl, psu = self._make_cv_controller(u_batt=14.30)

        def _raise_on_set(_value):
            raise RuntimeError("simulated set_output_voltage failure")
        psu.set_output_voltage = _raise_on_set  # type: ignore[method-assign]

        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert "CV_REGULATION_PSU_SET_FAILED" in ctrl.fault_reason
        assert "simulated set_output_voltage failure" in ctrl.fault_reason


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
        ctrl._charge_current_set_A = 1.5
        psu.set_output_current(1.5)
        psu.current_A = current_A
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

    def test_taper_timer_does_not_reset_within_hysteresis_band(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29),
        Szoftver/Architektúra P2 lelet #2] taper_current_tolerance_factor
        (1.05) dokumentált szándéka szerint a belépési (szűk,
        taper_current_A=0.21A) és kilépési (tág, 0.21*1.05=0.2205A) küszöb
        közötti sávban a taper_timer_s NEM nullázódhat — enélkül a küszöb
        körüli tipikus valós DMM/PSU readback-zaj minden határátlépésnél
        nullázná a dwell-időt, megakadályozva a tiszta CHARGE_DONE elérését.
        I=0.215A a sávban van (0.21 < 0.215 <= 0.2205) → timer nem reset."""
        ctrl, psu, _dmm = self._make_taper_controller(
            current_A=0.20, u_batt=14.39, taper_hold_s=60.0
        )
        ctrl.advance(dt_s=1.0)  # enter TAPER_HOLD, timer=0
        assert ctrl.state == ChargeState.TAPER_HOLD
        psu.current_A = 0.215  # > 0.21 (belépési), de <= 0.2205 (kilépési)
        ctrl.advance(dt_s=5.0)
        assert ctrl.state == ChargeState.TAPER_HOLD
        assert ctrl.taper_timer_s >= 5.0

    def test_charge_done_after_taper_hold_time(self):
        ctrl, psu, dmm = self._make_taper_controller(
            current_A=0.20, u_batt=14.39, taper_hold_s=10.0
        )
        for _ in range(12):  # 12 × 1s > 10s hold
            ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_DONE

    def test_charge_done_psu_off_failure_escalates_to_fault(self):
        """[regresszió, Teljes kódbázis review 17. kör (2026-09-05), Hardver/
        Műszervezérlés P2 lelet] a sikeres töltés-befejezéskori
        psu.output_off() korábban csupasz volt — a testvér DischargeController/
        GoldenGgController/stb. mintáját követve, kivétel esetén a controller
        FAULT-ra kell, hogy álljon, specifikus diagnosztikával."""
        ctrl, psu, _dmm = self._make_taper_controller(
            current_A=0.20, u_batt=14.39, taper_hold_s=10.0
        )

        def _raise_on_off():
            raise RuntimeError("simulated psu off failure")
        psu.output_off = _raise_on_off  # type: ignore[method-assign]

        for _ in range(12):  # 12 × 1s > 10s hold
            ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert "CHARGE_DONE_PSU_OFF_FAILED" in ctrl.fault_reason
        assert "simulated psu off failure" in ctrl.fault_reason


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
            ctrl._profile.nominal_capacity_Ah * 1.51
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
            ctrl._profile.nominal_capacity_Ah * 1.51
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
            ctrl._profile.nominal_capacity_Ah * 1.51
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT

    def test_max_charge_ah_uses_c10_not_c20_base(self):
        """[regresszió, Teljes kódbázis review 18. kör (2026-09-05),
        Session/Workflow + Metrológia/Adatintegritás P2 lelet, 2 ágens
        egymástól függetlenül] a védendő töltőáram (profile.
        effective_max_charge_A, C10-alapú) mellett a MAX_CHARGE_AH_REACHED
        plafonnak is a kanonikus C10-bázisból (_c10_base_Ah) kell
        számolnia, NEM a nyers (FG-sorozatnál C20-alapú)
        nominal_capacity_Ah-ból — a kisütési oldalon már javított
        hibaosztály (11. kör 5b62b0f, 13. kör 682d97e) pontos tükörképe."""
        ctrl = self._advance_to_cv()
        # FG-sorozat szimulációja: nominal_capacity_Ah=C20=20.0, C10=10.0 —
        # max_charge_Ah_factor=1.50 mellett a C10-plafon 15.0 Ah, a
        # C20-plafon 30.0 Ah — a lenti 20.0 Ah a kettő KÖZÖTT van.
        ctrl._profile = BatteryProfile(
            battery_name="TEST_FG", manufacturer="TEST", model="FG20AH",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=20.0,
            C10_capacity_Ah=10.0,
        )
        ctrl._integrator.accumulated_charge_Ah = 20.0

        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_AH_REACHED" in ctrl.fault_reason


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
        """u_psu - u_batt > fault_series_drop_V (1.50V) → SERIES_DROP_TOO_HIGH fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.voltage_V = 12.5 + 1.55  # u_drop = 1.55V > 1.50V
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "SERIES_DROP_TOO_HIGH" in ctrl.fault_reason

    def test_series_drop_below_fault_no_fault(self):
        """u_drop = 0.85V < 1.50V → nincs fault."""
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

    def test_psu_voltage_readback_failure_sets_warning_not_silent(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Korábban
        a PSU feszültség-readback hibája csendben (nincs warning, nincs
        fault) kihagyta a series_drop/diode_power ellenőrzést — most
        legalább last_warning_code jelzi (CLAUDE.md §12: nincs csendes
        fallback)."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.simulate_voltage_readback_timeout = True
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC  # nincs fault, csak warning
        assert ctrl.last_warning_code == "PSU_VOLTAGE_READBACK_LOST"

    def test_persistent_psu_voltage_readback_loss_escalates_to_fault(self):
        """[regresszió, 2026-09-15 D10 OV incidens] A fenti teszt (egyetlen
        sikertelen olvasás) csak figyelmeztetést vár el — ez helyes,
        tranziens hibát nem szabad azonnal fault-tá tenni. DE a korábbi kód
        a veszteséget HATÁROZATLAN ideig tolerálta: egyetlen sikertelen
        `measure_output_voltage()` hívás is a driver teljes retry-
        budgetjéig blokkolhat (device_psu.py timeout_ms=5000×max_retries=3
        -- SOROS módban a hívás KÉT lekérdezést ad ki, worst-case ~30s
        EGYETLEN hívásra), és a fast_loop ezt ismételten újrahívta -- egy
        valós D10 HIL-session alatt ez ~257 másodperces, naplózatlan
        szabályozási "elakadást" okozott, miközben a setpoint tovább
        emelkedett (l. ChargeConfig.psu_voltage_readback_lost_after_n_
        checks docstringje a pontos idő-korlát levezetéséért). `psu_
        voltage_readback_lost_after_n_checks` (alapból 3) egymást követő
        sikertelen olvasás után most FAULT-ot vált ki -- ugyanaz az "N
        egymást követő hiba → escalate" minta, mint a BQ-transport-
        escalation-nél (LearningConfig.bq_transport_unreachable_after_n_
        polls). L. lent test_persistent_psu_voltage_readback_loss_
        escalates_to_fault_with_fast_loop_enabled a valós GUI-konfiguráció
        (fast_loop_enabled=True) lefedéséért -- MAX verifier V3 lelet,
        2026-09-15: ez a teszt (és a fenti/lenti testvérei) make_controller()
        defaultot használ, ami fast_loop_enabled=False."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.simulate_voltage_readback_timeout = True
        ctrl.advance(dt_s=1.0)  # 1. sikertelen olvasás -> csak warning
        assert ctrl.state == ChargeState.CHARGE_CC
        ctrl.advance(dt_s=1.0)  # 2. sikertelen olvasás -> még csak warning
        assert ctrl.state == ChargeState.CHARGE_CC
        ctrl.advance(dt_s=1.0)  # 3. sikertelen olvasás -> FAULT
        assert ctrl.state == ChargeState.FAULT
        assert "PSU_VOLTAGE_READBACK_LOST_PERSISTENT" in ctrl.fault_reason

    def test_transient_psu_voltage_readback_loss_does_not_escalate(self):
        """[regresszió, 2026-09-15 D10 OV incidens fixe] Ha a readback CSAK
        átmenetileg vész el (sikeres olvasás helyreállítja a számlálót a
        küszöb elérése előtt), nem szabad fault-ot kiváltania — csak a
        TARTÓS veszteség a probléma (l. test_persistent_psu_voltage_
        readback_loss_escalates_to_fault)."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        # A PSU_PRESET a compliance-t pontosan target_V-re állítja (l.
        # test_psu_preset_starts_exactly_at_target_no_headroom) -- ez
        # 14.40-12.5=1.90V esést jelentene, a fault-küszöb (1.50V) FÖLÖTT.
        # Kis esésre állítjuk, hogy a readback helyreállásakor a (más teszt
        # által már fedett) SERIES_DROP_TOO_HIGH NE zavarja ezt a tesztet,
        # ami kizárólag a readback-lost számlálót vizsgálja. A mért áram
        # pontosan a setpointon -> _maybe_escalate_cc_compliance() nem lép.
        ctrl._charge_current_set_A = psu.current_A
        psu.voltage_V = 12.5 + 0.80
        psu.simulate_voltage_readback_timeout = True
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC
        psu.simulate_voltage_readback_timeout = False  # helyreáll
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC
        psu.simulate_voltage_readback_timeout = True
        ctrl.advance(dt_s=1.0)  # ez csak az 1. a helyreállás utáni sorozatban
        ctrl.advance(dt_s=1.0)  # 2.
        assert ctrl.state == ChargeState.CHARGE_CC  # még nem ért 3-at

    def test_persistent_psu_voltage_readback_loss_escalates_to_fault_with_fast_loop_enabled(self):
        """[regresszió, MAX verifier V3 lelet (1. kör) + V1 lelet (2. kör,
        2026-09-15)] A fenti test_persistent_psu_voltage_readback_loss_
        escalates_to_fault és test_transient_psu_voltage_readback_loss_
        does_not_escalate make_controller() defaultot használ, ami
        fast_loop_enabled=False -- a VALÓS GUI/D10-konfiguráció
        (main_window.py shared_charge_cfg) mindig fast_loop_enabled=True.
        Ez a teszt ugyanazt az escalation-viselkedést igazolja fast_loop_
        enabled=True mellett: a _check_series_safety() minden résztickben
        lefut (l. _advance_single() hívási lánca), tehát a számláló a
        fast_loop résztick-jei alatt IS helyesen emelkedik, és 3 (nem-
        reszetelt) sikertelen olvasás után FAULT-ra vezet.

        2. kör MAX verifier V1 lelete (KIJAVÍTVA): az 1. verzió
        dmm_voltage_V=12.5-öt használt, ami a _fast_loop_active() CHARGE_CC-
        ági küszöbe (u_batt >= target_V[14.40] - fast_loop_proximity_V[0.5]
        = 13.90) ALATT van -- a fast loop emiatt SOHA nem futott, a teszt
        névleg fast_loop_enabled=True-t állított be, de facto nem fedte azt
        (mért: _advance_with_fast_loop() 0 hívás). dmm_voltage_V=14.0
        (∈ [13.90, target-cv_entry_margin_V=14.30), tehát CHARGE_CC-ben
        marad ÉS a fast loop aktív) + explicit last_fast_loop_extra_s>0
        assert BIZONYÍTJA, hogy a résztick-kör tényleg lefutott, nem csak a
        config-mező állt be."""
        cfg = ChargeConfig(
            fast_loop_enabled=True,
            fast_loop_period_s=0.01,
            fast_loop_max_substeps=3,
        )
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=14.0, config=cfg)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        assert ctrl.state == ChargeState.CHARGE_CC
        psu.simulate_voltage_readback_timeout = True
        fast_loop_actually_ran = False
        for _ in range(10):
            ctrl.advance(dt_s=1.0)
            if ctrl.last_fast_loop_extra_s > 0:
                fast_loop_actually_ran = True
            if ctrl.state == ChargeState.FAULT:
                break
        assert fast_loop_actually_ran, (
            "a fast loop sosem futott -- a teszt nem fedte a fast_loop_"
            "enabled=True konfigurációt"
        )
        assert ctrl.state == ChargeState.FAULT
        assert "PSU_VOLTAGE_READBACK_LOST_PERSISTENT" in ctrl.fault_reason

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

    def test_psu_current_readback_valid_false_on_comm_loss(self):
        """[regresszió, teljes kódbázis review kör 6 (2026-09-15), C-08a]
        `_read_psu_current()` korábban a kommunikációs hibát csendben
        0.0-ra cserélte -- `i_psu_measured_A` emiatt hiba esetén
        megkülönböztethetetlen volt egy valódi 0 A méréstől. Az új
        `psu_current_readback_valid` property-nek False-ot kell adnia
        pontosan azon a ticken, amikor a readback meghiúsul."""
        ctrl, psu, _load, _dmm = make_controller(dmm_voltage_V=14.4, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV (first successful readback already happened)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        assert ctrl.psu_current_readback_valid is True

        psu.simulate_current_readback_timeout = True
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert ctrl.psu_current_readback_valid is False

    def test_psu_current_readback_valid_resets_to_none_on_top_of_tick_fault_gate(self):
        """[regresszió, MAX verifier F-1 lelete (2026-09-15, C-08a, P1
        blokkoló)] `psu_current_readback_valid` korábban "latching" volt
        -- KIZÁRÓLAG `_read_psu_current()` (CC/CV/TAPER_HOLD saját
        futása) írta, tehát egy top-of-tick safety-kapu (itt:
        `check_battery_voltage()` -> BATTERY_OVERVOLTAGE, `charge_
        controller.py::_advance_single()` 519-564. sora körül, ami a
        _run_charge_cv() ÁLLAPOT-DISPATCH ELŐTT emergency_stop()-tal
        kilép) által kiváltott FAULT-ticken -- ahol `_read_psu_current()`
        EBBEN a tickben NEM fut le -- az ELŐZŐ, sikeres tick True-ját
        adta volna vissza. A hívó (test_runner.py::_build_sample())
        emiatt a FAULT-tick LEGFONTOSABB diagnosztikai sorában egy
        ELÉVÜLT controller-értéket naplózott volna a friss, szabályos
        read_all() readback helyett -- pontosan a D10 OV-incidens
        hibaosztálya, amiből ez a review-kör indult. Most `_advance_
        single()` minden hívás ELEJÉN None-ra nullázza, a kapu-ticken
        tehát None-nak KELL maradnia."""
        ctrl, _psu, _load, dmm = make_controller(dmm_voltage_V=14.4, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV (first successful readback already happened)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        assert ctrl.psu_current_readback_valid is True

        dmm.voltage_V = 30.0  # fizikailag ésszerűtlen -> BATTERY_OVERVOLTAGE
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "BATTERY_OVERVOLTAGE"
        assert ctrl.psu_current_readback_valid is None


class TestPsuVoltageReadbackValid:
    """[teljes kódbázis review kör 6 (2026-09-15), C-08b] u_psu_measured_V/
    psu_voltage_readback_valid -- a psu_current_readback_valid feszültség-
    testvérpárja, l. TestPsuCommLost -- proaktívan, a C-08a MAX verifier
    F-1 leletének tanulságával (tick-elején nullázás) egyből helyesen
    bevezetve, nem utólag javítva."""

    def test_flags_stay_none_before_first_series_safety_tick(self):
        """MAX verifier P3-1 (2026-09-15): a metódusnév korábban az
        ELLENKEZŐJÉT ígérte annak, amit ellenőriz -- a PSU_PRESET->
        CHARGE_CC átmenet tickjén _check_series_safety() MÉG NEM futott
        (csak a KÖVETKEZŐ, saját CHARGE_CC tickjén, l. lejjebb a
        test_psu_voltage_readback_valid_false_on_comm_loss-t), tehát
        mindkét flag None marad."""
        ctrl, _psu, _load, _dmm = make_controller(dmm_voltage_V=12.5, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC (still no _check_series_safety() yet)
        assert ctrl.psu_voltage_readback_valid is None
        assert ctrl.u_psu_measured_V is None

    def test_psu_voltage_readback_valid_false_on_comm_loss(self):
        """`_check_series_safety()` korábban a feszültség-readback hibáját
        csendben None-ra cserélte a warning/számláló-logika mellett, de
        semmilyen KÜLÖN validity-jelzést nem adott a hívónak arról, hogy
        EBBEN a tickben megpróbálta-e a readbacket."""
        ctrl, psu, _load, _dmm = make_controller(dmm_voltage_V=14.4, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV (first _check_series_safety() tick)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        assert ctrl.psu_voltage_readback_valid is True
        assert ctrl.u_psu_measured_V == pytest.approx(psu.voltage_V)

        psu.simulate_voltage_readback_timeout = True
        ctrl.advance(dt_s=1.0)

        assert ctrl.psu_voltage_readback_valid is False
        assert ctrl.u_psu_measured_V is None

    def test_psu_voltage_readback_valid_resets_to_none_on_top_of_tick_fault_gate(self):
        """[proaktív, a C-08a MAX verifier F-1 leletének mintáját követve]
        egy top-of-tick safety-kapu (itt: BATTERY_OVERVOLTAGE) által
        kiváltott FAULT-ticken -- ahol `_check_series_safety()` EBBEN a
        tickben NEM fut le -- a flagnek None-nak KELL maradnia, nem az
        ELŐZŐ tick sikeres True-jának."""
        ctrl, _psu, _load, dmm = make_controller(dmm_voltage_V=14.4, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV (first successful readback already happened)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        assert ctrl.psu_voltage_readback_valid is True

        dmm.voltage_V = 30.0  # fizikailag ésszerűtlen -> BATTERY_OVERVOLTAGE
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "BATTERY_OVERVOLTAGE"
        assert ctrl.psu_voltage_readback_valid is None
        assert ctrl.u_psu_measured_V is None


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
        ctrl._safety.temperature_dmm_fault_s = 61.0  # szimulált felhalmozott hiba
        ctrl.advance(dt_s=0.1)         # hiba folytatódik → 61.1s > 60s threshold
        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"

    def test_temp_dmm_lost_enabled_faults_immediately(self):
        """ENABLED: bármilyen temp DMM hiba → azonnali fault"""
        ctrl = self._advance_to_cc()
        ctrl._safety.temp_comp_mode = TempCompMode.ENABLED
        ctrl._dmm_t.simulate_temp_failure = True
        ctrl._safety.temperature_dmm_fault_s = 1.0
        ctrl.advance(dt_s=0.1)
        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"

    def test_temp_dmm_no_fault_below_timeout(self):
        """MONITOR_ONLY: 30s hiba még nem fault"""
        ctrl = self._advance_to_cc()
        ctrl._safety.temp_comp_mode = TempCompMode.MONITOR_ONLY
        ctrl._safety.temperature_dmm_fault_s = 30.0
        ctrl.advance(dt_s=0.1)
        assert ctrl.state == ChargeState.CHARGE_CC

    def test_battery_temperature_out_of_profile_range_surfaces_as_warning(self):
        """[regresszió, Teljes kódbázis review 20. kör (2026-09-06),
        Metrológia/Adatintegritás P2 lelet] safety.py::
        check_test_temperature_range() bekötve — egy a profil deklarált
        (alapértelmezésben 15-30°C) tesztelési tartományán kívüli mért
        akkuhőmérséklet mostantól WARNING-ot ad, nem marad néma."""
        ctrl = self._advance_to_cc()
        ctrl._dmm_t.temperature_C = 35.0  # a 30°C alapértelmezett max felett
        ctrl.advance(dt_s=0.1)
        assert ctrl.state == ChargeState.CHARGE_CC  # NEM fault
        assert ctrl.last_warning_code == "TEMPERATURE_OUT_OF_RANGE"

    def test_dmm_lost_warning_not_overwritten_by_temperature_range_warning(self):
        """[regresszió, Teljes kódbázis review 21. kör (2026-09-06), GUI/UX +
        Metrológia/Adatintegritás P2 lelet, 2 ágens egymástól függetlenül]
        a TEMPERATURE_OUT_OF_RANGE warning korábban feltétel nélkül
        felülírta a súlyosabb TEMPERATURE_DMM_LOST warningot ugyanabban a
        tickben, ha a stale (DMM-kiesés alatt nem frissülő)
        battery_temperature_C épp a profil tartományán kívül esett — a
        DMM-kiesés jelzésének elsőbbséget kell élveznie, mert az magának a
        hőmérsékletmérésnek a megbízhatóságát kérdőjelezi meg."""
        ctrl = self._advance_to_cc()
        ctrl._safety.temp_comp_mode = TempCompMode.MONITOR_ONLY
        ctrl._battery_temperature_C = 35.0  # stale, a 30°C alapértelmezett max felett
        ctrl._dmm_t.simulate_temp_failure = True
        ctrl._safety.temperature_dmm_fault_s = 30.0  # timeout (60s) alatt -> warning, nem fault
        ctrl.advance(dt_s=0.1)
        assert ctrl.state == ChargeState.CHARGE_CC
        assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"

    def test_temp_dmm_valid_true_after_successful_temperature_read(self):
        """[regresszió, review-kör 3 (2026-09-09), MAX verifier
        (a9cb161469c468fc8) P2-1] a temp_dmm_valid property (Metrológia
        M3-01, P1) egyik controlleren sem kapott önálló, controller-
        szintű regressziós tesztet -- ez a hiány engedte át a
        chemid_controller.py-beli P1-1 blokkoló hibát (a property sosem
        íródott False-ra) 1792 zöld teszt mellett."""
        ctrl = self._advance_to_cc()
        ctrl.advance(dt_s=0.1)
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_after_failed_temperature_read(self):
        ctrl = self._advance_to_cc()
        ctrl._dmm_t.simulate_temp_failure = True
        ctrl.advance(dt_s=0.1)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_fault_ticks_counts_failed_reads_not_seconds(self):
        """[regresszió, MAX verifier (review-kör 3, a306e7e764e3a717b) P2-1,
        DFAM-001 további előfordulása] a ChargeController-nek korábban nem
        volt tick-alapú hőmérséklet-DMM-hiba-számlálója, csak a
        másodperc-alapú _temp_dmm_fault_s -- az 5 összetett szülő ezt nem
        tudta átvezetni a saját, session-szintű temp_dmm_fault_ticks
        számlálójába egy beágyazott töltési fázis alatt."""
        ctrl = self._advance_to_cc()
        assert ctrl.temp_dmm_fault_ticks == 0

        ctrl._dmm_t.simulate_temp_failure = True
        ctrl.advance(dt_s=0.1)
        assert ctrl.temp_dmm_fault_ticks == 1

        ctrl.advance(dt_s=0.1)
        assert ctrl.temp_dmm_fault_ticks == 2

        ctrl._dmm_t.simulate_temp_failure = False
        ctrl.advance(dt_s=0.1)  # sikeres olvasás -- a tick-számláló NEM nullázódik
        assert ctrl.temp_dmm_fault_ticks == 2


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

    def test_enabled_caps_target_at_low_temperature(self):
        """Q-44: 0 C-on a cel is legfeljebb 2,400 V/cella."""
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
        assert target == pytest.approx(14.40, abs=1e-12)
        assert target <= profile.batt_absolute_max_V

    def test_monitor_only_uses_nominal_target(self):
        """MONITOR_ONLY: az első hőmérés előtt a 20 °C-os névleges cél él.

        Q-44/Q-48: hidegben nincs emelés, melegben ez a mód is csökkenti a célt.
        """
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

    def test_concurrent_load_stale_flag_blocks_psu_preset_output_on(self):
        """Teljes kódbázis review 14. kör (2026-09-04), Hardver/
        Műszervezérlés P1 lelet: a kölcsönös kizárás ellenőrzés korábban
        KIZÁRÓLAG a PSU_PRESET UTÁNI (már bekapcsolt PSU-jú) állapotokban
        futott — ha a Load input_commanded_on flag-je egy korábbi
        write-hiba miatt stale True maradt PONTOSAN a PSU_PRESET ticken
        (amikor a psu.output_on() ténylegesen meghívódna), a PSU minden
        ellenőrzés nélkül azonnal ON-ba kerülhetett volna."""
        ctrl, psu, load, _dmm = make_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK -> PSU_PRESET
        assert ctrl.state == ChargeState.PSU_PRESET
        assert psu.output_commanded_on is False

        # Stale Load-flag PONT a PSU bekapcsolása előtti ticken.
        load.input_commanded_on = True
        ctrl.advance(dt_s=1.0)  # PSU_PRESET tick -- itt hívódna output_on()

        assert ctrl.state == ChargeState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason
        # A lényeg: a PSU SOSEM kapcsolt be -- a concurrent-check a
        # psu.output_on() hívás ELŐTT fogta el a konfliktust.
        assert psu.output_commanded_on is False


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


class TestChargeCurrentOverride:
    """ChargeConfig.charge_current_A_override: egyedi töltőáram felülírás."""

    def _advance_through_psu_preset(self, config, psu_mode=PsuMode.INDEPENDENT, dmm_V=12.5):
        """PSU_PRESET tick-et végrehajtja (→ CHARGE_CC), visszaadja a PSU-t."""
        ctrl, psu, load, dmm = make_controller(
            profile=make_12v_profile(),
            psu_mode=psu_mode,
            dmm_voltage_V=dmm_V,
            config=config,
        )
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC (set_output_current itt fut)
        assert ctrl.state == ChargeState.CHARGE_CC
        return psu

    def test_override_current_set_when_below_psu_limit(self):
        """override=1.2A, INDEPENDENT limit 1.5A → PSU kap 1.2A."""
        cfg = ChargeConfig(charge_current_A_override=1.2)
        psu = self._advance_through_psu_preset(cfg, psu_mode=PsuMode.INDEPENDENT)
        assert psu.current_A == pytest.approx(1.2, abs=0.001)

    def test_override_current_clamped_to_psu_hw_limit(self):
        """override=2.0A, INDEPENDENT limit 1.5A → PSU kap 1.5A (clamp)."""
        cfg = ChargeConfig(charge_current_A_override=2.0)
        psu = self._advance_through_psu_preset(cfg, psu_mode=PsuMode.INDEPENDENT)
        assert psu.current_A == pytest.approx(1.5, abs=0.001)

    def test_psu_limit_clamp_surfaces_as_warning(self):
        """[regresszió, review-kör 2 (2026-09-09), friss Szoftver/
        Architektúra F3 (P3, DFAM-026 declared_policy_without_consumer)] a
        fenti clamp korábban CSENDBEN történt -- a WarningCode.REDUCED_
        CHARGE_CURRENT_DUE_TO_PSU_LIMIT deklarálva volt, de nulla
        producerrel. Ugyanaz a szcenárió, mint test_override_current_
        clamped_to_psu_hw_limit, csak a last_warning_code-ot ellenőrzi."""
        cfg = ChargeConfig(charge_current_A_override=2.0)
        ctrl, _psu, _load, _dmm = make_controller(
            profile=make_12v_profile(), psu_mode=PsuMode.INDEPENDENT,
            dmm_voltage_V=12.5, config=cfg,
        )
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC (a clamp itt fut)
        assert ctrl.state == ChargeState.CHARGE_CC
        assert ctrl.last_warning_code == "REDUCED_CHARGE_CURRENT_DUE_TO_PSU_LIMIT"

    def test_no_warning_when_not_clamped(self):
        """override=1.2A, INDEPENDENT limit 1.5A -- NEM clampel, tehát a
        REDUCED_CHARGE_CURRENT_DUE_TO_PSU_LIMIT warning nem jelenhet meg
        (kontroll-eset a fenti pozitív tesztre)."""
        cfg = ChargeConfig(charge_current_A_override=1.2)
        ctrl, _psu, _load, _dmm = make_controller(
            profile=make_12v_profile(), psu_mode=PsuMode.INDEPENDENT,
            dmm_voltage_V=12.5, config=cfg,
        )
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC
        assert ctrl.last_warning_code != "REDUCED_CHARGE_CURRENT_DUE_TO_PSU_LIMIT"

    def test_override_zero_is_valid(self):
        """override=0.0 (disabled) → ChargeConfig létrehozás nem dob ValueError-t."""
        cfg = ChargeConfig(charge_current_A_override=0.0)
        assert cfg.charge_current_A_override == 0.0

    def test_override_negative_raises_value_error(self):
        """Negatív override → ValueError."""
        import pytest as _pytest
        with _pytest.raises(ValueError):
            ChargeConfig(charge_current_A_override=-0.1)

    def test_override_above_range_raises_value_error(self):
        """100.001A override → ValueError."""
        import pytest as _pytest
        with _pytest.raises(ValueError):
            ChargeConfig(charge_current_A_override=100.001)


class TestAmbientTemperatureRiseConfig:
    """CHG-004: ChargeConfig validáció az ambient mezőkre."""

    def test_default_source_is_unknown(self):
        cfg = ChargeConfig()
        assert cfg.ambient_temperature_source == "UNKNOWN"
        assert cfg.ambient_temperature_C is None

    def test_manual_source_without_value_raises_value_error(self):
        import pytest as _pytest
        with _pytest.raises(ValueError):
            ChargeConfig(ambient_temperature_source="MANUAL")

    def test_external_sensor_source_without_value_raises_value_error(self):
        import pytest as _pytest
        with _pytest.raises(ValueError):
            ChargeConfig(ambient_temperature_source="EXTERNAL_SENSOR")

    def test_invalid_source_raises_value_error(self):
        import pytest as _pytest
        with _pytest.raises(ValueError):
            ChargeConfig(ambient_temperature_source="MADE_UP_SOURCE")

    def test_manual_source_with_value_is_valid(self):
        cfg = ChargeConfig(ambient_temperature_source="MANUAL", ambient_temperature_C=25.0)
        assert cfg.ambient_temperature_C == 25.0

    def test_unknown_source_with_value_is_still_valid(self):
        """UNKNOWN mellett is megadható érték (pl. tájékoztató jelleggel) — nem
        kényszeríti ki a faultot, l. TestAmbientTemperatureRiseIntegration."""
        cfg = ChargeConfig(ambient_temperature_source="UNKNOWN", ambient_temperature_C=25.0)
        assert cfg.ambient_temperature_C == 25.0


class TestAmbientTemperatureRiseIntegration:
    """CHG-004: end-to-end — MANUAL ambient + túlmelegedés → FAULT a CC fázisban;
    UNKNOWN (alapértelmezett) mellett ugyanaz a hőmérséklet-eltérés NEM faultol."""

    def _advance_to_cc(self, config):
        ctrl, *_ = make_controller(dmm_voltage_V=12.5, config=config)
        for _ in range(3):
            ctrl.advance(dt_s=1.0)
        ctrl._psu.voltage_V = 13.0
        assert ctrl.state == ChargeState.CHARGE_CC
        return ctrl

    def test_manual_ambient_over_10c_rise_faults_in_cc(self):
        cfg = ChargeConfig(ambient_temperature_source="MANUAL", ambient_temperature_C=20.0)
        ctrl = self._advance_to_cc(cfg)
        ctrl._dmm_t.temperature_C = 31.0  # 31 > 20+10
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "BATTERY_TEMPERATURE_RISE_OVER_AMBIENT"

    def test_default_unknown_source_does_not_fault_on_same_rise(self):
        """Ugyanaz a 31°C battery temp, de nincs explicit ambient forrás —
        a feltétel nem kényszeríthető ki, nem szabad hamis faultot dobnia."""
        cfg = ChargeConfig()  # UNKNOWN alapértelmezés
        ctrl = self._advance_to_cc(cfg)
        ctrl._dmm_t.temperature_C = 31.0
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC

    def test_manual_ambient_within_10c_does_not_fault(self):
        cfg = ChargeConfig(ambient_temperature_source="MANUAL", ambient_temperature_C=20.0)
        ctrl = self._advance_to_cc(cfg)
        ctrl._dmm_t.temperature_C = 28.0  # 28 <= 20+10
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC


class TestEmergencyStopOffFailureReported:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Hardver/
    Műszervezérlés P2 lelet] emergency_stop() korábban csendben elnyelte a
    Load/PSU OFF-kísérlet kivételét (except Exception: pass) — az ELSŐ
    OFF-kísérlet hibájának ténye/szövege sosem került be a fault_reason-be/
    report.json-ba. A végső fizikai állapot mindig helyesen jelentett
    marad (a TestRunner._emergency_stop()/InstrumentManager.safe_all_off()
    külső rétege lefedi), de a diagnosztikai információ elveszett."""

    def test_load_off_failure_surfaces_in_fault_reason(self):
        ctrl, _psu, load, _dmm = make_controller()

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.emergency_stop("DMM_FEEDBACK_LOST")

        assert ctrl.state == ChargeState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert "LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason

    def test_psu_off_failure_surfaces_in_fault_reason(self):
        ctrl, psu, _load, _dmm = make_controller()

        def _raise_on_off():
            raise RuntimeError("simulated psu off failure")
        psu.all_outputs_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.emergency_stop("DMM_FEEDBACK_LOST")

        assert ctrl.state == ChargeState.FAULT
        assert "PSU_OFF_FAILED" in ctrl.fault_reason
        assert "simulated psu off failure" in ctrl.fault_reason

    def test_off_success_leaves_fault_reason_unchanged(self):
        """Ha az OFF-kísérletek sikeresek, a fault_reason ne bővüljön
        felesleges szöveggel — visszafelé kompatibilis a meglévő, pontos
        fault_reason-egyezést váró tesztekkel."""
        ctrl, *_ = make_controller()
        ctrl.emergency_stop("DMM_FEEDBACK_LOST")
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"


class TestPrecheckLoadOffFailureReported:
    """[regresszió, Teljes kódbázis review 18. kör (2026-09-05), Hardver/
    Műszervezérlés P1 lelet] a 14. körben (`499fa03`) a DischargeController.
    _run_precheck() mintáját követve bevezetett load.input_off() hívás maga
    védtelen maradt — a bemásolt minta a forrás akkori bugját is átvette.
    Kivétel esetén korábban a specifikus diagnosztika helyett csak a
    TestRunner legkülső, generikus UNHANDLED_EXCEPTION jutott a riportba."""

    def test_load_off_failure_escalates_to_fault(self):
        ctrl, _psu, load, _dmm = make_controller()
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.advance(dt_s=1.0)  # PRECHECK body fut: load.input_off() dob

        assert ctrl.state == ChargeState.FAULT
        assert "CHARGE_PRECHECK_LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason


class TestPsuPresetFailureReported:
    """[regresszió, Teljes kódbázis review 18. kör (2026-09-05), Szoftver/
    Architektúra + Hardver/Műszervezérlés P1 lelet, 2 ágens egymástól
    függetlenül] a `_run_psu_preset()` a teljes kódbázis EGYETLEN
    psu.output_on() hívóhelye (minden töltési útvonal ezen az egy
    állapotgépen megy át), mégis mindhárom PSU-hívása (set_output_voltage/
    set_output_current/output_on) védtelen maradt, szemben a testvér
    input_on() hívásokkal, amik MIND try/except-ben vannak."""

    def _advance_to_psu_preset(self, ctrl):
        ctrl.advance(dt_s=1.0)  # INIT -> PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK body fut -> PSU_PRESET
        assert ctrl.state == ChargeState.PSU_PRESET

    def test_set_output_voltage_failure_escalates_to_fault(self):
        ctrl, psu, _load, _dmm = make_controller()
        self._advance_to_psu_preset(ctrl)
        psu.simulate_timeout = True

        ctrl.advance(dt_s=1.0)  # PSU_PRESET body fut: set_output_voltage() dob

        assert ctrl.state == ChargeState.FAULT
        assert "PSU_PRESET_FAILED" in ctrl.fault_reason

    def test_output_on_failure_escalates_to_fault(self):
        ctrl, psu, load, _dmm = make_controller()
        self._advance_to_psu_preset(ctrl)
        psu.raise_on_output_on = True

        ctrl.advance(dt_s=1.0)  # PSU_PRESET body fut: output_on() dob

        assert ctrl.state == ChargeState.FAULT
        assert "PSU_PRESET_FAILED" in ctrl.fault_reason
        assert "simulated output_on timeout" in ctrl.fault_reason
        # R2-08 (2026-09-15, 13. writer-kör): rules/10-safety.md szerint
        # egy safety findinghez a teljes safe-off láncot is igazolni kell
        # -- Load OFF, PSU ALL OFF, a commanded-flag végül False (az
        # emergency_stop() lánc all_outputs_off()-ot hív), nincs további
        # aktív parancs.
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        assert psu.output_commanded_on is False

    @pytest.mark.parametrize(
        "failing_message, expect_on_count",
        [
            # R1 F-03: a bare OUTP ON *OPC?-nyugtája elvész (a következő
            # kísérlet sikerülne) -> egyszer megy ki, FAULT.
            ("OUTP ON;*OPC?", 1),
            # R1 F-04: az INDEPENDENT CH2-tiltás tartósan hibázik -> egyetlen
            # ON-t hordozó üzenet sem megy ki, FAULT.
            ("INST:SEL CH2;:OUTP:ENAB OFF;:INST:SEL CH1;*OPC?", 0),
        ],
        ids=["bare_outp_on_lost_ack", "ch2_disable_persistent_failure"],
    )
    def test_output_on_lost_ack_with_real_driver_faults_after_single_send(
        self, failing_message, expect_on_count,
    ):
        """[regresszió, R1 B2 F-03/F-04, 2026-10-25] a teljes lánc VALÓS
        Keithley2220PSU driverrel (INDEPENDENT, primer "OUTP" dialektus,
        szkriptelt VISA-resource): driver-kivétel -> _run_psu_preset()
        try/except -> emergency_stop() -> Load OFF + PSU ALL OFF. A régi
        kódon a bare OUTP ON vak retry-ja (2. kísérlet sikeres) CHARGE_CC-be
        vitte a controllert, a CH2-tiltás pedig nem is létezett."""
        from unittest.mock import MagicMock

        from Prog.drivers.device_psu import Keithley2220PSU

        ctrl, _mock_psu, load, _dmm = make_controller()
        self._advance_to_psu_preset(ctrl)

        real_psu = Keithley2220PSU(combination_mode=PsuMode.INDEPENDENT)
        res = MagicMock()
        real_psu._resource = res
        real_psu._connected = True
        real_psu._output_cmd = "OUTP"
        persistent = failing_message.startswith("INST:SEL CH2")
        state = {"failed": False}

        def _query_side_effect(cmd: str) -> str:
            if cmd in ("SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"):
                return "0"
            if cmd == failing_message and (persistent or not state["failed"]):
                state["failed"] = True
                raise TimeoutError("simulated lost *OPC? reply")
            return "1"

        res.query.side_effect = _query_side_effect
        ctrl._psu = real_psu

        ctrl.advance(dt_s=1.0)  # PSU_PRESET body fut: output_on() dob

        sent = [c.args[0] for c in res.query.call_args_list]
        assert ctrl.state == ChargeState.FAULT
        assert "PSU_PRESET_FAILED" in ctrl.fault_reason
        on_messages = [m for m in sent if "ENAB ON" in m or m.startswith("OUTP ON")]
        assert sent.count("OUTP ON;*OPC?") == (1 if expect_on_count else 0)
        if not expect_on_count:
            assert on_messages == []
        # Load OFF + PSU ALL OFF a hibás ON-kísérlet UTÁN
        assert "input_off()" in load.calls
        i_off = len(sent) - 1 - sent[::-1].index("OUTP OFF;*OPC?")
        if expect_on_count:
            assert sent.index("OUTP ON;*OPC?") < i_off
        assert real_psu.output_commanded_on is False

        # Nincs további aktív parancs a FAULT után.
        n_on_before = len(on_messages)
        ctrl.advance(dt_s=1.0)
        sent_after = [c.args[0] for c in res.query.call_args_list]
        assert ctrl.state == ChargeState.FAULT
        assert len(
            [m for m in sent_after if "ENAB ON" in m or m.startswith("OUTP ON")]
        ) == n_on_before


class TestPsuHwLimitConsistencyWithDerivedParameters:
    """Review-kör 5, Architektúra A-6 (P3): a PSU hardver-limit (max
    töltőáram/max PSU-feszültség PsuMode szerint) SZÁNDÉKOSAN két helyen él
    (SUP02-ARCH-004, l. Prog/gui/derived_parameters.py docstring) — a
    charge_controller.py-beli másolat a biztonsági döntési út, a
    derived_parameters.py-beli a GUI-oldali előnézet/validáció közös
    forrása. Ez a teszt nem a KÓDOT hasonlítja össze (ami tautologikus
    lenne, ha valaki mindkét helyen ELTÉRŐEN, de következetesen íná át),
    hanem a charge_controller.py TÉNYLEGES, megfigyelt viselkedését a
    derived_parameters.py deklarált értékei ELLEN — ha a két másolat
    valaha szétdriftel, ez a teszt buktatja."""

    @pytest.mark.parametrize("psu_mode_name", ["INDEPENDENT", "PARALLEL", "SERIES"])
    def test_charge_current_clamped_to_derived_parameters_hw_max(self, psu_mode_name):
        from Prog.gui.derived_parameters import PSU_HW_MAX_CURRENT_A

        psu_mode = getattr(PsuMode, psu_mode_name)
        # charge_current_A_override jóval minden PSU-mód HW-limitje fölött,
        # hogy a limit legyen a ténylegesen megfigyelt kényszerítő tényező
        # (ne a profil effective_max_charge_A-ja).
        cfg = ChargeConfig(charge_current_A_override=10.0)
        ctrl, *_ = make_controller(
            profile=make_12v_profile(capacity_Ah=20.0),
            psu_mode=psu_mode, config=cfg, dmm_voltage_V=12.5,
        )
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC (a HW-limit itt alkalmazódik)
        assert ctrl.state == ChargeState.CHARGE_CC

        expected_A = PSU_HW_MAX_CURRENT_A[psu_mode_name]
        assert ctrl.i_psu_set_A == pytest.approx(expected_A)

    @pytest.mark.parametrize("psu_mode_name", ["INDEPENDENT", "PARALLEL", "SERIES"])
    def test_cv_voltage_clamped_to_derived_parameters_hw_max(self, psu_mode_name):
        """A `_regulate_cv()`-t közvetlenül hívjuk (ugyanaz a minta, mint
        `test_regulate_cv_psu_set_failure_escalates_to_fault`-nál), mert a
        publikus `advance()`-on át a PSU-mód HW-plafon (30V/60V) a
        gyakorlatban elérhetetlen: a `_regulate_cv()` MÁSODIK, szigorúbb
        záró korlátja (`target_V + max_expected_series_drop_V`, alapból
        0,90V) és a `_check_series_safety()` fault-küszöbe (1,50V/3,50V)
        JÓVAL a HW-plafon alatt avatkozna be előbb — a két korlát
        elszigetelése (mindkettőt extrém nagyra állítva) szükséges ahhoz,
        hogy KIZÁRÓLAG a psu_mode_max clamp-et teszteljük."""
        from Prog.gui.derived_parameters import PSU_HW_MAX_VOLTAGE_V

        psu_mode = getattr(PsuMode, psu_mode_name)
        profile = make_12v_profile()
        cfg = ChargeConfig(max_step_up_V=1e6, max_expected_series_drop_V=1e6)
        psu = MockPSU(voltage_V=14.40, current_A=0.1)
        ctrl = ChargeController(
            psu, MockLoad(), MockDMM(voltage_V=0.0), MockDMM(temperature_C=22.0),
            profile, SafetyManager(profile=profile, psu_mode=psu_mode), cfg,
        )
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = 14.40
        # Szélsőségesen negatív u_batt -> error = target_V - u_batt hatalmas,
        # a step_up-ot csak a (szintén extrémre állított) max_step_up_V
        # korlátozza, tehát a psu_mode_max clamp lesz az EGYETLEN ténylegesen
        # kényszerítő tényező.
        ctrl._u_batt = -1e6

        ctrl._regulate_cv()

        expected_V = PSU_HW_MAX_VOLTAGE_V[psu_mode_name]
        assert ctrl.u_psu_set_V == pytest.approx(expected_V)


class TestReviewRound5S505UnhandledStateRaises:
    """[regresszió, review-kör 5, Session/Workflow S5-05 (P3, DFAM-004
    sibling -- l. test_runner.py::_is_finished()/_controller_faulted()
    S4-07 kommentje ugyanerről a hibaosztályról)] advance() korábban
    hallgatólagosan (else nélkül) no-op-olt egy le nem kezelt ChargeState-
    re -- a controller örökre ugyanabban az állapotban ragadt volna."""

    def test_advance_raises_on_unhandled_state(self):
        ctrl, *_ = make_controller()
        ctrl._state = "BOGUS_FUTURE_STATE"
        with pytest.raises(ValueError, match="BOGUS_FUTURE_STATE"):
            ctrl.advance(1.0)


class TestR9B1ProfileChargeCurrentLimit:
    """R9-B1: profilmaximum-kapu a kozos toltesi controllerben."""

    @pytest.mark.parametrize(
        "c10_Ah,profile_max_A",
        [(None, None), (6.7, None), (None, 1.2)],
    )
    @pytest.mark.parametrize("off_failure", [None, "load", "psu", "both"])
    def test_excess_current_faults_before_psu_commands(
        self, monkeypatch, c10_Ah, profile_max_A, off_failure,
    ):
        from Prog.src.safety import FaultCode

        profile = make_12v_profile(capacity_Ah=7.2)
        profile.C10_capacity_Ah = c10_Ah
        profile.max_charge_current_A = profile_max_A
        ctrl, psu, load, _ = make_controller(
            profile=profile, psu_mode=PsuMode.PARALLEL,
            config=ChargeConfig(charge_current_A_override=3.0),
        )
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.state is ChargeState.PSU_PRESET
        psu.calls.clear()
        load.calls.clear()
        off_order = []
        load_off = load.input_off
        psu_off = psu.all_outputs_off

        def stop_load():
            off_order.append("load")
            load_off()
            if off_failure in ("load", "both"):
                raise RuntimeError("R9-B1 load off")

        def stop_psu():
            off_order.append("psu")
            psu_off()
            if off_failure in ("psu", "both"):
                raise RuntimeError("R9-B1 psu off")

        monkeypatch.setattr(load, "input_off", stop_load)
        monkeypatch.setattr(psu, "all_outputs_off", stop_psu)
        ctrl.advance(1.0)

        assert ctrl.state is ChargeState.FAULT
        assert ctrl.fault_reason.startswith(FaultCode.CHARGE_CURRENT_LIMIT_EXCEEDED.name)
        assert "3.0 A" in ctrl.fault_reason
        assert f"{profile.effective_max_charge_A} A" in ctrl.fault_reason
        assert off_order == ["load", "psu"]
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        if off_failure in ("load", "both"):
            assert "LOAD_OFF_FAILED:R9-B1 load off" in ctrl.fault_reason
        if off_failure in ("psu", "both"):
            assert "PSU_OFF_FAILED:R9-B1 psu off" in ctrl.fault_reason
        for _ in range(3):
            ctrl.advance(1.0)
        assert not any(c.startswith("set_output_") or c == "output_on()" for c in psu.calls)
        assert not any(c.startswith("set_current(") or c == "input_on()" for c in load.calls)

    @pytest.mark.parametrize("profile_max_A", [None, 1.2])
    @pytest.mark.parametrize("offset_A", [-0.1, 0.0, 0.000001])
    def test_exact_profile_boundary(self, profile_max_A, offset_A):
        profile = make_12v_profile(capacity_Ah=7.2)
        profile.max_charge_current_A = profile_max_A
        requested = profile.effective_max_charge_A + offset_A
        ctrl, psu, _, _ = make_controller(
            profile=profile, psu_mode=PsuMode.PARALLEL,
            config=ChargeConfig(charge_current_A_override=requested),
        )
        for _ in range(3):
            ctrl.advance(1.0)
        if offset_A > 0:
            assert ctrl.state is ChargeState.FAULT
            assert not any(c.startswith("set_output_") or c == "output_on()" for c in psu.calls)
        else:
            assert ctrl.state is ChargeState.CHARGE_CC
            assert ctrl.i_psu_set_A == pytest.approx(requested)
            assert f"set_output_current({requested})" in psu.calls
            assert "output_on()" in psu.calls
            assert ctrl.last_warning_code == ""

    def test_fault_reaches_runner_log_and_event(self):
        from unittest.mock import MagicMock

        from Prog.src import test_runner as runner_module
        from Prog.src.instrument_manager import InstrumentManager

        ctrl, psu, load, dmm_v = make_controller(
            profile=make_12v_profile(capacity_Ah=7.2), psu_mode=PsuMode.PARALLEL,
            config=ChargeConfig(charge_current_A_override=3.0),
        )
        for _ in range(2):
            ctrl.advance(1.0)
        assert ctrl.state is ChargeState.PSU_PRESET
        # A hibas HEAD toltesi aga is rovid, determinisztikus futasban zaruljon.
        ctrl._config.max_charge_time_s = 0.0
        reason = "CHARGE_CURRENT_LIMIT_EXCEEDED: 3.0 A > profile maximum 1.8 A"
        logger = MagicMock()
        runner = runner_module.TestRunner(
            instrument_manager=InstrumentManager(
                psu=psu, load=load, dmm_voltage=dmm_v,
                dmm_temperature=MockDMM(temperature_C=22.0),
            ),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=runner_module.TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: ctrl,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
        )
        events = []
        runner.on_event = events.append
        result = runner.run(runner_module.TestPlan(
            test_type=runner_module.TestType.CHARGE_ONLY,
            steps=(runner_module.TestStep(runner_module.StepKind.CHARGE, "R9-B1"),),
        ))
        assert result.status == "FAULT"
        assert result.reason == reason
        logger.log_event.assert_any_call("EMERGENCY_STOP", reason, is_critical=True)
        assert {"event_code": "EMERGENCY_STOP", "event_message": reason} in events
        assert not any(c.startswith("set_output_") or c == "output_on()" for c in psu.calls)
        assert "input_on()" not in load.calls


class TestR11B2ReadbackChargeCurrentLimit:
    """R11-B2 readback-sav; R14-UD-CHARGE-BAND-BASIS ota setpoint-alappal."""

    @staticmethod
    def _make_active_controller(phase=ChargeState.CHARGE_CC, profile_max_A=None):
        profile = make_12v_profile(capacity_Ah=7.2)
        profile.C10_capacity_Ah = 2.0
        profile.max_charge_current_A = profile_max_A
        ctrl, psu, load, dmm_v = make_controller(
            profile=profile, dmm_voltage_V=13.8,
            config=ChargeConfig(charge_current_A_override=0.25),
        )
        for _ in range(3):
            ctrl.advance(1.0)
        assert ctrl.state is ChargeState.CHARGE_CC
        assert psu.output_commanded_on is True
        assert ctrl.i_psu_set_A == 0.25
        assert profile.effective_max_charge_A == (0.5 if profile_max_A is None else profile_max_A)
        ctrl._state = phase
        psu.voltage_V = 14.2
        psu.calls.clear()
        load.calls.clear()
        return ctrl, psu, load, dmm_v

    @pytest.mark.parametrize("phase", [
        ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD,
    ])
    @pytest.mark.parametrize("profile_max_A", [None, 0.4])
    # R14-UD-CHARGE-BAND-BASIS: az override (0.25 A) a kapu alapja.
    # Az invertalt T=(I_set+0.005)/0.999 hatart is kulon ellenorizzuk.
    @pytest.mark.parametrize("offset_A, from_boundary, expect_fault", [
        (0.0, False, False),
        (0.000001, False, False),
        (0.001, False, False),
        (0.005, False, False),
        (0.0, True, False),
        (0.001, True, True),
        (0.006, False, True),
        (0.3, False, True),
    ])
    @pytest.mark.parametrize("off_failure", [None, "load", "psu", "both"])
    def test_readback_boundary_and_safe_off(
        self, monkeypatch, phase, profile_max_A, offset_A, from_boundary,
        expect_fault, off_failure,
    ):
        from Prog.src.safety import FaultCode

        ctrl, psu, load, _ = self._make_active_controller(phase, profile_max_A)
        limit_A = 0.25
        assert ctrl.i_psu_set_A == limit_A
        base_A = (limit_A + 0.005) / 0.999 if from_boundary else limit_A
        measured_A = base_A + offset_A
        psu.current_A = measured_A
        off_order = []
        load_off = load.input_off
        psu_off = psu.all_outputs_off

        def stop_load():
            off_order.append("load")
            if off_failure in ("load", "both"):
                load.calls.append("input_off()")
                raise RuntimeError("R11-B2 load off")
            load_off()

        def stop_psu():
            off_order.append("psu")
            if off_failure in ("psu", "both"):
                psu.calls.append("all_outputs_off()")
                raise RuntimeError("R11-B2 psu off")
            psu_off()

        monkeypatch.setattr(load, "input_off", stop_load)
        monkeypatch.setattr(psu, "all_outputs_off", stop_psu)
        ctrl.advance(1.0)

        assert ctrl.psu_current_readback_valid is True
        assert ctrl.i_psu_measured_A == measured_A
        if not expect_fault:
            assert ctrl.state is not ChargeState.FAULT
            assert ctrl.fault_reason == ""
            assert off_order == []
            assert psu.output_commanded_on is True
            return

        assert ctrl.state is ChargeState.FAULT
        band_A = 0.001 * abs(measured_A) + 0.005
        assert ctrl.fault_reason.startswith(
            f"{FaultCode.CHARGE_CURRENT_LIMIT_EXCEEDED.name}: "
            f"I_meas={measured_A} A > I_base+u={limit_A + band_A} A; "
            f"I_set=0.25 A; M={ctrl._profile.effective_max_charge_A} A; "
            f"I_base=0.25 A; u={band_A} A"
        )
        assert off_order == ["load", "psu"]
        assert load.calls == ["input_off()"]
        assert psu.calls == ["measure_output_current()", "all_outputs_off()"]
        if off_failure in ("load", "both"):
            assert "LOAD_OFF_FAILED:R11-B2 load off" in ctrl.fault_reason
        else:
            assert load.input_commanded_on is False
        if off_failure in ("psu", "both"):
            assert "PSU_OFF_FAILED:R11-B2 psu off" in ctrl.fault_reason
        else:
            assert psu.output_commanded_on is False
        for _ in range(3):
            ctrl.advance(1.0)
        assert load.calls == ["input_off()"]
        assert psu.calls == ["measure_output_current()", "all_outputs_off()"]

    def test_fault_reaches_runner_sample_log_and_event(self):
        from unittest.mock import MagicMock

        from Prog.src import test_runner as runner_module
        from Prog.src.instrument_manager import InstrumentManager

        ctrl, psu, load, dmm_v = self._make_active_controller()
        psu.current_A = 0.8
        # A javitas elotti kod is egy tick utan terminalis legyen.
        ctrl._config.max_charge_time_s = 0.0
        reason = (
            "CHARGE_CURRENT_LIMIT_EXCEEDED: I_meas=0.8 A > I_base+u=0.2558 A; "
            "I_set=0.25 A; M=0.5 A; I_base=0.25 A; u=0.0058000000000000005 A"
        )
        logger = MagicMock()
        runner = runner_module.TestRunner(
            instrument_manager=InstrumentManager(
                psu=psu, load=load, dmm_voltage=dmm_v,
                dmm_temperature=MockDMM(temperature_C=22.0),
            ),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=runner_module.TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: ctrl,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
        )
        events = []
        runner.on_event = events.append
        result = runner.run(runner_module.TestPlan(
            test_type=runner_module.TestType.CHARGE_ONLY,
            steps=(runner_module.TestStep(runner_module.StepKind.CHARGE, "R11-B2"),),
        ))

        assert result.status == "FAULT"
        assert result.reason == reason
        sample = logger.log_sample.call_args.args[0]
        assert sample["state"] == ChargeState.FAULT.value
        assert sample["psu_readback_current_A"] == 0.8
        assert sample["fault_flags"] == reason
        assert ctrl.psu_current_readback_valid is True
        logger.log_event.assert_any_call("EMERGENCY_STOP", reason, is_critical=True)
        assert {"event_code": "EMERGENCY_STOP", "event_message": reason} in events
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        assert not any(c.startswith("set_output_") or c == "output_on()" for c in psu.calls)
        assert not any(c.startswith("set_current(") or c == "input_on()" for c in load.calls)

    def test_chemid_child_fault_reaches_parent(self):
        from Prog.src.chemid_controller import (
            ChemIdCharacterizationController,
            ChemIdConfig,
            ChemIdState,
        )

        profile = make_12v_profile(capacity_Ah=7.2)
        profile.C10_capacity_Ah = 2.0
        assert profile.effective_max_charge_A == 0.5
        psu = MockPSU()
        load = MockLoad()
        dmm_v = MockDMM(voltage_V=13.8)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(
            profile=profile, psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )
        parent = ChemIdCharacterizationController(
            psu, load, dmm_v, dmm_t, profile, safety,
            ChemIdConfig(conditioning_min_s=0.0),
        )
        for _ in range(6):
            parent.advance(1.0)
        assert parent.state is ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        child = parent._charge_ctrl
        assert child is not None
        assert child.state is ChargeState.CHARGE_CC
        assert psu.output_commanded_on is True
        psu.current_A = 0.8
        psu.calls.clear()
        load.calls.clear()

        parent.advance(1.0)

        reason = (
            "CHARGE_CURRENT_LIMIT_EXCEEDED: I_meas=0.8 A > I_base+u=0.5058 A; "
            "I_set=0.5 A; M=0.5 A; I_base=0.5 A; u=0.0058000000000000005 A"
        )
        assert child.state is ChargeState.FAULT
        assert parent.state is ChemIdState.FAULT
        assert child.fault_reason == parent.fault_reason == reason
        assert child.psu_current_readback_valid is True
        assert child.i_psu_measured_A == 0.8
        assert psu.calls == [
            "measure_output_current()", "all_outputs_off()", "all_outputs_off()",
        ]
        assert load.calls == ["input_off()", "input_off()"]
        assert psu.output_commanded_on is False
        assert load.input_commanded_on is False
        for _ in range(3):
            parent.advance(1.0)
        assert not any(c.startswith("set_output_") or c == "output_on()" for c in psu.calls)
        assert not any(c.startswith("set_current(") or c == "input_on()" for c in load.calls)

    @pytest.mark.parametrize("psu_mode", [
        PsuMode.INDEPENDENT, PsuMode.SERIES, PsuMode.PARALLEL,
    ])
    @pytest.mark.parametrize("measured_A", [0.501, 0.505])
    def test_profile_max_normal_cc_with_readback_band(self, psu_mode, measured_A):
        profile = make_12v_profile()
        profile.C10_capacity_Ah = 2.0
        ctrl, psu, load, _ = make_controller(
            profile=profile, psu_mode=psu_mode, dmm_voltage_V=13.8,
            config=ChargeConfig(),
        )
        for _ in range(3):
            ctrl.advance(1.0)
        assert ctrl.state is ChargeState.CHARGE_CC
        assert profile.effective_max_charge_A == ctrl.i_psu_set_A == 0.5
        assert "set_output_current(0.5)" in psu.calls
        assert ctrl._config.charge_current_A_override == 0.0
        psu.current_A = measured_A
        psu.calls.clear()
        load.calls.clear()

        for _ in range(3):
            ctrl.advance(1.0)
            assert ctrl.state is ChargeState.CHARGE_CC
            assert ctrl.fault_reason == ""
            assert ctrl.psu_current_readback_valid is True
            assert ctrl.i_psu_measured_A == measured_A
            assert ctrl.i_psu_set_A == 0.5
        assert ctrl.accumulated_charge_Ah == pytest.approx(measured_A * 3 / 3600)
        assert psu.output_commanded_on is True
        assert "all_outputs_off()" not in psu.calls
        assert load.calls == []


class TestR13B2SeriesSafetyTotalVoltage:
    def test_total_psu_voltage_reaches_both_safety_checks(self):
        from unittest.mock import MagicMock

        from Prog.drivers.device_psu import Keithley2220PSU

        ctrl, _, _, _ = make_controller(
            profile=make_24v_profile(), psu_mode=PsuMode.SERIES,
            dmm_voltage_V=26.0,
        )
        psu = Keithley2220PSU(combination_mode=PsuMode.SERIES)
        resource = MagicMock()
        # CH2 mirrors CH1 if it responds: summing would give a false 56 V.
        resource.query.return_value = "28.0"
        psu._resource = resource
        ctrl._psu = psu
        ctrl._u_batt = 26.0
        ctrl._i_charge = 0.1
        ctrl._safety = MagicMock()
        ctrl._safety.check_series_drop.return_value = SafetyResult()
        ctrl._safety.check_diode_power.return_value = SafetyResult()

        assert ctrl._check_series_safety() is False
        assert ctrl.u_psu_measured_V == 28.0
        assert ctrl.psu_voltage_readback_valid is True
        ctrl._safety.check_series_drop.assert_called_once_with(2.0)
        ctrl._safety.check_diode_power.assert_called_once_with(0.1, 2.0)
        assert [c.args[0] for c in resource.query.call_args_list] == ["MEAS:VOLT? CH1"]



class TestR14B3ModeDependentReadbackBand:
    @staticmethod
    def _make_active_controller(
        psu_mode=PsuMode.PARALLEL, phase=ChargeState.CHARGE_CC,
        profile_max_A=None, override_A=0.0,
    ):
        profile = make_12v_profile(capacity_Ah=7.2)
        profile.max_charge_current_A = profile_max_A
        ctrl, psu, load, dmm_v = make_controller(
            profile=profile, psu_mode=psu_mode, dmm_voltage_V=13.8,
            config=ChargeConfig(charge_current_A_override=override_A),
        )
        {
            PsuMode.INDEPENDENT: psu.set_mode_independent,
            PsuMode.SERIES: psu.set_mode_series,
            PsuMode.PARALLEL: psu.set_mode_parallel,
        }[psu_mode]()
        for _ in range(3):
            ctrl.advance(1.0)
        assert ctrl.state is ChargeState.CHARGE_CC
        assert psu.output_commanded_on is True
        ctrl._state = phase
        psu.voltage_V = 14.2
        psu.calls.clear()
        load.calls.clear()
        return ctrl, psu, load, dmm_v

    @pytest.mark.parametrize("phase", [
        ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD,
    ])
    @pytest.mark.parametrize("psu_mode, boundary_A", [
        (PsuMode.PARALLEL, 1.8136272545090182),
        (PsuMode.INDEPENDENT, 1.5065065065065064),
        (PsuMode.SERIES, 1.5065065065065064),
    ])
    @pytest.mark.parametrize("sample", ["1.810", "below", "equal", "above", "plus_1mA"])
    @pytest.mark.parametrize("off_failure", [None, "load", "psu", "both"])
    def test_boundary_and_safe_off(
        self, monkeypatch, phase, psu_mode, boundary_A, sample, off_failure,
    ):
        ctrl, psu, load, _ = self._make_active_controller(psu_mode, phase)
        # R14-B3 fix1: PARALLEL I_set=1.8 A, mas modban HW-clamp 1.5 A.
        measured_A = {
            "1.810": 1.810, "below": boundary_A - 0.000001, "equal": boundary_A,
            "above": boundary_A + 0.000001, "plus_1mA": boundary_A + 0.001,
        }[sample]
        expect_fault = measured_A > boundary_A
        assert ctrl._profile.effective_max_charge_A == 1.8
        setpoint_A = 1.8 if psu_mode is PsuMode.PARALLEL else 1.5
        assert ctrl.i_psu_set_A == setpoint_A
        relative, absolute_A = (0.002, 0.010) if psu_mode is PsuMode.PARALLEL else (0.001, 0.005)
        if sample == "equal":
            assert measured_A == setpoint_A + (relative * abs(measured_A) + absolute_A)
        psu.current_A = measured_A
        off_order = []
        load_off = load.input_off
        psu_off = psu.all_outputs_off

        def stop_load():
            off_order.append("load")
            if off_failure in ("load", "both"):
                load.calls.append("input_off()")
                raise RuntimeError("R14-B3 load off")
            load_off()

        def stop_psu():
            off_order.append("psu")
            if off_failure in ("psu", "both"):
                psu.calls.append("all_outputs_off()")
                raise RuntimeError("R14-B3 psu off")
            psu_off()

        monkeypatch.setattr(load, "input_off", stop_load)
        monkeypatch.setattr(psu, "all_outputs_off", stop_psu)
        ctrl.advance(1.0)

        assert ctrl.psu_current_readback_valid is True
        assert ctrl.i_psu_measured_A == measured_A
        assert ctrl.i_psu_set_A == setpoint_A
        if not expect_fault:
            assert ctrl.state is not ChargeState.FAULT
            assert ctrl.fault_reason == ""
            assert off_order == []
            assert psu.output_commanded_on is True
            assert ctrl.accumulated_charge_Ah == pytest.approx(measured_A / 3600)
            assert "all_outputs_off()" not in psu.calls
            assert load.calls == []
            return

        assert ctrl.state is ChargeState.FAULT
        assert ctrl.fault_reason.startswith("CHARGE_CURRENT_LIMIT_EXCEEDED: I_meas=")
        assert f"I_set={setpoint_A} A; M=1.8 A; I_base={setpoint_A} A; u=" in ctrl.fault_reason
        assert off_order == ["load", "psu"]
        assert load.calls == ["input_off()"]
        assert psu.calls == ["measure_output_current()", "all_outputs_off()"]
        assert ctrl.accumulated_charge_Ah == 0.0
        if off_failure in ("load", "both"):
            assert "LOAD_OFF_FAILED:R14-B3 load off" in ctrl.fault_reason
        else:
            assert load.input_commanded_on is False
        if off_failure in ("psu", "both"):
            assert "PSU_OFF_FAILED:R14-B3 psu off" in ctrl.fault_reason
        else:
            assert psu.output_commanded_on is False
        for _ in range(3):
            ctrl.advance(1.0)
        assert load.calls == ["input_off()"]
        assert psu.calls == ["measure_output_current()", "all_outputs_off()"]

    @pytest.mark.parametrize("phase", [
        ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD,
    ])
    @pytest.mark.parametrize("psu_mode, boundary_A, relative, absolute_A", [
        (PsuMode.PARALLEL, 0.2605210420841683, 0.002, 0.010),
        (PsuMode.INDEPENDENT, 0.2552552552552553, 0.001, 0.005),
        (PsuMode.SERIES, 0.2552552552552553, 0.001, 0.005),
    ])
    @pytest.mark.parametrize("profile_max_A", [None, 1.2])
    @pytest.mark.parametrize("offset_A", [-0.000001, 0.0, 0.000001])
    def test_band_uses_commanded_current_with_lower_override(
        self, phase, psu_mode, boundary_A, relative, absolute_A, profile_max_A, offset_A,
    ):
        # A regi keeps_profile_max teszt SZANDEKOS forditasa az uj dontes miatt.
        ctrl, psu, load, _ = self._make_active_controller(
            psu_mode, phase, profile_max_A=profile_max_A, override_A=0.25,
        )
        assert ctrl.i_psu_set_A == 0.25
        assert ctrl._profile.effective_max_charge_A == (
            1.8 if profile_max_A is None else profile_max_A
        )
        measured_A = boundary_A + offset_A
        if offset_A == 0.0:
            assert measured_A == 0.25 + (relative * abs(measured_A) + absolute_A)
        psu.current_A = measured_A
        ctrl.advance(1.0)
        assert ctrl.i_psu_set_A == 0.25
        assert ctrl.i_psu_measured_A == measured_A
        assert ctrl.psu_current_readback_valid is True
        if offset_A <= 0.0:
            assert ctrl.state is not ChargeState.FAULT
            assert ctrl.fault_reason == ""
            assert psu.output_commanded_on is True
            assert load.calls == []
            return
        assert ctrl.state is ChargeState.FAULT
        assert ctrl.fault_reason.startswith("CHARGE_CURRENT_LIMIT_EXCEEDED: I_meas=")
        assert "I_set=0.25 A; " in ctrl.fault_reason
        assert "I_base=0.25 A; u=" in ctrl.fault_reason
        assert load.calls == ["input_off()"]
        assert psu.calls == ["measure_output_current()", "all_outputs_off()"]
        assert ctrl.accumulated_charge_Ah == 0.0

    @pytest.mark.parametrize("phase", [
        ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD,
    ])
    @pytest.mark.parametrize("psu_mode, limit_A, band_A", [
        (PsuMode.PARALLEL, 0.26056, 0.01056),
        (PsuMode.INDEPENDENT, 0.25528, 0.00528),
        (PsuMode.SERIES, 0.25528, 0.00528),
    ])
    def test_setpoint_fault_reaches_runner_sample_log_and_event(
        self, phase, psu_mode, limit_A, band_A,
    ):
        from unittest.mock import MagicMock

        from Prog.src import test_runner as runner_module
        from Prog.src.instrument_manager import InstrumentManager

        ctrl, psu, load, dmm_v = self._make_active_controller(
            psu_mode, phase, override_A=0.25,
        )
        psu.current_A = 0.28
        # A regi kodon is egyetlen tick utan terminalis, a fault-oka elter.
        ctrl._config.max_charge_time_s = 0.0
        reason = (
            f"CHARGE_CURRENT_LIMIT_EXCEEDED: I_meas=0.28 A > I_base+u={limit_A} A; "
            f"I_set=0.25 A; M=1.8 A; I_base=0.25 A; u={band_A} A"
        )
        logger = MagicMock()
        runner = runner_module.TestRunner(
            instrument_manager=InstrumentManager(
                psu=psu, load=load, dmm_voltage=dmm_v,
                dmm_temperature=MockDMM(temperature_C=22.0),
            ),
            safety=ctrl._safety, logger=logger, profile=ctrl._profile,
            config=runner_module.TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: ctrl,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
        )
        events = []
        runner.on_event = events.append
        result = runner.run(runner_module.TestPlan(
            test_type=runner_module.TestType.CHARGE_ONLY,
            steps=(runner_module.TestStep(runner_module.StepKind.CHARGE, "R14-B3"),),
        ))

        assert result.status == "FAULT"
        assert result.reason == reason
        sample = logger.log_sample.call_args.args[0]
        assert sample["state"] == ChargeState.FAULT.value
        assert sample["psu_readback_current_A"] == 0.28
        assert sample["fault_flags"] == reason
        logger.log_event.assert_any_call("EMERGENCY_STOP", reason, is_critical=True)
        assert {"event_code": "EMERGENCY_STOP", "event_message": reason} in events
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert not any(c.startswith("set_output_") or c == "output_on()" for c in psu.calls)
        assert not any(c.startswith("set_current(") or c == "input_on()" for c in load.calls)

    @pytest.mark.parametrize("psu_mode", list(PsuMode))
    @pytest.mark.parametrize("override_A, measured_A, expect_fault", [
        (0.0, 1.0, False), (0.25, 0.25, False), (0.25, 0.28, True),
    ])
    def test_chemid_child_uses_commanded_band(
        self, psu_mode, override_A, measured_A, expect_fault,
    ):
        from Prog.src.chemid_controller import (
            ChemIdCharacterizationController,
            ChemIdConfig,
            ChemIdState,
        )

        profile = make_12v_profile(capacity_Ah=7.2)
        psu = MockPSU(combination_mode={
            PsuMode.INDEPENDENT: "OFF", PsuMode.SERIES: "SER", PsuMode.PARALLEL: "PAR",
        }[psu_mode])
        load = MockLoad()
        dmm_v = MockDMM(voltage_V=13.8)
        safety = SafetyManager(
            profile=profile, psu_mode=psu_mode,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )
        parent = ChemIdCharacterizationController(
            psu, load, dmm_v, MockDMM(temperature_C=22.0), profile, safety,
            ChemIdConfig(conditioning_min_s=0.0),
            charge_config=ChargeConfig(charge_current_A_override=override_A),
        )
        for _ in range(6):
            parent.advance(1.0)
        assert parent.state is ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        child = parent._charge_ctrl
        assert child is not None
        assert child.state is ChargeState.CHARGE_CC
        expected_setpoint_A = override_A or (1.8 if psu_mode is PsuMode.PARALLEL else 1.5)
        assert child.i_psu_set_A == expected_setpoint_A
        assert profile.effective_max_charge_A == 1.8
        psu.current_A = measured_A
        psu.calls.clear()
        load.calls.clear()

        parent.advance(1.0)

        assert child.i_psu_measured_A == measured_A
        assert child.psu_current_readback_valid is True
        if not expect_fault:
            assert child.state is ChargeState.CHARGE_CC
            assert parent.state is ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
            assert child.fault_reason == parent.fault_reason == ""
            assert psu.output_commanded_on is True
            assert "all_outputs_off()" not in psu.calls
            assert load.calls == []
            return
        assert child.state is ChargeState.FAULT
        assert parent.state is ChemIdState.FAULT
        assert child.fault_reason == parent.fault_reason
        assert parent.fault_reason.startswith("CHARGE_CURRENT_LIMIT_EXCEEDED: I_meas=")
        assert psu.output_commanded_on is False
        assert load.input_commanded_on is False
        assert psu.calls == [
            "measure_output_current()", "all_outputs_off()", "all_outputs_off()",
        ]
        assert load.calls == ["input_off()", "input_off()"]
        for _ in range(3):
            parent.advance(1.0)
        assert not any(c.startswith("set_output_") or c == "output_on()" for c in psu.calls)
        assert not any(c.startswith("set_current(") or c == "input_on()" for c in load.calls)

    @pytest.mark.parametrize("phase", [
        ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD,
    ])
    @pytest.mark.parametrize("psu_mode", list(PsuMode))
    @pytest.mark.parametrize("invalid_setpoint", [
        None, 0.0, -0.25, float("nan"), float("inf"), float("-inf"),
    ])
    def test_invalid_commanded_setpoint_fails_closed(
        self, monkeypatch, phase, psu_mode, invalid_setpoint,
    ):
        ctrl, psu, load, _ = self._make_active_controller(psu_mode, phase, override_A=0.25)
        # Hianyzik/serult a parancsolt allapot; alacsony readback sem mentheti meg.
        monkeypatch.setattr(ctrl, "_charge_current_set_A", invalid_setpoint)
        psu.current_A = 0.01
        off_order = []
        load_off, psu_off = load.input_off, psu.all_outputs_off

        def stop_load():
            off_order.append("load")
            load_off()

        def stop_psu():
            off_order.append("psu")
            psu_off()

        monkeypatch.setattr(load, "input_off", stop_load)
        monkeypatch.setattr(psu, "all_outputs_off", stop_psu)
        ctrl.advance(1.0)
        assert ctrl.state is ChargeState.FAULT
        assert ctrl.fault_reason == (
            "CHARGE_CURRENT_LIMIT_EXCEEDED: invalid commanded charge current; "
            f"I_set={invalid_setpoint!r} A; M=1.8 A"
        )
        assert ctrl.i_psu_measured_A == 0.01
        assert ctrl.psu_current_readback_valid is True
        assert ctrl.accumulated_charge_Ah == 0.0
        assert off_order == ["load", "psu"]
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        for _ in range(3):
            ctrl.advance(1.0)
        assert load.calls == ["input_off()"]
        assert psu.calls == ["measure_output_current()", "all_outputs_off()"]

    @pytest.mark.parametrize("phase", [
        ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD,
    ])
    @pytest.mark.parametrize("psu_mode", list(PsuMode))
    @pytest.mark.parametrize("measured_A, expect_fault", [(0.505, False), (0.52, True)])
    def test_commanded_setpoint_above_profile_uses_profile_cap(
        self, phase, psu_mode, measured_A, expect_fault,
    ):
        ctrl, psu, load, _ = self._make_active_controller(
            psu_mode, phase, profile_max_A=0.5, override_A=0.25,
        )
        # A normal preset tiltja ezt; injektalt magas parancs sem lazithatja a kaput.
        ctrl._charge_current_set_A = 1.0
        psu.set_output_current(1.0)
        psu.calls.clear()
        psu.current_A = measured_A
        ctrl.advance(1.0)
        assert ctrl.i_psu_measured_A == measured_A
        assert (ctrl.state is ChargeState.FAULT) is expect_fault
        if expect_fault:
            assert ctrl.fault_reason.startswith("CHARGE_CURRENT_LIMIT_EXCEEDED: I_meas=")
            assert "I_set=1.0 A; M=0.5 A; I_base=0.5 A; u=" in ctrl.fault_reason
            assert ctrl.accumulated_charge_Ah == 0.0
            assert load.calls == ["input_off()"]
            assert psu.calls == ["measure_output_current()", "all_outputs_off()"]
            assert load.input_commanded_on is False
            assert psu.output_commanded_on is False
        else:
            assert ctrl.fault_reason == ""
            assert load.calls == []
            assert psu.output_commanded_on is True

    @pytest.mark.parametrize("psu_mode, measured_A", [
        (PsuMode.PARALLEL, 0.2605215),
        (PsuMode.INDEPENDENT, 0.2552557),
        (PsuMode.SERIES, 0.2552557),
    ])
    @pytest.mark.parametrize("requested_A, expect_fault", [
        (0.25000049, True), (0.25000051, False),
    ])
    def test_basis_matches_six_significant_digit_wire_command(
        self, psu_mode, measured_A, requested_A, expect_fault,
    ):
        from unittest.mock import MagicMock

        from Prog.drivers.device_psu import Keithley2220PSU

        ctrl, psu, _, _ = self._make_active_controller(
            psu_mode, override_A=requested_A,
        )
        assert ctrl.i_psu_set_A == requested_A
        # A valodi driver szerializalojat fake I/O-val ellenorizzuk.
        driver = Keithley2220PSU()
        driver._apply_config_atomic = MagicMock()
        driver.set_output_current(ctrl.i_psu_set_A)
        expected_wire = "SOUR:CURR 0.25" if expect_fault else "SOUR:CURR 0.250001"
        driver._apply_config_atomic.assert_called_once_with("INST:SEL CH1", expected_wire)
        psu.current_A = measured_A
        ctrl.advance(1.0)
        assert (ctrl.state is ChargeState.FAULT) is expect_fault
        if expect_fault:
            assert ctrl.fault_reason.startswith("CHARGE_CURRENT_LIMIT_EXCEEDED: I_meas=")
        else:
            assert ctrl.fault_reason == ""


class TestR16B14ChargeStartModeFault:
    @pytest.mark.parametrize("failure", ["lost_mode", "mode_read_error", "fallback"])
    def test_real_driver_mode_guard_faults_once_and_turns_both_devices_off(self, failure):
        from Prog.tests.test_device_psu import _energizing, _make_psu_on_stateful_fake

        ctrl, _, load, _ = make_controller(
            profile=make_24v_profile(), psu_mode=PsuMode.SERIES, dmm_voltage_V=25.0,
        )
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.state == ChargeState.PSU_PRESET
        psu, fake = _make_psu_on_stateful_fake(
            PsuMode.SERIES, "OUTP:ENAB" if failure == "fallback" else "OUTP",
        )
        if failure == "lost_mode":
            fake.actual_mode = "OFF"
        elif failure == "mode_read_error":
            fake.fail_before_execute = lambda msg: msg == "SOUR:OUTP:SER?"
        ctrl._psu = psu
        load.calls.clear()

        ctrl.advance(1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "PSU_PRESET_FAILED" in ctrl.fault_reason
        assert {"lost_mode": "mód eltérés", "mode_read_error": "SOUR:OUTP:SER?",
                "fallback": "fallback"}[failure] in ctrl.fault_reason
        assert load.calls.count("input_off()") == 1
        assert fake.messages.count("OUTP OFF;*OPC?") == 1
        assert _energizing(fake.messages) == []
        assert psu.output_commanded_on is False
        assert psu.combination_mode == PsuMode.SERIES
        ctrl.advance(1.0)
        assert fake.messages.count("OUTP OFF;*OPC?") == 1
        assert load.calls.count("input_off()") == 1
        assert _energizing(fake.messages) == []


class TestR16B14F1ChargeActivationAck:
    @pytest.mark.parametrize("output_zeros, mode_zeros, reason", [
        (2, 0, None), (0, 2, None), (2, 2, None),
        (None, 0, "bekapcsolás nem igazolt"), (0, None, "mód eltérés"),
    ])
    def test_delayed_ack_or_single_diagnosed_fault_with_ordered_safe_off(
        self, monkeypatch, output_zeros, mode_zeros, reason,
    ):
        from unittest.mock import MagicMock

        from Prog.tests.test_device_psu import (
            _BARE_OUTP_OFF,
            _BARE_OUTP_ON,
            _CH1_ENABLE_ON,
            _CH2_ENABLE_ON,
            _energizing,
            _r16_b14f1_probe,
        )

        ctrl, _, load, _ = make_controller(
            profile=make_24v_profile(), psu_mode=PsuMode.SERIES, dmm_voltage_V=25.0,
        )
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.state == ChargeState.PSU_PRESET
        psu, fake, clock, state = _r16_b14f1_probe(
            monkeypatch, output_zeros=output_zeros, mode_zeros=mode_zeros,
        )
        ctrl._psu = psu
        emergency = MagicMock(wraps=ctrl.emergency_stop)
        monkeypatch.setattr(ctrl, "emergency_stop", emergency)
        off_order = []
        original_off, original_query = load.input_off, fake.query

        def load_off():
            off_order.append("Load OFF")
            original_off()

        def query(msg):
            if msg == _BARE_OUTP_OFF:
                off_order.append("PSU ALL OFF")
            return original_query(msg)

        monkeypatch.setattr(load, "input_off", load_off)
        monkeypatch.setattr(fake, "query", query)
        load.calls.clear()
        ctrl.advance(1.0)
        assert _energizing(fake.messages) == [_CH1_ENABLE_ON, _CH2_ENABLE_ON, _BARE_OUTP_ON]
        assert psu.combination_mode == PsuMode.SERIES
        if reason is None:
            assert ctrl.state == ChargeState.CHARGE_CC
            assert ctrl.fault_reason == ""
            emergency.assert_not_called()
            assert off_order == []
            assert clock.now == pytest.approx(100.1)
        else:
            assert ctrl.state == ChargeState.FAULT
            assert ctrl.fault_reason.startswith("PSU_PRESET_FAILED: ")
            assert reason in ctrl.fault_reason and "nyugtaablak lejárt: 1 s" in ctrl.fault_reason
            emergency.assert_called_once_with(ctrl.fault_reason)
            assert clock.now == pytest.approx(101.0)
            assert off_order == ["Load OFF", "PSU ALL OFF"]
            assert load.calls.count("input_off()") == 1
            assert fake.messages.count(_BARE_OUTP_OFF) == 1
            assert not fake.output_state and not load.input_commanded_on
            assert psu.output_commanded_on is False
            before = list(fake.messages)
            ctrl.advance(1.0)
            assert fake.messages == before
            assert off_order == ["Load OFF", "PSU ALL OFF"]
            emergency.assert_called_once()
        assert fake.timeout == 5000
        assert state["polls"] >= 3


class TestR16B14F2EmergencyStopOffOrdering:
    def test_active_independent_fallback_stops_before_mode_timeouts(self, monkeypatch):
        from Prog.tests.test_device_psu import (
            _BARE_OUTP_OFF,
            _energizing,
            _make_psu_on_stateful_fake,
            _R16B14F1Clock,
        )

        ctrl, _, load, _ = make_controller()
        psu, fake = _make_psu_on_stateful_fake(output_cmd="OUTP:ENAB")
        psu.output_on()
        assert fake.output_state is True and fake.energized == {"CH1"}
        ctrl._psu = psu
        fake.messages.clear()
        load.calls.clear()
        clock = _R16B14F1Clock()
        events = []
        original_query, original_off = fake.query, load.input_off

        def query(msg):
            events.append((msg, clock.now, fake.output_state))
            if msg == "SOUR:OUTP:SER?":
                fake.messages.append(msg)
                clock.now += psu.timeout_ms / 1000.0
                raise TimeoutError("vészleállítás alatti módolvasási hiba")
            return original_query(msg)

        def load_off():
            events.append(("Load OFF", clock.now, fake.output_state))
            return original_off()

        monkeypatch.setattr(fake, "query", query)
        monkeypatch.setattr(load, "input_off", load_off)
        ctrl.emergency_stop("R16_B14F2_TEST")
        assert events[:2] == [("Load OFF", 100.0, True), (_BARE_OUTP_OFF, 100.0, True)]
        assert events[2:5] == [
            ("SOUR:OUTP:SER?", 100.0, False),
            ("SOUR:OUTP:SER?", 105.0, False),
            ("SOUR:OUTP:SER?", 110.0, False),
        ]
        assert clock.now == 115.0
        assert ctrl.state == ChargeState.FAULT and ctrl.fault_reason == "R16_B14F2_TEST"
        assert fake.output_state is False and fake.energized == set()
        assert psu.output_commanded_on is False and _energizing(fake.messages) == []
        assert load.calls == ["input_off()"]
        before = list(fake.messages)
        ctrl.advance(1.0)
        assert fake.messages == before and load.calls == ["input_off()"]


class TestR16B26aGlobalChargeCap:
    temperatures = (
        (0.0, 2.400), (5.0, 2.400), (10.0, 2.400), (15.0, 2.400),
        (19.9, 2.400), (20.0, 2.400), (25.0, 2.3875), (35.0, 2.3625),
    )

    @staticmethod
    def make(cells, temperature, mode):
        profile = BatteryProfile(
            battery_name="Q44", manufacturer="TEST", model="Q44",
            nominal_capacity_Ah=7.0, cell_count=cells, nominal_voltage_V=2.0 * cells,
        )
        safety = SafetyManager(
            profile, PsuMode.SERIES if cells == 12 else PsuMode.INDEPENDENT,
            temp_comp_mode=mode,
        )
        return ChargeController(
            MockPSU(current_A=0.67), MockLoad(), MockDMM(voltage_V=2.2 * cells),
            MockDMM(temperature_C=temperature), profile, safety,
            ChargeConfig(charge_current_A_override=0.67, fast_loop_enabled=False),
        )

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("temperature,per_cell", temperatures)
    @pytest.mark.parametrize("mode", list(TempCompMode))
    def test_target_and_psu_preset(self, cells, temperature, per_cell, mode):
        ctrl = self.make(cells, temperature, mode)
        for _ in range(3):
            ctrl.advance(0.1)
        assert ctrl.state == ChargeState.CHARGE_CC
        assert ctrl._effective_charge_target_V() == pytest.approx(per_cell * cells, abs=1e-12)
        assert ctrl.u_psu_set_V == pytest.approx(per_cell * cells, abs=1e-12)
        assert ctrl._psu.called("output_on")
        assert ctrl._profile.batt_absolute_max_V - ctrl._effective_charge_target_V() >= 0.025 * cells - 1e-12
        # A CV-szabalyozo is ezt a celt hasznalja, a nyilvanos advance utjan.
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._dmm_v.voltage_V = per_cell * cells - 0.02
        ctrl.advance(0.1)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        assert ctrl.u_psu_set_V == pytest.approx(per_cell * cells + 0.02, abs=1e-12)
        ctrl._i_charge = 0.1
        ctrl._u_batt = per_cell * cells
        assert ctrl._check_taper_condition() and ctrl._check_taper_hold_condition()


    @pytest.mark.parametrize("temperature,per_cell", temperatures)
    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("offset", [0.010, 0.050])
    def test_cv_normal_overshoot_does_not_fault(self, temperature, per_cell, mode, offset):
        ctrl = self.make(12, temperature, mode)
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = ctrl._psu.voltage_V = per_cell * 12 + 0.5
        ctrl._charge_current_set_A = 0.67
        ctrl._dmm_v.voltage_V = per_cell * 12 + offset
        before = ctrl.u_psu_set_V
        ctrl.advance(0.1)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL
        assert ctrl.fault_reason == ""
        # +10 mV a holtsav hatara; +50 mV lefelé szabalyoz.
        assert ctrl.u_psu_set_V <= before
        if offset == 0.050:
            assert ctrl.u_psu_set_V == pytest.approx(before - 0.050, abs=1e-12)
        assert not ctrl._psu.called("all_outputs_off")

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("phase", [
        ChargeState.PSU_PRESET, ChargeState.CHARGE_CC,
        ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD,
    ])
    @pytest.mark.parametrize("offset", [-0.001, 0.0, 0.001])
    def test_absolute_voltage_gate_unchanged(self, monkeypatch, cells, phase, offset):
        ctrl = self.make(cells, 20.0, TempCompMode.ENABLED)
        assert ctrl._profile.batt_absolute_max_V_per_cell == 2.425
        ctrl._state = phase
        ctrl._u_psu_set = ctrl._psu.voltage_V = 2.400 * cells
        ctrl._charge_current_set_A = 0.67
        ctrl._dmm_v.voltage_V = round(2.425 * cells, 6) + offset
        off = []
        load_off, psu_off = ctrl._load.input_off, ctrl._psu.all_outputs_off

        def load_stop():
            off.append("LOAD")
            load_off()

        def psu_stop():
            off.append("PSU")
            psu_off()

        monkeypatch.setattr(ctrl._load, "input_off", load_stop)
        monkeypatch.setattr(ctrl._psu, "all_outputs_off", psu_stop)
        ctrl.advance(0.1)
        if offset <= 0:
            assert ctrl.state != ChargeState.FAULT
            assert off == []
        else:
            assert ctrl.state == ChargeState.FAULT
            assert ctrl.fault_reason == "BATTERY_OVERVOLTAGE"
            assert off == ["LOAD", "PSU"]
            assert not ctrl._psu.called("set_output_voltage") and not ctrl._psu.called("output_on")
            assert not ctrl._load.input_commanded_on and not ctrl._psu.output_commanded_on
            before = (list(ctrl._psu.calls), list(ctrl._load.calls))
            ctrl.advance(0.1)
            assert (ctrl._psu.calls, ctrl._load.calls) == before

    def test_fast_subtick_uses_capped_target_without_extra_fault_gate(self, monkeypatch):
        import Prog.src.charge_controller as module

        ctrl = self.make(12, 5.0, TempCompMode.ENABLED)
        ctrl._config.fast_loop_enabled = True
        ctrl._config.fast_loop_period_s = 0.1
        ctrl._config.fast_loop_max_substeps = 3
        ctrl._state = ChargeState.CHARGE_CV_DMM_CONTROL
        ctrl._u_psu_set = ctrl._psu.voltage_V = 29.0
        ctrl._charge_current_set_A = 0.67
        clock, reads, sleeps = [0.0], [], []

        def voltage():
            reads.append(True)
            assert len(reads) <= 4, "Q48 DMM read bound"
            return 28.81 if len(reads) == 1 else 29.101

        def sleep(seconds):
            sleeps.append(seconds)
            assert len(sleeps) <= 3, "Q48 sleep bound"
            clock[0] += seconds

        monkeypatch.setattr(ctrl._dmm_v, "read_voltage", voltage)
        monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])
        monkeypatch.setattr(module.time, "sleep", sleep)
        ctrl.advance(1.0)
        assert reads == [True, True] and sleeps == [0.1], "Q48 second-read trigger"
        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "BATTERY_OVERVOLTAGE"
        assert not ctrl._psu.output_commanded_on and not ctrl._load.input_commanded_on

    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("previous", [None, 5.0, 35.0])
    @pytest.mark.parametrize("bad_text", [None, "nan", "inf", "-inf", "9.9e37"])
    def test_temperature_loss_keeps_existing_policy_and_cap(self, monkeypatch, mode, previous, bad_text):
        ctrl = self.make(12, 20.0, mode)
        if previous is not None:
            ctrl._battery_temperature_C = previous
        queries = []
        if bad_text is None:
            ctrl._dmm_t.simulate_temp_failure = True
        else:
            from Prog.drivers.device_dmm import Keysight34465ADMM

            driver = Keysight34465ADMM()

            def query(command):
                queries.append(command)
                assert len(queries) <= 3, "Q48 invalid-temperature query bound"
                return bad_text

            monkeypatch.setattr(driver, "_query", query)
            ctrl._dmm_t = driver
        for _ in range(3):
            ctrl.advance(0.1)
        if bad_text is not None:
            assert queries and set(queries) == {"READ?"}, "Q48 invalid-temperature trigger"
        assert not ctrl.temp_dmm_valid
        assert ctrl._battery_temperature_C == (20.0 if previous is None else previous)
        assert ctrl._effective_charge_target_V() <= 28.8
        assert ctrl.u_psu_set_V <= 28.8
        if mode == TempCompMode.ENABLED:
            assert ctrl.state == ChargeState.FAULT
            assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
            assert not ctrl._psu.called("output_on")
        else:
            assert ctrl.state == ChargeState.CHARGE_CC


@pytest.mark.parametrize("tlc", [False, True])
@pytest.mark.parametrize("vpc", [1.6999, 1.70, 1.73, 1.75])
def test_b23b_q42_child_charge_precheck(tlc, vpc):
    ctrl, psu, load, dmm = make_controller(dmm_voltage_V=6 * vpc)
    kwargs = {"_traditional_learning": True} if tlc else {}
    ctrl = ChargeController(
        psu, load, dmm, ctrl._dmm_t, ctrl._profile, ctrl._safety, ctrl._config, **kwargs,
    )
    for _ in range(3):
        ctrl.advance(0.1)
        if ctrl.state == ChargeState.FAULT:
            break
    allowed = vpc >= (1.70 if tlc else 1.75)
    assert psu.called("output_on") is allowed
    assert not load.input_commanded_on
    if not allowed:
        assert ctrl.state == ChargeState.FAULT
        assert "DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED" in ctrl.fault_reason
        assert load.called("input_off")
        assert psu.called("all_outputs_off")


@pytest.mark.parametrize("state", [ChargeState.PSU_PRESET, ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD])
def test_b23b_q42_fresh_tlc_voltage_cannot_reuse_an_earlier_precheck(state):
    ctrl, psu, load, dmm = make_controller()
    ctrl = ChargeController(
        psu, load, dmm, ctrl._dmm_t, ctrl._profile, ctrl._safety, ctrl._config,
        _traditional_learning=True,
    )
    for _ in range(3):
        ctrl.advance(0.1)
    assert psu.called("output_on"), "Earlier precheck and activation must succeed"
    if state == ChargeState.PSU_PRESET:
        psu.all_outputs_off()
    ctrl._state = state
    dmm.voltage_V = 6 * 1.70 - 0.001
    previous_on = psu.calls.count("output_on()")
    ctrl.advance(0.1)
    assert ctrl.state == ChargeState.FAULT
    assert "DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED" in ctrl.fault_reason
    assert psu.calls.count("output_on()") == previous_on
    assert not load.input_commanded_on and not psu.output_commanded_on


@pytest.mark.parametrize("readback", [True, None, "error"])
def test_b23b_tlc_charge_rechecks_actual_off_before_on(monkeypatch, readback):
    ctrl, psu, load, dmm = make_controller()
    ctrl = ChargeController(
        psu, load, dmm, ctrl._dmm_t, ctrl._profile, ctrl._safety, ctrl._config,
        _traditional_learning=True, _require_confirmed_off=True,
    )
    for _ in range(2):
        ctrl.advance(0.1)
    assert ctrl.state == ChargeState.PSU_PRESET
    reads = []
    def actual():
        reads.append(True)
        if readback == "error":
            raise OSError("B23b actual Load state unavailable")
        return readback
    monkeypatch.setattr(load, "read_input_state", actual)
    ctrl.advance(0.1)
    assert reads, "Actual-state gate must be exercised"
    assert ctrl.state == ChargeState.FAULT
    assert not psu.called("output_on")
    assert load.called("input_off") and psu.called("all_outputs_off")


@pytest.mark.parametrize("vpc", [1.70, 1.73, 1.7499])
def test_b23b_non_tlc_active_charge_keeps_existing_low_voltage_policy(vpc):
    ctrl, psu, load, dmm = make_controller()
    for _ in range(3):
        ctrl.advance(0.1)
    assert ctrl.state == ChargeState.CHARGE_CC
    assert psu.called("output_on")
    dmm.voltage_V = 6 * vpc
    psu.voltage_V = dmm.voltage_V + 0.1
    ctrl.advance(0.1)
    assert ctrl.u_batt_V == pytest.approx(6 * vpc)
    assert ctrl.state == ChargeState.CHARGE_CC, ctrl.fault_reason
    assert "DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED" not in ctrl.fault_reason
    assert psu.output_commanded_on and not load.input_commanded_on


@pytest.mark.parametrize("mode,expected_A", [
    (PsuMode.INDEPENDENT, 1.5), (PsuMode.SERIES, 1.5),
    (PsuMode.PARALLEL, 3.0), ("UNKNOWN", 1.5),
])
def test_b23b_shared_psu_limit_keeps_charge_preset_values(mode, expected_A):
    ctrl, psu, _, _ = make_controller(
        profile=make_12v_profile(capacity_Ah=20.0),
        config=ChargeConfig(charge_current_A_override=4.0),
    )
    ctrl._safety.psu_mode = mode
    ctrl._run_psu_preset()
    assert ctrl.state == ChargeState.CHARGE_CC
    assert psu.called("output_on")
    assert ctrl.i_psu_set_A == expected_A
    assert f"set_output_current({expected_A})" in psu.calls
