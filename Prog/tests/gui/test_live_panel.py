"""LivePanel unit tesztek.

Felhasználói kérés (2026-08-23):
  1. dA/dt kijelzés (a meglévő dV/dt mintája szerint).
  2. GOLDEN_GG/TRADITIONAL_LEARNING alatt a "Fázis" mező mutassa a
     controller SAJÁT belső állapotát (pl. "GOLDEN GG TÖLTÉS"), ne csak a
     nyers step_kind-ot ("golden_gg").
"""
from __future__ import annotations

import pytest

from Prog.gui.panels.live_panel import (
    _LIVE_LABEL_BASE_STYLE,
    _LIVE_LABEL_STALE_STYLE,
    _MAX_POINTS,
    _PHASE_MAP,
    _RB_LABEL_BASE_STYLE,
    _STEP_KIND_DISPLAY,
    LivePanel,
)


def _sample(**overrides) -> dict:
    base = {
        "elapsed_s": 0.0,
        "battery_voltage_V": 13.0,
        "signed_current_A": 0.0,
        "battery_temperature_C": 22.0,
        "accumulated_charge_Ah": 0.0,
        "accumulated_discharge_Ah": 0.0,
        "state": None,
    }
    base.update(overrides)
    return base


class TestDaDt:
    def test_shows_dash_before_enough_samples(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(elapsed_s=0.0, signed_current_A=1.0))
        assert panel._dadt_lbl.text() == "– mA/min"

    def test_computes_positive_slope_over_window(self, qapp):
        panel = LivePanel()
        for i in range(11):
            panel.update_sample(_sample(elapsed_s=float(i), signed_current_A=float(i) * 0.1))
        # 10 mintás ablak: 1.0A - 0.1A = 0.9A, 9s alatt -> 0.9/9*1000*60 = 6000 mA/min
        text = panel._dadt_lbl.text()
        assert "mA/min" in text
        assert text.startswith("+")

    def test_reset_plots_clears_dadt_label(self, qapp):
        panel = LivePanel()
        for i in range(11):
            panel.update_sample(_sample(elapsed_s=float(i), signed_current_A=float(i) * 0.1))
        panel.reset_plots()
        assert panel._dadt_lbl.text() == "– mA/min"

    def test_reset_plots_clears_status_row_labels(self, qapp):
        """[regresszió, Teljes kódbázis review 17. kör (2026-09-05),
        GUI/UX/Állapot P2 lelet] reset_plots() korábban nem törölte a
        7 státusz-sor címkét — új session induláskor az ELŐZŐ session
        utolsó értékei látszottak az első update_sample() tickig."""
        panel = LivePanel()
        panel.update_sample(_sample(
            battery_voltage_V=13.0, signed_current_A=1.5,
            battery_temperature_C=25.0, accumulated_charge_Ah=0.5,
            accumulated_discharge_Ah=0.3, psu_mode="CV", warning_flags="OVP",
        ))
        assert panel._ubatt_lbl.text() != "– V"
        panel.reset_plots()
        assert panel._ubatt_lbl.text() == "– V"
        assert panel._isign_lbl.text() == "– A"
        assert panel._temp_lbl.text() == "– °C"
        assert panel._charge_lbl.text() == "– Ah"
        assert panel._disch_lbl.text() == "– Ah"
        assert panel._psumode_lbl.text() == "–"
        assert panel._warn_lbl.text() == "–"

    def test_reset_plots_clears_state_label(self, qapp):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F8 (P3,
        DFAM-001 sibling-omission a 17. kör fixjéhez képest)] a 17. kör
        reset_plots()-fixje 7 státusz-sor címkét törölt, de az "Állapot"
        (_state_lbl, set_status() által írva) kimaradt a listából -- egy
        FAULT-tal zárult session után Start-ra kattintva a mező a teljes
        hardveres bring-up idejére az ELŐZŐ session terminális állapotát
        (pl. piros "FAULT") mutatta."""
        panel = LivePanel()
        panel.set_status("FAULT")
        assert panel._state_lbl.text() == "FAULT"

        panel.reset_plots()

        assert panel._state_lbl.text() == "IDLE"
        assert "ffebee" not in panel._state_lbl.styleSheet()  # a FAULT piros hattere eltunt


class TestPhaseLabelForOpaqueStepKinds:
    """Korábban a GOLDEN_GG/TRADITIONAL_LEARNING lépés a Fázis mezőben
    mindig csak a nyers step_kind-ot mutatta ("GOLDEN GG"/"LEARNING"),
    mert a _PHASE_MAP nem ismerte a controller belső állapotait."""

    def test_golden_gg_full_charge_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "GOLDEN_GG", "step_index": 0, "step_count": 1,
            "step_label": "golden_gg",
        })
        panel.update_sample(_sample(state="FULL_CHARGE"))
        assert panel._phase_lbl.text() == "GOLDEN GG TÖLTÉS"

    def test_golden_gg_system_rate_discharge_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "GOLDEN_GG", "step_index": 0, "step_count": 1,
            "step_label": "golden_gg",
        })
        panel.update_sample(_sample(state="SYSTEM_RATE_DISCHARGE"))
        assert panel._phase_lbl.text() == "GOLDEN GG KISÜTÉS"

    def test_golden_gg_unmapped_state_falls_back_to_step_kind_display(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "GOLDEN_GG", "step_index": 0, "step_count": 1,
            "step_label": "golden_gg",
        })
        panel.update_sample(_sample(state=None))
        assert panel._phase_lbl.text() == "GOLDEN GG"

    def test_traditional_learning_precharge_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "TRADITIONAL_LEARNING", "step_index": 0, "step_count": 1,
            "step_label": "traditional_learning",
        })
        panel.update_sample(_sample(state="PRECHARGE"))
        assert panel._phase_lbl.text() == "LEARNING TÖLTÉS 1"

    def test_traditional_learning_discharge_2_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "TRADITIONAL_LEARNING", "step_index": 0, "step_count": 1,
            "step_label": "traditional_learning",
        })
        panel.update_sample(_sample(state="DISCHARGE_2"))
        assert panel._phase_lbl.text() == "LEARNING KISÜTÉS 2"

    def test_charge_step_phase_unaffected_by_new_entries(self, qapp):
        """Regresszió: az új GOLDEN_GG/TRADITIONAL_LEARNING bejegyzések nem
        érinthetik a meglévő CHARGE/DISCHARGE/RELAX/OCV_SOC leképezést."""
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHARGE", "step_index": 0, "step_count": 1,
            "step_label": "charge",
        })
        panel.update_sample(_sample(state="CHARGE_CC"))
        assert panel._phase_lbl.text() == "CHARGE CC"


class TestCf19StepTimeUsesPhaseElapsedSWhenPresent:
    """[CF-19, governance-v2 kör 2, DFAM-023 opaque_composite_step_hides_
    progress] a "Lépés idő" korábban mindig a teljes (top-level TestStep
    indulása óta eltelt) időt mutatta -- összetett módoknál (GOLDEN_GG/
    TRADITIONAL_LEARNING/CHEMID/OCV_SOC) ez EGYENLŐ volt a teljes, akár
    több tíz órás session-idővel, sosem a JELENLEGI belső fázisban
    eltöltött idővel. Ha a minta hordozza a phase_elapsed_s mezőt, azt
    kell kijelezni; ha nem (sima StepKindek), a régi viselkedés marad."""

    def test_uses_phase_elapsed_s_when_present(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "GOLDEN_GG", "step_index": 0, "step_count": 1,
            "step_label": "golden_gg",
        })
        panel.update_sample(_sample(state="RELAX_2H", phase_elapsed_s=125.0))
        assert panel._step_time_lbl.text() == "00:02:05"

    def test_falls_back_to_perf_counter_when_absent(self, qapp, monkeypatch):
        import time as time_module
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHARGE", "step_index": 0, "step_count": 1,
            "step_label": "charge",
        })
        fake_start = time_module.perf_counter()
        panel._step_start_perf = fake_start - 7.0
        panel.update_sample(_sample(state="CHARGE_CC"))
        assert panel._step_time_lbl.text() == "00:00:07"


class TestR214SessionTimeUsesSessionElapsedS:
    """R2-14 (2026-09-15, DFAM-022 rokona): a "Session idő" korábban
    mindig a sample["elapsed_s"]-t (run()-SZEGMENS ideje) mutatta -- egy
    checkpoint-folytatás/cross-process resume után ez 0-ról indult újra,
    holott a valódi session órák/napok óta tartott. Az új session_
    elapsed_s mezőt kell használni, ha jelen van; ha nem (régi minta,
    közvetlen teszthívás), a korábbi elapsed_s-viselkedés a fallback."""

    def test_uses_session_elapsed_s_when_present(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(elapsed_s=10.0, session_elapsed_s=3725.0))
        assert panel._total_time_lbl.text() == "01:02:05"

    def test_falls_back_to_elapsed_s_when_session_elapsed_s_absent(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(elapsed_s=10.0))
        assert panel._total_time_lbl.text() == "00:00:10"
        assert panel._xs[-1] == 10.0

    def test_plot_x_axis_uses_session_elapsed_s_when_present(self, qapp):
        """[Kör 20 Batch E10, F20-G1, DFAM-022 RECURRING + DFAM-001] A
        `_total_time_lbl` (fent) MÁR helyesen a session_elapsed_s-t
        használta (R2-14), de a `self._xs` (a kapacitás-integráló görbék
        x-tengelye) KORÁBBAN mindig `elapsed_s`-t használt -- egy
        cross-process resume után a görbe x=0-tól indult újra egy
        NEM-nulla Ah-értékkel, fizikailag lehetetlen ugrást rajzolva az
        origóban, miközben a mellette lévő felirat helyesen folytonos
        időt mutatott. Ez a teszt a fix ELŐTTI kódon bukna (`panel._xs[-1]`
        10.0 lenne, nem 3725.0)."""
        panel = LivePanel()
        panel.update_sample(_sample(elapsed_s=10.0, session_elapsed_s=3725.0))
        assert panel._xs[-1] == 3725.0

    def test_plot_x_axis_and_session_label_use_bit_identical_value_on_non_resume_path(self, qapp):
        """FIX-INDUCED RISK (empirikusan igazolva, NEM levezetésből
        elfogadva, l. feedback_never_assert_equivalence_without_empirical_
        verification.md): a nem-resume úton session_elapsed_s == elapsed_s,
        tehát a fix bit-azonos viselkedést kell adjon a korábbi kóddal --
        ez a teszt mindkét (fix előtti és utáni) kódon zöld, csak a
        fenti két teszt diszkriminál a tényleges hibára."""
        panel = LivePanel()
        panel.update_sample(_sample(elapsed_s=42.5, session_elapsed_s=42.5))
        assert panel._xs[-1] == 42.5
        assert panel._total_time_lbl.text() == "00:00:42"


class TestCF26PhaseMapCompleteness:
    """Governance-v2 review-kör 1 (CF-26, GUI-R1-04/05, DFAM-001+018):
    a CHEMID StepKind egyáltalán nem szerepelt a _PHASE_MAP/
    _STEP_KIND_DISPLAY-ben (nyers "CHEMID" jelent meg 11 állapot
    helyett); az OCV_SOC 2 alállapota (IMPULSE_FAST_ON/ACQUIRE) is
    hiányzott; és a controller-FAULT 4 módnál (CHARGE/DISCHARGE/
    OCV_SOC/CHEMID) NORMÁL FUTÓ FÁZISKÉNT jelent meg (megegyező
    címkével, mint egy ténylegesen futó fázis)."""

    def test_chemid_precheck_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHEMID", "step_index": 0, "step_count": 1,
            "step_label": "chemid",
        })
        panel.update_sample(_sample(state="PRECHECK"))
        assert panel._phase_lbl.text() == "CHEMID ELŐKÉSZÍTÉS"

    def test_chemid_c20_discharge_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHEMID", "step_index": 0, "step_count": 1,
            "step_label": "chemid",
        })
        panel.update_sample(_sample(state="C20_DISCHARGE_TO_MANUFACTURER_MINIMUM"))
        assert panel._phase_lbl.text() == "CHEMID KISÜTÉS"

    def test_chemid_unmapped_state_falls_back_to_chemid_not_raw_step_kind(self, qapp):
        """A fix ELŐTT ez a fallback a nyers 'CHEMID' step_kind stringet
        adta vissza -- változatlanul, de MOST expliciten a
        _STEP_KIND_DISPLAY bejegyzésen keresztül, nem véletlenül."""
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHEMID", "step_index": 0, "step_count": 1,
            "step_label": "chemid",
        })
        panel.update_sample(_sample(state=None))
        assert panel._phase_lbl.text() == "CHEMID"

    def test_ocv_soc_fast_transient_on_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "OCV_SOC", "step_index": 0, "step_count": 1,
            "step_label": "ocv_soc",
        })
        panel.update_sample(_sample(state="IMPULSE_FAST_ON"))
        assert panel._phase_lbl.text() == "OCV-SOC IMPULSE"

    def test_ocv_soc_fast_transient_acquire_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "OCV_SOC", "step_index": 0, "step_count": 1,
            "step_label": "ocv_soc",
        })
        panel.update_sample(_sample(state="IMPULSE_FAST_ACQUIRE"))
        assert panel._phase_lbl.text() == "OCV-SOC IMPULSE"

    def test_charge_fault_shows_fault_phase_not_normal_running_label(self, qapp):
        """A fix ELŐTT ez ugyanazt a 'CHARGE' címkét mutatta volna, mint
        egy ténylegesen futó töltési fázis -- megkülönböztethetetlenül."""
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHARGE", "step_index": 0, "step_count": 1,
            "step_label": "charge",
        })
        panel.update_sample(_sample(state="FAULT"))
        assert panel._phase_lbl.text() == "CHARGE HIBA"

    def test_discharge_fault_shows_fault_phase_not_normal_running_label(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "DISCHARGE", "step_index": 0, "step_count": 1,
            "step_label": "discharge",
        })
        panel.update_sample(_sample(state="FAULT"))
        assert panel._phase_lbl.text() == "DISCHARGE HIBA"

    def test_ocv_soc_fault_shows_fault_phase_not_normal_running_label(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "OCV_SOC", "step_index": 0, "step_count": 1,
            "step_label": "ocv_soc",
        })
        panel.update_sample(_sample(state="FAULT"))
        assert panel._phase_lbl.text() == "OCV-SOC HIBA"

    def test_chemid_fault_shows_fault_phase_not_normal_running_label(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHEMID", "step_index": 0, "step_count": 1,
            "step_label": "chemid",
        })
        panel.update_sample(_sample(state="FAULT"))
        assert panel._phase_lbl.text() == "CHEMID HIBA"


class TestIecDcResistancePhaseMap:
    """MAX verifier lelet (governance-v2 kör 2, F4, DFAM-001+018 3.
    előfordulás -- l. TestCF26PhaseMapCompleteness osztály kommentje
    ugyanerről a hibaosztályról a CHEMID esetén): az IEC_DC_RESISTANCE
    StepKind hiányzott a _PHASE_MAP/_STEP_KIND_DISPLAY-ből."""

    def test_load_1_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "IEC_DC_RESISTANCE", "step_index": 0, "step_count": 1,
            "step_label": "iec_dc_resistance",
        })
        panel.update_sample(_sample(state="LOAD_1"))
        assert panel._phase_lbl.text() == "IEC RB TERHELÉS 1 (4×I10)"

    def test_open_circuit_rest_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "IEC_DC_RESISTANCE", "step_index": 0, "step_count": 1,
            "step_label": "iec_dc_resistance",
        })
        panel.update_sample(_sample(state="OPEN_CIRCUIT_REST"))
        assert panel._phase_lbl.text() == "IEC RB NYITOTT KÖRI PIHENŐ"

    def test_unmapped_state_falls_back_to_step_kind_display_not_raw_step_kind(self, qapp):
        """A fix ELŐTT ez a nyers 'IEC_DC_RESISTANCE' step_kind stringet
        adta volna vissza."""
        panel = LivePanel()
        panel.set_step({
            "step_kind": "IEC_DC_RESISTANCE", "step_index": 0, "step_count": 1,
            "step_label": "iec_dc_resistance",
        })
        panel.update_sample(_sample(state=None))
        assert panel._phase_lbl.text() == "IEC RB"

    def test_fault_shows_fault_phase_not_normal_running_label(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "IEC_DC_RESISTANCE", "step_index": 0, "step_count": 1,
            "step_label": "iec_dc_resistance",
        })
        panel.update_sample(_sample(state="FAULT"))
        assert panel._phase_lbl.text() == "IEC RB HIBA"


class TestStructuralPreventionStepKindDisplayCoverage:
    """MAX verifier lelet (governance-v2 kör 2, F4): ez a hibaosztály
    (egy új összetett StepKind bevezetésekor a _STEP_KIND_DISPLAY/
    _PHASE_MAP bővítésének elmaradása) HARMADSZOR fordult elő
    (GOLDEN_GG/TRADITIONAL_LEARNING 2026-08-23, CHEMID CF-26, most
    IEC_DC_RESISTANCE) -- CLAUDE.md §8 szerint 2. előfordulás után
    STRUCTURAL_PREVENTION_REVIEW kötelező. Refaktor helyett (ami növelné
    a viselkedési teret) egy őrteszt: minden StepKind tagnak KELL legyen
    _STEP_KIND_DISPLAY bejegyzése, hogy egy jövőbeli új StepKind
    bevezetése ne maradhasson csendben lefedetlen."""

    def test_every_step_kind_has_a_display_label(self, qapp):
        from Prog.src.test_runner import StepKind
        missing = [
            kind.value for kind in StepKind
            if kind.value not in _STEP_KIND_DISPLAY
        ]
        assert missing == [], f"Hiányzó _STEP_KIND_DISPLAY bejegyzés: {missing}"


class TestReviewRound5GuiF8PhaseMapMissingKeys:
    """[regresszió, review-kör 5 (2026-09-10), GUI/UX F8 (P3, DFAM-001+018
    4. előfordulása)] A CHARGE/DISCHARGE/RELAX/OCV_SOC controllerek
    INIT/PRECHECK/PSU_PRESET/*_DONE állapotai MÁR RÉGÓTA léteztek a saját
    State enumjukban (l. charge_controller.py::ChargeState stb.), de a
    _PHASE_MAP sosem térképezte őket -- a fallback (_STEP_KIND_DISPLAY)
    generikus címkéje miatt egy MÁR BEFEJEZETT töltés/kisütés/relax/OCV-SOC
    fázis megkülönböztethetetlen volt egy ténylegesen még futótól."""

    def test_charge_psu_preset_shows_detailed_phase(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHARGE", "step_index": 0, "step_count": 1,
            "step_label": "charge",
        })
        panel.update_sample(_sample(state="PSU_PRESET"))
        assert panel._phase_lbl.text() == "CHARGE PSU BEÁLLÍTÁS"

    def test_charge_done_shows_detailed_phase_not_generic_charge(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHARGE", "step_index": 0, "step_count": 1,
            "step_label": "charge",
        })
        panel.update_sample(_sample(state="CHARGE_DONE"))
        assert panel._phase_lbl.text() == "CHARGE KÉSZ"

    def test_discharge_done_shows_detailed_phase_not_generic_discharge(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "DISCHARGE", "step_index": 0, "step_count": 1,
            "step_label": "discharge",
        })
        panel.update_sample(_sample(state="DISCHARGE_DONE"))
        assert panel._phase_lbl.text() == "DISCHARGE KÉSZ"

    def test_relax_done_shows_detailed_phase_not_generic_relax(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "RELAX", "step_index": 0, "step_count": 1,
            "step_label": "relax",
        })
        panel.update_sample(_sample(state="RELAX_DONE"))
        assert panel._phase_lbl.text() == "RELAX KÉSZ"

    def test_ocv_soc_done_shows_detailed_phase_not_generic_ocv_soc(self, qapp):
        panel = LivePanel()
        panel.set_step({
            "step_kind": "OCV_SOC", "step_index": 0, "step_count": 1,
            "step_label": "ocv_soc",
        })
        panel.update_sample(_sample(state="DONE"))
        assert panel._phase_lbl.text() == "OCV-SOC KÉSZ"


class TestStructuralPreventionPhaseMapCoverage:
    """CLAUDE.md §8 (DFAM-001+018, 4. előfordulás -- l.
    TestCF26PhaseMapCompleteness/TestIecDcResistancePhaseMap/
    TestStructuralPreventionStepKindDisplayCoverage fenti kommentjei
    ugyanerről a hibaosztályról): a _STEP_KIND_DISPLAY-teljesség meglévő
    őrtesztje csak a StepKind-szintű fallback-címkét fedte le, a
    _PHASE_MAP saját, (StepKind, controller-state) granularitású
    teljességét NEM -- review-kör 5 GUI F8 pont emiatt talált 11 rég
    létező, de sosem térképezett (StepKind, state) párt. Refaktor helyett
    (CLAUDE.md §8: 2. előfordulás után kötelező mérlegelés, de nem
    automatikus refaktor) egy générikus lefedettségi őrteszt minden
    ismert, saját state-enummal rendelkező StepKind-ra, hogy egy jövőbeli
    új controller-állapot bevezetése ne maradhasson csendben lefedetlen a
    Fázis-kijelzésben."""

    def test_every_controller_state_has_a_phase_map_entry(self, qapp):
        from Prog.src.charge_controller import ChargeState
        from Prog.src.chemid_controller import ChemIdState
        from Prog.src.discharge_controller import DischargeState
        from Prog.src.golden_gg_controller import GoldenGgState
        from Prog.src.iec_dc_resistance_controller import IecDcResistanceState
        from Prog.src.ocv_soc_controller import OcvSocState
        from Prog.src.relax_controller import RelaxState
        from Prog.src.traditional_learning_controller import LearningState

        step_kind_to_state_enum = {
            "CHARGE": ChargeState,
            "DISCHARGE": DischargeState,
            "RELAX": RelaxState,
            "OCV_SOC": OcvSocState,
            "GOLDEN_GG": GoldenGgState,
            "TRADITIONAL_LEARNING": LearningState,
            "CHEMID": ChemIdState,
            "IEC_DC_RESISTANCE": IecDcResistanceState,
        }
        missing = [
            f"{step_kind}.{state.value}"
            for step_kind, state_enum in step_kind_to_state_enum.items()
            for state in state_enum
            if (step_kind, state.value) not in _PHASE_MAP
        ]
        assert missing == [], f"Hiányzó _PHASE_MAP bejegyzés: {missing}"


class TestLongSessionDecimation:
    """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), GUI/UX P2
    lelet] A korábbi deque(maxlen=_MAX_POINTS) egy CSÚSZÓABLAK volt — a
    session KORAI mintái véglegesen eldobódtak minden új minta érkezésekor
    (nem decimation, adatvesztő csonkítás). A mostani _decimate_all()
    minden MÁSODIK pontot megtartva megfelezi a puffert, amikor eléri a
    _MAX_POINTS-ot — a session TELJES időtartama (t=0-tól a jelenlegi
    pillanatig) mindig lefedett marad a grafikonon, csak a felbontás
    csökken."""

    def test_first_sample_survives_beyond_max_points_window(self, qapp):
        """A korábbi csúszóablakkal az 1. minta (elapsed_s=0.0) VÉGLEG
        eltűnt volna, amint a puffer betelt (_MAX_POINTS mintánál) — a
        decimation után is jelen kell maradnia a pufferben (ritkítva)."""
        panel = LivePanel()
        for i in range(_MAX_POINTS + 50):
            panel.update_sample(_sample(elapsed_s=float(i), battery_voltage_V=13.0))

        assert panel._xs[0] == 0.0
        assert len(panel._xs) <= _MAX_POINTS

    def test_buffer_still_reflects_latest_sample(self, qapp):
        panel = LivePanel()
        n = _MAX_POINTS + 50
        for i in range(n):
            panel.update_sample(_sample(elapsed_s=float(i), battery_voltage_V=13.0))

        assert panel._xs[-1] == pytest.approx(float(n - 1))

    def test_all_series_stay_synchronized_after_decimation(self, qapp):
        """A 6 puffernek (xs/u_batt/i_signed/temp/charge_ah/discharge_ah)
        MINDIG azonos hosszúnak és index-szinkronban kell maradnia a
        decimation után is — különben a görbék eltolódnának egymáshoz
        képest."""
        panel = LivePanel()
        for i in range(_MAX_POINTS + 50):
            panel.update_sample(_sample(
                elapsed_s=float(i), battery_voltage_V=13.0 + i * 0.0001,
                signed_current_A=float(i) * 0.01,
            ))

        lengths = {
            len(panel._xs), len(panel._u_batt), len(panel._i_signed),
            len(panel._temp), len(panel._charge_ah), len(panel._discharge_ah),
        }
        assert len(lengths) == 1  # mind ugyanaz a hossz

    def test_reset_plots_clears_decimated_buffers(self, qapp):
        panel = LivePanel()
        for i in range(_MAX_POINTS + 50):
            panel.update_sample(_sample(elapsed_s=float(i)))
        panel.reset_plots()
        assert panel._xs == []
        assert panel._u_batt == []


class TestReviewRound4GuiF7DvdtWindowIsTimeBasedNotSampleCount:
    """[regresszió, review-kör 4/5 GUI F7 (P2)] a dV/dt/dA/dt kijelzés
    korábban egy FIX MINTASZÁMÚ (10 minta) ablakot használt -- a
    _decimate_all() (l. TestLongSessionDecimation fenti osztálya) a
    puffert a session előrehaladtával ismétlődően megfelezi, hogy a
    TELJES session időtartama lefedett maradjon a grafikonon, tehát az
    egymást követő TÁROLT minták közti valós időköz a session hosszával
    nő. Egy fix 10-mintás ablak emiatt egyre SZÉLESEBB VALÓS IDŐT fedett
    le minden decimálás után -- egy éles, friss feszültségváltozás
    egyre jobban "elhígult" (kisebb mV/min-t mutatott a ténylegesnél)
    egy hosszú (15-30 órás) session végefelé, az operátor számára
    láthatatlanul. A teszt közvetlenül a TÁROLT pufferek időközét
    állítja be nagyra (100s/minta -- függetlenül attól, hogy ez valódi
    _decimate_all()-hívásokból vagy csak ritka mintavételből ered, a
    windowing-logika szempontjából egyenértékű), hogy gyors, determi-
    nisztikus módon reprodukálja a hatást 3600+ valódi update_sample()
    hívás nélkül."""

    def test_dvdt_uses_time_window_not_stale_wide_sample_spacing(self, qapp):
        panel = LivePanel()
        # 15 "decimált" minta, 100s-onként, lapos 10.0V feszültséggel --
        # ez szimulálja egy hosszú session decimált előtörténetét.
        for i in range(15):
            panel.update_sample(_sample(elapsed_s=float(i * 100), battery_voltage_V=10.0))
        # Az UTOLSÓ, 100s-os intervallumban éles +1.0V ugrás történik.
        panel.update_sample(_sample(elapsed_s=1500.0, battery_voltage_V=11.0))

        # A FIX ELŐTT: 10 mintás ablak (elapsed_s=600..1500, 900s) ->
        # (11.0-10.0)/900*60000 ~= 66.67 mV/min -- erősen elhígítva.
        # A FIX UTÁN: ~20s-os (alapértelmezett 2000ms tick x 10) célablak,
        # de a legközelebbi elérhető korábbi minta 100s-ra van (elapsed_s=
        # 1400) -> (11.0-10.0)/100*60000 = 600.0 mV/min -- a TÉNYLEGES,
        # nem elhígított legutóbbi trend.
        text = panel._dvdt_lbl.text()
        assert text == "+600.00 mV/min"

    def test_dvdt_window_stays_constant_across_repeated_decimation_equivalent_spacing(
        self, qapp
    ):
        """Ugyanaz az éles ugrás-forgatókönyv, de MÉG SZÉLESEBB (400s/minta)
        tárolt-időközzel -- a dV/dt-nek EKKOR IS a legutóbsó tárolt
        intervallum tényleges meredekségét kell mutatnia, ne egy
        egyre táguló régi ablakot."""
        panel = LivePanel()
        for i in range(5):
            panel.update_sample(_sample(elapsed_s=float(i * 400), battery_voltage_V=10.0))
        panel.update_sample(_sample(elapsed_s=2000.0, battery_voltage_V=12.0))

        # (12.0-10.0)/400*60000 = 300.0 mV/min -- a legutóbbi tárolt
        # intervallum tényleges meredeksége, függetlenül attól, hogy az
        # időköz mennyire tág.
        text = panel._dvdt_lbl.text()
        assert text == "+300.00 mV/min"


class TestCF27StalenessIndicator:
    """Governance-v2 review-kör 1 (CF-27, GUI-R1-08, DFAM-001): a
    bq_diagnostics_panel.py::_mark_all_stale()/_refresh_freshness_label()
    mintáját követve — a Fázis/Lépés-idő/Session-idő kijelzés korábban
    CSAK update_sample()-ből frissült, tehát mintaszünetben vagy egy
    terminális állapot UTÁN staleness-jelzés nélkül fagyott meg."""

    def test_labels_not_stale_immediately_after_a_fresh_sample(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample())

        panel._refresh_staleness()

        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        assert panel._total_time_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        assert panel._phase_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_labels_marked_stale_after_threshold_with_no_new_sample(self, qapp):
        """A staleness-küszöb (expected_tick_interval_ms * 2.0) letelte
        UTÁN, ha nem érkezett újabb minta, a kijelzésnek jeleznie kell,
        hogy a mutatott érték már nem élő -- ezt a QTimer-vezérelt
        _refresh_staleness() teszi, update_sample() hívása NÉLKÜL."""
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(100)  # 100ms tick a teszt gyorsításához
        panel.update_sample(_sample())
        panel._last_sample_perf -= 1.0  # szimulált 1s mintaszünet (>> 200ms küszöb)

        panel._refresh_staleness()

        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._total_time_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._phase_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

    def test_physical_measurement_labels_marked_stale_too(self, qapp):
        """Review-kör 5, GUI G5-03 (P2): korábban a staleness-jelzés
        KIZÁRÓLAG a Fázis/Lépés-idő/Session-idő címkét fedte le a panel
        17 kijelzett mezőjéből — a fizikai mérési értékek (U/I/T, PSU/
        Load setpontok, dV/dt/dA/dt, warning) egy blokkolt tick alatt
        JELÖLETLENÜL fagytak be, holott update_sample() mindegyiküket
        ugyanabban a tick-vezérelt hívásban tölti fel."""
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(100)
        panel.update_sample(_sample())
        panel._last_sample_perf -= 1.0

        panel._refresh_staleness()

        for lbl in (
            panel._ubatt_lbl, panel._isign_lbl, panel._temp_lbl,
            panel._charge_lbl, panel._disch_lbl, panel._psumode_lbl,
            panel._udrop_lbl, panel._upsu_set_lbl, panel._iload_set_lbl,
            panel._dvdt_lbl, panel._dadt_lbl, panel._warn_lbl,
        ):
            assert lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

    def test_psu_regulation_label_marked_stale_after_threshold(self, qapp):
        """R2-13 (2026-09-15, DFAM-001 sibling-omission a G5-03 fixhez
        képest): a "PSU reguláció" címke a worker psu_regulation_ready
        szignálján frissül, NEM update_sample()-ből, de UGYANABBAN a
        runner tick-ciklusban keletkezik -- egy blokkolt tick/terminális
        állapot UTÁN eddig jelöletlenül fagyott be az utolsó CV/CC
        értéken. Az érték szövege a staleness-jelzés UTÁN is olvasható
        marad -- csak a stílus vált."""
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(100)
        panel.update_sample(_sample())
        panel.update_psu_regulation({"ON": True, "CV": True})
        panel._last_sample_perf -= 1.0

        panel._refresh_staleness()

        assert panel._psu_regulation_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._psu_regulation_lbl.text() == "CV"

    def test_new_sample_clears_psu_regulation_staleness(self, qapp):
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(100)
        panel.update_sample(_sample())
        panel.update_psu_regulation({"ON": True, "CV": True})
        panel._last_sample_perf -= 1.0
        panel._refresh_staleness()
        assert panel._psu_regulation_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.update_sample(_sample())

        assert panel._psu_regulation_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_event_driven_labels_not_affected_by_staleness(self, qapp):
        """L. test_physical_measurement_labels_marked_stale_too
        docstringje: a _rb*_lbl (pulzus-ellenállás), _state_lbl/
        _fault_lbl (esemény-vezérelt) és _step_info_lbl/_device_err_lbl
        (számláló-jellegű) SZÁNDÉKOSAN NEM tagja a staleness-halmaznak —
        ezek legitim módon maradhatnak változatlanok több tick alatt is."""
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(100)
        panel.update_sample(_sample())
        panel._last_sample_perf -= 1.0
        base_styles = {
            lbl: lbl.styleSheet()
            for lbl in (
                panel._rb1s_lbl, panel._rb10s_lbl, panel._rb30s_lbl,
                panel._rb_dv_lbl, panel._state_lbl, panel._fault_lbl,
                panel._step_info_lbl, panel._device_err_lbl,
            )
        }

        panel._refresh_staleness()

        for lbl, style_before in base_styles.items():
            assert lbl.styleSheet() == style_before

    def test_new_sample_clears_stale_marking(self, qapp):
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(100)
        panel.update_sample(_sample())
        panel._last_sample_perf -= 1.0
        panel._refresh_staleness()
        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.update_sample(_sample())

        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_no_staleness_check_before_first_sample(self, qapp):
        """Session indítás előtt (nincs még egyetlen minta sem) a
        staleness-timer ne dobjon kivételt és ne jelöljön semmit
        elavultnak."""
        panel = LivePanel()
        panel._refresh_staleness()
        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_reset_plots_clears_staleness_state(self, qapp):
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(100)
        panel.update_sample(_sample())
        panel._last_sample_perf -= 1.0
        panel._refresh_staleness()
        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.reset_plots()

        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        assert panel._last_sample_perf is None


class TestST1FastLoopAwareStaleness:
    """ST1 (G1-UD-7, DFAM-028): a küszöb (runner_tick_s + fast-loop keret)
    × 2 -- a fast loop miatt normál töltés alatt több másodperces mintaköz
    nem jelez, a valódi elakadás és a DONE/FAULT/STOPPED utáni befagyás
    igen; a validity-/Rb-jelölés és a névleges dV/dt-ablak változatlan."""

    @staticmethod
    def _panel(budget_s: float = 4.0) -> LivePanel:
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(2000)
        panel.set_fast_loop_budget_s(budget_s)
        return panel

    @staticmethod
    def _tracked_styles(panel: LivePanel) -> set[str]:
        return {lbl.styleSheet() for lbl in panel._stale_tracked_labels}

    @pytest.mark.parametrize("age_s", [7.5, 11.5])
    def test_normal_long_fast_loop_interval_is_not_stale(self, qapp, age_s):
        panel = self._panel()
        panel.update_sample(_sample())
        panel._last_sample_perf -= age_s

        panel._refresh_staleness()

        assert self._tracked_styles(panel) == {_LIVE_LABEL_BASE_STYLE}

    def test_stall_beyond_threshold_is_stale_and_new_sample_clears_it(self, qapp):
        panel = self._panel()
        panel.update_sample(_sample())
        panel._last_sample_perf -= 12.5

        panel._refresh_staleness()
        assert self._tracked_styles(panel) == {_LIVE_LABEL_STALE_STYLE}

        panel.update_sample(_sample())
        assert self._tracked_styles(panel) == {_LIVE_LABEL_BASE_STYLE}

    @pytest.mark.parametrize("status", ["DONE", "FAULT", "STOPPED"])
    def test_frozen_display_after_terminal_status_is_stale(self, qapp, status):
        panel = self._panel()
        panel.update_sample(_sample())
        panel.set_status(status)
        state_style = panel._state_lbl.styleSheet()
        panel._last_sample_perf -= 12.5

        panel._refresh_staleness()

        assert self._tracked_styles(panel) == {_LIVE_LABEL_STALE_STYLE}
        assert panel._state_lbl.styleSheet() == state_style

    def test_validity_and_rb_marking_survive_expected_silence(self, qapp):
        panel = self._panel()
        panel.update_sample(_sample(
            battery_voltage_V=12.85, dmm_voltage_valid=False,
            rb_1s_mohm=48.2, rb_timing_degraded=True,
        ))
        panel._last_sample_perf -= 7.5

        panel._refresh_staleness()

        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._isign_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        for lbl in (panel._rb1s_lbl, panel._rb10s_lbl, panel._rb30s_lbl):
            assert lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

    def test_reset_plots_keeps_fast_loop_budget(self, qapp):
        panel = self._panel()
        panel.reset_plots()
        panel.update_sample(_sample())
        panel._last_sample_perf -= 7.5

        panel._refresh_staleness()

        assert self._tracked_styles(panel) == {_LIVE_LABEL_BASE_STYLE}

    def test_zero_budget_keeps_nominal_threshold(self, qapp):
        panel = self._panel(budget_s=0.0)
        panel.update_sample(_sample())
        panel._last_sample_perf -= 4.5

        panel._refresh_staleness()

        assert self._tracked_styles(panel) == {_LIVE_LABEL_STALE_STYLE}

    @pytest.mark.parametrize("budget_s", [0.0, 4.0])
    def test_fast_loop_budget_does_not_widen_derivative_window(self, qapp, budget_s):
        """A dV/dt-ablak 10 névleges tick (20 s); négyzetes sorral a
        20–40 s-os ablak eredménye eltér a hibásan 60 s-ra tágítottétól
        (+240.00 mV/min, +2400.0 mA/min)."""
        panel = self._panel(budget_s=budget_s)
        for k in range(21):
            t = 2.0 * k
            panel.update_sample(_sample(
                elapsed_s=t,
                battery_voltage_V=13.0 + 1e-4 * t * t,
                signed_current_A=0.5 + 1e-3 * t * t,
            ))

        assert panel._dvdt_lbl.text() == "+360.00 mV/min"
        assert panel._dadt_lbl.text() == "+3600.0 mA/min"


class TestR1702DmmValidityMarking:
    """[regresszió, Kör 17 MAX-effort review, R17-02, DFAM-001] a sample
    dict már régóta hordozza a dmm_voltage_valid/dmm_temperature_valid
    kulcsokat (test_runner.py::_build_sample()), de a LivePanel korábban
    sosem olvasta -- egy sikertelen DMM-olvasás után a controllerek
    MEGŐRZÖTT (nem nullázott) utolsó-jó értéke hihető, friss mérésnek
    látszott. A meglévő staleness-stílust (nem új vizuális nyelvet)
    hasznosítja újra."""

    def test_invalid_dmm_voltage_marks_ubatt_label(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(battery_voltage_V=12.85, dmm_voltage_valid=False))

        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._ubatt_lbl.text() == "12.850 V"

    def test_valid_dmm_voltage_leaves_ubatt_label_normal(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(battery_voltage_V=12.85, dmm_voltage_valid=True))

        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_invalid_dmm_temperature_marks_temp_label(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(battery_temperature_C=25.0, dmm_temperature_valid=False))

        assert panel._temp_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._temp_lbl.text() == "25.0 °C"

    def test_valid_dmm_temperature_leaves_temp_label_normal(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(battery_temperature_C=25.0, dmm_temperature_valid=True))

        assert panel._temp_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_missing_validity_keys_are_treated_as_valid(self, qapp):
        """Egy régi sémájú (vagy direktben tesztelt) sample dict, ami
        MÉG nem hordozza a *_valid kulcsokat, NEM eshet csendben
        ''invalid''-nak -- sample.get(key, True), nem sample.get(key)."""
        panel = LivePanel()
        panel.update_sample(_sample())  # nincs dmm_*_valid kulcs

        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        assert panel._temp_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_fresh_valid_sample_clears_previous_invalid_marking(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(dmm_voltage_valid=False))
        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.update_sample(_sample(dmm_voltage_valid=True))
        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_signed_current_label_not_marked_when_psu_readback_invalid(self, qapp):
        """[KEEP_AS_IS döntés rögzítése] psu_readback_valid/
        load_readback_valid SZÁNDÉKOSAN nincs jelölve -- ha False,
        _build_sample() már signed_current_A=None-t ír, tehát _isign_lbl
        már "– A"-t mutat, nincs hihető stale szám a kijelzőn. Több
        StepKind nem-energia alfázisában ez a flag legitim módon False
        signed_current_A=0.0 mellett -- jelölése permanens hamis
        riasztást adna (DFAM-028)."""
        panel = LivePanel()
        panel.update_sample(_sample(signed_current_A=None, psu_readback_valid=False))

        assert panel._isign_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        assert panel._isign_lbl.text() == "– A"

    def test_every_sample_valid_key_is_marked_or_explicitly_excluded(self, qapp):
        """Séma-teljességi meta-teszt (DFAM-001 saját, ismételten
        regisztrált tanulsága: egy új *_valid mezőt AZONNAL, teljes
        sibling-sweeppel kell lefedni, nem réteges felfedezéssel). Minden
        jelenleg létező, a sample dict-ben megjelenő "*_valid" kulcs
        vagy a _VALIDITY_MARKED_LABELS tagja, vagy explicit
        _VALIDITY_KEYS_INTENTIONALLY_NOT_MARKED-ban van felsorolva.

        [MAX verifier lelete, 2026-09-16] a "ground truth" halmaz a
        `logger.CSV_COLUMNS`-ból SZÁRMAZTATOTT (nem egy második,
        kézzel karbantartott literál) -- ez a sample dict TÉNYLEGES
        producer-listája (test_runner.py::_build_sample() ebből
        seedel), tehát egy jövőbeli 8. *_valid kulcs, ami a
        CSV_COLUMNS-ba bekerül, de sem a marked, sem az excluded
        tuple-ből nem hivatkozott, TÉNYLEGESEN buktatja ezt a tesztet
        (nem csak egy másik, ugyanolyan kézzel karbantartott
        literál-listával egyezik). A temperature_valid/temperature_2_
        valid (OCV_SOC_COLUMNS/IEC_DC_RESISTANCE_COLUMNS) szándékosan
        NEM ide tartoznak -- azok egy MÁSIK CSV-írót szolgálnak,
        update_sample() sample dict-jébe sosem jutnak el."""
        from Prog.gui.panels.live_panel import _VALIDITY_KEYS_INTENTIONALLY_NOT_MARKED
        from Prog.src.logger import CSV_COLUMNS

        panel = LivePanel()
        marked_keys = {key for key, _lbl in panel._VALIDITY_MARKED_LABELS}
        excluded_keys = set(_VALIDITY_KEYS_INTENTIONALLY_NOT_MARKED)
        all_known_valid_keys = {c for c in CSV_COLUMNS if c.endswith("_valid")}
        assert all_known_valid_keys == marked_keys | excluded_keys
        assert marked_keys.isdisjoint(excluded_keys)

    def test_worker_signal_chain_delivers_invalid_marking(self, qapp):
        """[rules/60-testing.md: worker->signal->handler teljes lánc]

        MEGJEGYZÉS (MAX verifier lelete, 2026-09-16): ez a kapcsolat
        AZONOS száloz kötött (direct connection), a produkciós út
        (main_window.py::Worker, moveToThread + queued connection)
        NEM. A verifier empirikusan lemért egy valódi queued
        Signal(dict) átvitelt és megerősítette, hogy az `is False`
        identitás-ellenőrzés a bool típust megőrzi -- ez a teszt tehát
        NEM bizonyítja a queued-marshalling esetet, csak a kapcsolási
        láncot. QThread-alapú teszt szándékosan NEM készült (l.
        feedback_qthread_test_leak_and_stash_chaining -- cleanup
        nélküli QThread-teszt a következő tesztet fagyasztja)."""
        from PySide6.QtCore import QObject, Signal

        class _FakeWorker(QObject):
            sample_ready = Signal(dict)

        panel = LivePanel()
        fake = _FakeWorker()
        fake.sample_ready.connect(panel.update_sample)
        fake.sample_ready.emit(_sample(battery_voltage_V=12.9, dmm_voltage_valid=False))

        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

    def test_invalid_dmm_voltage_plots_nan_not_stale_value(self, qapp):
        """[regresszió, Kör 18 MAX-effort review, GUI/UX reviewer P2-3,
        DFAM-001 (R17-02 sibling-hézag)] R17-02 a CÍMKÉT jelöli, de a
        chart-görbébe eddig a sticky last-good értéket írta feltétel
        nélkül -- egy tartós DMM-kiesés alatt a grafikon sima, hihető
        szakaszt rajzolt, megkülönböztethetetlenül egy valós méréstől.
        A plot-pufferben most NaN kell legyen érvénytelen mintánál,
        MÍG a címke a stale értéket (stílussal jelölve) továbbra is
        megjeleníti -- l. a fenti test_invalid_dmm_voltage_marks_ubatt_
        label docstringje ugyanerről a szándékos kettősségről."""
        panel = LivePanel()
        panel.update_sample(_sample(battery_voltage_V=13.0, dmm_voltage_valid=True))
        panel.update_sample(_sample(battery_voltage_V=12.85, dmm_voltage_valid=False))

        plotted = list(panel._u_batt)
        assert plotted[0] == 13.0
        assert plotted[1] != plotted[1]  # NaN
        # A címke a R17-02 mintájának megfelelően a stale értéket mutatja:
        assert panel._ubatt_lbl.text() == "12.850 V"
        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

    def test_invalid_dmm_temperature_plots_nan_not_stale_value(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(battery_temperature_C=22.0, dmm_temperature_valid=True))
        panel.update_sample(_sample(battery_temperature_C=21.5, dmm_temperature_valid=False))

        plotted = list(panel._temp)
        assert plotted[0] == 22.0
        assert plotted[1] != plotted[1]  # NaN
        assert panel._temp_lbl.text() == "21.5 °C"
        assert panel._temp_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

    def test_valid_dmm_voltage_still_plots_the_measured_value(self, qapp):
        """Ellenpróba: érvényes mintánál a plot-puffer a MÉRT értéket
        kapja, nem NaN-t -- a guard csak az invalid ágon aktív."""
        panel = LivePanel()
        panel.update_sample(_sample(battery_voltage_V=13.1, dmm_voltage_valid=True))

        assert panel._u_batt[-1] == 13.1

    def test_step_change_does_not_erase_dmm_validity_marking(self, qapp):
        """[regresszió, Kör 18 Batch C14, GUI-P3-3, DFAM-001 sibling]
        set_step() (lépéshatár, pl. RELAX -> CHARGE fázisváltás)
        _clear_staleness()-t hív, ami korábban FELTÉTEL NÉLKÜL letörölte
        a _VALIDITY_MARKED_LABELS stílusát is -- egy DMM-kiesés alatt a
        lépéshatár átmenetileg "frissnek" mutatta az érvénytelen
        feszültség-címkét, holott a mögöttes utolsó-ismert érvényesség
        NEM változott."""
        panel = LivePanel()
        panel.update_sample(_sample(battery_voltage_V=12.85, dmm_voltage_valid=False))
        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.set_step({
            "step_kind": "CHARGE", "step_index": 1, "step_count": 2,
            "step_label": "charge",
        })

        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._ubatt_lbl.text() == "12.850 V"

    def test_reset_plots_drops_the_validity_memo(self, qapp):
        """Új session induláskor a lépéshatár-védelem memóriája is
        nullázódik -- egy ÚJ session ne őrizze meg az ELŐZŐ session
        érvénytelenség-jelzését."""
        panel = LivePanel()
        panel.update_sample(_sample(battery_voltage_V=12.85, dmm_voltage_valid=False))
        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.reset_plots()

        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        panel.set_step({
            "step_kind": "CHARGE", "step_index": 0, "step_count": 1,
            "step_label": "charge",
        })
        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_step_change_still_clears_timer_driven_staleness_on_other_labels(self, qapp):
        """[NORMAL verifier megjegyzése, Kör 18 Batch C14/GUI-P3-3, P3-1]
        a set_step()-be épített _invalid_marked_labels-memó SZŰKÍTI
        _clear_staleness() hatását -- ez a teszt bizonyítja, hogy a
        szűkítés NEM terjed túl a DMM-érvénytelenség-jelölt címkéken: a
        G5-03/CF-27 időzítés-vezérelt staleness (_refresh_staleness())
        a TÖBBI 14 nyomkövetett címkén (pl. _step_time_lbl) továbbra is
        törlődik egy lépéshatáron, akárcsak eddig."""
        panel = LivePanel()
        panel.set_expected_tick_interval_ms(100)
        panel.update_sample(_sample(dmm_voltage_valid=False))
        panel._last_sample_perf -= 1.0  # szimulált mintaszünet
        panel._refresh_staleness()
        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.set_step({
            "step_kind": "CHARGE", "step_index": 1, "step_count": 2,
            "step_label": "charge",
        })

        assert panel._step_time_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        assert panel._ubatt_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE


class TestD7RbTimingDegradedMarking:
    """[regresszió, Kör 19 D7, GUI-R19-01, DFAM-001 2. előfordulása
    UGYANEBBEN a fájlban, l. TestR1702DmmValidityMarking az eredeti
    alakzatért] a sample dict már régóta hordozza a rb_timing_degraded
    kulcsot (test_runner.py::_build_sample(), discharge_controller.py-ból),
    de a LivePanel korábban sosem olvasta -- egy időzítés-degradált Rb
    érték ugyanúgy nézett ki, mint egy pontos."""

    def test_rb_labels_have_their_own_style_at_construction(self, qapp):
        """[Kör 21 F14, F21-G2] a 3 Rb-címke SAJÁT (22px/bold/lila)
        stílust kap __init__-ben (l. _rb_value_label()), MÉG update_
        sample() ELSŐ hívása ELŐTT -- ez a harmadik hely, ahol az
        archívum MINIMAL FIX-je a `_RB_LABEL_BASE_STYLE` konstanst
        előírja, a másik kettő (update_sample()/reset_plots() reset-
        ágai) mellett."""
        panel = LivePanel()

        assert panel._rb1s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE
        assert panel._rb10s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE
        assert panel._rb30s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE

    def test_rb_timing_degraded_marks_all_three_rb_labels_stale(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(rb_1s_mohm=48.2, rb_timing_degraded=True))

        assert panel._rb1s_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._rb10s_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._rb30s_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE
        assert panel._rb1s_lbl.text() == "48.2 mΩ"

    def test_rb_timing_not_degraded_leaves_rb_labels_normal(self, qapp):
        """[Kör 21 F14, F21-G2] a "normál" (nem-degradált) stílus a
        CÍMKÉK SAJÁT (22px/bold/lila) alapstílusa, NEM a generikus
        15px-es _LIVE_LABEL_BASE_STYLE -- ez a teszt korábban a hibát
        magát kódolta (a régi kód a generikus stílusra IS visszaesett)."""
        panel = LivePanel()
        panel.update_sample(_sample(rb_1s_mohm=48.2, rb_timing_degraded=False))

        assert panel._rb1s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE
        assert panel._rb10s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE
        assert panel._rb30s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE

    def test_rb_timing_degraded_missing_key_leaves_rb_labels_normal(self, qapp):
        """Régi sémájú/direkt tesztelt sample dict (kulcs hiányzik) ne
        essen csendben degradáltnak -- l. a validity-jelölés azonos
        elvét a fenti R17-02 osztályban."""
        panel = LivePanel()
        panel.update_sample(_sample(rb_1s_mohm=48.2))

        assert panel._rb1s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE

    def test_rb_timing_degraded_style_survives_set_step(self, qapp):
        """L. TestR1702DmmValidityMarking::
        test_step_change_still_clears_timer_driven_staleness_on_other_labels
        azonos mintáját -- a _rb*_lbl SOSEM tagja _stale_tracked_labels-nek,
        tehát set_step() _clear_staleness()-hívása eleve nem érintheti, DE
        ez a teszt explicit bizonyítja a tulajdonságot."""
        panel = LivePanel()
        panel.update_sample(_sample(rb_1s_mohm=48.2, rb_timing_degraded=True))
        assert panel._rb1s_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.set_step({
            "step_kind": "CHARGE", "step_index": 1, "step_count": 2,
            "step_label": "charge",
        })

        assert panel._rb1s_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

    def test_next_step_non_degraded_sample_clears_rb_style(self, qapp):
        """A degradált stílus a KÖVETKEZŐ lépés friss, nem-degradált
        mintájával helyesen visszaáll -- nem ragad a session hátralévő
        részére."""
        panel = LivePanel()
        panel.update_sample(_sample(rb_1s_mohm=48.2, rb_timing_degraded=True))
        assert panel._rb1s_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.update_sample(_sample(rb_1s_mohm=50.1, rb_timing_degraded=False))

        assert panel._rb1s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE

    def test_reset_plots_clears_rb_timing_degraded_style(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(rb_1s_mohm=48.2, rb_timing_degraded=True))
        assert panel._rb1s_lbl.styleSheet() == _LIVE_LABEL_STALE_STYLE

        panel.reset_plots()

        assert panel._rb1s_lbl.styleSheet() == _RB_LABEL_BASE_STYLE
        assert panel._rb1s_lbl.text() == "– mΩ"


class TestD7WarningFlagsEdgeTriggeredEventLog:
    """[regresszió, Kör 19 D7, GUI-R19-02, DFAM-018 severity-collapse +
    DFAM-021-adjacent] a warning_flags korábban kizárólag a _warn_lbl
    szövegeként jelent meg, ami minden ticken BASE stílusra állt vissza
    (_stale_tracked_labels tagjaként) -- zéró vizuális súlyú, eltűnő
    jelzés. Élő eseménnyé alakítva, ÁTMENET-alapon (bq_diagnostics_panel.py
    ::_check_field_change() mintáját követve)."""

    def test_new_warning_code_appends_event_to_log(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))

        assert panel._event_log._list.count() == 1
        assert "LOAD_CURRENT_SETPOINT_MISMATCH" in panel._event_log._list.item(0).text()

    def test_same_warning_code_repeated_does_not_spam_log(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))

        assert panel._event_log._list.count() == 1

    def test_different_warning_code_fires_second_event(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))
        panel.update_sample(_sample(warning_flags="TEMPERATURE_HIGH"))

        assert panel._event_log._list.count() == 2
        assert "TEMPERATURE_HIGH" in panel._event_log._list.item(1).text()

    def test_warning_cleared_then_same_code_again_refires(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))
        panel.update_sample(_sample(warning_flags=None))     # figyelmeztetés megszűnt
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))  # újra fellép

        assert panel._event_log._list.count() == 2

    def test_no_warning_never_appends_event(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(warning_flags=None))
        panel.update_sample(_sample())

        assert panel._event_log._list.count() == 0

    def test_reset_plots_allows_same_code_to_refire_in_new_session(self, qapp):
        panel = LivePanel()
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))
        assert panel._event_log._list.count() == 1

        panel.reset_plots()
        panel.update_sample(_sample(warning_flags="LOAD_CURRENT_SETPOINT_MISMATCH"))

        assert panel._event_log._list.count() == 1


class TestCF29DeviceErrorIndicator:
    """[regresszió, governance-v2 review-kör 1, CF-29, GUI-R1-11,
    DFAM-012 rokona] a device_error_ready worker-signal korábban
    KIZÁRÓLAG a háttér DeviceErrorPanel-re volt kötve -- élő nézeten
    semmi nem jelezte, hogy SCPI hiba történt, amíg valaki külön át nem
    váltott a háttérfülre. LivePanel.append_device_error() egy kompakt,
    élő számlálót ad ehhez."""

    def test_initial_indicator_shows_zero(self, qapp):
        panel = LivePanel()
        assert panel._device_err_lbl.text() == "0"
        assert panel._device_err_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_single_device_error_increments_and_marks_stale_style(self, qapp):
        panel = LivePanel()
        panel.append_device_error({"device": "PSU", "error": "err1"})
        assert panel._device_err_lbl.text() == "1"
        assert panel._device_err_lbl.styleSheet() != _LIVE_LABEL_BASE_STYLE

    def test_multiple_device_errors_accumulate(self, qapp):
        panel = LivePanel()
        for _ in range(3):
            panel.append_device_error({"device": "PSU", "error": "err"})
        assert panel._device_err_lbl.text() == "3"

    def test_reset_plots_clears_device_error_indicator(self, qapp):
        panel = LivePanel()
        panel.append_device_error({"device": "PSU", "error": "err"})
        panel.reset_plots()
        assert panel._device_err_lbl.text() == "0"
        assert panel._device_err_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE

    def test_reset_device_error_count_resets_only_the_counter(self, qapp):
        """[regresszió, review-kör 4/5 GUI F5 (P2)] a dedikált
        reset_device_error_count() -- amit a DeviceErrorPanel.cleared jele
        hív mid-session -- KIZÁRÓLAG az eszközhiba-számlálót nullázza, NEM
        a teljes élő nézetet (pl. a Fázis-címke NEM esik vissza "–"-ra)."""
        panel = LivePanel()
        panel.set_step({
            "step_kind": "CHARGE", "step_index": 0, "step_count": 1,
            "step_label": "charge",
        })
        panel.update_sample(_sample(state="CHARGE_CC"))
        panel.append_device_error({"device": "PSU", "error": "err"})

        panel.reset_device_error_count()

        assert panel._device_err_lbl.text() == "0"
        assert panel._device_err_lbl.styleSheet() == _LIVE_LABEL_BASE_STYLE
        assert panel._phase_lbl.text() == "CHARGE CC"


class TestPsuRegulationDisplay:
    """Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói
    döntés 2026-09-10): a PSU CV/CC regulációs állapotát megjelenítő
    élő mező -- KÜLÖN a _psumode_lbl-től (kombinációs mód), a worker
    psu_regulation_ready szignálján keresztül frissül, SOSEM piros/
    hiba-stílusú (nem hibajelzés)."""

    def test_initial_state_shows_placeholder(self, qapp):
        panel = LivePanel()
        assert panel._psu_regulation_lbl.text() == "–"

    def test_output_off_shows_off(self, qapp):
        panel = LivePanel()
        panel.update_psu_regulation({"CV": False, "CC": False, "ON": False})
        assert panel._psu_regulation_lbl.text() == "OFF"

    def test_constant_voltage_shows_cv(self, qapp):
        panel = LivePanel()
        panel.update_psu_regulation({"CV": True, "CC": False, "ON": True})
        assert panel._psu_regulation_lbl.text() == "CV"

    def test_constant_current_shows_cc(self, qapp):
        panel = LivePanel()
        panel.update_psu_regulation({"CV": False, "CC": True, "ON": True})
        assert panel._psu_regulation_lbl.text() == "CC"

    def test_on_with_neither_bit_shows_on(self, qapp):
        panel = LivePanel()
        panel.update_psu_regulation({"CV": False, "CC": False, "ON": True})
        assert panel._psu_regulation_lbl.text() == "ON"

    def test_reset_plots_clears_to_placeholder(self, qapp):
        panel = LivePanel()
        panel.update_psu_regulation({"CV": True, "CC": False, "ON": True})
        panel.reset_plots()
        assert panel._psu_regulation_lbl.text() == "–"


class TestGui14DecimateAllNotABrokenQtSlot:
    """[regresszió, governance-v2 review-kör 1, CF-44/GUI-14] _decimate_all()
    egy privát, 0 argumentumos segédmetódus, amit KIZÁRÓLAG közvetlen Python
    hívásként (self._decimate_all()) hívunk update_sample()-ből -- soha nem
    kötjük Qt signalhoz .connect()-tel. Egy elcsúszott (feltehetően
    update_sample(self, sample: dict) fölötti copy-paste-ből maradt)
    @Slot(dict) dekorátor mégis regisztrálta Qt meta-object szinten
    '_decimate_all(QVariantMap)' szignatúrával -- ha valaha egy dict-et
    emittáló signalhoz kötnék, a tényleges (0 paraméteres) Python-függvény
    hívása garantáltan TypeError-ral bukna. A dekorátor semmilyen jelenlegi
    hívási úton nem okoz hibát (direkt Python hívás megkerüli a Qt
    meta-object rendszert), de a helytelen regisztráció önmagában hibás."""

    def test_decimate_all_not_registered_as_qt_slot(self, qapp):
        panel = LivePanel()
        mo = panel.metaObject()
        names = [bytes(mo.method(i).name()).decode() for i in range(mo.methodCount())]
        assert "_decimate_all" not in names

    def test_decimation_still_works_via_direct_call(self, qapp):
        """Regresszió: a dekorátor eltávolítása nem érintheti a tényleges
        decimation-viselkedést, csak a (soha nem használt) Qt-regisztrációt."""
        panel = LivePanel()
        for i in range(_MAX_POINTS + 10):
            panel.update_sample(_sample(elapsed_s=float(i)))
        assert len(panel._xs) <= _MAX_POINTS


class TestR14B2PsuRegulationDisplay:
    @pytest.mark.parametrize("on", [False, True])
    @pytest.mark.parametrize(("cv", "cc"), [(False, False), (True, False), (False, True), (True, True)])
    def test_invalid_cvcc_takes_precedence(self, qapp, on, cv, cc):
        panel = LivePanel()
        style = panel._psu_regulation_lbl.styleSheet()

        panel.update_psu_regulation(
            {"CVCC_VALID": False, "CV": cv, "CC": cc, "ON": on}
        )

        assert panel._psu_regulation_lbl.text() == (
            "ON (SERIES: CV/CC n/a)" if on else "OFF"
        )
        assert panel._psu_regulation_lbl.styleSheet() == style


class TestR14B2PsuRegulationSignalChain:
    """R14-B2: valódi driver/poll/worker-szignál/widget, szinkron Qt-lánc."""

    @pytest.mark.parametrize("mode_name", ["SERIES", "PARALLEL", "INDEPENDENT"])
    @pytest.mark.parametrize(
        ("raw", "cv", "cc", "on", "label"),
        [
            ("0", False, False, False, "OFF"),
            ("9", True, False, True, "CV"),
            ("10", False, True, True, "CC"),
        ],
    )
    def test_driver_to_live_panel(self, qapp, mode_name, raw, cv, cc, on, label):
        from unittest.mock import MagicMock

        from Prog.drivers.device_psu import Keithley2220PSU
        from Prog.gui.worker import TestRunnerWorker
        from Prog.src.safety import PsuMode
        from Prog.src.test_runner import TestPlan, TestRunner, TestRunnerConfig

        mode = PsuMode(mode_name)
        resource = MagicMock()
        resource.query.return_value = raw
        psu = Keithley2220PSU(combination_mode=mode)
        psu._resource = resource
        psu._connected = True
        instruments = MagicMock(psu=psu)
        logger = MagicMock()
        runner = TestRunner(
            instrument_manager=instruments,
            safety=MagicMock(),
            logger=logger,
            profile=MagicMock(),
            config=TestRunnerConfig(),
            charge_ctrl_factory=MagicMock(),
            discharge_ctrl_factory=MagicMock(),
            relax_ctrl_factory=MagicMock(),
        )
        worker = TestRunnerWorker(runner, TestPlan.characterization())
        panel = LivePanel()
        style = panel._psu_regulation_lbl.styleSheet()
        received = []
        unexpected = []
        worker.psu_regulation_ready.connect(received.append)
        worker.psu_regulation_ready.connect(panel.update_psu_regulation)
        for signal in (
            worker.fault, worker.device_error_ready, worker.event_ready,
            worker.sample_ready, worker.finished,
        ):
            signal.connect(unexpected.append)

        # SERIES "9": archivált CC-próba, psu_series_energized_20261105T203730Z.json.
        # A "0" minden módban szintetikus OFF-kontroll, nem HIL-bizonyíték.
        runner._poll_psu_regulation_state()

        if mode == PsuMode.SERIES:
            expected = {"CVCC_VALID": False, "ON": on}
            label = "ON (SERIES: CV/CC n/a)" if on else "OFF"
        else:
            expected = {"CV": cv, "CC": cc, "ON": on}
        assert received == [expected]
        assert panel._psu_regulation_lbl.text() == label
        assert panel._psu_regulation_lbl.styleSheet() == style
        resource.query.assert_called_once_with("STAT:OPER:INST:ISUM1:COND?")
        resource.write.assert_not_called()
        assert logger.mock_calls == []
        assert instruments.mock_calls == []
        assert unexpected == []
        assert runner.status == "IDLE"
        assert runner.emergency_stop_requested is False
