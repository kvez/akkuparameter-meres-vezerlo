"""
TestRunner — fő folyamatvezérlő.
CHARACTERIZATION és BQ_LEARNING_PHYSICAL teszttípusok.
GUI-független, blokkoló run(), stop flag alapú megállítás.
[d4.txt][d5.txt][d6.txt]
"""
from __future__ import annotations

import logging
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from Prog.drivers.device_load import INPUT_ACTIVATION_ACK_TIMEOUT_S
from Prog.src.battery_profile import BatteryProfile
from Prog.src.charge_controller import ChargeState
from Prog.src.chemid_controller import ChemIdState
from Prog.src.discharge_controller import DischargeState
from Prog.src.exceptions import ActivationStopHook, TestAbortedByUser
from Prog.src.golden_gg_controller import GoldenGgController, GoldenGgState
from Prog.src.iec_dc_resistance_controller import (
    IecDcResistanceController,
    IecDcResistanceState,
)
from Prog.src.instrument_manager import (
    InstrumentManager,
    is_energy_state_uncertain,
    is_error_queue_poll_deferred,
    is_input_activation_pending,
)
from Prog.src.logger import CSV_COLUMNS, Logger
from Prog.src.ocv_soc_controller import (
    _PULSE_SERVICE_DMM_OWNER_STATES,
    OcvSocController,
    OcvSocState,
)
from Prog.src.relax_controller import RelaxController, RelaxSamplingService, RelaxState
from Prog.src.safety import SafetyManager, WarningCode
from Prog.src.scpi_error_policy import is_critical_scpi_error
from Prog.src.traditional_learning_controller import (
    LearningState,
    TraditionalLearningController,
)


class TestType(Enum):
    CHARACTERIZATION = "CHARACTERIZATION"
    BQ_LEARNING_PHYSICAL = "BQ_LEARNING_PHYSICAL"
    OCV_SOC_CHARACTERIZATION = "OCV_SOC_CHARACTERIZATION"
    CHARGE_ONLY = "CHARGE_ONLY"
    DISCHARGE_ONLY = "DISCHARGE_ONLY"
    CHEMID_CHARACTERIZATION_PBA = "CHEMID_CHARACTERIZATION_PBA"  # SUP03-CHEMID-010
    TRADITIONAL_LEARNING = "TRADITIONAL_LEARNING"  # WP-AK-16
    GOLDEN_GG = "GOLDEN_GG"  # WP-AK-16
    # Új, önálló mód (governance-v2 kör 2): IEC 60896-21 §6.3 szerinti
    # kétpontos DC belső ellenállás mérés -- FÜGGETLEN az OCV_SOC_
    # CHARACTERIZATION 1s/10s/30s pulzus-alapú módszerétől (az VÁLTOZATLAN
    # marad). L. iec_dc_resistance_controller.py modul-docstringje.
    IEC_DC_INTERNAL_RESISTANCE = "IEC_DC_INTERNAL_RESISTANCE"


class StepKind(Enum):
    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"
    RELAX = "RELAX"
    MANUAL_CHECKPOINT = "MANUAL_CHECKPOINT"
    OCV_SOC = "OCV_SOC"
    CHEMID = "CHEMID"
    TRADITIONAL_LEARNING = "TRADITIONAL_LEARNING"  # WP-AK-16
    GOLDEN_GG = "GOLDEN_GG"  # WP-AK-16
    IEC_DC_RESISTANCE = "IEC_DC_RESISTANCE"


# Teljes kódbázis review 6. kör (2026-08-29), Session/Workflow P2 lelet #3:
# a TRADITIONAL_LEARNING/GOLDEN_GG StepKind egyetlen, opak lépésen belül
# CC/CV töltési ÉS relax-fázisokat is átfog — a NPLC-t korábban a
# _configure_dmm_for_step() a teljes lépésre EGYSZER, a lépés elején
# állította be (charge_discharge_nplc, gyors ág), az OCV_SOC/CHEMID
# StepKind-hoz hasonló relax-fázisai (PRECHARGE_RELAX/RELAX_1/RELAX_2/
# RELAX_PRE_DISCHARGE_2, illetve RELAX_2H/RELAX_5H) SOSEM kapták meg a
# szűk dV/dt-küszöb (TLC: Q-41 szerint 3,5e-6 V/s) kiértékeléséhez szükséges
# alacsony DMM-zajú (magas NPLC) mérést. A fix NEM a teljes StepKind-ot
# sorolja át a magas-NPLC csoportba (az a CC/CV fast_loop szabályozás
# DMM-olvasási ciklusidejét is lassítaná — HIL-kockázat), hanem
# _reconfigure_dmm_nplc_for_relax_substate() dinamikusan, tickenként vált
# a controller ÉLŐ állapotától függően — l. ott a docstringet.
_RELAX_SUBSTATE_NAMES: dict[StepKind, frozenset[str]] = {
    StepKind.TRADITIONAL_LEARNING: frozenset(
        s.name for s in (
            LearningState.PRECHARGE_RELAX, LearningState.RELAX_1,
            LearningState.RELAX_PRE_DISCHARGE_2, LearningState.RELAX_2,
            # Teljes kódbázis review 10. kör (2026-08-31), Session/Workflow
            # P2 lelet: REBOUND_CHECK_1/2 (2026-08-24, `7f714f2`, MIUTÁN ez
            # a térkép megépült — sosem lett utólag felvéve) fizikailag/
            # mérésmódszertanilag megegyezik RELAX_1/RELAX_2-vel (Load OFF,
            # settle-várakozás, majd feszültség-összehasonlítás a target_V +
            # rebound_tolerance_V egyoldalú felső korláttal, l.
            # _run_rebound_check()) — ugyanaz
            # az alacsony DMM-zajú (magas NPLC) mérés indokolt, mint a
            # relax-fázisoknál, a projekt saját NPLC-konvenciója szerint
            # (TUDÁSBÁZIS.txt: "magasabb NPLC = lassabb de pontosabb, pl.
            # 10 PLC akkufeszültséghez").
            LearningState.REBOUND_CHECK_1, LearningState.REBOUND_CHECK_2,
        )
    ),
    StepKind.GOLDEN_GG: frozenset(
        s.name for s in (GoldenGgState.RELAX_2H, GoldenGgState.RELAX_5H)
    ),
}

# Teljes kódbázis review 7. kör (2026-08-30), Session/Workflow P2 lelet #2:
# az OCV_SOC/CHEMID StepKind a TELJES lépésre relax_ocv_nplc-t (lassú,
# alacsony DMM-zaj) kap _configure_dmm_for_step()-ben — ez helyes a
# legtöbb alállapotra (STEP_RELAX/PRECHARGE_RELAX/IMPULSE_*/LOG_SOC_POINT/
# ROOM_TEMPERATURE_CONDITIONING/RELAX_5H_FULL/RELAX_5H_EMPTY stb.), DE a
# PRECHARGE/FULL_CHARGE_MANUFACTURER_METHOD alállapotban egy beágyazott
# ChargeController fut, aminek fast_loop_enabled=True szabályozása (a
# 2026-08-23 OV-incidens mitigációja, ugyanazon TF1/soros dióda útvonalon,
# mint CHARGE_ONLY/TRADITIONAL_LEARNING/GOLDEN_GG) a gyors NPLC-t igényli
# a reakcióképességéhez — l. advance() saját, egy-DMM-tulajdonos kizárási
# listája mindkét controllerben (ugyanezek az állapotok). Ez a térkép az
# ELLENTÉTES irányban működik, mint a fenti _RELAX_SUBSTATE_NAMES: itt a
# lassú az ALAP, és ezek az állapotok DEMOTE-olnak a gyorsra (l.
# _reconfigure_dmm_nplc_for_relax_substate()).
_FAST_SUBSTATE_NAMES: dict[StepKind, frozenset[str]] = {
    StepKind.OCV_SOC: frozenset({OcvSocState.PRECHARGE.name}),
    StepKind.CHEMID: frozenset({ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD.name}),
    # Governance-v2 kör 2: IecDcResistanceController PRECHARGE fázisa is a
    # beágyazott ChargeController fast_loop_enabled=True szabályozását
    # használja, ugyanazon okból, mint OCV_SOC/CHEMID fent -- a PRECHARGE_
    # RELAX/LOAD_1/OPEN_CIRCUIT_REST/LOAD_2/COMPUTE_RESULT állapotok
    # SZÁNDÉKOSAN maradnak lassú/alacsony-zajú NPLC-n (l.
    # _configure_dmm_for_step()), mert az Ua/Ub mintavétel pontossága
    # közvetlenül az Ri/Isc eredmény pontosságát határozza meg.
    StepKind.IEC_DC_RESISTANCE: frozenset({IecDcResistanceState.PRECHARGE.name}),
}

# Kör 20 Batch E2 (F20-S1, P2, DFAM-001 + DFAM-043): a LÉPÉS-SZINTŰ alap-NPLC
# kiválasztási szabálya EGYETLEN helyen. Korábban kizárólag a
# _configure_dmm_for_step() törzsében élt, ezért a per-tick út (l.
# _reconfigure_dmm_nplc_for_relax_substate()) egy sikertelen lépés-eleji
# set_nplc() után NEM tudta újra alkalmazni. A halmaz TARTALMA VÁLTOZATLAN
# a korábbi in-line feltételhez képest (RELAX/OCV_SOC/CHEMID/
# IEC_DC_RESISTANCE = lassú, alacsony zajú alap; minden más = gyors).
_SLOW_BASE_NPLC_STEP_KINDS: frozenset[StepKind] = frozenset({
    StepKind.RELAX, StepKind.OCV_SOC, StepKind.CHEMID,
    StepKind.IEC_DC_RESISTANCE,
})

# R1 R-1 (B7_B7S_fixplan_FINAL.md B7S/2, DFAM-001/028): az aktívan PSU-t/
# Loadot vezérlő StepKind-ok EGYETLEN forrása -- a _run_step() cadence-
# pollja (INS-003 kritikus SCPI-hiba -> FAULT) és a lépéshatár-poll
# (energiaátadó lépés első advance()-e előtt) ugyanezt használja. Kivétel:
# RELAX (nincs PSU/Load-vezérlés) és MANUAL_CHECKPOINT (a runner saját
# safe-offja fut, l. _run_manual_checkpoint()).
_ENERGY_TRANSFER_STEP_KINDS: frozenset[StepKind] = frozenset({
    StepKind.CHARGE, StepKind.DISCHARGE, StepKind.GOLDEN_GG,
    StepKind.TRADITIONAL_LEARNING, StepKind.OCV_SOC, StepKind.CHEMID,
    StepKind.IEC_DC_RESISTANCE,
})


def _critical_polled_errors(step: TestStep, polled_errors: list[dict]) -> list[dict]:
    """R1 R-1: a _poll_device_errors() eredményéből a FAULT-ot kiváltó
    bejegyzések -- a cadence-ág és a lépéshatár-poll közös szűrője. A poll
    az error-queue-t destruktívan fogyasztja, ezért mindkét fogyasztónak a
    TELJES kritikus policyt kell alkalmaznia (l. a cadence-ág INS-003 és
    R1 F-01/F-05 kommentjét a _run_step()-ben)."""
    active_transfer_kind = step.kind in _ENERGY_TRANSFER_STEP_KINDS
    return [
        e for e in polled_errors
        if not is_error_queue_poll_deferred(e) and not is_input_activation_pending(e) and (
            is_energy_state_uncertain(e)
            or (active_transfer_kind and is_critical_scpi_error(e["error"]))
        )
    ]


@dataclass(frozen=True)
class TestStep:
    kind: StepKind
    label: str
    # Governance-v2 review-kör 1 (CF-33/DFAM-025 `implicit_dispatch_on_
    # unstructured_label`, batch 4.5): main_window.py::_make_relax_ctrl()
    # korábban a relax-időtartamot a `step.label` SZÖVEGÉBEN keresett
    # "discharge" substringgel döntötte el ("discharge" in step.label.
    # lower()) — a label szabadon átnevezhető (semmi nem védi struktúrálisan),
    # egy jövőbeli átnevezés csendben rossz relax-időt (2h a szükséges 5h
    # helyett) választana. `relax_purpose` az EXPLICIT, strukturált jelző —
    # üres string ("") = nincs speciális relax-cél (pl. charge_only()
    # egyetlen relax lépése, ahol a cfg.test_type dönt), "post_charge"/
    # "post_discharge" = a plan-konstrukció (TestPlan.characterization()/
    # bq_learning_physical()) explicit szándéka, a label szövegétől
    # függetlenül.
    relax_purpose: str = ""


@dataclass(frozen=True)
class TestPlan:
    test_type: TestType
    steps: tuple[TestStep, ...]

    @staticmethod
    def characterization() -> "TestPlan":
        return TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(
                TestStep(StepKind.CHARGE,     "charge"),
                TestStep(StepKind.RELAX,      "relax_after_charge", relax_purpose="post_charge"),
                TestStep(StepKind.DISCHARGE,  "discharge"),
                TestStep(StepKind.RELAX,      "relax_after_discharge", relax_purpose="post_discharge"),
            ),
        )

    @staticmethod
    def bq_learning_physical() -> "TestPlan":
        return TestPlan(
            test_type=TestType.BQ_LEARNING_PHYSICAL,
            steps=(
                TestStep(StepKind.CHARGE,             "charge_1"),
                TestStep(StepKind.RELAX,              "relax_after_charge_1", relax_purpose="post_charge"),
                TestStep(StepKind.DISCHARGE,          "discharge_1"),
                TestStep(StepKind.RELAX,              "relax_after_discharge_1", relax_purpose="post_discharge"),
                TestStep(StepKind.CHARGE,             "charge_2"),
                TestStep(StepKind.RELAX,              "relax_after_charge_2", relax_purpose="post_charge"),
                TestStep(StepKind.DISCHARGE,          "discharge_2"),
                TestStep(StepKind.RELAX,              "relax_after_discharge_2", relax_purpose="post_discharge"),
                TestStep(StepKind.MANUAL_CHECKPOINT,  "manual_bq_checkpoint"),
            ),
        )

    @staticmethod
    def ocv_soc_characterization() -> "TestPlan":
        return TestPlan(
            test_type=TestType.OCV_SOC_CHARACTERIZATION,
            steps=(
                TestStep(StepKind.OCV_SOC, "ocv_soc"),
            ),
        )

    @staticmethod
    def charge_only() -> "TestPlan":
        return TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(
                TestStep(StepKind.CHARGE, "charge"),
                TestStep(StepKind.RELAX,  "relax_after_charge"),
            ),
        )

    @staticmethod
    def discharge_only() -> "TestPlan":
        return TestPlan(
            test_type=TestType.DISCHARGE_ONLY,
            steps=(
                TestStep(StepKind.DISCHARGE, "discharge"),
            ),
        )

    @staticmethod
    def chemid_characterization_pba() -> "TestPlan":
        """SUP03-CHEMID-010/011: TI GPCCHEM PbA ChemID karakterizáció."""
        return TestPlan(
            test_type=TestType.CHEMID_CHARACTERIZATION_PBA,
            steps=(
                TestStep(StepKind.CHEMID, "chemid_characterization"),
            ),
        )

    @staticmethod
    def traditional_learning() -> "TestPlan":
        """WP-AK-16: a TraditionalLearningController (WP-AK-10) egy önálló, több
        fázisú állapotgép — a TestRunner szintjén egyetlen, opak lépésként fut."""
        return TestPlan(
            test_type=TestType.TRADITIONAL_LEARNING,
            steps=(
                TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning"),
            ),
        )

    @staticmethod
    def golden_gg() -> "TestPlan":
        """WP-AK-16: a GoldenGgController (WP-AK-11) egy önálló, több fázisú
        állapotgép — a TestRunner szintjén egyetlen, opak lépésként fut."""
        return TestPlan(
            test_type=TestType.GOLDEN_GG,
            steps=(
                TestStep(StepKind.GOLDEN_GG, "golden_gg"),
            ),
        )

    @staticmethod
    def iec_dc_internal_resistance() -> "TestPlan":
        """Governance-v2 kör 2: IecDcResistanceController (IEC 60896-21
        §6.3) egy önálló, több fázisú állapotgép — az OCV_SOC/GOLDEN_GG
        mintáját követve a TestRunner szintjén egyetlen, opak lépésként fut
        (a CF-19 fix után a "Lépés idő" GUI-kijelzés a controller tényleges
        belső fázisát mutatja, nem csak ezt az opak lépéscímkét)."""
        return TestPlan(
            test_type=TestType.IEC_DC_INTERNAL_RESISTANCE,
            steps=(
                TestStep(StepKind.IEC_DC_RESISTANCE, "iec_dc_resistance"),
            ),
        )


@dataclass
class TestResult:
    status: str
    reason: str = ""
    total_charge_ah: float = 0.0
    total_discharge_ah: float = 0.0
    # Teljes kódbázis review 17. kör (2026-09-05), Metrológia/Adatintegritás
    # P2 lelet: az Ah-testvérpár Wh-oldala — a session-szintű futó Wh-összeg
    # (self._total_charge_wh/_total_discharge_wh) a 3. kör (1cbde3f) óta
    # létezik és a _flush_totals() pontosan úgy frissíti, mint az Ah-t, de
    # korábban SOHA nem jutott el a TestResult-ig/report.json-ig — egy
    # teljes session energiamérlege (Wh) ezért soha nem volt rekonstruálható
    # a végleges riportból, csak a töltésmennyiség (Ah).
    total_charge_wh: float = 0.0
    total_discharge_wh: float = 0.0
    resume_possible: bool = False    # ← új
    next_step_index: int = 0         # ← új
    active_step_label: str = ""              # ← DATA-003: utolsó/aktív lépés a riporthoz
    safe_off_attempted: bool = False          # ← DATA-003: safe_all_off() ténylegesen meghívva
    safe_off_result: dict = field(default_factory=dict)  # ← per-eszköz siker/hiba (CLAUDE.md §10.4/§13)
    communication_faults_count: int = 0       # ← DATA-003: session alatt észlelt SCPI hibák száma
    # Teljes kódbázis review 9. kör (2026-08-30), Metrológia/Adatintegritás
    # P2 lelet: session-szintű összeg/max a controllerek Integrator-alapú
    # extreme_dt_s_count/max_observed_dt_s property-jéből — korábban ez
    # kizárólag samples.csv soronkénti mezőként létezett, session-szinten
    # sosem aggregálódott, report.json-ban egyáltalán nem jelent meg.
    extreme_dt_s_count: int = 0
    max_observed_dt_s: float = 0.0
    # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/Adatintegritás
    # P2 lelet: RelaxController.dmm_fault_count (önálló StepKind.RELAX úton)
    # session-szintű összege — korábban KIZÁRÓLAG a beágyazott GoldenGg/
    # TraditionalLearning RelaxController-eknél (relax_dmm_fault_total)
    # aggregálódott, az önálló úton sosem jutott el a riportig.
    # B5b: SERVICE-ben hibás rekord, LEGACY_TICK-ben hibás tick az egység.
    # Régi checkpoint tick-összegét nem konvertáljuk utólag rekorddá.
    relax_dmm_fault_count: int = 0
    # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
    # lelet: a 20. körben bevezetett check_test_temperature_range()
    # WARNING session-szintű összege — ugyanaz a delta-alapú aggregációs
    # minta, mint extreme_dt_s_count/relax_dmm_fault_count-nál.
    temperature_out_of_range_count: int = 0
    # Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb) P2,
    # DFAM-001 4. előfordulása: a temp_dmm_fault_ticks (l. charge_
    # controller.py/discharge_controller.py property) korábban KIZÁRÓLAG
    # az 5 összetett controlleren, a GUI-rétegben (main_window.py
    # temp_dmm_fault_ticks_total) került összegzésre -- egy önálló
    # StepKind.CHARGE/DISCHARGE lépés (bare ChargeController/
    # DischargeController, nincs szülő) hőmérséklet-DMM-kiesését emiatt
    # a capacity_result_quality DEGRADED-gate sosem látta. Ugyanaz a
    # delta-alapú, minden StepKind-et lefedő aggregációs minta, mint a
    # fenti temperature_out_of_range_count-nál.
    temp_dmm_fault_ticks: int = 0
    # Governance-v2 review-kör 1 (CF-18/DFAM-022, batch 4.3, SPR-04): a
    # TELJES, megszakítás(ok)on átívelő session időtartama — l.
    # TestRunner._session_start_time kommentje. Csak a 4 valódi terminális
    # ágon (DONE/_emergency_stop/_graceful_stop/_run_manual_checkpoint)
    # töltődik ki, ugyanúgy, mint safe_off_attempted/safe_off_result.
    # MAX verifier P3-4 pontosítás: falióra-alapú (cross-process resume-nál
    # ez technikai kényszer, l. MET-R1-17/DECISION_REGISTER.md GOV-013
    # kiegészítés) — cross-process resume esetén a KIESÉS ALATTI,
    # teljesen áramtalan idő is beleszámít, tehát ez NEM a ténylegesen
    # aktív mérési idő, hanem a session-origótól eltelt naptári idő.
    # Rendszeróra-visszaállítás esetén akár negatív is lehet. Nem
    # integrációhoz használt (CLAUDE.md §7), kizárólag riport-metaadat.
    total_session_duration_s: float = 0.0
    # Governance-v2 review-kör 1 (CF-20/DFAM-021 computed_but_never_
    # persisted, batch 4.6): a safety.py::WarningCode 13 lehetséges
    # kódja közül a `_build_sample()` minden tickben csak az AKTUÁLIS
    # (átmeneti) figyelmeztetést exponálja a samples.csv `warning_flags`
    # oszlopában — session-szintű összesítés (melyik kód hányszor fordult
    # elő) korábban SEHOL nem létezett, ezért a report.json-ból nem volt
    # rekonstruálható, hogy egy adott session alatt ÖSSZESEN hányszor
    # (vagy egyáltalán) jelentkezett egy adott figyelmeztetés — csak az,
    # hogy az UTOLSÓ tickben volt-e éppen aktív. Ugyanaz a "computed-but-
    # never-persisted" hibaosztály, mint CF-21 (fast_transient_* mezők).
    warning_code_counts: dict = field(default_factory=dict)
    error_queue_deferred_s: dict[str, float] = field(default_factory=dict)
    # Kihagyott SERVICE-határidők; nem DMM-hibák vagy runner-tickek.
    relax_missed_deadline_count: int = 0


@dataclass
class TestRunnerConfig:
    runner_tick_s: float = 2.0
    test_name: str = "unnamed"
    sleep_enabled: bool = True
    device_error_poll_interval_s: float = 60.0
    charge_discharge_nplc: float = 1.0   # R10-B3: CC/CV, dokumentált 1 PLC
    relax_ocv_nplc: float = 10.0         # relaxáció/OCV: 10 PLC

    # WP-AK-02 (REC-001..004): checkpoint-traceability mezők — a hívó
    # (main_window._build_runner()) tölti ki, a TestRunner csak beágyazza
    # minden write_checkpoint()-hívásba. Üres alapérték = "nincs kitöltve"
    # (pl. régebbi híváshelyek/tesztek, ahol a resume nem releváns) — ilyenkor
    # a session_recovery.evaluate_resume() fail-safe módon BLOCKED_MISSING_
    # CHECKPOINT_DATA-t ad, sosem feltételez biztonságos állapotot.
    session_id: str = ""
    process_run_id: str = ""
    parent_session_id: str = ""
    config_hash: str = ""
    profile_hash: str = ""
    sample_id: str = ""
    instrument_identities: dict = field(default_factory=dict)
    # WP-AK-02 review-lelet: cross-process resume esetén a megszakadt session
    # checkpointjából átvett Ah-összegek — enélkül run() 0.0-ról indulna,
    # elveszítve a megszakadás előtti töltés/kisütés adatát a végső riportból.
    initial_charge_ah: float = 0.0
    initial_discharge_ah: float = 0.0
    # Governance-v2 review-kör 1 (CF-17/MET-R1-08, DFAM-022, batch 4.3; a
    # commit-üzenetben tévesen DFAM-001-ként hivatkozva, javítva l.
    # DECISION_REGISTER.md GOV-013): az Ah-testvérpár Wh-oldala korábban NEM
    # lett sem checkpointolva, sem resume-nál seedelve — a fenti
    # initial_charge_ah/initial_discharge_ah mintáját ismétli meg a Wh-ra.
    # Enélkül egy cross-process resume utáni riportban a total_charge_ah/
    # total_discharge_ah a TELJES (megszakadás előtti + utáni) session-t
    # fedte le, a total_charge_wh/total_discharge_wh viszont CSAK a resume
    # utáni run()-szegmensre vonatkozott — ugyanabban a riportban két
    # különböző időintervallum, anélkül hogy ez bárhol jelölve lett volna
    # (CLAUDE.md §7 "nyers és származtatott érték külön").
    initial_charge_wh: float = 0.0
    initial_discharge_wh: float = 0.0
    # Governance-v2 review-kör 1 (CF-18 + V-4/CF-17 MAX verifier lelet,
    # DFAM-022, batch 4.3, SPR-04 hátralévő fele): a fenti Ah/Wh-mintát az
    # idő-origóra és 5 további session-szintű diagnosztikai számlálóra
    # bővíti. `initial_session_start_time_iso` a MEGSZAKADÁS ELŐTTI session
    # kezdetének ISO-időbélyege — enélkül minden run()-hívás (l. run()
    # self._start_time = datetime.now(...)) az idő-origót is nullázta volna
    # resume-nál, miközben az Ah/Wh a teljes (megszakadás előtti + utáni)
    # session-t fedi le — ugyanaz a "két különböző időintervallum egy
    # riportban" hiba, mint fent a Wh-nál, most az elapsed-idő oldalán (l.
    # GUI-R1-06/MET-R1-17/Session F11). A további 5 mező (5 db, session-
    # szintű diagnosztikai összeg/maximum a controllerekből) pontosan ugyanazt
    # a resume-hiányt mutatta, mint a Wh eredetileg — a CF-17 MAX verifier
    # (2026-09-08) saját, önálló P2 leletként (V-4) azonosította, és
    # kifejezetten javasolta, hogy NE külön fixként, hanem ide, a CF-18/
    # SPR-04 hatókörébe kerüljön, hogy a viselkedési tér ne nőjön darabosan.
    initial_session_start_time_iso: str = ""
    initial_communication_faults_count: int = 0
    initial_extreme_dt_s_count: int = 0
    initial_max_observed_dt_s: float = 0.0
    initial_relax_dmm_fault_count: int = 0
    initial_temperature_out_of_range_count: int = 0
    initial_temp_dmm_fault_ticks: int = 0
    # Review-kör 4 (2026-09-09), S4-05/M4-05 (P2/P3, DFAM-022) -- l.
    # self._warning_code_counts docstringje run()-ban ugyanerről a
    # resume-korforgásról, 1:1 az initial_temp_dmm_fault_ticks mintáját
    # követve.
    initial_warning_code_counts: dict = field(default_factory=dict)
    initial_error_queue_deferred_s: dict[str, float] = field(default_factory=dict)
    initial_relax_missed_deadline_count: int = 0


class TestRunner:
    """Fő folyamatvezérlő — CHARACTERIZATION és BQ_LEARNING_PHYSICAL teszttípusok."""

    def __init__(
        self,
        instrument_manager: InstrumentManager,
        safety: SafetyManager,
        logger: Logger,
        profile: BatteryProfile,
        config: TestRunnerConfig,
        charge_ctrl_factory: Callable,
        discharge_ctrl_factory: Callable,
        relax_ctrl_factory: Callable,
        ocv_soc_ctrl_factory: Optional[Callable] = None,
        chemid_ctrl_factory: Optional[Callable] = None,
        traditional_learning_ctrl_factory: Optional[Callable] = None,
        golden_gg_ctrl_factory: Optional[Callable] = None,
        iec_dc_resistance_ctrl_factory: Optional[Callable] = None,
    ) -> None:
        self._instruments = instrument_manager
        self._safety = safety
        self._logger = logger
        self._profile = profile
        self._config = config
        self._chemid_ctrl_factory = chemid_ctrl_factory
        self._charge_ctrl_factory = charge_ctrl_factory
        self._discharge_ctrl_factory = discharge_ctrl_factory
        self._relax_ctrl_factory = relax_ctrl_factory
        self._ocv_soc_ctrl_factory = ocv_soc_ctrl_factory
        self._traditional_learning_ctrl_factory = traditional_learning_ctrl_factory
        self._golden_gg_ctrl_factory = golden_gg_ctrl_factory
        self._iec_dc_resistance_ctrl_factory = iec_dc_resistance_ctrl_factory

        self.stop_requested: bool = False
        self.emergency_stop_requested: bool = False
        self.emergency_stop_reason: str = ""

        self.status: str = "IDLE"
        self.on_sample: Optional[Callable[[dict], None]] = None
        self.on_event: Optional[Callable[[dict], None]] = None
        self.on_device_error: Optional[Callable[[dict], None]] = None
        # Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói
        # döntés 2026-09-10): a PSU CV/CC regulációs állapotának GUI-only,
        # NEM-escaláló csatornája -- SZÁNDÉKOSAN külön az on_device_error-
        # tól (nem device-error, nem SCPI-hiba), SOHA nem kerül a session
        # CSV/riportba (l. _poll_psu_regulation_state() docstringje).
        self.on_psu_regulation: Optional[Callable[[dict], None]] = None
        self.current_step: Optional[TestStep] = None

        self._total_charge_ah: float = 0.0
        self._total_discharge_ah: float = 0.0
        # Teljes kódbázis review 3. kör (2026-08-29), Metrológia/
        # Adatintegritás P2 lelet: a Wh-testvérpár (2. kör) nem kapott
        # keresztirányú session-szintű fallback-et CHARGE/DISCHARGE
        # StepKind-nál, ellentétben az Ah-val (self._total_discharge_ah/
        # self._total_charge_ah lent) — l. _build_sample() CHARGE/DISCHARGE
        # ágai és _flush_totals().
        #
        # MAX verifier V-1 pontosítás (CF-17/MET-R1-08, batch 4.3): az
        # alábbi mondat AZÓTA HAMISSÁ vált — "Csak samples.csv-szintű
        # bővítés, a TestResult/checkpoint/report_generator API-t
        # szándékosan nem érinti (a Wh nem session-összegző mennyiség)".
        # A 17. kör (2026-09-05) már bekötötte a TestResult/report_
        # generator oldalt (l. total_charge_wh a TestResult-ban, :222,
        # és report_generator.py:27-28), a CF-17 fix (41f9fcc, batch 4.3)
        # pedig a checkpoint oldalt is — a Wh MOSTANTÓL TELJES értékű,
        # resume-képes session-összegző mennyiség, az Ah-val azonos
        # szinten (l. run() initial_charge_wh/initial_discharge_wh
        # seedelése + mind a 3 resumable write_checkpoint()-hívás).
        self._total_charge_wh: float = 0.0
        self._total_discharge_wh: float = 0.0
        # Teljes kódbázis review 9. kör (2026-08-30), Metrológia/
        # Adatintegritás P2 lelet: a controllerek extreme_dt_s_count/
        # max_observed_dt_s (Integrator) property-je korábban SOHA nem
        # jutott el a TestResult/report.json-ig — l. _flush_totals()
        # docstringje ugyanerről a session-szintű összegzési mintáról.
        self._total_extreme_dt_s_count: int = 0
        self._max_observed_dt_s: float = 0.0
        # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
        # Adatintegritás P2 lelet: a RelaxController.dmm_fault_count
        # (önálló StepKind.RELAX úton, CHARGE_ONLY/CHARACTERIZATION/
        # BQ_LEARNING_PHYSICAL relax-lépései — a projekt leghosszabb,
        # 2-5 órás fázisai) korábban KIZÁRÓLAG a beágyazott (GoldenGg/
        # TraditionalLearning) RelaxController-eknél kerül kiolvasásra
        # (a saját relax_dmm_fault_total-jukba gyűjtve) — az önálló úton
        # sosem jutott el sem a TestResult-ig, sem a report.json-ig.
        # Ugyanaz a delta-alapú aggregációs minta, mint extreme_dt_s_count-nál.
        self._total_relax_dmm_fault_count: int = 0
        # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        # lelet — l. _flush_totals() docstringje ugyanerről a hiányzó
        # session-szintű összesítésről.
        self._total_temperature_out_of_range_count: int = 0
        # Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb) P2,
        # DFAM-001 4. előfordulása -- l. TestResult.temp_dmm_fault_ticks
        # docstringje. Ugyanaz a delta-alapú aggregációs minta, mint
        # temperature_out_of_range_count-nál.
        self._total_temp_dmm_fault_ticks: int = 0
        # Governance-v2 review-kör 1 (CF-20/DFAM-021, batch 4.6): l.
        # TestResult.warning_code_counts kommentje.
        # Review-kör 4 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb)
        # nyomán talalt S4-05/M4-05 (P2/P3, DFAM-022): a fenti "nem resume-
        # seedelt" dontes idokozben elavult -- azota (c79b8f5) ez a mezo
        # KOZVETLEN bemenete lett a capacity_result_quality DEGRADED-
        # gate-nek (main_window.py "TEMPERATURE_MONITOR_LOST_CRITICAL" in
        # warning_code_counts), tehat mar nem tisztan riport-metaadat --
        # pontosan az az atminosules, ami a temp_dmm_fault_ticks resume-
        # korforgasat (fe8289c) is indokolta. Most 1:1 koveti azt a mintat
        # -- l. _config.initial_warning_code_counts lent.
        self._warning_code_counts: dict[str, int] = {}
        self._start_time: Optional[datetime] = None
        # CF-18/DFAM-022 (batch 4.3, SPR-04): a run()-szegmens kezdete
        # (self._start_time, fent) MINDEN run()-hívásnál helyesen újraindul
        # (per-szegmens elapsed_s-hez, l. _build_sample()/samples.csv) — de
        # emiatt a TELJES, megszakítás(ok)on átívelő session időtartama
        # (total_session_duration_s) resume-nál elveszett volna. Ez az
        # ELKÜLÖNÍTETT mező a valódi session-origót tartja, resume-nál a
        # checkpoint-ból seedelve (l. run() initial_session_start_time_iso).
        self._session_start_time: Optional[datetime] = None
        self._last_device_error_poll_t: float = -1e9  # első tick azonnal pollol
        self._last_checkpoint_t: float = -1e9          # checkpoint throttling
        self._device_error_total_count: int = 0         # ← DATA-003 traceability
        self._active_plan: Optional[TestPlan] = None
        self.on_step_changed: Optional[Callable[[dict], None]] = None
        self._checkpoint_interval_s: float = 10.0
        # Review-kör 7 (2026-09-15), C-12: l. a periodikus checkpoint-ágnál
        # (_checkpoint_traceability_fields() hívás közelében) a `_pending_
        # changed` komment -- ez a mező teszi lehetővé, hogy a
        # pending_bq_df_restore mező VÁLTOZÁSA (nem csak a 10s-es
        # checkpoint_interval_s letelte) azonnali write_checkpoint()-ot
        # váltson ki, legfeljebb 1 tick (runner_tick_s) késleltetéssel.
        self._last_pending_bq_df_restore: dict | None = None
        # R2-11b (2026-09-15): a C-12 slot mellé egy MÁSODIK, SZÁNDÉKOSAN
        # ELKÜLÖNÍTETT utolsó-ismert-érték mező a GoldenGgController.
        # pending_bq_unsealed-hez -- l. ott a property docstringjét, miért
        # nem szabad a két maradványt egy mezőbe összeolvasztani (DFAM-018).
        self._last_pending_bq_unsealed: dict | None = None
        self._last_tlc_completion: dict | None = None
        # R17-01 (2026-09-16, Kör 17 MAX-effort review, MAX verifier F-3
        # lelete): a periodikus write_checkpoint()-hiba eseménye TRANZÍCIÓ-
        # alapú, nem minden-tickes -- egy TARTÓSAN sikertelen lemezírás
        # (pl. tartós sharing violation) BQ-maradvánnyal együtt eddig minden
        # tickben (akár ~1 Hz-cel) új PERIODIC_CHECKPOINT_WRITE_FAILED
        # eseményt adott volna, ami a GUI event-log 500-soros gyűrűspufferét
        # ~17 percen belül kitörli, elfedve a korábbi diagnosztikát. Az
        # ÍRÁSI KÍSÉRLET maga továbbra is MINDEN tickben megtörténik
        # (a maradvány-láthatóság elsőbbséget kap), csak az esemény-kiadás
        # ritkított a hiba-állapot ÁTMENETÉRE (False->True / True->False).
        # R1 F-16 (DFAM-016+DFAM-018): ez a flag a periodikus trigger
        # OR-ága is -- amíg True, minden tick új írást kísérel meg,
        # maradvánnyal VAGY anélkül (explicit viselkedésváltozás: tartós
        # OSError alatt maradvány nélkül is tickenkénti kísérlet); a
        # korábbi pending-rollback megszűnt, l. a periodikus ágat.
        self._periodic_checkpoint_write_failing: bool = False
        # R17-03+R17-17+V4-1 (2026-09-16, Kör 17 MAX-effort review,
        # DFAM-016+DFAM-018+DFAM-026): a `_safe_all_off_with_uncertainty_
        # check()` (lásd ott) eredménye korábban SEMMILYEN perzisztens
        # (checkpoint.json-beli) vagy élő-GUI csatornán nem jelent meg --
        # egy bizonytalan (nem igazoltan sikeres) PSU/Load OFF a session
        # végén csendben "DONE"/"Kész"-ként jelent meg az operátornak, ÉS
        # egy crash esetén a KÖVETKEZŐ indításnál sem volt semmi jele
        # annak, hogy a kimenet ENERGIZÁLT maradhatott. Három állapot:
        # None = nincs rekord (session még fut, VAGY sosem futott le
        # safe-off), dict minden eszközre True = PARANCS-SZINTŰ siker
        # (NEM hardveres readback -- l. main_window.py:2387 azonos
        # hivatkozású pontosítása), dict bármely eszközre False =
        # bizonytalan. SZÁNDÉKOSAN NEM seedelhető process-határon át
        # (nincs `initial_last_safe_off_result` a TestRunnerConfig-ban) --
        # egy resume-olt process nem örökölheti a SZÜLŐ safe-off
        # állítását egy hardverről, amit MAGA sosem parancsolt.
        # SZÁNDÉKOSAN NEM esik vissza egy bukott write_checkpoint() után
        # (R1 F-16 óta a _last_pending_bq_* sem) -- ez a mező azt tükrözi,
        # mit tett EZ a process, nem azt, mi van a lemezen; egy IRÁSI
        # hiba nem teszi hamissá a MÁR megtörtént safe-off tényét.
        self._last_safe_off_result: dict | None = None
        # Teljes kódbázis review 6. kör (2026-08-29), Session/Workflow P2
        # lelet #3: l. _reconfigure_dmm_nplc_for_relax_substate() docstringje.
        self._last_configured_nplc: float | None = None
        self._relax_sampler_controller: RelaxController | None = None
        self._relax_sampler_started = False
        self._relax_sampling_service: RelaxSamplingService | None = None
        self._relax_totals_flush: Callable[[], None] | None = None
        self._deferred_since: dict[str, float] = {}
        # Csak az összeg öröklődik, az offline idő nem halasztási idő.
        self._deferred_totals: dict[str, float] = dict(config.initial_error_queue_deferred_s)
        self._total_relax_missed_deadline_count = config.initial_relax_missed_deadline_count
        self._relax_shutdown_failure: str | None = None
        # A lezárás előtti OFF eredményét a terminális ág egyszer használja fel.
        self._pending_relax_safe_off_result: dict | None = None
        # Kör 20 Batch E2 (F20-S2, P3, DFAM-011+DFAM-016): a NPLC_SET_FAILED
        # esemény ÉLŐ (on_event) csatornája TRANZÍCIÓ-alapú, pontosan az
        # R17-01 (l. _periodic_checkpoint_write_failing lent) mintája
        # szerint -- az F20-S1 retry óta egy TARTÓS set_nplc()-hiba minden
        # ticken (alapértelmezés: 2,0 s) új eseményt adna, ami ~17 perc
        # alatt kiürítené a GUI 500 soros event-gyűrűjét. Az events.csv
        # írási KÍSÉRLETE SZÁNDÉKOSAN NEM ritkított (fájl, nem gyűrű).
        self._nplc_set_failing: bool = False
        # Governance-v2 review-kör 1 (CF-01/DFAM-016, MASTER_CONSOLIDATED.md
        # batch 4.1): a `_run_step()`-ben létrehozott controller korábban
        # LOKÁLIS volt — a runner-vezérelt megszakítás (_emergency_stop()/
        # _graceful_stop()) nem tudta elérni, hogy a controller SAJÁT
        # emergency_stop()-ját (pl. Traditional Learning Cell BL restore,
        # BQ learning eredmény klasszifikáció) meghívja. L.
        # _abort_active_controller().
        self._active_controller = None

    @property
    def logger(self) -> Logger:
        """WP-AK-07 (BQ Diagnostics közös session logging): a MainWindow-nak
        szüksége van az aktív session Logger-jére, hogy a BQ diagnosztikai
        pollingot ugyanabba a session könyvtárba naplózza — ugyanaz a minta,
        mint a már meglévő `TestRunnerWorker.runner` property (worker.py)."""
        return self._logger

    def request_stop(self) -> None:
        self.stop_requested = True

    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None:
        # F-C (MAX verifier, D6 2. kör, 2026-09-13, P3 pre-existing, D6
        # második producert ad hozzá ugyanerre az útra): a reasont a
        # requested flag ELŐTT kell írni. A fogyasztó (pl. test_runner.py
        # tick-hurkai) a flaget ellenőrzi, majd a reasont olvassa -- ha a
        # sorrend fordított, egy GIL-váltás a két írás között
        # `requested=True` + elavult/üres reasont eredményezhetne, ami
        # semelyik taxonómia-prefixre nem illeszkedik, és a
        # traditional_learning_controller.py::_classify_result() hamisan
        # LEARNING_RESULT_FAILED-re ("BQ_LEARNING_FAILED") klasszifikálna
        # egy ártalmatlan megszakítást (CLAUDE.md §2/§7).
        self.emergency_stop_reason = reason
        self.emergency_stop_requested = True

    def reset_control_flags(self) -> None:
        self.stop_requested = False
        self.emergency_stop_requested = False
        self.emergency_stop_reason = ""

    def _remember_tlc_completion(self, controller) -> None:
        if isinstance(controller, TraditionalLearningController):
            self._last_tlc_completion = controller.tlc_completion

    @property
    def tlc_completion(self) -> dict | None:
        self._remember_tlc_completion(self._active_controller)
        return deepcopy(self._last_tlc_completion)

    def _checkpoint_traceability_fields(
        self, safe_boundary: bool, next_safe_step_index: int
    ) -> dict:
        """WP-AK-02 (REC-001..004): minden write_checkpoint()-hívás ugyanezt a
        traceability-mezőhalmazt kapja — session_recovery.evaluate_resume()
        erre épít. `safe_boundary` SOHA nem lehet hallgatólagos True — mindig
        a hívó dönti el explicit, a tényleges lépéstípus alapján.

        Review-kör 7 (2026-09-15), C-12: `pending_bq_df_restore` — l.
        TraditionalLearningController.pending_bq_df_restore docstringje.
        `getattr(..., None)` a NEM-TLC controllereken mindig `None`-t ad —
        nincs hatása egyetlen más StepKind-re sem.

        MAX verifier F1 lelete (2026-09-15): mind a 4 terminális write_
        checkpoint()-hívás (DONE/FAULT/STOPPED/manual checkpoint) ELŐTT a
        hívó MÁR `self._active_controller = None`-t állít (korábbi
        review-körök bevált, szándékos mintája — l. a fenti nullázások
        kommentjeit) — egy közvetlen `getattr(self._active_controller,
        ...)` ekkor MINDIG `None`-t adna EZEKEN a hívásokon, akkor is, ha
        az UTOLSÓ ÉLŐ érték egy meg-nem-oldott BQ DF maradványt jelzett.
        Ha a controller még él, ez a hívás EGYBEN frissíti is a
        `_last_pending_bq_df_restore`-t (nem csak a periodikus checkpoint-
        ág teszi) — minden write_checkpoint()-hívás potenciális "utolsó
        élő pillanat", nem csak a 10s-es periodikus. Ha a controller már
        `None`, erre a legutóbb ismert értékre esik vissza -- így a
        terminális írás sem veszíti el a maradványt (a legfeljebb 1 tick
        késleltetés korábbi korlátozása megmarad, de a korábbi "mindig
        None a terminális írásokon" hiba megszűnik).

        R2-11b (2026-09-15): `pending_bq_unsealed` -- l. GoldenGgController.
        pending_bq_unsealed docstringje. Ugyanaz a fallback-to-last-known-
        value minta, KÜLÖN mezővel (DFAM-018 semantic-collapse-avoidance:
        a Cell BL Data Flash-maradvány és a BQ seal-állapot-maradvány KÉT
        KÜLÖN kockázat, nem vonhatók össze egy mezőbe).

        R17-03+R17-17+V4-1 (2026-09-16): `last_safe_off_result` -- l.
        `self._last_safe_off_result` docstringje az __init__-ben. EZ a
        mező NEM esik vissza `self._active_controller`-re (nincs ilyen
        kapcsolata), közvetlenül a `_safe_all_off_with_uncertainty_
        check()` legutóbbi eredményét tükrözi -- SZÁNDÉKOSAN HARMADIK,
        KÜLÖN kockázat a fenti kettőtől (a chip ÁLLAPOTA helyett a
        PSU/Load KIMENETI állapotát jelzi)."""
        pending = getattr(self._active_controller, "pending_bq_df_restore", None)
        pending_unsealed = getattr(self._active_controller, "pending_bq_unsealed", None)
        if self._active_controller is not None:
            self._last_pending_bq_df_restore = pending
            self._last_pending_bq_unsealed = pending_unsealed
        else:
            pending = self._last_pending_bq_df_restore
            pending_unsealed = self._last_pending_bq_unsealed
        return {
            "session_id": self._config.session_id,
            "process_run_id": self._config.process_run_id,
            "parent_session_id": self._config.parent_session_id,
            "config_hash": self._config.config_hash,
            "profile_hash": self._config.profile_hash,
            "sample_id": self._config.sample_id,
            "instrument_identities": self._config.instrument_identities,
            "safe_boundary": safe_boundary,
            "next_safe_step_index": next_safe_step_index,
            "pending_bq_df_restore": pending,
            "pending_bq_unsealed": pending_unsealed,
            "last_safe_off_result": self._last_safe_off_result,
            "tlc_completion": self.tlc_completion,
        }

    def _checkpoint_summary_fields(self) -> dict:
        """Kor 18, Batch C7 R17-20 (DFAM-001+DFAM-021): a kumulativ session-
        osszegzes (Ah/Wh + ido-origo + 6 diagnosztikai szamlalo) az 5
        write_checkpoint() hivohely kozul HAROMBAN szerepelt (periodikus
        RUNNING, DONE, manualis checkpoint), a FAULT es a STOPPED agban
        NEM -- holott azok irjak az UTOLSO checkpointot a lemezre, es
        FELULIRJAK a periodikus iras gazdagabb rekordjat. Egy megszakadt
        FIZIKAI futas post-mortem allapota pontosan akkor semmisult meg,
        amikor a legtobbet erte volna. Mind az ot hivohely ezt a helpert
        hasznalja; a periodikus RUNNING ag (_run_step()) a hivas elott
        _flush_totals()-szal frissiti az osszegeket. Uj osszegzo mezot
        kizarolag itt kell felvenni. SZANDEKOSAN NEM tartalmaz
        `checkpoint_is_terminal`-t: az `evaluate_resume()` ELSO kapuja a
        `safe_boundary` (itt mindig False FAULT/STOPPED-nel), tehat a
        flag itt VISELKEDESILEG HATASTALAN volna (a `checkpoint_is_
        terminal` kapu sosem eri el ezt a checkpointot), ugyanakkor a
        jelentese ("a terv VEGEN keszult") egy 2/9. lepesnel elhasalt
        futasra HAMIS lenne."""
        return {
            "charge_ah": self._total_charge_ah,
            "discharge_ah": self._total_discharge_ah,
            "charge_wh": self._total_charge_wh,
            "discharge_wh": self._total_discharge_wh,
            "session_start_time_iso": (
                self._session_start_time.isoformat() if self._session_start_time else ""
            ),
            "error_queue_deferred_s": self.error_queue_deferred_s,
            "relax_missed_deadline_count": self._total_relax_missed_deadline_count,
            "communication_faults_count": self._device_error_total_count,
            "extreme_dt_s_count": self._total_extreme_dt_s_count,
            "max_observed_dt_s": self._max_observed_dt_s,
            "relax_dmm_fault_count": self._total_relax_dmm_fault_count,
            "temperature_out_of_range_count": self._total_temperature_out_of_range_count,
            "temp_dmm_fault_ticks": self._total_temp_dmm_fault_ticks,
            "warning_code_counts": dict(self._warning_code_counts),
        }

    @property
    def error_queue_deferred_s(self) -> dict[str, float]:
        now = time.monotonic()
        totals = dict(self._deferred_totals)
        for device, started in self._deferred_since.items():
            totals[device] = totals.get(device, 0.0) + max(0.0, now - started)
        return totals

    def run(self, test_plan: TestPlan, start_step_index: int = 0) -> TestResult:
        result = self._run_plan(test_plan, start_step_index)
        result.error_queue_deferred_s = self.error_queue_deferred_s
        result.relax_missed_deadline_count = self._total_relax_missed_deadline_count
        return result

    def _run_plan(self, test_plan: TestPlan, start_step_index: int = 0) -> TestResult:
        if start_step_index < 0 or start_step_index >= len(test_plan.steps):
            # Governance-v2 review-kör 1 (CF-44/F9, DFAM-001 sibling): ez a
            # korai FAULT-visszatérés eddig NEM hívta a
            # _safe_all_off_with_uncertainty_check()-et és nem zárta le a
            # loggert -- a run() MINDEN MÁS terminális ágán (DONE, _emergency_
            # stop, _graceful_stop, _run_manual_checkpoint) ez a MINDEN
            # esetben lefutó, garantált lépés (CLAUDE.md §6/§10). Elérhetőség
            # (frissítve: governance-v2 review-kör, MAX verifier O-3/F5): a
            # cross-process resume úton a session_recovery.evaluate_resume()
            # MOSTANTÓL validálja a checkpoint next_safe_step_index-ét a
            # TÉNYLEGES terv lépésszámához képest (BLOCKED_INVALID_STEP_INDEX,
            # main_window.py adja át a `total_step_count`-ot) -- ez az itteni
            # kapu emiatt már a MÁSODIK, redundáns védelmi réteg, és a
            # közvetlen (nem-GUI) run() hívásokat is fedi. KEEP_AS_IS: egy
            # garantált terminális safe-off/logger-close soha nem függhet
            # egyetlen felsőbb rétegbeli ellenőrzéstől. A logger már létezik
            # ezen a ponton (l. __init__), a
            # session-összegek viszont még nem indultak el (run() előtti
            # állapot) -- ezért csak a safe-off és a logger-zárás történik
            # meg itt, a többi TestResult mező alapértéken marad.
            reason = (
                f"Érvénytelen start_step_index={start_step_index} "
                f"({len(test_plan.steps)} lépéses tervhez)"
            )
            safe_off_result = self._safe_all_off_with_uncertainty_check()
            self.status, reason = self._resolve_terminal(reason)
            try:
                self._logger.log_event("INVALID_START_STEP_INDEX", reason, is_critical=True)
            except Exception as exc:
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "INVALID_START_STEP_INDEX_LOGGING_FAILED",
                        "event_message": str(exc),
                    })
            finally:
                try:
                    self._logger.close()
                except Exception:
                    pass
            # S4-06 (review-kör 4, P3, DFAM-021/022): a self._config.initial_*
            # session-összegek (resume esetén a checkpointból) EZEN a ponton
            # már ki vannak töltve, de korábban minden lenti mező alapértéken
            # (0.0/{}) maradt -- pont egy sikertelen resume esetén (a
            # legértékesebb helyzetben) tűnt el a megszakadás előtti Ah/Wh/
            # diagnosztikai adat a riportból, holott a checkpoint.json maga
            # megőrizte. A _session_start_time-alapú total_session_duration_s
            # (l. lent, a 4 valódi terminális ágon) NEM része ennek a
            # minimális fixnek -- az igényelné a teljes ISO-parse/fallback-
            # logikát (fent), ami ezen a korai, run() ELŐTTI ágon aránytalanul
            # nagy diff lenne egy P3 findinghez.
            #
            # Kör 19 D8d (F19-S4, DFAM-016 terminal_cleanup_restore_omission,
            # 2. előfordulás az S5-07/_run_manual_checkpoint() után): ez az
            # ág a teljes terminális szekvenciát lefuttatja (safe-off, event-
            # log, logger.close()), DE korábban self.status-t SOSEM
            # állította -- egy UGYANAZON TestRunner-példányon történő resume
            # esetén (pl. teszt-kód/scripting út, ami újra hívja run()-t) a
            # self.status a MEGELŐZŐ szegmens értékén maradt volna (pl.
            # "CHECKPOINT_STOPPED"), holott a visszaadott TestResult
            # helyesen "FAULT"-ot jelez.
            return TestResult(
                status=self.status,
                reason=reason,
                safe_off_attempted=True,
                safe_off_result=safe_off_result,
                total_charge_ah=self._config.initial_charge_ah,
                total_discharge_ah=self._config.initial_discharge_ah,
                total_charge_wh=self._config.initial_charge_wh,
                total_discharge_wh=self._config.initial_discharge_wh,
                communication_faults_count=self._config.initial_communication_faults_count,
                extreme_dt_s_count=self._config.initial_extreme_dt_s_count,
                max_observed_dt_s=self._config.initial_max_observed_dt_s,
                relax_dmm_fault_count=self._config.initial_relax_dmm_fault_count,
                temperature_out_of_range_count=self._config.initial_temperature_out_of_range_count,
                temp_dmm_fault_ticks=self._config.initial_temp_dmm_fault_ticks,
                warning_code_counts=dict(self._config.initial_warning_code_counts),
            )
        self._active_plan = test_plan
        self._start_time = datetime.now(timezone.utc)
        self.status = "RUNNING"
        # Kör 20 Batch E2 (F20-S6 + WORK_STATE.json open_findings[63]/O2,
        # P3, DFAM-032): mindhárom flag SZEGMENS-hatókörű, de eddig kizárólag
        # az __init__-ben nullázódott. UGYANAZON runner-példány MÁSODIK
        # run()-hívásánál (manuális BQ checkpoint utáni folytatás,
        # worker.py::request_continue_from_checkpoint()) az előző szegmens
        # értéke öröklődött volna: (a) minden ezutáni periodikus RUNNING
        # checkpoint a RÉGI (akár mind-True) safe-off eredményt írná ki,
        # PSU/Load újra-energizálása UTÁN is -- egy összeomlott session
        # hamisan NEM mutatna indítási figyelmeztetést (main_window.py:996);
        # (b) egy előző szegmensben már True-ra állt checkpoint-írási
        # hibajelző elnyomná az ÚJ szegmens első
        # PERIODIC_CHECKPOINT_WRITE_FAILED eseményét; (c) a harmadik,
        # ugyanebben a Batch E2-ben bevezetett `_nplc_set_failing` (l.
        # _report_nplc_set_failed()) ugyanígy elnyomná az ÚJ szegmens első
        # NPLC_SET_FAILED élő eseményét. Ma MINDHÁROM NOT_REACHABLE
        # (MANUAL_CHECKPOINT strukturálisan az utolsó tervezett lépés,
        # `resume_possible` a jelenlegi tervkészletben mindig False) --
        # profilaktikus, KÖZÖS diffben, mert egzakt testvérek. SZÁNDÉKOSAN
        # NEM nullázzuk itt a `_last_configured_nplc`-t: az a MŰSZER
        # tényleges állapotát tükrözi, ami a szegmenshatárt fizikailag
        # túléli.
        self._last_safe_off_result = None
        self._periodic_checkpoint_write_failing = False
        self._nplc_set_failing = False
        # MAX verifier lelet (P2-1, CF-18/V-4 utóellenőrzés, batch 4.3
        # follow-up): a lenti session-szintű seedelés (Ah/Wh/idő-origó/5
        # diagnosztikai számláló) korábban FELTÉTEL NÉLKÜL, minden run()-
        # hívásnál lefutott — ez helyes egy ÚJ TestRunner-példány ELSŐ
        # run()-hívásánál (cross-process resume, a config a checkpointból
        # jön), de HIBÁS egyazon UGYANAZON runner-példány EGYMÁST KÖVETŐ
        # run()-hívásainál (worker.py::request_continue_from_checkpoint() ->
        # _do_run() -> self._runner.run(...), l. ott — a manuális BQ
        # checkpoint UTÁNI folytatás ugyanazt a runner-példányt hívja
        # ÚJRA): a 2. run()-hívás visszaállította volna a MÁR felhalmozott
        # session-összegeket a config EREDETI (a 2. hívás pillanatában már
        # elavult) initial_* értékeire, csendben eldobva az 1. run()-
        # szegmens alatt mért adatot. self._session_start_time (alant)
        # KIZÁRÓLAG a runner-példány ELSŐ run()-hívásáig marad None — ez a
        # helyes, meglévő jelző az "ez az első run() ezen a példányon"
        # eldöntésére, self._start_time (fent) VÁLTOZATLANUL minden
        # run()-hívásnál újraindul (a per-szegmens elapsed_s/samples.csv-hez
        # ez helyes és szándékos).
        if self._session_start_time is None:
            # WP-AK-02 review-lelet (Important/P1): cross-process resume
            # esetén (start_step_index > 0, a megszakadt session Ah-összegei
            # a checkpointból) NEM 0.0-ról kell indulni — különben a
            # megszakadás előtti töltés/kisütés adata csendben elveszne a
            # végső riportból. Alapértelmezésben (nem resume)
            # TestRunnerConfig.initial_charge_ah/initial_discharge_ah ==
            # 0.0, tehát a viselkedés változatlan.
            self._total_charge_ah = self._config.initial_charge_ah
            self._total_discharge_ah = self._config.initial_discharge_ah
            # Governance-v2 review-kör 1 (CF-17/MET-R1-08, batch 4.3): az Ah-
            # testvérpár Wh-oldalának ugyanaz a resume-seedelése, mint fent —
            # enélkül ez a két sor implicit 0.0-n maradt volna (l.
            # TestRunnerConfig.initial_charge_wh/initial_discharge_wh
            # kommentje).
            self._total_charge_wh = self._config.initial_charge_wh
            self._total_discharge_wh = self._config.initial_discharge_wh
            # CF-18/DFAM-022 (batch 4.3): a valódi session-origó — resume
            # esetén a checkpoint-ból átvéve, hogy a total_session_duration_s
            # (l. lent, a 4 terminális TestResult-ágon) a TELJES,
            # megszakítás(ok)on átívelő session-t fedje le, ne csak az
            # utolsó run()-szegmenst. Hibás/csonka ISO-string esetén (pl.
            # kézzel sérült checkpoint.json) konzervatívan a friss
            # self._start_time-ra esik vissza — ez a resume ELŐTTI
            # viselkedést adja vissza (csak a szegmens-idő), nem crashel el
            # egy egyébként érvényes resume-ot egy metaadat-mező miatt, de
            # az esemény naplózásra kerül, nem hallgatólagos. A tz-naiv (de
            # szintaktikailag érvényes ISO) string és a nem-string bemenet
            # is idetartozik (MAX verifier P2-2 lelet) — mindkettő a
            # fallback-ágat futtatja, sosem szökik ki TypeError-ként a
            # terminális TestResult-konstrukcióig.
            if self._config.initial_session_start_time_iso:
                try:
                    parsed = datetime.fromisoformat(
                        self._config.initial_session_start_time_iso
                    )
                    if parsed.tzinfo is None:
                        raise ValueError(
                            "initial_session_start_time_iso tz-naiv (nincs "
                            "explicit időzóna) — a TestResult.total_session_"
                            "duration_s kiszámításához (datetime.now(timezone."
                            "utc) - self._session_start_time) tz-aware érték "
                            "kell, különben TypeError-t dobna a run() a "
                            "terminális ágon."
                        )
                    self._session_start_time = parsed
                except (ValueError, TypeError) as exc:
                    self._session_start_time = self._start_time
                    # MAX independent verifier P3-A lelete (f9027e3
                    # utóellenőrzés, batch 4.3): ez a hiba korábban CSAK
                    # on_event-re ment, a session logjába (events.csv) nem —
                    # ugyanaz a hiányzó audit-nyom, mint amit a V-7 lelet
                    # (l. _abort_active_controller() CONTROLLER_ABORT_FAILED
                    # mintája fent) már egyszer feltárt: a live GUI-n kívül
                    # (CF-07 esemény-továbbítás) semmi nem őrzi meg, hogy egy
                    # sérült checkpoint.json miatt degradált idő-origóval
                    # futott a session. Nem safety-kritikus (report-metaadat
                    # degradáció, nem fizikai hiba), ezért is_critical=False.
                    event_message = (
                        f"initial_session_start_time_iso="
                        f"{self._config.initial_session_start_time_iso!r}: {exc}"
                    )
                    try:
                        self._logger.log_event(
                            "SESSION_START_TIME_PARSE_FAILED", event_message,
                        )
                    except Exception:
                        pass
                    if self.on_event is not None:
                        self.on_event({
                            "event_code": "SESSION_START_TIME_PARSE_FAILED",
                            "event_message": event_message,
                        })
            else:
                self._session_start_time = self._start_time
            # V-4 (CF-17 MAX verifier lelet, batch 4.3, ide vonva l.
            # TestRunnerConfig initial_communication_faults_count kommentje):
            # ugyanaz a resume-seedelés, mint az Ah/Wh-nál fent, az 5
            # diagnosztikai számlálóra. self._max_observed_dt_s FUTÓ MAXIMUM
            # (nem összeg) — a lenti tick-hurok max(...) hívása (l. ott) a
            # seedelt kezdőértéket és az új szegmens értékét helyesen veti
            # össze, nincs szükség külön esetkezelésre.
            self._device_error_total_count = self._config.initial_communication_faults_count
            self._total_extreme_dt_s_count = self._config.initial_extreme_dt_s_count
            self._max_observed_dt_s = self._config.initial_max_observed_dt_s
            self._total_relax_dmm_fault_count = self._config.initial_relax_dmm_fault_count
            self._total_temperature_out_of_range_count = (
                self._config.initial_temperature_out_of_range_count
            )
            self._total_temp_dmm_fault_ticks = (
                self._config.initial_temp_dmm_fault_ticks
            )
            self._warning_code_counts = dict(self._config.initial_warning_code_counts)

        try:
            for step_index, step in enumerate(
                list(test_plan.steps)[start_step_index:],
                start=start_step_index,
            ):
                self.current_step = step

                if self.emergency_stop_requested:
                    return self._stop_terminal(emergency_reason=self.emergency_stop_reason)

                if self.stop_requested:
                    return self._graceful_stop("USER_STOP_REQUESTED")

                step_result = self._run_step(step_index, step)

                if step_result.status == "FAULT":
                    # Governance-v2 review-kör 1 (CF-01 fix melléklelet, batch
                    # 4.1): ha `_run_step()` a FAULT-ot MÁR a saját
                    # `_emergency_stop()`-hívásán keresztül adta vissza (a
                    # runner-vezérelt emergency_stop_requested ág — ilyenkor
                    # `safe_off_attempted` már True, az esemény/checkpoint/
                    # controller-abort már megtörtént), ne dolgozzuk fel
                    # MÉGEGYSZER — az korábban duplán naplózott eseményt/
                    # checkpointot írt, és a CF-01 fix után duplán hívta
                    # volna a controller saját emergency_stop()-ját is. A
                    # `_run_step()`-en belüli, saját maga még nem terminált
                    # (SCPI_CRITICAL_ERROR/controller_faulted) FAULT-ágak
                    # `safe_off_attempted=False`-t hagynak — azok változatlanul
                    # itt futnak át `_emergency_stop()`-on.
                    if step_result.safe_off_attempted:
                        return step_result
                    return self._emergency_stop(step_result.reason or "STEP_FAULT")

                if step_result.status in ("STOPPED", "CHECKPOINT_STOPPED"):
                    if step_result.status == "CHECKPOINT_STOPPED" and not step_result.resume_possible:
                        # Governance-v2 review-kör 1 (MAX verifier F1, batch
                        # 4.1 follow-up, 3. kör): ez volt az EGYETLEN
                        # védtelen terminális-úti logger-I/O a fájlban (a
                        # másik 4 — DONE/_emergency_stop/_graceful_stop/
                        # _run_manual_checkpoint — mind try/except-ben van).
                        # Egy itt keletkező I/O hiba enélkül a run() külső
                        # try/except-jéig futott volna fel, ami egy MÁR
                        # sikeresen elért, terminális BQ checkpointot
                        # UNHANDLED_EXCEPTION FAULT-tá minősített volna át
                        # — eldobva a legitim CHECKPOINT_STOPPED eredményt
                        # (next_step_index, checkpoint_is_terminal, saját
                        # safe_off_result) egy MÁSODIK, redundáns safe-off
                        # kísérlet mellett.
                        try:
                            self._logger.close()
                        except Exception as exc:
                            if self.on_event is not None:
                                self.on_event({
                                    "event_code": "CHECKPOINT_CLOSE_FAILED",
                                    "event_message": str(exc),
                                })
                    return step_result

                # Governance-v2 review-kör 1 (MAX verifier NV-3, batch 4.1
                # follow-up): ha idáig eljutunk, step_result.status == "DONE"
                # — ez a KÖZTES lépés fejeződött be sikeresen (NEM a teljes
                # terv), a controllere még self._active_controller-ben áll
                # (l. _run_step() eleje). A verifier empirikusan igazolta,
                # hogy enélkül egy a KÖVETKEZŐ lépés indulása ELŐTT (a
                # for-ciklus tetején, fent) érkező runner-vezérelt
                # megszakítás ezt a MÁR lezárt controllert abortálná újra —
                # ugyanaz a minta, mint a checkpoint-ágon (l.
                # _run_manual_checkpoint() és test_manual_checkpoint_nulls_
                # active_controller_so_resume_time_stop_does_not_reabort).
                #
                # Kör 21 F4/ARC21-2 (DFAM-004, a Kör 20 E17/worker.py fix
                # upstream testvere -- ott a hiányzó guard csak a GUI-cimkét
                # hamisitotta hamis DONE-ra, ITT viszont a terv FOLYTATNA a
                # KÖVETKEZŐ lépéssel egy be NEM fejezett lépés után -- tovabbi
                # energiaatadas): a fenti "ha idáig eljutunk, DONE" allitas
                # korabban FELTETEL NELKUL igaz volt feltetelezve. Explicit
                # ellenorzes hozzaadva, MIELOTT a ciklus a kovetkezo lepesre
                # lepne. Ez BIZTONSAGOSABB, mint az E17 worker.py mintaja: az
                # itt dobott ValueError a run() SAJAT, ezt a for-ciklust
                # korulvevo `except BaseException` agan at `_emergency_stop()`-ba
                # fut, MIG a worker.py-beli raise a szal sajat excepthookjaig
                # szokik (l. worker.py:128-143 MAX verifier megjegyzese) --
                # itt NINCS hasonlo escape-kockazat.
                if step_result.status != "DONE":
                    raise ValueError(
                        f"Unknown TestResult.status: {step_result.status!r}"
                    )
                self._active_controller = None

        except BaseException as exc:
            # Review 2026-09-15 (C-04/DFAM-002, RECURRING -- 2. előfordulás,
            # precedens: pulse_measurement_service.py:452,487,764 már
            # `except BaseException`-t használ ugyanezen indokkal): a tick-
            # hurok valós `time.sleep()`-et hív (:1338 körül) -- egy Ctrl+C
            # (KeyboardInterrupt) a felhasználó terminálablakában korábban
            # `BaseException` volt, NEM `Exception`, tehát KIKERÜLTE ezt az
            # ágat: nincs `_emergency_stop()`, nincs `_safe_all_off_with_
            # uncertainty_check()`, nincs FAULT-esemény, nincs terminális
            # checkpoint, nincs `logger.close()`. A hardver a launcher-script
            # SAJÁT `finally`-jén (ha van) még biztonságos állapotba kerülhet,
            # de a session-log FAULT/DONE nélkül, csonkán marad -- pontosan
            # ez volt a 2026-09-15 D10 "nyomtalan leállás" egyik lehetséges
            # magyarázata. A `raise` a nem-`Exception` ágon MEGŐRZI a Ctrl+C
            # szemantikáját (a hívó/terminál még reagálhat rá), de a
            # garantált safe-off/FAULT-log/logger.close() előbb lefut.
            # Az IEC cleanup callbackhibája nem írhatja felül a blokk
            # már rögzített elsődleges okát; BaseException továbbra is továbbmegy.
            # A BQ-olvasási abort vezérlési jel: saját FAULT > vészjel > STOP.
            if isinstance(exc, TestAbortedByUser) and isinstance(
                self._active_controller, (TraditionalLearningController, GoldenGgController),
            ):
                bq_reason = (
                    self._active_controller.fault_reason or "CONTROLLER_FAULT"
                    if self._controller_faulted(self._active_controller) else ""
                )
                if bq_reason or self.stop_requested or self.emergency_stop_requested:
                    return self._stop_terminal(primary_reason=bq_reason)
            iec_reason = (
                self._active_controller.fault_reason
                if isinstance(self._active_controller, IecDcResistanceController)
                and self._controller_faulted(self._active_controller) else ""
            )
            # A stopjel nélküli abort runner-oka a HEAD szerinti osztályozás;
            # a cleanup kiegészítő okai a controllerben és az ablakban maradnak.
            if isinstance(exc, TestAbortedByUser) and iec_reason.startswith(f"UNHANDLED_EXCEPTION: {exc}"):
                iec_reason = f"UNHANDLED_EXCEPTION: {exc}"
            result = self._emergency_stop(iec_reason or f"UNHANDLED_EXCEPTION: {exc}")
            if not isinstance(exc, Exception):
                raise
            return result

        # Review-kör 2 (2026-09-08), Session/Workflow/Állapotgép S-3 lelet:
        # a `while not self._is_finished(controller)` tick-hurok (l.
        # _run_step()) az `emergency_stop_requested`-et a hurok TETEJÉN, a
        # `controller.advance()` hívás ELŐTT ellenőrzi -- ha a vészleállítás
        # PONT az UTOLSÓ lépés UTOLSÓ tickje (a `controller.advance()` maga)
        # KÖZBEN érkezik, és ez a tick fejezi be a lépést, a hurok simán,
        # `emergency_stop_requested` ÚJRAELLENŐRZÉSE NÉLKÜL lép ki -- a
        # fenti for-ciklus tetején lévő ellenőrzés (a KÖVETKEZŐ lépés előtt)
        # ekkor már nem fut le, mert nincs következő lépés. A session ekkor
        # DONE-t jelentett, annak ellenére, hogy a kezelő explicit
        # vészleállítást kért -- CLAUDE.md §6/§10: "Hamis PASS/DONE
        # magasabb prioritású, mint kényelmi vagy esztétikai javítás." A
        # fizikai biztonságot ez a rés NEM érintette (a DONE ág alább
        # feltétel nélkül hívja a `_safe_all_off_with_uncertainty_check()`-
        # et, CF-02 óta) -- ez egy STÁTUSZ/RIPORT-integritási hiba, nem
        # aktív energiaátadás alatti safety-rés.
        if self.emergency_stop_requested:
            return self._stop_terminal(emergency_reason=self.emergency_stop_reason)
        if self.stop_requested:
            return self._graceful_stop("USER_STOP_REQUESTED")

        self.status = "DONE"
        # Governance-v2 review-kör 1 (CF-02/DFAM-016+018, MASTER_CONSOLIDATED.md
        # batch 4.1; javítva a MAX independent verifier V-1 leletére): a
        # DONE ág korábban NEM hívta a _safe_all_off_with_uncertainty_
        # check()-et — a végső fizikai állapot kizárólag az utolsó
        # controller saját DONE-ágának (pl. ChargeController._run_taper()
        # output_off()-ja) sikerén múlt, bizonyíték/readback nélkül a
        # riportban. A GUI ennek ellenére feltétel nélküli "Kész"/OK
        # minőséget mutatott. FONTOS PONTOSÍTÁS: ez NEM volt az egyetlen
        # ilyen terminális út — a _run_manual_checkpoint() (CHECKPOINT_
        # STOPPED) ugyanezt a hiányt mutatta, és külön javításra került
        # (l. ott). A CF-02 finding további alhelyei (checkpoint-parkolt
        # vészleállítás worker.py-ban, connect/config-hiba rollback,
        # session-végi disconnect_all() main_window.py-ban) tudatosan
        # KÍVÜL esnek ezen a test_runner.py-ra szűkített fixen — l.
        # WORK_STATE.json next_action, batch 4.1b.
        #
        # V-5 (MAX verifier) + NV-3 pontosítás (batch 4.1 follow-up): ez az
        # utolsó lépés UTÁNI, teljes terv végi nullázás — a KÖZTES lépések
        # közötti azonos veszélyt (egy köztes DONE UTÁN, a KÖVETKEZŐ lépés
        # ELŐTT érkező megszakítás újra-abortálná a már lezárt controllert)
        # a fenti for-ciklusban, minden köztes "DONE" step_result után
        # elhelyezett nullázás fedi le — l. ott. Ez a hely csak a defenzív
        # záró állapotot biztosítja (pl. ha ugyanezt a runner-példányt egy
        # KÉSŐBBI run() hívás újrahasználná).
        self._active_controller = None
        # R17-19 (DFAM-002, 3. elofordulas, "scope" varians): a DONE-tail a
        # run() korabbi, :811-en nyilo try-jan (l. az azt lezaro except
        # BaseException-t) KIVUL all -- egy Ctrl+C (BaseException) a lenti
        # GARANTALT safe-off KOZBEN (safe_all_off() csak Exception-t nyel
        # per eszkoz) a Load OFF utan, a PSU OFF ELOTT szakithatott, esemeny
        # es logger.close() nelkul. SZANDEKOSAN SAJAT, LOKALIS guard: a korabbi
        # try szkopjanak szelesitese a DONE-utat a FAULT-klasszifikacion
        # vezetne at, ami egy sikeres sessiont FAULT-tá minositene.
        try:
            safe_off_result = self._safe_all_off_with_uncertainty_check()
            # Kör 21 F6 (METRO-R21-1) -- l. _emergency_stop() azonos indoklása.
            # SAJÁT try/except, hogy egy poll-hiba ne minősítse a lenti,
            # már körülvevő `except BaseException` DONE_TAIL_INTERRUPTED
            # ágán át hamisan megszakítottá ezt az egyébként sikeres DONE
            # lezárást. SZÁNDÉKOSAN `except Exception`, NEM `BaseException`
            # (MAX verifier PROBE D megerősítése, 2026-09-19, szemben a
            # másik 3 forced-poll hellyel, l. `_emergency_stop()` B3
            # lelete): itt a KÖRÜLVEVŐ `except BaseException` (lentebb) a
            # helyes, SZÁNDÉKOLT tulajdonos egy `KeyboardInterrupt`-ra --
            # az retry-safe-offot, DONE_TAIL_INTERRUPTED log_eventet ÉS
            # logger.close()-t is futtat, majd újra dobja az eredeti
            # kivételt. Egy itt lévő `except BaseException` ELNYELNÉ ezt,
            # MIELŐTT a külső guard egyáltalán észrevenné.
            # R1 F-42: safe_off_result átadva -- l. _poll_device_errors().
            try:
                self._poll_device_errors(safe_off_result)
            except Exception:
                pass
            # Teljes kódbázis review 20. kör (2026-09-06), Session/Workflow P2
            # lelet: a másik 3 terminális út (_emergency_stop()/_graceful_stop()/
            # _run_manual_checkpoint()) mindegyike ír egy záró write_checkpoint()-
            # et a saját terminális állapotával — ez az ág korábban NEM, csak
            # logger.close()-t hívott. Emiatt az utolsó lemezre írt checkpoint.json
            # a legutóbbi periodikus ("RUNNING", jellemzően safe_boundary=False,
            # egy MÁR lezárt lépésre mutató next_safe_step_index-szel) állapot
            # maradt — ha a crash pont a logger.close() és a GUI-oldali
            # report.json-írás közötti (kicsi, de nem nulla) ablakban történt, a
            # session_recovery.discover_incomplete_sessions() egy TÉNYLEGESEN
            # sikeresen, biztonságosan befejezett sessiont tévesen "megszakadt"-
            # ként mutatott volna, és — ha az utolsó lépés RELAX volt (stale
            # safe_boundary=True) — akár egy már lezárt lépés megismétlését is
            # felkínálhatta volna. checkpoint_is_terminal=True itt is jelölve,
            # hogy az evaluate_resume() BLOCKED_ALREADY_TERMINAL kapuja ezt az
            # esetet is lefedje.
            status, reason = self._resolve_terminal()
            if status != "DONE":
                self._pending_relax_safe_off_result = safe_off_result
                return self._stop_terminal()
            try:
                self._logger.write_checkpoint({
                    "status": status,
                    "reason": reason,
                    "checkpoint_is_terminal": True,
                    # Közös mezőkészlet minden checkpoint-ágon (DFAM-001/021).
                    **self._checkpoint_summary_fields(),
                    **self._checkpoint_traceability_fields(
                        safe_boundary=True, next_safe_step_index=len(test_plan.steps)
                    ),
                })
            except Exception as exc:
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "DONE_CHECKPOINT_WRITE_FAILED",
                        "event_message": str(exc),
                    })
            finally:
                try:
                    self._logger.close()
                except Exception:
                    pass
        except BaseException as exc:
            # A 4 lepes egymastol FUGGETLENUL kiserlendo meg (CLAUDE.md §6:
            # "Egy OFF-hiba ne akadalyozza a masik megkiserleset"), a vegen a
            # `raise` az EREDETI kivetelt adja tovabb -- semmi nincs elnyelve,
            # a Ctrl+C szemantikaja megmarad. SZANDEKOSAN NEM
            # self._emergency_stop()-ot hivja -- az FAULT-ta irna a statuszt
            # es egy teljes MASODIK terminalis lancot futtatna -- hanem a
            # `_safe_all_off_with_uncertainty_check()` kanonikus wrappert
            # (MAX verifier O-1 lelete: ez adja ki a SAFE_OFF_RESULT_
            # UNCERTAIN esemenyt a PSU_OUTPUT_STATE_UNCERTAIN/LOAD_INPUT_
            # STATE_UNCERTAIN WarningCode-dal, ha a retry maga is
            # bizonytalan marad -- ugyanaz a csatorna, mint a masik 5
            # terminalis uton).
            _retry_safe_off: dict | None = None
            try:
                _retry_safe_off = self._safe_all_off_with_uncertainty_check()
            except BaseException:
                pass
            try:
                self._logger.log_event(
                    "DONE_TAIL_INTERRUPTED",
                    f"{type(exc).__name__}: {exc} | retry_safe_off={_retry_safe_off}",
                    is_critical=True,
                )
            except BaseException:
                pass
            try:
                self._logger.close()
            except BaseException:
                pass
            if self.on_event is not None:
                self.on_event({
                    "event_code": "DONE_TAIL_INTERRUPTED",
                    "event_message": f"{type(exc).__name__}: {exc}",
                    # R3-B3 (DFAM-018): a fenti log_event is_critical=True-jával
                    # azonos minősítés az élő csatornán is.
                    "severity": "CRITICAL",
                })
            raise
        return TestResult(
            status=status,
            reason=reason,
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
            total_charge_wh=self._total_charge_wh,
            total_discharge_wh=self._total_discharge_wh,
            active_step_label=self.current_step.label if self.current_step else "",
            safe_off_attempted=True,
            safe_off_result=safe_off_result,
            communication_faults_count=self._device_error_total_count,
            extreme_dt_s_count=self._total_extreme_dt_s_count,
            relax_dmm_fault_count=self._total_relax_dmm_fault_count,
            temperature_out_of_range_count=self._total_temperature_out_of_range_count,
            temp_dmm_fault_ticks=self._total_temp_dmm_fault_ticks,
            max_observed_dt_s=self._max_observed_dt_s,
            warning_code_counts=dict(self._warning_code_counts),
            total_session_duration_s=(
                (datetime.now(timezone.utc) - self._session_start_time).total_seconds()
                if self._session_start_time else 0.0
            ),
        )

    def _relax_critical(self, message: str) -> None:
        try:
            self._logger.log_event("RELAX_RELEASE_FAILED", message, is_critical=True)
        except Exception as exc:  # noqa: BLE001 -- az élő esemény őrzi a naplóhibát
            message = f"{message}; naplóhiba: {exc}"
        if self.on_event is not None:
            self.on_event({
                "event_code": "RELAX_RELEASE_FAILED", "event_message": message,
                "severity": "CRITICAL",
            })

    def _assert_relax_sampling_released(self) -> None:
        ctrl = self._relax_sampler_controller
        if (
            (ctrl is not None and not ctrl.sampling_shutdown_complete)
            or getattr(self._instruments.dmm_voltage, "measurement_owned", False) is True
        ):
            self._relax_critical("RELAX_SAMPLING_RELEASE_PENDING")
            raise RuntimeError("RELAX_SAMPLING_RELEASE_PENDING")

    def _prepare_relax_sampling(self, step: TestStep, controller) -> None:
        if step.kind in _ENERGY_TRANSFER_STEP_KINDS:
            # A gyermekek is ezt az élő hookot kapják; csak a nyugta megszakítása jelöl.
            controller.handoff_stop_requested = ActivationStopHook(
                lambda: self.emergency_stop_requested or self.stop_requested
            )
        if isinstance(controller, (TraditionalLearningController, GoldenGgController)):
            # A gyermek csak később jön létre; az első advance előtt már SERVICE-et kap.
            controller.prepare_relax_sampling = self._attach_relax_sampling
        elif step.kind == StepKind.RELAX and isinstance(controller, RelaxController):
            self._attach_relax_sampling(controller)

    def _attach_relax_sampling(self, controller: RelaxController) -> None:
        self._assert_relax_sampling_released()
        service = RelaxSamplingService(self._instruments.dmm_voltage)
        controller.attach_sampling_service(service, release_poll=self.poll_relax_sampling_shutdown)
        self._relax_sampler_controller = controller
        self._relax_sampling_service = service
        self._relax_sampler_started = False
        self._relax_shutdown_failure = None
        if self._last_configured_nplc == self._config.relax_ocv_nplc:
            service.start(configuration_ready=True)
            self._relax_sampler_started = True

    def _start_prepared_relax_sampling(self) -> None:
        ctrl = self._relax_sampler_controller
        service = self._relax_sampling_service
        if (
            service is not None and self._relax_sampler_started
            and service.heartbeat_age_s > service.shutdown_timeout_s
        ):
            self._relax_critical("RELAX_SAMPLING_HEARTBEAT_LOST")
            raise RuntimeError("RELAX_SAMPLING_HEARTBEAT_LOST")
        if (
            ctrl is not None and not self._relax_sampler_started
            and self._last_configured_nplc == self._config.relax_ocv_nplc
        ):
            # A sikertelen első NPLC-beállítás utáni sikeres retry nyugtája.
            service = self._relax_sampling_service
            if service is None:
                raise RuntimeError("Hiányzó előkészített relaxgyűjtő")
            service.start(configuration_ready=True)
            self._relax_sampler_started = True

    def poll_relax_sampling_shutdown(self) -> bool:
        """Egyetlen nem blokkoló lezárási tick; nincs join vagy műszer-I/O."""
        ctrl = self._relax_sampler_controller
        if ctrl is None:
            return True
        ctrl.collect_sampling_records()
        if self._relax_totals_flush is not None:
            self._relax_totals_flush()
        service = self._relax_sampling_service
        if service is not None and service.shutdown_error is not None:
            self._relax_critical(service.shutdown_error)
            raise RuntimeError(service.shutdown_error)
        if not ctrl.sampling_shutdown_complete:
            return False
        self._relax_sampler_controller = None
        self._relax_sampling_service = None
        self._relax_sampler_started = False
        return True

    def _finish_relax_sampling(self, *, safe_off_done: bool = False) -> None:
        ctrl = self._relax_sampler_controller
        if ctrl is None:
            return
        ctrl.request_sampling_stop()
        # Egy sikertelen lezárás után csak egyszeri, nem blokkoló poll engedett.
        # A controller/owner tiltókapuja nyugta nélkül továbbra is zárva marad.
        if self._relax_shutdown_failure is not None:
            if not self.poll_relax_sampling_shutdown():
                raise RuntimeError(self._relax_shutdown_failure)
            return
        safe_off_attempted = safe_off_done or self._pending_relax_safe_off_result is not None
        service = self._relax_sampling_service
        timeout = service.shutdown_timeout_s if service is not None else 31.5
        deadline = time.monotonic() + timeout
        try:
            while not self.poll_relax_sampling_shutdown():
                if time.monotonic() >= deadline:
                    self._relax_critical("RELAX_SAMPLING_RELEASE_TIMEOUT")
                    raise RuntimeError("RELAX_SAMPLING_RELEASE_TIMEOUT")
                if not safe_off_attempted and (self.emergency_stop_requested or self.stop_requested):
                    self._pending_relax_safe_off_result = self._safe_all_off_with_uncertainty_check()
                    safe_off_attempted = True
                time.sleep(0.01)
        except BaseException as exc:
            if self._relax_shutdown_failure is None:
                self._relax_shutdown_failure = f"{type(exc).__name__}: {exc}"
            raise

    def _run_step(self, step_index: int, step: TestStep) -> TestResult:
        self._assert_relax_sampling_released()
        result = self._run_step_body(step_index, step)
        if result.status == "FAULT" and not result.safe_off_attempted:
            result = self._stop_terminal(primary_reason=result.reason or "STEP_FAULT")
        elif result.status == "DONE":
            status, _reason = self._resolve_terminal()
            if status in ("FAULT", "STOPPED"):
                result = self._stop_terminal()
        # A korai return objektuma a finally előtt készült; a végső I/O hibája
        # a lezáró drain után kerüljön a visszaadott eredménybe is.
        result.relax_dmm_fault_count = self._total_relax_dmm_fault_count
        result.error_queue_deferred_s = self.error_queue_deferred_s
        result.relax_missed_deadline_count = self._total_relax_missed_deadline_count
        return result

    def _run_step_body(self, step_index: int, step: TestStep) -> TestResult:
        if self.on_step_changed is not None and self._active_plan is not None:
            self.on_step_changed({
                "runner_status": "RUNNING",
                "step_kind": step.kind.value,
                "step_label": step.label,
                "step_index": step_index,
                "step_count": len(self._active_plan.steps),
            })

        if step.kind == StepKind.MANUAL_CHECKPOINT:
            return self._run_manual_checkpoint(step_index, step)

        self._configure_dmm_for_step(step)
        # R1 R-1 (B7_B7S_fixplan_FINAL.md B7S/2, DFAM-001/028): lépéshatár-
        # poll az energiaátadó lépés első advance()-e előtt -- a megelőző
        # lépés utolsó tickje (pl. RELAX_DONE) csak a cadence szerint
        # pollolt, így egy actual=ON / kritikus SCPI-hiba a következő lépés
        # energizálásáig észrevétlen maradhatott. A DMM-konfiguráció UTÁN
        # (a set_nplc() saját hibái is beleférnek), a controller-factory
        # ELŐTT fut; FAULT esetén nincs controller-létrehozás és energizálás,
        # a run() _emergency_stop()-ot hív. A kadencia-időbélyeg visszaáll,
        # hogy az 1. tick meglévő pollja változatlanul lefusson.
        if step.kind in _ENERGY_TRANSFER_STEP_KINDS:
            cadence_t = self._last_device_error_poll_t
            boundary_critical = _critical_polled_errors(step, self._poll_device_errors())
            self._last_device_error_poll_t = cadence_t
            if boundary_critical:
                first = boundary_critical[0]
                return TestResult(
                    status="FAULT",
                    reason=f"SCPI_CRITICAL_ERROR[{first['device']}]: {first['error']}",
                    total_charge_ah=self._total_charge_ah,
                    total_discharge_ah=self._total_discharge_ah,
                    total_charge_wh=self._total_charge_wh,
                    total_discharge_wh=self._total_discharge_wh,
                    active_step_label=step.label,
                    communication_faults_count=self._device_error_total_count,
                    extreme_dt_s_count=self._total_extreme_dt_s_count,
                    relax_dmm_fault_count=self._total_relax_dmm_fault_count,
                    temperature_out_of_range_count=self._total_temperature_out_of_range_count,
                    temp_dmm_fault_ticks=self._total_temp_dmm_fault_ticks,
                    max_observed_dt_s=self._max_observed_dt_s,
                )
        controller = self._controller_for_step(step)
        # Governance-v2 review-kör 1 (CF-01): l. _abort_active_controller().
        self._active_controller = controller

        charge_ah_before = getattr(controller, "accumulated_charge_Ah", 0.0)
        discharge_ah_before = getattr(controller, "accumulated_discharge_Ah", 0.0)
        charge_wh_before = getattr(controller, "accumulated_charge_Wh", 0.0)
        discharge_wh_before = getattr(controller, "accumulated_discharge_Wh", 0.0)
        extreme_dt_s_count_before = getattr(controller, "extreme_dt_s_count", 0)
        dmm_fault_count_before = getattr(controller, "dmm_fault_count", 0)
        missed_deadline_count_before = getattr(controller, "missed_deadline_count", 0)
        # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        # lelet — ugyanaz a delta-alapú futó-összeg minta, mint
        # extreme_dt_s_count-nál.
        temperature_out_of_range_count_before = getattr(
            controller, "temperature_out_of_range_count", 0
        )
        # Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb) P2,
        # DFAM-001 4. előfordulása -- ugyanaz a delta-alapú futó-összeg
        # minta, mint temperature_out_of_range_count-nál, most már MINDEN
        # StepKind-re kiterjedően (nem csak az 5 összetett controlleren,
        # ahol korábban KIZÁRÓLAG a GUI-rétegben, main_window.py
        # temp_dmm_fault_ticks_total-jában összegződött).
        temp_dmm_fault_ticks_before = getattr(controller, "temp_dmm_fault_ticks", 0)

        def _flush_totals() -> None:
            # 2026-08-24 (élő panel audit, session_fg1_20260823_200337 nyomán):
            # korábban ez az összegzés KIZÁRÓLAG a StepKind.CHARGE/DISCHARGE
            # ágban és KIZÁRÓLAG a while-ciklus SIKERES lezárása után futott
            # (l. lent, a korábbi kód helye) — sem a TRADITIONAL_LEARNING/
            # GOLDEN_GG/CHEMID/OCV_SOC StepKind-okra, sem semmilyen FAULT/
            # EMERGENCY_STOP/GRACEFUL_STOP/SCPI_CRITICAL_ERROR korai return-re
            # nem terjedt ki — emiatt self._total_charge_ah/
            # self._total_discharge_ah (report.json, checkpoint) 0.0 maradt
            # egy órákig futó, ténylegesen töltött/kisütött traditional_
            # learning session faultja után is (CLAUDE.md §13: minden
            # terminális állapot, nem csak DONE, kapjon pontos riportot).
            # A getattr(...) 0.0 defaultja miatt a controller által NEM
            # exponált irány delta-ja mindig 0 — no-op, biztonságos: a
            # RelaxController egyiket sem (accumulated_charge_Ah/
            # accumulated_discharge_Ah), a ChargeController csak a töltési, a
            # DischargeController csak a kisütési irányt exponálja. (R1 F-39:
            # a korábbi "RELAX/CHEMID/OCV_SOC" felsorolás elavult -- a ChemID-
            # és az OCV_SOC-controller mindkét irányt exponálja.)
            #
            # Governance-v2 review-kör 1 (CF-14/DFAM-016, MASTER_CONSOLIDATED.md
            # batch 4.1): a "before" snapshotok mostantól minden híváskor
            # frissülnek (nonlocal) — a függvény IDEMPOTENSSÉ vált, ismételt
            # hívás nulla deltát ad. Ez szükséges, mert a hívás most KÉT
            # helyen történik minden korai-return ágon: explicit, a
            # _emergency_stop()/_graceful_stop()/FAULT TestResult ELŐTT (hogy
            # azok már a friss összeggel dolgozzanak), ÉS a while-ciklust
            # körülvevő try/finally-ban (biztonsági háló egy váratlan
            # kivételre, pl. controller.advance() belsejéből). Idempotencia
            # nélkül a finally-beli második hívás duplán adná hozzá a deltát.
            nonlocal charge_ah_before, discharge_ah_before
            nonlocal charge_wh_before, discharge_wh_before
            nonlocal extreme_dt_s_count_before, dmm_fault_count_before
            nonlocal missed_deadline_count_before
            nonlocal temperature_out_of_range_count_before
            nonlocal temp_dmm_fault_ticks_before

            charge_ah_now = getattr(controller, "accumulated_charge_Ah", 0.0)
            discharge_ah_now = getattr(controller, "accumulated_discharge_Ah", 0.0)
            charge_wh_now = getattr(controller, "accumulated_charge_Wh", 0.0)
            discharge_wh_now = getattr(controller, "accumulated_discharge_Wh", 0.0)
            extreme_dt_s_count_now = getattr(controller, "extreme_dt_s_count", 0)
            dmm_fault_count_now = getattr(controller, "dmm_fault_count", 0)
            missed_deadline_count_now = getattr(controller, "missed_deadline_count", 0)
            temperature_out_of_range_count_now = getattr(
                controller, "temperature_out_of_range_count", 0
            )
            temp_dmm_fault_ticks_now = getattr(controller, "temp_dmm_fault_ticks", 0)

            self._total_charge_ah += charge_ah_now - charge_ah_before
            self._total_discharge_ah += discharge_ah_now - discharge_ah_before
            # Teljes kódbázis review 3. kör (2026-08-29), Metrológia/
            # Adatintegritás P2 lelet: ugyanaz a futó-összeg minta, mint az
            # Ah-nál fent — a Wh-testvérpár keresztirányú (CHARGE-nál
            # discharge, DISCHARGE-nál charge) samples.csv-fallback-jéhez.
            self._total_charge_wh += charge_wh_now - charge_wh_before
            self._total_discharge_wh += discharge_wh_now - discharge_wh_before
            # Teljes kódbázis review 9. kör (2026-08-30), Metrológia/
            # Adatintegritás P2 lelet: ugyanaz a futó-összeg minta, mint az
            # Ah/Wh-nál fent — extreme_dt_s_count kumulatív a delta-módszerrel,
            # max_observed_dt_s pedig session-szintű maximum (nem additív).
            self._total_extreme_dt_s_count += extreme_dt_s_count_now - extreme_dt_s_count_before
            self._max_observed_dt_s = max(
                self._max_observed_dt_s, getattr(controller, "max_observed_dt_s", 0.0)
            )
            # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
            # Adatintegritás P2 lelet: ugyanaz a delta-alapú futó-összeg
            # minta, mint extreme_dt_s_count-nál — csak a RelaxController
            # (önálló StepKind.RELAX) exponálja ezt a property-t, más
            # controllereken a getattr(...) 0.0 defaultja miatt no-op.
            self._total_relax_dmm_fault_count += dmm_fault_count_now - dmm_fault_count_before
            self._total_relax_missed_deadline_count += (
                missed_deadline_count_now - missed_deadline_count_before
            )
            # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow
            # P2 lelet: a 20. körben bevezetett check_test_temperature_
            # range() WARNING-ja korábban csak a per-tick warning_flags-ig
            # (samples.csv) jutott el — session-szintű összeg nélkül a
            # report.json/capacity_result_quality nem tudott róla,
            # ellentétben a projekt 4 másik, analóg mintájával
            # (extreme_dt_s_count, relax_dmm_fault_total).
            self._total_temperature_out_of_range_count += (
                temperature_out_of_range_count_now - temperature_out_of_range_count_before
            )
            # Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb) P2,
            # DFAM-001 4. előfordulása -- l. temp_dmm_fault_ticks_before
            # fenti komment.
            self._total_temp_dmm_fault_ticks += (
                temp_dmm_fault_ticks_now - temp_dmm_fault_ticks_before
            )

            self._remember_tlc_completion(controller)
            charge_ah_before = charge_ah_now
            discharge_ah_before = discharge_ah_now
            charge_wh_before = charge_wh_now
            discharge_wh_before = discharge_wh_now
            extreme_dt_s_count_before = extreme_dt_s_count_now
            dmm_fault_count_before = dmm_fault_count_now
            missed_deadline_count_before = missed_deadline_count_now
            temperature_out_of_range_count_before = temperature_out_of_range_count_now
            temp_dmm_fault_ticks_before = temp_dmm_fault_ticks_now

        if isinstance(controller, TraditionalLearningController):
            def persist_tlc_completion(record: dict) -> None:
                self._last_tlc_completion = deepcopy(record)
                _flush_totals()
                self._logger.write_checkpoint({
                    "status": "RUNNING", "step": step.label,
                    **self._checkpoint_summary_fields(),
                    **self._checkpoint_traceability_fields(
                        safe_boundary=False, next_safe_step_index=step_index,
                    ),
                })
                self._logger.flush_all()
            controller.persist_tlc_completion = persist_tlc_completion

        _last_tick_t = time.perf_counter()

        # Governance-v2 review-kör 1 (CF-14/DFAM-016, MASTER_CONSOLIDATED.md
        # batch 4.1): a teljes tick-hurok try/finally-be került, EGYETLEN
        # _flush_totals() hívással — korábban 4 külön, explicit hívás előzte
        # meg az egyes early-return ágakat, de egy VÁRATLAN kivétel (pl.
        # controller.advance() belsejéből) egyiket sem érintette, és több
        # órányi Ah/Wh veszett el pont a hibás session diagnosztikájánál. A
        # `finally` pontosan egyszer fut le, bármelyik return/kivétel-úton.
        step_error: BaseException | None = None
        terminal_return = False
        def _stop_if_requested() -> TestResult | None:
            nonlocal terminal_return
            if not (self.emergency_stop_requested or self.stop_requested):
                return None
            # A már eldőlt controller-fault elsőbbsége és egyszeri abortja megmarad.
            primary_reason = (
                getattr(controller, "fault_reason", "") or "CONTROLLER_FAULT"
            ) if self._controller_faulted(controller) else ""
            if self._is_finished(controller):
                self._active_controller = None
            _flush_totals()
            terminal_return = True
            if primary_reason:
                return self._emergency_stop(primary_reason)
            if self.emergency_stop_requested:
                return self._stop_terminal(emergency_reason=self.emergency_stop_reason)
            return self._graceful_stop("USER_STOP_REQUESTED")

        try:
            self._prepare_relax_sampling(step, controller)
            # Beágyazott relaxnál a gyermek az első advance során jön létre.
            self._relax_totals_flush = _flush_totals
            while not self._is_finished(controller):
                if self.emergency_stop_requested:
                    _flush_totals()
                    terminal_return = True
                    return self._stop_terminal(emergency_reason=self.emergency_stop_reason)

                # Review-kör 2 (2026-09-09), friss Szoftver/Architektúra F8a
                # (NEW_FAMILY misleading_conditional_without_effect): a
                # korábbi `and self._step_can_be_gracefully_interrupted(step)`
                # feltétel egy MINDIG True-t adó, a `step` paramétert nem
                # használó metódusra hivatkozott -- azt sugallta, hogy a
                # graceful stop lépéstípusonként kapuzott, holott nem volt
                # az. Egy lépéstípus-függő korlátozás (pl. IMPULSE_*
                # alállapotokban ne szakadjon meg) mérnöki döntést igényelne,
                # amit ez a fix NEM hoz meg -- a feltétel a tényleges (mindig
                # engedő) viselkedésre egyszerűsítve.
                if self.stop_requested:
                    _flush_totals()
                    terminal_return = True
                    return self._graceful_stop("USER_STOP_REQUESTED")

                _tick_start_t = time.perf_counter()
                # Első tickben actual_dt_s ≈ 0 (INIT/PRECHECK fázis, integráció nem fut).
                actual_dt_s = _tick_start_t - _last_tick_t
                if isinstance(controller, (OcvSocController, IecDcResistanceController)):
                    controller.prepare_pulse_block()
                controller.advance(actual_dt_s)
                pulse_warnings = None
                if isinstance(controller, OcvSocController):
                    # Q-36/A2: az ON és az első acquisition közé nem kerül runner-I/O vagy sleep.
                    try:
                        pulse_warnings = controller.run_pulse_block(
                            lambda force, before_io: self._pulse_poll_fault(step, force=force, before_io=before_io),
                        )
                    finally:
                        # STOP/FAULT/kivétel előtt, blokkonként és kódonként egyszer.
                        for warning, count in controller.take_pulse_warnings().items():
                            self._warning_code_counts[warning] = self._warning_code_counts.get(warning, 0) + count
                    if pulse_warnings is not None:
                        pulse_warnings = {}
                if isinstance(controller, IecDcResistanceController):
                    try:
                        pulse_warnings = controller.run_pulse_block(
                            lambda force, before_io: self._pulse_poll_fault(step, force=force, before_io=before_io),
                        )
                    finally:
                        # STOP/FAULT/kivétel előtt, blokkonként és kódonként egyszer.
                        for warning, count in controller.take_pulse_warnings().items():
                            self._warning_code_counts[warning] = self._warning_code_counts.get(warning, 0) + count
                    if pulse_warnings is not None:
                        pulse_warnings = {}
                        # A blokk utolsó I/O-ja alatt is érkezhet stop/fault.
                        # Blokk nélkül a tick mintája összesíti a warningot.
                        pending_stop = _stop_if_requested()
                        if pending_stop is not None:
                            return pending_stop
                        if self._controller_faulted(controller):
                            _flush_totals()
                            terminal_return = True
                            return self._emergency_stop(controller.fault_reason or "CONTROLLER_FAULT")
                # Teljes kódbázis review 14. kör (2026-09-04), Metrológia/
                # Adatintegritás P1 lelet (a fenti probléma FELE -- a MÁSIK
                # felét l. lent, Review-kör 5 M5-01 kommentje): a
                # ChargeController fast_loop_enabled ága (l.
                # charge_controller.py::_advance_with_fast_loop()) CV/
                # TAPER_HOLD/CC-cél-közelben ugyanabban a controller.advance()
                # hívásban saját, belső time.sleep()-alapú résztick-eket futtat,
                # ami VALÓS időt fogyaszt és MÁR beintegrál. Ha _last_tick_t-t a
                # hívás ELŐTTI időponthoz (_tick_start_t) rögzítenénk
                # VÁLTOZATLANUL, a KÖVETKEZŐ tick actual_dt_s-e ugyanazt a
                # valós időintervallumot MÉGEGYSZER tartalmazná (a fast-loop
                # már elfogyasztott ideje + az azt követő pacing-sleep is
                # benne lenne) — ez a CV/TAPER_HOLD fázisban rendszeresen
                # közel duplázta az integrált Ah/Wh-t.
                #
                # Review-kör 5 (2026-09-10), Metrológia M5-01 (P1, MAX
                # verifier VERIFIED): a 14. körben bevezetett fix (egy FRISS
                # `time.perf_counter()`-rel frissített _last_tick_t az
                # advance() hívás UTÁN) a duplázást megszüntette, de a
                # SPEKTRUM másik végébe lökte a hibát -- a `controller.
                # advance()` hívás SAJÁT, tényleges végrehajtási ideje (a
                # ChargeController.advance()-en BELÜLI kezdeti
                # `_advance_single(dt_s)` I/O-ideje, VALAMINT minden NEM
                # fast-loopos controller/state ESETÉN a teljes advance()-
                # hívás I/O-ideje -- azaz a DMM/PSU/Load SCPI olvasások
                # tényleges wall-clock időtartama) SEMMILYEN dt_s-be nem
                # kerül be, SOHA -- sem ebbe a tickbe (mert azt MÁR a hívás
                # ELŐTT rögzített `actual_dt_s` határozta meg), sem egy
                # későbbibe (mert a friss `time.perf_counter()` ezt az időt
                # már "elteltnek és elintegráltnak" tekintette). Ez egy
                # SZISZTEMATIKUS, minden controllert/state-et érintő Ah/Wh
                # alul-integrálást okozott, amennyivel a hívás saját I/O-ideje
                # meghaladja az itt lent következő ciklus-adminisztráció
                # (minta-építés/naplózás) idejét. A helyes megoldás
                # megkülönbözteti a KÉT esetet: a fast-loop ÁLTAL már
                # elintegrált extra időt (amit a controller a saját
                # `last_fast_loop_extra_s` property-jén jelent -- l.
                # charge_controller.py) KI kell zárni a következő tick
                # dt_s-éből (a 14. kör indoklása szerint, VÁLTOZATLANUL), DE
                # az advance() hívás SAJÁT (nem fast-loop) I/O-idejét BE kell
                # engedni. Ezt a `_tick_start_t` (a hívás ELŐTTI időpont,
                # NEM egy utólagos friss `time.perf_counter()`) PLUSZ a
                # jelentett fast-loop-extra idő adja: nem-fast-loop
                # controllernél/state-nél (getattr alapértéke 0.0) ez
                # egyszerűen `_tick_start_t` -- azaz a KÖVETKEZŐ tick
                # actual_dt_s-e a TELJES, valós wall-clock időt tartalmazza
                # ezen tick advance()-hívásának végétől (I/O-idő BENNE VAN).
                _last_tick_t = (
                    _tick_start_t + getattr(controller, "last_fast_loop_extra_s", 0.0)
                    + getattr(controller, "last_pulse_observed_extra_s", 0.0)
                )

                # Nyugtamegszakításkor nincs érvényes aktiválási minta: az OFF
                # minden új mérési I/O előtt fusson, graceful STOP esetén is.
                # Más STOP-nál a C-01/V-6 tick-mintája és a FAULT/DONE-diagnosztika megmarad.
                activation_hook = getattr(controller, "handoff_stop_requested", None)
                activation_interrupted = (
                    isinstance(activation_hook, ActivationStopHook)
                    and activation_hook.activation_interrupted
                )
                if (
                    self.emergency_stop_requested or activation_interrupted
                ) and not self._is_finished(controller):
                    pending_stop = _stop_if_requested()
                    if pending_stop is not None:
                        return pending_stop

                # STOP után nincs új konfiguráció vagy mintagyűjtési várakozás.
                # A már eldőlt controller-fault elsőbbsége is megmarad.
                if not self.stop_requested and not self._controller_faulted(controller):
                    self._reconfigure_dmm_nplc_for_relax_substate(step, controller)
                    self._start_prepared_relax_sampling()

                # Review-kör 5 (2026-09-10), GUI/UX F2 (P2, DFAM-021 rokona):
                # a "before" snapshotok (fent) a LÉPÉS BELÉPÉSEKORI, session-
                # szintű bázist és a controller SAJÁT (ekkor még 0-hoz közeli)
                # kiindulóértékét rögzítik (R1 F-14 óta a periodikus
                # checkpoint előtti _flush_totals() a session-összeggel
                # EGYÜTT lépteti őket) -- l. _build_sample() CHARGE/
                # DISCHARGE ágainak kommentje, miért kell ide átadni őket.
                sample = self._build_sample(
                    step, controller,
                    charge_ah_before=charge_ah_before, discharge_ah_before=discharge_ah_before,
                    charge_wh_before=charge_wh_before, discharge_wh_before=discharge_wh_before,
                )
                self._logger.log_sample(sample)
                if self.on_sample is not None:
                    self.on_sample(sample)
                # Governance-v2 review-kör 1 (CF-20/DFAM-021, batch 4.6): l.
                # TestResult.warning_code_counts kommentje — a
                # sample["warning_flags"] (fent, log_sample()-nek átadva)
                # PONTOSAN azt a WarningCode-ot hordozza, amit ez a tick
                # aktívnak jelentett; ez a session-szintű összesítő, ami
                # korábban sehol nem létezett.
                warning_flags = sample.get("warning_flags")
                warnings_this_tick = (
                    pulse_warnings if pulse_warnings is not None
                    else {warning_flags: 1} if warning_flags else {}
                )
                for warning, count in warnings_this_tick.items():
                    self._warning_code_counts[warning] = self._warning_code_counts.get(warning, 0) + count

                # Graceful STOP: a tick mintája és számlálói megmaradnak, de
                # új diagnosztikai I/O és pacing előtt a közös lezárás következik.
                pending_stop = _stop_if_requested()
                if pending_stop is not None:
                    return pending_stop

                if self._should_poll_device_errors():
                    polled_errors = self._poll_device_errors()
                    # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: a StepKind-
                    # felsorolás korábban csak a két legrégebbi (CHARGE/DISCHARGE)
                    # lépéstípust nevezte meg — a GOLDEN_GG/TRADITIONAL_LEARNING/
                    # OCV_SOC/CHEMID StepKind-ok (mind aktívan vezérlik a PSU-t/Loadot
                    # bizonyos belső állapotokban, l. _build_sample() fenti ágai) nem
                    # kaptak SCPI-kritikushiba → FAULT escalationt, csak naplózást —
                    # egy több órás GOLDEN_GG/TRADITIONAL_LEARNING ciklus alatt egy
                    # csak error-queue-ban jelentkező hiba (pl. output disable
                    # protection trip) észrevétlen maradt volna.
                    # R1 R-1: a StepKind-halmaz és a szűrő a modulszintű
                    # _ENERGY_TRANSFER_STEP_KINDS / _critical_polled_errors()
                    # -- a lépéshatár-poll (fent) ugyanezt használja.
                    # INS-003: aktív energiaátadás alatt egy nem-whitelistelt SCPI
                    # hiba alapból FAULT-ot vált ki (l. scpi_error_policy.py).
                    # R1 F-01/F-05 (B1B2_fixplan_FINAL.md B1/1): a producer által
                    # típusosan jelölt PSU/Load energia-állapot-bizonytalanság
                    # (commanded=OFF/actual=ON, állapotolvasás hibája vagy timeout
                    # miatti kimaradása) StepKind-től függetlenül -- önálló RELAX-ban
                    # is -- ugyanezen a FAULT-ágon fut át. Aktív lépésben a döntés a
                    # mai üres whitelist mellett változatlan (a jelölt bejegyzés
                    # szövege is_critical_scpi_error() szerint is kritikus).
                    critical_errors = _critical_polled_errors(step, polled_errors)
                    if critical_errors:
                        first = critical_errors[0]
                        _flush_totals()
                        terminal_return = True
                        return TestResult(
                            status="FAULT",
                            reason=f"SCPI_CRITICAL_ERROR[{first['device']}]: {first['error']}",
                            total_charge_ah=self._total_charge_ah,
                            total_discharge_ah=self._total_discharge_ah,
                            total_charge_wh=self._total_charge_wh,
                            total_discharge_wh=self._total_discharge_wh,
                            active_step_label=step.label,
                            communication_faults_count=self._device_error_total_count,
                            extreme_dt_s_count=self._total_extreme_dt_s_count,
                            relax_dmm_fault_count=self._total_relax_dmm_fault_count,
                            temperature_out_of_range_count=self._total_temperature_out_of_range_count,
                            temp_dmm_fault_ticks=self._total_temp_dmm_fault_ticks,
                            max_observed_dt_s=self._max_observed_dt_s,
                        )
                    # HW-R5-B: ugyanazon a tick-cadence-en (nem külön
                    # időzítéssel), a polled_errors-t nem olvassa -- l.
                    # _poll_psu_regulation_state() docstringje. R1 (B1B2_fixplan_
                    # FINAL.md B1/5): a FAULT-döntés UTÁN fut, így egy már
                    # eldönthető FAULT elé nem kerül diagnosztikai PSU-I/O.
                    self._poll_psu_regulation_state()

                self._logger.flush_all()
                _now_t = time.monotonic()
                # Review-kör 7 (2026-09-15), C-12: a sima 10s-es időzítő NEM
                # elég egy Cell BL restore-maradvány (pending_bq_df_restore)
                # jelzésére -- egy session-vég közeli crash a 10s-es ablakon
                # BELÜL is bekövetkezhetne, a checkpoint.json ekkor MÉG a
                # None (nincs maradvány) állapotot tükrözné. `_pending_changed`
                # a mező VÁLTOZÁSÁRA (None->dict VAGY dict->None) azonnali
                # write_checkpoint()-ot vált ki, a sima idő-alapú feltételtől
                # FÜGGETLENÜL -- így a maradvány legfeljebb 1 tick
                # (runner_tick_s) késleltetéssel jelenik meg a checkpointban,
                # NEM a teljes checkpoint_interval_s alatt. (Nem "azonnal a
                # temp-write ELŐTT" -- az egy Logger-injekció igényelne a
                # TraditionalLearningController-be, ez a fix-planner szerint
                # külön kör hatóköre.)
                _pending_now = getattr(self._active_controller, "pending_bq_df_restore", None)
                _pending_changed = _pending_now != self._last_pending_bq_df_restore
                # R2-11b (2026-09-15): ugyanaz a változás-alapú, azonnali
                # checkpoint-írás a GG seal-maradványra -- KÜLÖN mező, KÜLÖN
                # összehasonlítás (DFAM-018), OR-ral komponálva a fenti
                # pending_bq_df_restore-éval.
                _pending_unsealed_now = getattr(
                    self._active_controller, "pending_bq_unsealed", None
                )
                _pending_unsealed_changed = (
                    _pending_unsealed_now != self._last_pending_bq_unsealed
                )
                # R1 F-16 (DFAM-016+DFAM-018): a `_periodic_checkpoint_write_
                # failing` OR-ága a retry EGYETLEN forrása -- amíg az utolsó
                # periodikus írás bukott, minden tick újrapróbálja (a
                # `_last_pending_bq_*` NEM a lemezt, hanem az utolsó
                # MEGFIGYELT értéket tükrözi, l. lent).
                if (
                    _pending_changed
                    or _pending_unsealed_changed
                    or self._periodic_checkpoint_write_failing
                    or _now_t - self._last_checkpoint_t >= self._checkpoint_interval_s
                ):
                    # R17-01 (DFAM-001 + DFAM-032), R1 F-16 pontosítás: a
                    # két `_last_pending_bq_*` mező az utolsó MEGFIGYELT
                    # controller-értéket tartja (a terminális írások erre
                    # esnek vissza, amikor `_active_controller` már None),
                    # ezért egy bukott írás után SEM áll vissza a lemezen
                    # lévő régi értékre -- a korábbi rollback egy ugyanabban
                    # a tickben bekövetkező FAULT/DONE terminális írásában a
                    # maradványt elvesztette (None) vagy egy már megszűnt
                    # maradványt írt vissza. A "lemez nem friss" állapotot
                    # kizárólag `_periodic_checkpoint_write_failing` jelzi.
                    self._last_pending_bq_df_restore = _pending_now
                    self._last_pending_bq_unsealed = _pending_unsealed_now
                    self._last_checkpoint_t = _now_t
                    # WP-AK-02 (REC-001, "Tilos: resume active CC/CV/DISCHARGE/PULSE
                    # state"): egyedül a RELAX lépés tekinthető biztonságos
                    # köztes határnak (RelaxController sosem hívja a PSU/Load API-t,
                    # l. RELAX-001) — minden más lépéstípus (CHARGE/DISCHARGE/OCV_SOC/
                    # CHEMID) aktívan vezérli a PSU-t/Load-ot, ott a periodikus
                    # checkpoint SOSEM biztonságos folytatási pont.
                    #
                    # Release-audit 2026-08-23 (2.16, DESIGN WEAKNESS — tudatos,
                    # dokumentált egyszerűsítés, az OCV_SOC belső relax-alfázis
                    # mintáját követve, l. CLAUDE.md §14): a TRADITIONAL_LEARNING/
                    # GOLDEN_GG step-ek (WP-AK-16) a TestRunner szintjén egyetlen,
                    # OPAK lépésként futnak — a belső állapotgépük saját, ténylegesen
                    # biztonságos relax-alfázisait (PRECHARGE_RELAX/RELAX_1/RELAX_2H/
                    # RELAX_5H stb., ahol a PSU/Load ténylegesen OFF) a runner itt NEM
                    # látja. Emiatt `is_safe_boundary` ezekre a StepKind-okra MINDIG
                    # False — egy crash/áramkimaradás a több órás fizikai ciklus
                    # BÁRMELY pontján (akár egy hosszú, ténylegesen biztonságos relax
                    # közepén is) a teljes ciklus elölről kezdését kényszeríti ki.
                    # Ez a konzervatív irány NEM safety-hiba (helyesen blokkolja a
                    # potenciálisan nem-biztonságos resume-ot), de jelentős,
                    # dokumentálatlan üzemeltetési költség volt — finomítás
                    # (a controllerek saját internal_safe_boundary property-jének
                    # bevezetése) explicit tervezési/review kört igényel, mivel a
                    # resume-safety-boundary logika hibás módosítása valódi safety-
                    # kockázatot jelentene; ezért itt csak dokumentáljuk a
                    # korlátozást, nem implementáljuk elhamarkodottan.
                    is_safe_boundary = (step.kind == StepKind.RELAX)
                    # R1 F-14 (DFAM-001+DFAM-021): a lépés közbeni Ah/Wh és
                    # diagnosztikai számláló-haladás a `_flush_totals()`
                    # nélkül csak lépés végén került a session-összegekbe --
                    # a resume-képes RUNNING checkpoint (RELAX-nál safe_
                    # boundary=True) lépés közben a lépés-belépéskori
                    # összegeket írta. A flush idempotens (a delta-bázisok
                    # együtt frissülnek), a finally-flush nem dupláz.
                    _flush_totals()
                    try:
                        self._logger.write_checkpoint({
                            "status": "RUNNING",
                            "step": step.label,
                            "elapsed_s": sample.get("elapsed_s"),
                            # CF-17/CF-18/V-4 (batch 4.3) + R17-20: Ah/Wh +
                            # idő-origó + diagnosztikai számlálók, l.
                            # _checkpoint_summary_fields(). EZ a ténylegesen
                            # resume-képes (RUNNING, RELAX-nál safe_boundary=
                            # True) írás — a CF-17 MAX verifier V-2 lelete
                            # szerint a DONE-ági (terminális, resume-nál
                            # mindig blokkolt) írás önmagában NEM bizonyítja
                            # a valós resume-útvonalat.
                            **self._checkpoint_summary_fields(),
                            **self._checkpoint_traceability_fields(
                                safe_boundary=is_safe_boundary,
                                next_safe_step_index=step_index,
                            ),
                        })
                        self._periodic_checkpoint_write_failing = False
                    except OSError as exc:
                        # R17-01 (DFAM-001, a periodikus ag volt az 5
                        # write_checkpoint() hivohely egyetlen vedtelenje --
                        # l. a run() DONE-tail, _emergency_stop(),
                        # _graceful_stop() es _run_manual_checkpoint()
                        # testverek -- sorhivatkozas helyett metodusnev,
                        # mert tobb egyideju fix miatt driftel). SZANDEKOSAN
                        # OSError, NEM Exception: azok utan a session VEGET ER,
                        # itt viszont MEG ORAKIG/NAPOKIG fut -- egy logikai hiba
                        # (pl. TypeError a payloadban) elnyelese csendben
                        # megszuntetne a resume-kepesseget a teljes hatralevo
                        # BQ learning ciklusra, ezert az TOVABBRA is FAULT-ol.
                        # R1 F-16: nincs pending-rollback -- a KOVETKEZO tick
                        # ujraprobalkozasat a lenti flag (a trigger OR-aga)
                        # garantalja, maradvannyal vagy anelkul.
                        # MAX verifier F-3: esemeny csak a hiba-allapot
                        # ATMENETEN (nem meg-True volt -> True) -- az irasi
                        # KISERLET maga (es a maradvany-lathatosag) minden
                        # tickben megismetlodik, csak a GUI event-log
                        # arasztasat kerulendo NEM minden bukott kiserlet
                        # ad uj esemenyt.
                        if not self._periodic_checkpoint_write_failing:
                            self._periodic_checkpoint_write_failing = True
                            if self.on_event is not None:
                                self.on_event({
                                    "event_code": "PERIODIC_CHECKPOINT_WRITE_FAILED",
                                    "event_message": str(exc),
                                })

                if self._controller_faulted(controller):
                    # Forced poll fault esetén — a hibát okozó SCPI parancs kontextusa
                    # még az error queue-ban lehet.
                    self._poll_device_errors()
                    _flush_totals()
                    # Review 2026-09-15 (C-13/DFAM-016, RECURRING): ez az ág
                    # safe_off_attempted=False-szal tér vissza, tehát a run()
                    # (l. fent, "safe_off_attempted" kommentje) MÉGEGYSZER hívja
                    # _emergency_stop()-ot -> _abort_active_controller()-en át a
                    # controller SAJÁT emergency_stop()-ja MÁSODSZOR is lefutna,
                    # holott a controller MÁR FAULT-ban van (ő maga hívta ezt ki).
                    # TRADITIONAL_LEARNING/GOLDEN_GG-nél ez valós, ismételt BQ
                    # I/O-t jelentett volna a Cell BL restore-úton. A runner saját,
                    # alacsonyabb szintű safe-off-ja (Load OFF + PSU ALL OFF) a
                    # run() _emergency_stop()-ágán VÁLTOZATLANUL lefut -- csak a
                    # controller-szintű, már elvégzett takarítás nem ismétlődik.
                    # Ugyanaz a minta, mint a DONE-ági self._active_controller =
                    # None (lent, köztes és záró ágon).
                    self._active_controller = None
                    terminal_return = True
                    return TestResult(
                        status="FAULT",
                        reason=getattr(controller, "fault_reason", "CONTROLLER_FAULT"),
                        total_charge_ah=self._total_charge_ah,
                        total_discharge_ah=self._total_discharge_ah,
                        total_charge_wh=self._total_charge_wh,
                        total_discharge_wh=self._total_discharge_wh,
                        active_step_label=step.label,
                        communication_faults_count=self._device_error_total_count,
                        extreme_dt_s_count=self._total_extreme_dt_s_count,
                        relax_dmm_fault_count=self._total_relax_dmm_fault_count,
                        temperature_out_of_range_count=self._total_temperature_out_of_range_count,
                        temp_dmm_fault_ticks=self._total_temp_dmm_fault_ticks,
                        max_observed_dt_s=self._max_observed_dt_s,
                    )

                # A naplózás/readback alatt érkező jelzés után sem várunk energizálva.
                pending_stop = _stop_if_requested()
                if pending_stop is not None:
                    return pending_stop
                if self._config.sleep_enabled:
                    # A pacing-cél a TELJES tick (advance()+log) valós időtartama
                    # legyen ~runner_tick_s — ezért itt a tick-kezdeti _tick_start_t
                    # a helyes referenciapont, NEM a fent kiszámolt _last_tick_t
                    # (az utóbbi az integrációs végpontot jelöli -- l. Review-kör 5
                    # M5-01 kommentje -- NEM egy advance() utáni friss
                    # time.perf_counter()-hívás eredménye, l. MAX verifier N1).
                    _elapsed = time.perf_counter() - _tick_start_t
                    time.sleep(max(0.0, self._config.runner_tick_s - _elapsed))
        except BaseException as exc:
            # Lokális kivételállapot: egy külső except nem eredeti lépéshiba.
            step_error = exc
            raise
        finally:
            try:
                # A handler már lezárt; második drain nem írhatja át az eredményt.
                if self.status not in ("FAULT", "STOPPED"):
                    if (
                        self._relax_sampler_controller is not None
                        and (step_error is not None or terminal_return)
                        and self._pending_relax_safe_off_result is None
                    ):
                        self._pending_relax_safe_off_result = self._safe_all_off_with_uncertainty_check()
                    self._finish_relax_sampling()
            except BaseException as exc:
                # Az eredeti tick/fault megmarad, a release-hibát a feloldó csatolja.
                # Normál drain után a wrapper dispatchol; nincs return a finally-ban.
                if (
                    not isinstance(exc, Exception) and step_error is None and not terminal_return
                    and not (self.emergency_stop_requested or self.stop_requested)
                ):
                    raise
                if self._relax_shutdown_failure is None:
                    if step_error is None and not terminal_return:
                        raise
                    self._relax_shutdown_failure = f"{type(exc).__name__}: {exc}"
            finally:
                # Az OCV impulzus saját NPLC-tranzakciót végez; a régi cache
                # sikeres és bizonytalan restore után sem tekinthető readbacknek.
                if step.kind == StepKind.OCV_SOC:
                    self._last_configured_nplc = None
                self._relax_totals_flush = None
                _flush_totals()

        return TestResult(
            status="DONE",
            active_step_label=step.label,
            communication_faults_count=self._device_error_total_count,
            extreme_dt_s_count=self._total_extreme_dt_s_count,
            relax_dmm_fault_count=self._total_relax_dmm_fault_count,
            temperature_out_of_range_count=self._total_temperature_out_of_range_count,
            temp_dmm_fault_ticks=self._total_temp_dmm_fault_ticks,
            max_observed_dt_s=self._max_observed_dt_s,
        )

    def _pulse_poll_fault(
        self, step: TestStep, *, force: bool = False,
        before_io: Callable[[], bool] | None = None,
    ) -> str | None:
        """B10b/B13: helyi kapu a poll minden új műszerhívása előtt."""
        halted = False

        def continue_poll(entries: list[dict]) -> bool:
            nonlocal halted
            if halted:
                return False
            critical = _critical_polled_errors(step, entries)
            if critical:
                first = critical[0]
                halted = True
                if self._active_controller is not None:
                    self._active_controller.emergency_stop(
                        f"SCPI_CRITICAL_ERROR[{first['device']}]: {first['error']}",
                    )
                return False
            if before_io is not None and not before_io():
                halted = True
                return False
            return True

        if self.stop_requested or self.emergency_stop_requested:
            return None
        if force or self._should_poll_device_errors():
            errors = self._poll_device_errors(continue_poll=continue_poll)
            critical = _critical_polled_errors(step, errors)
            if critical:
                first = critical[0]
                return f"SCPI_CRITICAL_ERROR[{first['device']}]: {first['error']}"
        return None

    def _should_poll_device_errors(self) -> bool:
        now = time.monotonic()
        load = self._instruments.load
        commanded_at = getattr(load, "input_on_commanded_at", None)
        # R16-B10bF1: a lejárt ON-nyugtát a következő meglévő kapuban
        # ellenőrizzük; a 60 s-os hibapoll nem halaszthatja el a döntést.
        if (
            load.input_commanded_on and commanded_at is not None
            and now - commanded_at >= INPUT_ACTIVATION_ACK_TIMEOUT_S
        ):
            return True
        return now - self._last_device_error_poll_t >= self._config.device_error_poll_interval_s

    def _poll_device_errors(
        self, safe_off_result: dict | None = None, *,
        continue_poll: Callable[[list[dict]], bool] | None = None,
    ) -> list[dict]:
        """`safe_off_result`: R1 F-42 -- KIZÁRÓLAG a 4 safe-off utáni
        kényszerített hívóhely adja át (DONE-tail, `_emergency_stop()`,
        `_graceful_stop()`, `_run_manual_checkpoint()`); a lefokozás a
        producer eredményének megszerzése UTÁN, a naplózás ELŐTT fut (l.
        `_demote_safe_off_result_on_energy_uncertainty()`). None (cadence-
        ág, controller-fault ág, lépéshatár-poll): a viselkedés változatlan."""
        self._last_device_error_poll_t = time.monotonic()
        # Kör 21 F6 (METRO-R21-1 + open_findings[70], DFAM-027): a
        # `_build_sample()` MÁR MEGÁLLAPÍTOTT, adott tickre vonatkozó
        # `load_current_readback_valid is False` jelzését (1:1 a
        # `skip_load_voltage=ctrl_i_load_valid is False` mintával)
        # felhasználva ne küldjünk TOVÁBBI SCPI System:Error?/kérdéses-
        # státusz/bemenet-állapot lekérdezést egy MÁR bizonyítottan
        # halott Loadnak -- ez az ág (a MEGLÉVŐ cadence-gated hívás, l.
        # `_should_poll_device_errors()`, ÉS az ez alatti ÚJ,
        # kényszerített terminális-úti hívások, l. `_emergency_stop()`/
        # `_graceful_stop()`/DONE-tail lent) MOST egységesen ezen a
        # helyen kapja meg a kaput, nem hívóhelyenként külön-külön
        # (DFAM-016 "kézzel felsorolt terminális ágak" mintájának
        # elkerülése). `self._active_controller` a DONE-tail/manual-
        # checkpoint hívásakor jellemzően MÁR None -- ekkor a jelzés
        # hiányában `skip_load` False, tehát a viselkedés VÁLTOZATLAN
        # (a régi, esetlegesen lassabb, de nem kevésbé biztonságos utat
        # követi).
        skip_load = getattr(
            self._active_controller, "load_current_readback_valid", None
        ) is False
        errors = (
            self._instruments.poll_device_errors(skip_load=skip_load)
            if continue_poll is None
            else self._instruments.poll_device_errors(skip_load=skip_load, continue_poll=continue_poll)
        )
        self._device_error_total_count += sum(
            not is_error_queue_poll_deferred(err) and not is_input_activation_pending(err)
            for err in errors
        )
        # R1 F-42 (B7_B7S_fixplan_FINAL.md B7S/1): a már megszerzett
        # energia-bizonytalanság a lenti naplózás/callback esetleges hibája
        # ELŐTT kerül a safe_off_result-ba -- egy log_device_error() OSError
        # (amit a terminális hívóhely except-je elnyel) nem hagyhatja True-n.
        if safe_off_result is not None:
            self._demote_safe_off_result_on_energy_uncertainty(safe_off_result, errors)
        now = time.monotonic()
        deferred = {err["device"] for err in errors if is_error_queue_poll_deferred(err)}
        for device in tuple(self._deferred_since):
            if device not in deferred:
                started = self._deferred_since.pop(device)
                self._deferred_totals[device] = (
                    self._deferred_totals.get(device, 0.0) + max(0.0, now - started)
                )
        for err in errors:
            if is_input_activation_pending(err):
                # A nyugta függőben van; nem hiba és nem igazolt ON-állapot.
                message = "Load: bekapcsolási nyugta függőben (1 s-os ablak)"
                self._logger.log_event("INPUT_ACTIVATION_PENDING", message)
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "INPUT_ACTIVATION_PENDING", "event_message": message,
                    })
                continue
            if is_error_queue_poll_deferred(err):
                device = err["device"]
                if device in self._deferred_since:
                    continue
                self._deferred_since[device] = now
                # Forrás: 34460-70-Manual.txt:12008–12017, SYSTem:ERRor[:NEXT]?.
                # A 34465A hibasora véges: hosszú halasztás alatt túlcsordulhat,
                # ezért a nyugta utáni poll és a halasztási idő nem hagyható el.
                message = f"{err['device']}: hibasor-poll HALASZTVA (DMM foglalt)"
                logging_error: str | None = None
                try:
                    self._logger.log_event("ERROR_QUEUE_POLL_DEFERRED", message)
                except Exception as exc:  # noqa: BLE001 -- a Python-napló is őrzi
                    logging_error = str(exc)
                    logging.getLogger(__name__).critical("Halasztási esemény naplóhibája: %s", exc)
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "ERROR_QUEUE_POLL_DEFERRED",
                        "event_message": message,
                    })
                    if logging_error is not None:
                        self.on_event({
                            "event_code": "ERROR_QUEUE_POLL_DEFERRED_LOGGING_FAILED",
                            "event_message": logging_error, "severity": "CRITICAL",
                        })
                continue
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self._logger.log_device_error(err["device"], err["error"])
            if self.on_device_error is not None:
                self.on_device_error({
                    "timestamp_iso": ts,
                    "device": err["device"],
                    "error": err["error"],
                })
        return errors

    def _demote_safe_off_result_on_energy_uncertainty(
        self, safe_off_result: dict, polled_errors: list[dict]
    ) -> None:
        """R1 F-42 / NS-06 (B7_B7S_fixplan.md B7S, D-B7S-1): ha a safe-off
        UTÁNI kényszerített poll egy eszközre energia-állapot-bizonytalan
        bejegyzést ad (`is_energy_state_uncertain()`: commanded=OFF/
        actual=ON, *_STATE_POLL_FAILED, F-05 timeout-kihagyás), az eszköz
        safe_off_result-ja HELYBEN False lesz. Ugyanaz a dict, mint
        `self._last_safe_off_result`, így a checkpoint (`last_safe_off_
        result`), a TestResult és a manuális esemény egységesen False-t
        kap. True = parancs-szintű siker (SAFE-OFF-EVIDENCE-PERSISTENCE-001);
        előléptetés nincs; a workflow-státusz nem változik (D-B7S-1).
        Az esemény csak a ténylegesen visszaminősített (True -> False)
        eszközökre jön; a parancs-szinten már False eszköz eseményét a
        `_safe_all_off_with_uncertainty_check()` adja."""
        demoted: list[str] = []
        for entry in polled_errors:
            if not is_energy_state_uncertain(entry):
                continue
            device = entry["device"]
            if safe_off_result.get(device) is True:
                demoted.append(f"{device} ({entry['error']})")
            safe_off_result[device] = False
        if not demoted:
            return
        names = ", ".join(demoted)
        message = (
            f"safe-off utáni kényszerített poll: az energia-állapot nem igazolt "
            f"a következő eszköz(ök)ön: {names} -- safe_off_result False-ra "
            "minősítve (a parancs-szintű OFF sikeres volt; readback-alapú "
            "bizonytalanság)."
        )
        try:
            self._logger.log_event("SAFE_OFF_RESULT_UNCERTAIN", message, is_critical=True)
        except Exception as exc:  # noqa: BLE001 - l. _safe_all_off_with_uncertainty_check() V-4 indoklása
            if self.on_event is not None:
                self.on_event({
                    "event_code": "SAFE_OFF_RESULT_UNCERTAIN_LOGGING_FAILED",
                    "event_message": f"{names}: {exc}",
                })
        if self.on_event is not None:
            self.on_event({
                "event_code": "SAFE_OFF_RESULT_UNCERTAIN",
                "event_message": message,
            })

    def _poll_psu_regulation_state(self) -> None:
        """Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói
        döntés 2026-09-10): a PSU CV/CC regulációs állapotát olvassa ki
        (`device_psu.py::read_regulation_state()`, TISZTÁN olvasás-only,
        `STAT:OPER:INST:ISUM1:COND?` alapú) és `on_psu_regulation`
        callback-en keresztül teszi közzé -- KIZÁRÓLAG egy GUI-only, élő
        kijelzési célra.

        SZÁNDÉKOSAN nem megy semerre máshova:
        - NEM `poll_device_errors()`/`on_device_error` -- az a kritikus
          SCPI-hiba escalation csatornája (`is_critical_scpi_error()` ->
          FAULT); a CV bit normál töltésnél SZINTE MINDIG aktív, ugyanaz
          a hibaosztály lenne, mint amit HW-R5-A javított a Load oldalon.
        - NEM `self._logger` -- nem kerül a session CSV/riportba (a
          felhasználó explicit "GUI-only" döntése, nem naplózási/
          jelentési adat).
        - NEM `self.on_sample`/`_build_sample()` -- nincs saját
          `CSV_COLUMNS` mezője, tehát a mock/teszt-infrastruktúra és a
          logger-schema egyáltalán nem érintett.

        Best-effort: egy readback-hiba (pl. PSU nincs csatlakoztatva egy
        Load-only teszthez) csendben kimarad -- ez egy tisztán
        diagnosztikai kényelmi funkció, nem safety- vagy adatintegritás-
        kritikus, tehát nem szabad, hogy megzavarja a fő tick-ciklust."""
        if self.on_psu_regulation is None:
            return
        try:
            state = self._instruments.psu.read_regulation_state()
        except Exception:
            return
        self.on_psu_regulation(state)

    def _abort_active_controller(self, reason: str) -> None:
        """Governance-v2 review-kör 1 (CF-01/DFAM-016, MASTER_CONSOLIDATED.md
        batch 4.1): a `_run_step()`-ben aktuálisan futó controller a runner
        alacsonyabb szintű safe-off-ja (`_safe_all_off_with_uncertainty_
        check()`, ami csak PSU/Load OFF-ot ad ki) mellett SAJÁT
        `emergency_stop()`-ot is igényel — ez végzi pl. a Traditional
        Learning Cell BL ideiglenes learning-értékének visszaállítását és a
        BQ learning eredmény klasszifikációját (`_classify_result()`).
        Korábban a controller LOKÁLIS volt `_run_step()`-ben, és a runner-
        vezérelt megszakítás (`_emergency_stop()`/`_graceful_stop()`, akár
        lépések között, akár egy lépés kellős közepén) sosem érte el —
        ennek eredménye tartós, néma BQ Data Flash maradvány volt.
        Best-effort: egy itt keletkező hiba nem akadályozhatja a hívó
        (`_emergency_stop()`/`_graceful_stop()`) saját, garantált safe-off
        lépését."""
        controller = self._active_controller
        if controller is None:
            return
        abort = getattr(controller, "emergency_stop", None)
        # Review-kör 5, Architektúra A-7 (P3, dokumentációs pontosítás,
        # KEEP_AS_IS -- nulla viselkedésváltozás): jelenleg KIZÁRÓLAG a
        # RelaxController nem rendelkezik emergency_stop()-pal (l.
        # relax_controller.py modul docstring RELAX-001 elve: "sosem hívja
        # a PSU/Load API-t") -- nincs mit visszaállítania/lezárnia egy
        # controller-szintű vészleállításnál, a runner ALACSONYABB szintű
        # `_safe_all_off_with_uncertainty_check()`-je (PSU/Load OFF)
        # önmagában elegendő. Ha a jövőben egy ÚJ controller kerül be
        # emergency_stop() nélkül, ez a néma no-op ÚGYANÚGY lefedné —
        # ezért minden ÚJ controllernél explicit ellenőrizni kell, hogy
        # tényleg nincs-e saját, controller-szintű takarítanivalója
        # (BQ Data Flash restore, belső állapot, stb.), mielőtt
        # emergency_stop() nélkül hagyjuk.
        if abort is None:
            return
        # Review 2026-09-15 (C-09/DFAM-025): TIPIZÁLT jelzés a controllernek,
        # hogy ez KÍVÜLRŐL kezdeményezett abort, NEM a saját safety-logikája
        # -- csak az a controller látja, amelyik explicit deklarálja ezt az
        # attribútumot (jelenleg kizárólag a TraditionalLearningController,
        # l. _classify_result()); a másik controllerek viselkedése bit-
        # azonos, egyetlen riport-/eseményszöveg sem változik.
        #
        # KORREKCIÓ (2026-09-15, MAX verifier 1. kör, P1-A REJECTED lelet):
        # a `_run_step()` tick-hurkában TÖBB, EGYMÁSTÓL FÜGGETLEN korai-
        # return ág vezethet ide (pl. a SCPI_CRITICAL_ERROR poll-ág, ami a
        # `_controller_faulted()` ág ELŐTT térhet vissza UGYANABBAN a
        # tickben, amikor a controller a SAJÁT advance()-ében már FAULT-ba
        # lépett) -- ha egy ilyen, a controllertől FÜGGETLEN esemény miatt
        # hívódik ez a metódus, miközben a controller MÁR a saját safety-
        # logikája szerint FAULT-ban van, a `runner_abort_origin` téves
        # jelzés lenne: egy VALÓDI biztonsági hibát (pl. BATTERY_
        # OVERVOLTAGE) fokozna le hamisan AWAITING-re _classify_result()-
        # ben. A `_controller_faulted()` -- ugyanaz a predikátum, amit a
        # `_run_step()` a saját ágán használ -- itt, a KÖZÖS hívóhelyen
        # deríti ki ezt, NEM a korai-return ágak mindegyikén külön-külön
        # (az utóbbi maga a DFAM-016 "kézzel felsorolt terminális ágak"
        # mintája lenne).
        #
        # KORREKCIÓ (2026-09-15, MAX verifier 2. kör, F-1 BLOKKOLÓ lelet): a
        # `_controller_faulted()` ismeretlen state-típusra explicit
        # `raise ValueError`-t dob (l. ott) -- ezt a hívást korábban a `try`
        # ELŐTT helyeztük el, tehát egy ilyen kivétel KIKERÜLTE volna a lenti
        # `except BaseException`-t, és KIFELÉ vinné a hívó (`_emergency_
        # stop()`/`_graceful_stop()`) garantált safe-off-ja ELŐL -- pontosan
        # az a hibaosztály, amit ez a `try` maga véd. A guard mostantól a
        # `try`-on BELÜL fut.
        try:
            # Review-kör 8 (2026-09-15), R2-02/DFAM-016 (RECURRING, 3.+
            # előfordulás -- a registry `prevention` mezője EGYETLEN, közös
            # őrt ír elő az ágankénti kézi nullázás helyett). A `_run_step()`
            # tick-hurkából TÖBB, egymástól független korai-return ág vezet
            # ide; a SCPI_CRITICAL_ERROR ág -- a `_controller_faulted()` ággal
            # ellentétben -- NEM nullázza az `_active_controller`-t, így egy
            # MÁR terminális (DONE/FAULT) controller MÁSODSZOR is megkapta a
            # saját `emergency_stop()`-ját. Terminális állapotba a controller
            # KIZÁRÓLAG a SAJÁT terminális útján jut (DONE-ág, ill. saját
            # `emergency_stop()`), tehát a controller-szintű takarítás (Cell
            # BL restore, seal-visszaállítás, klasszifikáció) MÁR lefutott --
            # itt nincs mit pótolni, csak elrontani: a 7 controller EGYIKÉBEN
            # sincs terminális-állapot őr, a TraditionalLearningControllernél
            # a második hívás `_enter(LearningState.FAULT)` + újrafutó
            # `_classify_result()` révén egy BIZONYÍTOTT LEARNING_RESULT_
            # VERIFIED eredményt LEARNING_RESULT_AWAITING + ABORTED_BY_
            # RUNNER-re fokoz le a report.json-ban. A hívó (`_emergency_
            # stop()`/`_graceful_stop()`) garantált, alacsonyabb szintű
            # `_safe_all_off_with_uncertainty_check()`-je ettől FÜGGETLENÜL
            # lefut. Az őr a `try`-on BELÜL van: `_is_finished()` ismeretlen
            # state-típusra explicit `raise ValueError`-t dob -- ugyanaz az
            # F-1 tanulság, ami a tagging-guardot is ide hozta. MAX verifier
            # P3-1 megjegyzés (2026-09-15): egy HIPOTETIKUS, ismeretlen
            # state-típusú ÉS `runner_abort_origin` attribútum NÉLKÜLI
            # controllernél ez a sor most FELTÉTLENÜL dob (elnyelve a lenti
            # `except`-ben) -- korábban a `hasattr()` rövidzár miatt
            # `_controller_faulted()` sosem futott, és `abort(reason)`
            # LEFUTOTT volna. Jelenleg a 8 ismert controller-típus mindegyike
            # elérhetetlen ide `_is_finished()` ValueError-je nélkül (a
            # `_run_step()` tick-hurok `_is_finished()`-je már az ELSŐ
            # `advance()` előtt kizárná), tehát ez NEM aktív viselkedés-
            # változás -- csak dokumentálva, ne kelljen újra felfedezni.
            if self._is_finished(controller):
                return
            if hasattr(controller, "runner_abort_origin") and not self._controller_faulted(controller):
                controller.runner_abort_origin = reason
            abort(reason)
        except BaseException as exc:
            # MAX independent verifier V-7 lelete: korábban ez a hiba csak
            # on_event-re ment, a session logjába (events.csv) nem — pont
            # a CF-01 tárgya (pl. Cell BL restore hiba) maradt volna
            # nyomtalan, ha a live GUI nem figyeli (CF-07, nyitott).
            #
            # KORREKCIÓ (2026-09-15, MAX verifier 1. kör, P2-B lelet): a
            # metódus SAJÁT docstringje ígéri, hogy "egy itt keletkező hiba
            # nem akadályozhatja a hívó (_emergency_stop()/_graceful_stop())
            # saját, garantált safe-off lépését" -- egy `KeyboardInterrupt`
            # (pl. egy MÁSODIK Ctrl+C, amíg a controller saját, esetlegesen
            # több másodperces BQ Data Flash I/O-t végző emergency_stop()-ja
            # fut) korábban ÁTUGROTTA a hívó _safe_all_off_with_uncertainty_
            # check()-jét és a logger.close()-t is (ellentétben a C-04 fix
            # legkülső, run()-szintű ágával, ITT a helyes viselkedés a
            # KIVÉTEL ELNYELÉSE, nem újradobása -- ez a réteg kifejezetten
            # azért létezik, hogy a hívó garantált, alacsonyabb szintű
            # takarítását SOHA ne akadályozza, bármilyen kivétel-osztály
            # legyen is az oka).
            try:
                self._logger.log_event(
                    "CONTROLLER_ABORT_FAILED", f"{reason}: {exc}", is_critical=True,
                )
            except Exception:
                pass
            if self.on_event is not None:
                self.on_event({
                    "event_code": "CONTROLLER_ABORT_FAILED",
                    "event_message": f"{reason}: {exc}",
                })
        finally:
            self._remember_tlc_completion(controller)

    def _safe_all_off_with_uncertainty_check(self) -> dict:
        """safe_all_off() + CLAUDE.md §10.4 kötelező kiegészítés: egy
        bizonytalan (nem igazoltan sikeres) PSU/Load OFF sosem maradhat
        csendben — ha bármelyik sikertelen, kritikus SAFE_OFF_RESULT_
        UNCERTAIN eseményt naplózunk a megfelelő WarningCode névvel.

        MAX independent verifier V-4 lelete (batch 4.1): a `log_event()`
        hívás (I/O — `flush_all()`-t is végez) korábban védtelen volt
        MINDEN hívóhelyen (_emergency_stop/_graceful_stop/DONE/checkpoint)
        — egy I/O-hiba itt (pl. tele lemez) kiszállt volna a teljes
        terminális eredmény (Ah/Wh összegek, safe_off_result — épp a CF-02
        bizonyítéka) és a `logger.close()` elvesztésével. Egyetlen helyen
        védve, nem hívóhelyenként — az utóbbi maga a DFAM-016 mintája
        lenne (kézzel felsorolt, könnyen lemaradó terminális ágak).

        R17-03+V4-1 (2026-09-16, Kör 17 MAX-effort review, DFAM-016+018):
        `self._last_safe_off_result` a hívás UTÁN frissül (DFAM-032:
        flag csak a tényleges eredményről tükrözhet állapotot), így ez
        az EGYETLEN, mind a 6 hívóhelyet lefedő pont, ahonnan a
        checkpoint-plumbing (`_checkpoint_traceability_fields()`) és a
        GUI (élő feed + befejezés-dialógus) egyaránt eléri. A bizonytalan
        ág mostantól `on_event`-et is ad (nem csak `log_event()`-et) --
        korábban az élő GUI-feed EGYÁLTALÁN nem látta ezt, csak az
        `events.csv`. SZÁNDÉKOSAN NINCS "SAFE_OFF_PERFORMED" siker-esemény
        itt (szemben main_window.py::_perform_gui_safe_off()-fal) -- a
        sikeres eredményt a `last_safe_off_result` checkpoint-mező és a
        `report.json` safe_off_result mezője már hordozza, egy plusz
        élő-esemény minden terminális híváson (hatnál) elárasztaná az
        event-logot minden egyetlen sessionre."""
        result = self._instruments.safe_all_off()
        self._last_safe_off_result = result
        uncertain = [
            (name, code)
            for name, code in (
                ("PSU", WarningCode.PSU_OUTPUT_STATE_UNCERTAIN),
                ("Load", WarningCode.LOAD_INPUT_STATE_UNCERTAIN),
            )
            if not result.get(name, False)
        ]
        if uncertain:
            names = ", ".join(f"{name} ({code.name})" for name, code in uncertain)
            try:
                self._logger.log_event(
                    "SAFE_OFF_RESULT_UNCERTAIN",
                    f"safe_off() nem igazoltan sikeres a következő eszköz(ök)ön: {names}",
                    is_critical=True,
                )
            except Exception as exc:
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "SAFE_OFF_RESULT_UNCERTAIN_LOGGING_FAILED",
                        "event_message": f"{names}: {exc}",
                    })
            if self.on_event is not None:
                self.on_event({
                    "event_code": "SAFE_OFF_RESULT_UNCERTAIN",
                    "event_message": (
                        f"safe_off() nem igazoltan sikeres a következő "
                        f"eszköz(ök)ön: {names} (parancs-szintű eredmény, "
                        "nem hardveres readback)."
                    ),
                })
        return result

    def _terminal_safe_off(self) -> dict:
        # Csak a jelen lezárásban már kiadott OFF nyugtáját fogyasztjuk el.
        result = self._pending_relax_safe_off_result
        self._pending_relax_safe_off_result = None
        if result is None:
            return self._safe_all_off_with_uncertainty_check()
        return result

    def _resolve_terminal(
        self, primary_reason: str = "", *, emergency_reason: str = "",
        graceful_reason: str = "", default_status: str = "DONE",
        default_reason: str = "",
    ) -> tuple[str, str]:
        """Egyetlen okelsőbbség: tick/fault > vészok > STOP > normál eredmény.

        A megjegyzett release-hiba késői nyugta után sem tűnik el.
        FAULT: elsődleges hiba, emergency vagy release-hiba; STOPPED: tiszta STOP.
        A TLC runner_abort_origin az abort pillanatának feloldott okát kapja;
        a későbbi release-diagnózis nem vált ki második controller-abortot.
        Az OFF nyugtáját a közös lezáró külön, pontosan egyszer fogyasztja.
        """
        # A vészállapotot a flag jelzi; az üres ok nem törölheti a kérést.
        emergency = (
            (self.emergency_stop_reason or "USER_EMERGENCY_STOP")
            if self.emergency_stop_requested else emergency_reason
        )
        graceful = graceful_reason or ("USER_STOP_REQUESTED" if self.stop_requested else "")
        reason = primary_reason or emergency or graceful or default_reason
        if primary_reason.startswith("PULSE_NPLC_RESTORE_FAILED") and (emergency or graceful):
            # A restore-hiba elsődleges; a mindenkor választott jelzés oka is megmarad.
            reason += f" | {emergency or graceful}"
        status = "FAULT" if primary_reason or emergency else ("STOPPED" if graceful else default_status)
        failure = self._relax_shutdown_failure
        if failure is not None:
            suffix = f"RELAX_RELEASE_FAILED: {failure}"
            reason = f"{reason}; {suffix}" if reason else suffix
            status = "FAULT"
        return status, reason

    def _emergency_stop(self, reason: str) -> TestResult:
        # Az ures elsodleges ok sem valtoztathatja a hibautat sikeres zarassa.
        return self._stop_terminal(primary_reason=reason or "STEP_FAULT")

    def _stop_terminal(
        self, primary_reason: str = "", *, emergency_reason: str = "",
        graceful_reason: str = "",
    ) -> TestResult:
        """Közös stop-lezárás: egy OFF, egy abort, egy terminális esemény."""
        self.status, reason = self._resolve_terminal(
            primary_reason, emergency_reason=emergency_reason, graceful_reason=graceful_reason,
        )
        # CF-01: a controller saját emergency_stop()-ja ELŐSZÖR, best-effort
        # (l. _abort_active_controller()) — a garantált, alacsonyabb szintű
        # safe-off ALATTA/UTÁNA fut, függetlenül attól, hogy ez sikerült-e.
        self._abort_active_controller(reason)
        safe_off_result = self._terminal_safe_off()
        try:
            self._finish_relax_sampling(safe_off_done=True)
        except BaseException as exc:  # noqa: BLE001 -- Ctrl+C alatt is végig naplózunk
            if self._relax_shutdown_failure is None:
                self._relax_shutdown_failure = f"{type(exc).__name__}: {exc}"
            self._relax_critical(str(exc))
            # Késői nyugtára egyetlen nem blokkoló poll; a hibatörténet megmarad.
            try:
                self.poll_relax_sampling_shutdown()
            except BaseException as poll_exc:
                self._relax_critical(str(poll_exc))
        # Kör 21 F6 (METRO-R21-1): kényszerített hiba-poll a safe-off UTÁN
        # -- a Load OFF/PSU ALL OFF parancsok SAJÁT hibái is bekerülnek a
        # device_errors.csv-be/communication_faults_count-ba, nem csak a
        # legfeljebb `device_error_poll_interval_s` (alapértelmezetten
        # 60s) cadence-en belüli. Best-effort: egy itt keletkező hiba nem
        # akadályozhatja a lentebbi, garantált naplózást/logger.close()-t
        # -- ÉS (MAX verifier B3 lelete, 2026-09-19) `except BaseException`,
        # NEM csak `Exception`: 1:1 a `_abort_active_controller()` mellette
        # lévő, MÁR javított mintájával (MAX verifier P2-B, 2026-09-15) --
        # egy `Exception`-en szűkebb except itt egy `KeyboardInterrupt`-ot
        # kiengedne ebből a metódusból, MIELŐTT a lentebbi garantált
        # log_event()/write_checkpoint()/logger.close() lefutna.
        # R1 F-42: safe_off_result átadva -- energia-bizonytalanságnál
        # HELYBEN False (l. _demote_safe_off_result_on_energy_uncertainty()).
        try:
            self._poll_device_errors(safe_off_result)
        except BaseException:
            pass
        # Teljes kódbázis review 19. kör (2026-09-06), Session/Workflow P2
        # lelet: a safe-off MINDIG elsőként, védetten fut (fent) — de az
        # utána következő naplózás korábban védtelen volt. Egy I/O-hiba
        # (pl. tele lemez) itt kiszállt volna egészen worker.py külső
        # catch-all-jáig, ami egy ÚJ, szintetikus TestResult-ot gyártott
        # volna safe_off_attempted=False/safe_off_result={} alapértékkel —
        # csendesen elveszítve a fent MÁR sikeresen kiszámolt safe-off
        # eredményt a végleges riportból (CLAUDE.md §13 "safe-off
        # eredménye" + "logírási hiba látható kezelése").
        # A drain és a végső poll alatt érkezett vészok itt is elsőbbséget kap.
        self.status, reason = self._resolve_terminal(
            primary_reason, emergency_reason=emergency_reason, graceful_reason=graceful_reason,
        )
        event_code = "EMERGENCY_STOP" if self.status == "FAULT" else "GRACEFUL_STOP"
        try:
            self._logger.log_event(event_code, reason, is_critical=self.status == "FAULT")
            self._logger.write_checkpoint({
                "status": self.status,
                "reason": reason,
                # R17-20 -- l. _checkpoint_summary_fields() docstringje.
                **self._checkpoint_summary_fields(),
                **self._checkpoint_traceability_fields(safe_boundary=False, next_safe_step_index=-1),
            })
        except Exception as exc:
            if self.on_event is not None:
                self.on_event({
                    "event_code": f"{event_code}_LOGGING_FAILED",
                    "event_message": str(exc),
                })
        finally:
            # A fájlleírók lezárása mindig megkísérlődik, még akkor is, ha a
            # fenti naplózás hibázott — különben a session mappa fájljai
            # nyitva maradnának (Windows-on ez a KÖVETKEZŐ session/teszt
            # fájlműveleteit is akadályozná).
            try:
                self._logger.close()
            except Exception:
                pass
        if self.on_event is not None:
            self.on_event({"event_code": event_code, "event_message": reason})
        return TestResult(
            status=self.status,
            reason=reason,
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
            total_charge_wh=self._total_charge_wh,
            total_discharge_wh=self._total_discharge_wh,
            active_step_label=self.current_step.label if self.current_step else "",
            safe_off_attempted=True,
            safe_off_result=safe_off_result,
            communication_faults_count=self._device_error_total_count,
            extreme_dt_s_count=self._total_extreme_dt_s_count,
            relax_dmm_fault_count=self._total_relax_dmm_fault_count,
            temperature_out_of_range_count=self._total_temperature_out_of_range_count,
            temp_dmm_fault_ticks=self._total_temp_dmm_fault_ticks,
            max_observed_dt_s=self._max_observed_dt_s,
            warning_code_counts=dict(self._warning_code_counts),
            total_session_duration_s=(
                (datetime.now(timezone.utc) - self._session_start_time).total_seconds()
                if self._session_start_time else 0.0
            ),
        )

    def _graceful_stop(self, reason: str) -> TestResult:
        return self._stop_terminal(graceful_reason=reason or "USER_STOP_REQUESTED")

    def _run_manual_checkpoint(self, step_index: int, step: TestStep) -> TestResult:
        """BQ kézi ellenőrzési pont — CHECKPOINT_STOPPED státusz, on_event hívással.

        Governance-v2 review-kör 1 (CF-02 (b) alhely, MAX independent
        verifier V-1 lelete a batch 4.1 fixhez): a manuális BQ checkpoint
        MINDIG biztonságos határ (nincs aktív PSU/Load-vezérlés itt), DE a
        session akár órákig/napokig PARKOLVA marad ezen a ponton operátori
        BQ-munka alatt — a `_safe_all_off_with_uncertainty_check()` itt is
        garantáltan lefut, ugyanúgy, mint a másik 3 terminális ágon, hogy a
        `report.json`/checkpoint `safe_off_attempted`/`safe_off_result`
        mezője itt se maradjon hamisan False/üres. Az itteni `self._active_
        controller = None` (MAX verifier V-5) BÁR a valós `run()`-útvonalon
        a hívás pillanatában MÁR None (a megelőző lépés DONE-jánál a
        for-ciklus saját nullázása — MAX verifier NV-3, l. `run()` — ezt
        MÁR elvégezte, hiszen a MANUAL_CHECKPOINT lépés `_run_step()`
        elején, a `self._active_controller = controller` sor ELŐTT tér
        vissza ide), védelmi/direkt-hívási háló marad: ha ezt a metódust
        közvetlenül (nem `run()`-on keresztül, pl. tesztből) hívjuk egy
        korábban beállított `self._active_controller`-rel, vagy ha egy
        jövőbeli refaktor megváltoztatná a for-ciklus nullázásának
        elhelyezését, enélkül egy KÉSŐBBI, resume-kor (`run(...,
        start_step_index=next)`) ELŐTT érkező runner-vezérelt megszakítás
        újra meghívná a checkpoint ELŐTTI, MÁR lezárt controllert (l.
        `test_manual_checkpoint_nulls_active_controller_so_resume_time_
        stop_does_not_reabort` regressziós teszt, amely ezt közvetlen
        hívással, kézzel beállított állapoton bizonyítja)."""
        # Review-kör 5, Session/Workflow S5-07 (P3): a másik 3 terminális/
        # félig-terminális ág (DONE/_emergency_stop()="FAULT"/
        # _graceful_stop()="STOPPED") mindegyike frissíti a self.status
        # mezőt -- ez az ág korábban NEM, "RUNNING"-on ragadt volna. Ma
        # nincs élő GUI-fogyasztója (a MainWindow a worker signal-jaiból,
        # nem ebből olvas), de az attribútum saját, tesztelt szerződése
        # (l. test_test_runner.py IDLE/RUNNING/DONE/FAULT/STOPPED
        # ellenőrzései) hiányos volt a CHECKPOINT_STOPPED esetre.
        self.status = "CHECKPOINT_STOPPED"
        self._active_controller = None
        safe_off_result = self._safe_all_off_with_uncertainty_check()
        # Kör 21 F6 (METRO-R21-1) + MAX verifier B3 lelete (2026-09-19) --
        # l. _emergency_stop() azonos, teljes indoklása (except
        # BaseException, nem csak Exception). A checkpoint NEM terminális
        # a session életciklusára nézve, de a safe-off (fent) itt is fut
        # -- a SAJÁT hibái (pl. Load OFF/PSU ALL OFF válasz-hibái)
        # ugyanúgy megérik a kényszerített poll-t. R1 F-42: l.
        # _emergency_stop() -- a lenti esemény/checkpoint a lefokozott
        # safe_off_result-ot kapja.
        try:
            self._poll_device_errors(safe_off_result)
        except BaseException:
            pass
        status, reason = self._resolve_terminal(
            default_status="CHECKPOINT_STOPPED", default_reason="MANUAL_BQ_CHECKPOINT_REACHED",
        )
        if status != "CHECKPOINT_STOPPED":
            self._pending_relax_safe_off_result = safe_off_result
            return self._stop_terminal()
        assert self._active_plan is not None
        next_step_index = step_index + 1
        checkpoint_is_terminal = (next_step_index >= len(self._active_plan.steps))
        # Review-kör 5, Session/Workflow S5-03 (P2, S4-02 feloldása,
        # NOT_REACHABLE -- dokumentációs jelölés, NEM funkcionális fix):
        # a jelenlegi TestPlan-készletben (l. TestPlan.bq_learning_
        # physical()) a StepKind.MANUAL_CHECKPOINT MINDIG az utolsó lépés
        # -- tehát checkpoint_is_terminal MINDIG True, resume_possible
        # MINDIG False. A worker.py::request_continue_from_checkpoint()
        # "Folytatás" ágának teljes logikája (és a checkpoint_panel.py
        # "Folytatás" gomb resume_possible-alapú engedélyezése) emiatt MA
        # SZERKEZETILEG elérhetetlen -- ez NEM hiba, csak egy előre
        # felkészített, ma kihasználatlan képesség egy JÖVŐBELI tervhez,
        # ahol egy MANUAL_CHECKPOINT lépés NEM az utolsó lenne. A 4
        # korábban lezárt P1 fix (worker.py resume-ági safe-off/vészleállítás
        # kezelés) és az S4-02 finding ezt a ma elérhetetlen utat védi --
        # KEEP_AS_IS, nem törlendő és nem "javítandó", amíg nincs olyan
        # terv, ami ezt ténylegesen kihasználná.
        resume_possible = not checkpoint_is_terminal
        event = {
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
            "event_message": "BQ learning fizikai ciklus kézi ellenőrzési pontja elérve.",
            "step_name": step.label,
            "status": status,
            "resume_possible": resume_possible,
            "checkpoint_is_terminal": checkpoint_is_terminal,
            "next_step_index": next_step_index,
            "total_charge_ah": self._total_charge_ah,
            "total_discharge_ah": self._total_discharge_ah,
            # Kör 18 (2026-09-16), hardver/instrument-control reviewer P2-2
            # (DFAM-018+DFAM-001): a fenti safe_off_result korábban KIZÁRÓLAG
            # a checkpoint.json-ba jutott el (self._last_safe_off_result-on
            # keresztül) -- a checkpoint_panel.py egyetlen, ÉLŐ jelzése
            # ("A mérés biztonságos állapotban megállt — PSU OFF, Load OFF")
            # feltétel nélküli, statikus szöveg volt, sosem nézte ezt az
            # eredményt. Most az esemény maga is hordozza, hogy a panel
            # kondicionális figyelmeztetést rendereljen.
            "safe_off_result": safe_off_result,
        }
        # Governance-v2 review-kör 1 (MAX verifier NV-4, batch 4.1
        # follow-up): a másik 3 terminális ág (DONE/_emergency_stop()/
        # _graceful_stop()) mindegyike védi a log_event()/write_checkpoint()
        # hívásait egy I/O hibától — ez az ág korábban NEM, holott a fent
        # (1064) MÁR kiszámolt safe_off_result egyébként is elveszett volna
        # egy itt kiváltott, a run() külső try/except-jéig felfutó
        # kivétellel (ami egy MÁSODIK, redundáns safe-off kísérletet
        # indított volna _emergency_stop()-on keresztül). A checkpoint NEM
        # terminális a session életciklusára nézve (folytatható), ezért
        # itt — ellentétben a másik 3 ággal — nincs logger.close().
        try:
            self._logger.log_event("MANUAL_BQ_CHECKPOINT_REACHED", step.label)
            self._logger.write_checkpoint({
                "status": status,
                "reason": reason,
                "step": step.label,
                # Közös mezőkészlet minden checkpoint-ágon (DFAM-001/021).
                **self._checkpoint_summary_fields(),
                # Teljes kódbázis review 20. kör (2026-09-06), Session/Workflow
                # P1 lelet: a checkpoint_is_terminal (fent kiszámolva) korábban
                # csak az in-memory event-dictbe került, a lemezre írt
                # checkpoint.json-ba NEM — session_recovery.evaluate_resume()
                # emiatt egy sikeresen, a terv VÉGÉig eljutott checkpointot is
                # folytathatónak minősíthetett (l. session_recovery.py azonos
                # hivatkozású kommentje).
                "checkpoint_is_terminal": checkpoint_is_terminal,
                # WP-AK-02: a manuális BQ checkpoint MINDIG biztonságos határ —
                # ez a fogalom eredeti definíciója (nincs aktív PSU/Load-vezérlés
                # itt), és cross-process resume esetén is a next_step_index-től
                # kell folytatni, ugyanúgy, mint a same-process folytatásnál.
                **self._checkpoint_traceability_fields(
                    safe_boundary=True, next_safe_step_index=next_step_index
                ),
            })
            self._logger.flush_all()
        except Exception as exc:
            if self.on_event is not None:
                self.on_event({
                    "event_code": "MANUAL_CHECKPOINT_LOGGING_FAILED",
                    "event_message": str(exc),
                })
        if self.on_event is not None:
            self.on_event(event)
        return TestResult(
            status=status,
            reason=reason,
            total_charge_ah=self._total_charge_ah,
            total_discharge_ah=self._total_discharge_ah,
            total_charge_wh=self._total_charge_wh,
            total_discharge_wh=self._total_discharge_wh,
            resume_possible=resume_possible,
            next_step_index=next_step_index,
            active_step_label=step.label,
            safe_off_attempted=True,
            safe_off_result=safe_off_result,
            communication_faults_count=self._device_error_total_count,
            extreme_dt_s_count=self._total_extreme_dt_s_count,
            relax_dmm_fault_count=self._total_relax_dmm_fault_count,
            temperature_out_of_range_count=self._total_temperature_out_of_range_count,
            temp_dmm_fault_ticks=self._total_temp_dmm_fault_ticks,
            max_observed_dt_s=self._max_observed_dt_s,
            warning_code_counts=dict(self._warning_code_counts),
            total_session_duration_s=(
                (datetime.now(timezone.utc) - self._session_start_time).total_seconds()
                if self._session_start_time else 0.0
            ),
        )

    def _is_finished(self, controller) -> bool:
        state = getattr(controller, "state", None)
        if isinstance(state, ChargeState):
            return state in (ChargeState.CHARGE_DONE, ChargeState.FAULT)
        if isinstance(state, DischargeState):
            return state in (DischargeState.DISCHARGE_DONE, DischargeState.FAULT)
        if isinstance(state, RelaxState):
            return state == RelaxState.RELAX_DONE
        if isinstance(state, OcvSocState):
            return state in (OcvSocState.DONE, OcvSocState.FAULT)
        if isinstance(state, ChemIdState):
            return state in (ChemIdState.RECHARGE_REQUIRED, ChemIdState.FAULT)
        if isinstance(state, LearningState):
            return state in (LearningState.DONE, LearningState.FAULT)
        if isinstance(state, GoldenGgState):
            return state in (GoldenGgState.DONE, GoldenGgState.FAULT)
        if isinstance(state, IecDcResistanceState):
            return state in (IecDcResistanceState.DONE, IecDcResistanceState.FAULT)
        # S4-07 (review-kör 4, P3, DFAM-004 silent_catchall_dispatch): a
        # korábbi `return True` egy ismeretlen state-típust csendben "a lépés
        # kész"-ként kezelt -- egy jövőbeli StepKind bevezetésekor (ahogy a
        # 2. körben az IEC-nél történt) elég egy kimaradt `isinstance` ág
        # ahhoz, hogy `_is_finished()` az ELSŐ tick ELŐTT True-t adjon,
        # `_run_step()` advance() hívás nélkül "DONE"-t jelentsen, és a run()
        # a DONE-ágra fusson nulla mintával/méréssel -- ugyanaz a hibaosztály,
        # amit a `_controller_for_step()` már bevált `raise ValueError`
        # mintája (lent) explicit kizár. A run() külső except-je ezt
        # UNHANDLED_EXCEPTION FAULT + safe-off útra viszi, ami helyes.
        raise ValueError(f"Unknown controller state type: {type(state)!r}")

    def _controller_faulted(self, controller) -> bool:
        state = getattr(controller, "state", None)
        if isinstance(state, ChargeState):
            return state == ChargeState.FAULT
        if isinstance(state, DischargeState):
            return state == DischargeState.FAULT
        # S4-07 (review-kör 4, P3, DFAM-004) MELLÉKLELET: ez az ág korábban
        # HIÁNYZOTT -- a RelaxState-nek nincs FAULT tagja (INIT/RELAXING/
        # RELAX_DONE, l. relax_controller.py), tehát önálló StepKind.RELAX
        # lépés eddig is a (most explicit raise-re váltott) fail-open
        # `return False`-on ment át, csendben. Explicit fel véve, mert a
        # RelaxState-nek TÉNYLEG sosincs hiba-állapota (l. _is_finished()
        # azonos típusú ága fent) -- ez NEM ismeretlen state, hanem egy
        # eddig dokumentálatlan, de helyes eset.
        if isinstance(state, RelaxState):
            return False
        if isinstance(state, OcvSocState):
            return state == OcvSocState.FAULT
        if isinstance(state, ChemIdState):
            return state == ChemIdState.FAULT
        if isinstance(state, LearningState):
            return state == LearningState.FAULT
        if isinstance(state, GoldenGgState):
            return state == GoldenGgState.FAULT
        if isinstance(state, IecDcResistanceState):
            return state == IecDcResistanceState.FAULT
        # S4-07 (review-kör 4, P3, DFAM-004) -- l. _is_finished() azonos
        # hivatkozású kommentje ugyanerről a hibaosztályról. A korábbi
        # `return False` egy ismeretlen state-típust csendben "nincs hiba"-
        # ként kezelt volna.
        raise ValueError(f"Unknown controller state type: {type(state)!r}")

    def _base_nplc_for_step(self, step: TestStep) -> float:
        """A lépés-szintű ALAP-NPLC — az EGYETLEN hely, ahol ez a szabály él.
        Két hívója van: _configure_dmm_for_step() (a lépés elején) és
        _reconfigure_dmm_nplc_for_relax_substate() retry-ága (l. ott,
        Kör 20 Batch E2 / F20-S1). A duplikálás elkerülése NEM kozmetikai:
        a két út eltérő alap-NPLC-je csendben, fault nélkül rossz
        integrációs idővel futtatná végig a lépést (CLAUDE.md §8)."""
        if step.kind in _SLOW_BASE_NPLC_STEP_KINDS:
            return self._config.relax_ocv_nplc
        return self._config.charge_discharge_nplc

    def _configure_dmm_for_step(self, step: TestStep) -> None:
        """NPLC beállítás worker threadben — megelőzi a GUI thread race conditiont."""
        self._assert_relax_sampling_released()
        nplc = self._base_nplc_for_step(step)
        previous_nplc = self._last_configured_nplc
        try:
            self._instruments.dmm_voltage.set_nplc(nplc)
            self._last_configured_nplc = nplc
            self._nplc_set_failing = False
        except Exception as exc:
            # Governance-v2 review-kör 1 (CF-37/HW-R1-07): a korábbi
            # "DMM fault a mérésnél derül ki" feltevés hamis — rossz
            # NPLC-vel a READ? érvényes értéket ad, csak kisebb integrációs
            # idővel/zajelnyomással, fault/warning/quality flag nélkül. A
            # lépés NEM áll meg (viselkedés-döntés megmarad), de az esemény
            # látható lesz.
            # KORREKCIÓ (Kör 20 Batch E2, F20-S1, DFAM-043): az eredeti
            # komment zárómondata ("a következő próbálkozás nem marad ki a
            # (target_nplc == _last_configured_nplc) korai-visszatérés
            # miatt") HAMIS volt StepKind.CHARGE/DISCHARGE/RELAX-re: azok
            # egyik substate-térképben SINCSENEK, ezért a
            # _reconfigure_dmm_nplc_for_relax_substate() a `fast_substates
            # is None` ágon MÉG a hivatkozott összehasonlítás ELŐTT
            # visszatért -- a retry ott STRUKTURÁLISAN nem létezett. A
            # `_last_configured_nplc is None` melletti alap-NPLC-retry (l.
            # ott) ezt pótolja; az állítás MOSTANTÓL mind a 8 NPLC-t
            # konfiguráló StepKindre igaz.
            self._last_configured_nplc = None
            self._report_nplc_set_failed(nplc, exc)
        else:
            # R1 B8b, F-26: a try-on KÍVÜL (else-ág), hogy egy naplózási
            # hiba ne jusson a fenti instrumentum-hibakezelésbe.
            if nplc != previous_nplc:
                self._log_nplc_set(nplc)

    def _log_nplc_set(self, nplc: float) -> None:
        """R1 B8b, F-26 (P3, DFAM-021 + DFAM-017): sikeres NPLC-VÁLTÁS
        (a `_last_configured_nplc`-től eltérő érték, None is eltérés)
        naplózása az events.csv-be -- korábban csak a cache frissült, a
        sessionből nem volt rekonstruálható a lépésenkénti NPLC. A
        set_nplc() `VOLT:DC:NPLC x;*OPC?` (device_dmm.py): parancsnyugta,
        NEM műszer-visszaolvasás -- az üzenet ezt explicit jelöli. Nincs
        on_event (nem operátori figyelmeztetés). Naplózási hiba (pl. lemez-
        I/O) SZÁNDÉKOSAN elnyelve: nem FAULT, nem NPLC_SET_FAILED, és a
        `_last_configured_nplc` cache-t (a műszer tényleges parancsolt
        állapota) sem írja felül."""
        try:
            self._logger.log_event(
                "NPLC_SET",
                f"VOLT:DC:NPLC {nplc:.6g} parancs nyugtázva (*OPC?); "
                "műszer-visszaolvasás nincs",
            )
        except Exception:  # noqa: BLE001, S110 - l. docstring: audit-napló, nem instrumentum-hiba
            pass

    def _report_nplc_set_failed(self, nplc: float, exc: BaseException) -> None:
        """Kör 20 Batch E2 (F20-S2): a NPLC_SET_FAILED volt a runner
        EGYETLEN olyan hibája, amely SEM eseményként, SEM FAULT-ként nem
        jutott el az élő GUI-ra, ÉS mindkét kibocsátója védtelen
        log_event()-et hívott -- a per-tick
        hívóhely (_reconfigure_dmm_nplc_for_relax_substate()) egy lemez-
        I/O hibát UNHANDLED_EXCEPTION FAULT + emergency stop útra vitt
        volna (l. run() legkülső except ága). A minta a
        _safe_all_off_with_uncertainty_check()-é (l. ott)."""
        message = f"set_nplc({nplc}): {exc}"
        logging_error: Exception | None = None
        try:
            self._logger.log_event("NPLC_SET_FAILED", message)
        except Exception as log_exc:  # noqa: BLE001 - l. _configure_dmm_for_step() CF-37 indoklása
            logging_error = log_exc
        if self._nplc_set_failing:
            return
        self._nplc_set_failing = True
        if self.on_event is not None:
            if logging_error is not None:
                self.on_event({
                    "event_code": "NPLC_SET_FAILED_LOGGING_FAILED",
                    "event_message": f"{message}: {logging_error}",
                })
            self.on_event({
                "event_code": "NPLC_SET_FAILED",
                "event_message": message,
            })

    def _reconfigure_dmm_nplc_for_relax_substate(self, step: TestStep, controller) -> None:
        """Teljes kódbázis review 6. kör (2026-08-29), Session/Workflow P2
        lelet #3: a TRADITIONAL_LEARNING/GOLDEN_GG StepKind egyetlen, opak
        lépésen belül CC/CV töltési ÉS relax-fázisokat is átfog —
        _configure_dmm_for_step() a lépés ELEJÉN egyszer állítja be a NPLC-t
        (charge_discharge_nplc, gyors ág), a belső relax-fázisok
        (_RELAX_SUBSTATE_NAMES) emiatt sosem kapták meg a szűk dV/dt-küszöb
        megbízható kiértékeléséhez szükséges alacsony DMM-zajú mérést.

        Ez a metódus minden ticken meghívva, a controller ÉLŐ állapotától
        függően dinamikusan vált relax_ocv_nplc és charge_discharge_nplc
        között — CSAK a ténylegesen relax-substate-ekben emeli a NPLC-t,
        a CC/CV/taper fázisokban a charge_discharge_nplc marad.
        R10-B3: az alapérték 1 PLC; a fast_loop_period_s nem változik.
        A tényleges zaj és ciklusidő HIL_PENDING. Csak ténylegesen VÁLTOZÓ
        NPLC esetén ír SCPI-t
        (self._last_configured_nplc), elkerülve a felesleges, minden
        ticken ismételt set_nplc()-hívást.

        Teljes kódbázis review 7. kör (2026-08-30), Session/Workflow P2
        lelet #2: az OCV_SOC/CHEMID StepKind (l. _FAST_SUBSTATE_NAMES) az
        ELLENKEZŐ irányban dinamikus — a lassú (relax_ocv_nplc) az ALAP
        (_configure_dmm_for_step()), és a PRECHARGE/FULL_CHARGE_
        MANUFACTURER_METHOD alállapot (beágyazott, fast_loop_enabled=True
        ChargeController) DEMOTE-ol a gyorsra, hogy a 2026-08-23
        OV-incidens mitigációja ott is ténylegesen érvényesüljön.

        Kör 20 Batch E2 (F20-S1, P2): a fenti két térképben NEM szereplő
        StepKindekre (CHARGE/DISCHARGE/RELAX) ez a metódus továbbra sem
        vált NPLC-t -- EGYETLEN kivétellel: ha a lépés-eleji
        _configure_dmm_for_step() set_nplc()-je HIBÁRA futott
        (_last_configured_nplc is None), a következő ticken újrapróbálja az
        ALAP-NPLC-t (_base_nplc_for_step(), ugyanaz a szabály, ugyanaz a
        forrás). Sikeres lépés-eleji konfiguráció esetén a viselkedés
        bitre változatlan: nulla plusz SCPI-írás."""
        state = getattr(controller, "state", None)
        state_name = getattr(state, "name", None)

        # A service DMM-tulajdonlása alatt a controlleré az NPLC-tranzakció.
        # A PREP saját OCV-olvasása előtt a runner még újrapróbálhatja az NPLC-t.
        if step.kind == StepKind.OCV_SOC and state in _PULSE_SERVICE_DMM_OWNER_STATES:
            return

        relax_substates = _RELAX_SUBSTATE_NAMES.get(step.kind)
        if relax_substates is not None:
            target_nplc = (
                self._config.relax_ocv_nplc if state_name in relax_substates
                else self._config.charge_discharge_nplc
            )
        else:
            fast_substates = _FAST_SUBSTATE_NAMES.get(step.kind)
            if fast_substates is not None:
                target_nplc = (
                    self._config.charge_discharge_nplc if state_name in fast_substates
                    else self._config.relax_ocv_nplc
                )
            elif self._last_configured_nplc is None:
                # Kör 20 Batch E2 (F20-S1, P2, DFAM-001): a StepKind.CHARGE/
                # DISCHARGE/RELAX EGYIK substate-térképben SINCS benne (nincs
                # dinamikus NPLC-váltásuk -- ez helyes és VÁLTOZATLAN), ezért
                # ez az ág korábban FELTÉTEL NÉLKÜL visszatért -- egy bukott
                # lépés-eleji set_nplc() után a DMM az ELŐZŐ lépés integrációs
                # idején maradt a TELJES lépésre (akár órákra), retry nélkül.
                # A `_last_configured_nplc is None` ŐR KÖTELEZŐ: sikeres
                # konfiguráció után ez az ág NEM fut le, tehát a CC/CV
                # fast_loop DMM-ciklusideje érintetlen marad (HIL-kockázat,
                # l. _RELAX_SUBSTATE_NAMES docstringje).
                target_nplc = self._base_nplc_for_step(step)
            else:
                return

        if target_nplc == self._last_configured_nplc:
            return
        if self._relax_sampler_started:
            self._assert_relax_sampling_released()
        try:
            self._instruments.dmm_voltage.set_nplc(target_nplc)
            self._last_configured_nplc = target_nplc
            self._nplc_set_failing = False
        except Exception as exc:  # noqa: BLE001 - l. _configure_dmm_for_step() CF-37 indoklása
            self._last_configured_nplc = None
            self._report_nplc_set_failed(target_nplc, exc)
        else:
            # R1 B8b, F-26: a fenti `target_nplc == _last_configured_nplc`
            # korai return miatt itt mindig tényleges változás történt.
            self._log_nplc_set(target_nplc)

    def _controller_for_step(self, step: TestStep):
        if step.kind == StepKind.CHARGE:
            return self._charge_ctrl_factory()
        if step.kind == StepKind.DISCHARGE:
            return self._discharge_ctrl_factory()
        if step.kind == StepKind.RELAX:
            return self._relax_ctrl_factory(step)
        if step.kind == StepKind.CHEMID:
            if self._chemid_ctrl_factory is None:
                raise ValueError("CHEMID step requires chemid_ctrl_factory")
            return self._chemid_ctrl_factory()
        if step.kind == StepKind.OCV_SOC:
            if self._ocv_soc_ctrl_factory is None:
                raise ValueError("OCV_SOC step requires ocv_soc_ctrl_factory")
            return self._ocv_soc_ctrl_factory()
        if step.kind == StepKind.TRADITIONAL_LEARNING:
            if self._traditional_learning_ctrl_factory is None:
                raise ValueError(
                    "TRADITIONAL_LEARNING step requires traditional_learning_ctrl_factory"
                )
            return self._traditional_learning_ctrl_factory()
        if step.kind == StepKind.GOLDEN_GG:
            if self._golden_gg_ctrl_factory is None:
                raise ValueError("GOLDEN_GG step requires golden_gg_ctrl_factory")
            return self._golden_gg_ctrl_factory()
        if step.kind == StepKind.IEC_DC_RESISTANCE:
            if self._iec_dc_resistance_ctrl_factory is None:
                raise ValueError(
                    "IEC_DC_RESISTANCE step requires iec_dc_resistance_ctrl_factory"
                )
            return self._iec_dc_resistance_ctrl_factory()
        raise ValueError(f"No controller for step kind: {step.kind}")

    def _build_sample(
        self, step: TestStep, controller,
        charge_ah_before: float = 0.0, discharge_ah_before: float = 0.0,
        charge_wh_before: float = 0.0, discharge_wh_before: float = 0.0,
    ) -> dict:
        now = datetime.now(timezone.utc)
        # 2026-08-23 traditional_learning OV incidens elemzés: korábban itt
        # egy MÁSODIK, a controller saját (safety-check-hez ténylegesen
        # használt) DMM-olvasásától független read_all() futott — gyors
        # dV/dt mellett (l. incidens: ~0,65 V/s) ez a naplózott
        # battery_voltage_V-t érdemben eltéríthette attól, amit a controller
        # a saját tick-jében ténylegesen kiértékelt, ami megtévesztő volt a
        # hibaelemzésnél (CLAUDE.md §9: egy DMM-et egy tickben egy komponens
        # olvasson). Ha a controller expose-olja a saját u_batt_V-jét
        # (ChargeController és az őt beágyazó TraditionalLearningController/
        # GoldenGgController), azt használjuk — a DMM-feszültség csatornát
        # ekkor a read_all() ki is hagyja (skip_battery_voltage), nem csak a
        # naplózott érték forrása változik, hanem a fizikai dupla-olvasás is
        # megszűnik. Ugyanez az elv a hőmérséklet-DMM-re (battery_temperature_C/
        # skip_battery_temperature) — l. TraditionalLearningController.
        # battery_temperature_C docstringje.
        # Egyetlen snapshot a teljes mintasorhoz; read_all közben is telhet az idő.
        sampling_controller = (
            controller if isinstance(controller, RelaxController) else self._relax_sampler_controller
        )
        relax_snapshot = (
            sampling_controller.voltage_snapshot()
            if sampling_controller is not None and sampling_controller.sampling_mode == "SERVICE"
            else None
        )
        controller_u_batt = (
            relax_snapshot.voltage_V if relax_snapshot is not None
            else getattr(controller, "u_batt_V", None)
        )
        controller_temp = getattr(controller, "battery_temperature_C", None)
        # Review-kör 6 (2026-09-15), C-08b (a C-08a folytatása, ugyanaz az
        # elv, mint a battery_voltage/temperature csatornákon fent): a
        # beágyazott ChargeController _check_series_safety()/_read_psu_
        # current() hívása MÁR elvégezte a fizikai PSU-feszültség/áram
        # olvasást EBBEN a tickben — a validity-flagek (nem a nyers
        # pontérték!) jelzik, hogy a controller ténylegesen próbálkozott-e
        # EBBEN a tickben (i_psu_measured_A sosem None, sikertelen olvasás
        # esetén is 0.0-t ad vissza -- l. charge_controller.py -- ezért a
        # validity flag a megbízható "futott-e ebben a tickben" jelző, nem
        # a pontérték). Ha igen, a read_all()-t skip_psu_voltage/
        # skip_psu_current=True-val hívjuk -- a fizikai dupla-olvasás itt
        # is megszűnik, ugyanúgy, mint a battery-csatornáknál.
        ctrl_u_psu = getattr(controller, "u_psu_measured_V", None)
        ctrl_u_psu_valid = getattr(controller, "psu_voltage_readback_valid", None)
        ctrl_i_psu = getattr(controller, "i_psu_measured_A", None)
        ctrl_i_psu_valid = getattr(controller, "psu_current_readback_valid", None)
        # Teljes kódbázis review 2. kör (2026-09-15, R2-07, 12. writer-kör):
        # a PSU KÉT csatornája (feszültség/áram) EGY fizikai eszköz — ha a
        # controller ezen a ticken BÁRMELYIK csatornát olvasta, a log-célú
        # read_all() NE indítson egy MÁSODIK, fizikai PSU-lekérdezést a
        # MÁSIK csatornán sem, mert a controller egy tick-közbeni FAULT-on
        # (pl. PSU_COMM_LOST az áram-olvasás után) a safe-off UTÁN, egy már
        # bizonyítottan halott/lekapcsolt PSU-n futtatta volna ezt a
        # lekérdezést -- akár ~30s blokkoló I/O-val (rules/10-safety.md
        # #3 "további aktív parancs tiltása"), és a mintasor a safe-off
        # ELŐTTI (áram) és UTÁNI (feszültség) intra-tick epochot vegyítette
        # volna. A korábbi kód a két skip-flaget külön-külön a saját
        # csatornája validity-jéhez kötötte -- ez hagyta ezt a rést nyitva.
        psu_read_by_controller = (ctrl_u_psu_valid is not None) or (ctrl_i_psu_valid is not None)
        ctrl_i_load = getattr(controller, "i_load_measured_A", None)
        ctrl_i_load_valid = getattr(controller, "load_current_readback_valid", None)
        # Kör 20 / SW20-1 (2026-09-18): a Load-FESZÜLTSÉG csatorna külön,
        # SZŰKEBB kapu -- NEM a skip_load_current tükre. Az `is not None`
        # (dupla-olvasás elleni) minta itt HELYTELEN lenne: egyetlen
        # controller sem olvas Load-feszültséget, tehát a `True` (sikeres
        # áram-olvasás) ágon egy ma is naplózott, legitim mérést törölne.
        # Az `is False` ág viszont azt jelenti, hogy a controller EBBEN a
        # tickben megállapította a Load halálát és MÁR lefuttatta az
        # emergency_stop("LOAD_COMM_LOST")-ot + a safe-off-ot (a 6
        # controller mind a 7 hívóhelyén) -- egy MEAS:VOLT? ekkor
        # bizonyítottan halott eszközre menne, akár ~15 s (3 x 5000 ms,
        # device_load.py) blokkoló I/O-val a terminális visszatérés
        # ELŐTT: .claude/rules/10-safety.md #3 (DFAM-027).
        readings = self._instruments.read_all(
            skip_battery_voltage=relax_snapshot is not None or controller_u_batt is not None,
            skip_battery_temperature=controller_temp is not None,
            skip_psu_voltage=psu_read_by_controller,
            skip_psu_current=psu_read_by_controller,
            skip_load_current=ctrl_i_load_valid is not None,
            skip_load_voltage=ctrl_i_load_valid is False,
        )

        sample: dict = {col: None for col in CSV_COLUMNS}

        elapsed = (now - self._start_time).total_seconds() if self._start_time else 0.0
        sample["timestamp_iso"] = now.isoformat()
        sample["elapsed_s"] = round(elapsed, 3)
        # R2-14 (2026-09-15): l. logger.py CSV_COLUMNS "session_elapsed_s"
        # kommentje. A self._session_start_time (nem self._start_time) a
        # megszakítás(ok)on átívelő session-origó, resume-nál a
        # checkpointból seedelve -- ugyanaz az egyetlen forrás, amit a
        # TestResult.total_session_duration_s is használ, tehát nincs
        # duplikált idő-origó/parse-logika. A guard (is not None) az
        # elapsed_s fenti mintáját követi: _build_sample() run() NÉLKÜL is
        # hívható.
        sample["session_elapsed_s"] = (
            round((now - self._session_start_time).total_seconds(), 3)
            if self._session_start_time is not None else None
        )
        sample["test_name"] = self._config.test_name
        sample["step_name"] = step.label
        state_obj = getattr(controller, "state", None)
        sample["state"] = state_obj.value if state_obj is not None else None
        # CF-19 (governance-v2 kör 2, DFAM-023): a 4 összetett controller
        # (OCV_SOC/CHEMID/TRADITIONAL_LEARNING/GOLDEN_GG) mostantól
        # exponálja, mennyi ideje van a JELENLEGI belső állapotban -- l.
        # ocv_soc_controller.py::phase_elapsed_s docstringje. A sima
        # StepKindek (CHARGE/DISCHARGE/RELAX/stb.) controllerein ez a
        # property nem létezik, a mező None marad (a GUI a meglévő
        # perf_counter-alapú "Lépés idő" számítást használja tovább rájuk).
        sample["phase_elapsed_s"] = getattr(controller, "phase_elapsed_s", None)

        u_batt = (
            controller_u_batt if relax_snapshot is not None or controller_u_batt is not None
            else readings.get("battery_voltage_V")
        )
        sample["battery_voltage_V"] = u_batt
        battery_temp = (
            controller_temp if controller_temp is not None else readings.get("battery_temperature_C")
        )
        sample["battery_temperature_C"] = battery_temp

        # Review-kör 6 (2026-09-15), C-08b (a C-08a folytatása): psu_
        # readback_voltage_V/psu_readback_current_A korábban MINDIG a
        # nyers read_all() readbacket adta, most -- pontosan ugyanúgy,
        # mint battery_voltage_V/battery_temperature_C fent -- a
        # controller SAJÁT, EBBEN a tickben végzett olvasását részesíti
        # előnyben, ha az elérhető (skip_psu_voltage/skip_psu_current
        # miatt read_all() ekkor már nem is végzett fizikai dupla-
        # olvasást). u_psu: 2-ágú (a controller Optional[float]-ot ad,
        # None-ja MÁR a hiba jelzése). i_psu: 3-ágú (i_psu_measured_A
        # SOSEM None -- hiba esetén is 0.0-t ad vissza, l.
        # charge_controller.py::_read_psu_current() -- ezért a validity
        # flagen kell elágazni, NEM a pontértéken, hogy hiba esetén a
        # naplózott érték None maradjon, NE a controller belső 0.0-ja,
        # CLAUDE.md §6 "aktív energiaátadás közben kritikus readback
        # hibát tilos 0-val csendben helyettesíteni").
        u_psu = ctrl_u_psu if ctrl_u_psu_valid is not None else readings.get("psu_readback_voltage_V")
        if ctrl_i_psu_valid is True:
            i_psu = ctrl_i_psu
        elif ctrl_i_psu_valid is False:
            i_psu = None
        else:
            i_psu = readings.get("psu_readback_current_A")
        sample["psu_readback_voltage_V"] = u_psu
        sample["psu_readback_current_A"] = i_psu
        sample["load_readback_voltage_V"] = readings.get("load_readback_voltage_V")
        sample["psu_mode"] = self._safety.psu_mode.value
        sample["isolation_state"] = "PSU_OUTPUT_OFF_ONLY"

        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # ugyanaz a controller-elsőbbségű, tick-konzisztens merge, mint a
        # fenti i_psu-nál -- a Load-áramot kisütő fázisban MÁR beolvasta a
        # controller (l. DischargeController/GoldenGgController/ChemId
        # Controller/OcvSocController/IecDcResistanceController/
        # TraditionalLearningController saját i_load_measured_A/load_
        # current_readback_valid property-je), a naplózott érték emiatt
        # UGYANAZ a fizikai minta, amit az integrálás/Rb/IEC Ua-Ia is
        # használt, nem egy MÁSODIK, önálló read_all()-olvasás.
        if ctrl_i_load_valid is True:
            i_load = ctrl_i_load
        elif ctrl_i_load_valid is False:
            i_load = None
        else:
            i_load = readings.get("load_readback_current_A")
        sample["load_readback_current_A"] = i_load

        # P1-C: driver állapot + integrátor minőségi mezők (a psu_output_
        # commanded_on-t itt, korábban olvassuk ki, mint korábban -- a
        # u_drop_V/diode_power_W (dióda-esés) csak PSU OUTPUT ON mellett
        # értelmezhető: OFF állapotban a PSU kimenete nincs rákapcsolva a
        # sorosdiódás töltőágra, a mért u_psu-u_batt különbség fizikailag
        # értelmetlen (a kezelő/log korábban "-"-tól eltérő, félrevezető
        # számot látott minden nem-töltő fázisban, pl. relax/kisütés alatt).
        # C-08b óta u_psu/i_psu (fent) MÁR a controller-elsőbbségű,
        # tick-konzisztens merge -- l. fenti komment -- tehát ez a
        # számítás is automatikusan a helyes párral dolgozik.
        psu_output_commanded_on = getattr(
            self._instruments.psu, "output_commanded_on", None
        )
        sample["psu_output_commanded_on"] = psu_output_commanded_on

        if psu_output_commanded_on and u_batt is not None and u_psu is not None:
            sample["u_drop_V"] = round(u_psu - u_batt, 4)
            if i_psu is not None:
                sample["diode_power_W"] = round(sample["u_drop_V"] * i_psu, 4)

        controller_dmm_valid = (
            relax_snapshot.quality == "CACHE_FRESH" if relax_snapshot is not None
            else getattr(controller, "dmm_valid", None)
        )
        sample["dmm_voltage_valid"] = (
            controller_dmm_valid if controller_dmm_valid is not None else u_batt is not None
        )
        controller_temp_dmm_valid = getattr(controller, "temp_dmm_valid", None)
        sample["dmm_temperature_valid"] = (
            controller_temp_dmm_valid
            if controller_temp_dmm_valid is not None
            else battery_temp is not None
        )
        # Release-audit 2026-08-23 (2.4): korábban a kritikus readback-hiba
        # (i_psu/i_load == None, SCPI/VISA hiba) csendben 0.0-ra esett vissza
        # aktív energiaátadás alatt (CLAUDE.md 10.4 "TILOS" megsértése). Mostantól
        # a mező None marad hiba esetén, a psu_readback_valid/load_readback_valid
        # (meglévő, korábban sosem töltött CSV placeholder-oszlopok) jelzik a
        # readback érvényességét — az élő GUI (`_f()` NaN-fallback) és a
        # validation_matrix (`_safe_float`) már None-toleránsan olvassa ezeket.
        sample["psu_readback_valid"] = i_psu is not None
        sample["load_readback_valid"] = i_load is not None

        if step.kind == StepKind.CHARGE:
            sample["charge_current_A"] = i_psu
            sample["discharge_current_A"] = 0.0
            sample["signed_current_A"] = i_psu
            # Review-kör 5 (2026-09-10), GUI/UX F2 (P2, DFAM-021 rokona):
            # bq_learning_physical() tervben KÉT KÜLÖN StepKind.CHARGE lépés
            # van (charge_1/charge_2, l. TestPlan.bq_learning_physical()) --
            # a controller SAJÁT accumulated_charge_Ah-ja minden ÚJ CHARGE
            # lépésnél 0-ról indul (friss ChargeController-példány), tehát
            # ez itt ÖNMAGÁBAN csak a JELENLEGI lépés töltését mutatta,
            # NEM a session teljes töltését -- charge_2 alatt a Live panel
            # (és a samples.csv) hamisan "visszaesett" volna a charge_1
            # végén mutatott értékről a charge_2 saját, friss (0-hoz közeli)
            # összegére. A session-szintű bázis (self._total_charge_ah,
            # ami E LÉPÉS ELEJÉN rögzült, l. _flush_totals() -- lépés végén
            # és minden periodikus checkpoint előtt frissül (R1 F-14), a
            # before-bázissal EGYÜTT, így az összeg teleszkopikus) + a
            # controller lépésen belüli SAJÁT deltája
            # (jelenlegi - charge_ah_before) adja a helyes, folytonos,
            # session-szintű futó összeget. Első CHARGE lépésnél (baseline=0,
            # before=0) ez pontosan a régi értékkel egyezik -- nincs
            # viselkedés-változás a CHARACTERIZATION/CHARGE_ONLY egy-CHARGE-
            # lépéses terveken.
            sample["accumulated_charge_Ah"] = self._total_charge_ah + (
                getattr(controller, "accumulated_charge_Ah", 0.0) - charge_ah_before
            )
            sample["accumulated_discharge_Ah"] = self._total_discharge_ah
            sample["accumulated_charge_Wh"] = self._total_charge_wh + (
                getattr(controller, "accumulated_charge_Wh", 0.0) - charge_wh_before
            )
            sample["accumulated_discharge_Wh"] = self._total_discharge_wh
            sample["integration_current_source"] = getattr(controller, "last_integration_source", None)
        elif step.kind == StepKind.DISCHARGE:
            sample["charge_current_A"] = 0.0
            sample["discharge_current_A"] = i_load
            sample["signed_current_A"] = None if i_load is None else -i_load
            sample["accumulated_charge_Ah"] = self._total_charge_ah
            # Review-kör 5 (2026-09-10), GUI/UX F2 -- l. a CHARGE ág fenti
            # kommentjét, pontos testvér ugyanarra a hibaosztályra
            # (bq_learning_physical() discharge_1/discharge_2).
            sample["accumulated_discharge_Ah"] = self._total_discharge_ah + (
                getattr(controller, "accumulated_discharge_Ah", 0.0) - discharge_ah_before
            )
            sample["accumulated_charge_Wh"] = self._total_charge_wh
            sample["accumulated_discharge_Wh"] = self._total_discharge_wh + (
                getattr(controller, "accumulated_discharge_Wh", 0.0) - discharge_wh_before
            )
            sample["integration_current_source"] = getattr(controller, "last_integration_source", None)
        elif step.kind == StepKind.GOLDEN_GG:
            # WP-AK-16: a GoldenGgController belül FULL_CHARGE-kor a PSU-t,
            # SYSTEM_RATE_DISCHARGE-kor a Load-ot vezérli aktívan (l.
            # golden_gg_controller.py _run_full_charge/_run_system_rate_discharge)
            # — az almező nélküli else-ág korábban itt is csendben 0-t adott
            # aktív energiaátadás alatt (CLAUDE.md 10.4 megsértése).
            golden_gg_state = getattr(controller, "state", None)
            if golden_gg_state == GoldenGgState.FULL_CHARGE:
                sample["charge_current_A"] = i_psu
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = i_psu
            elif golden_gg_state == GoldenGgState.SYSTEM_RATE_DISCHARGE:
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = i_load
                sample["signed_current_A"] = None if i_load is None else -i_load
            else:
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = 0.0
            # 2026-08-24 (élő panel audit, session_fg1_20260823_200337 nyomán):
            # korábban itt self._total_charge_ah/self._total_discharge_ah (a
            # runner szintű, csak SIKERES lépés-lezáráskor frissülő futó
            # összeg) került a mintába — ez a GOLDEN_GG lépés TELJES
            # időtartama alatt befagyott (jellemzően 0.0) értéket mutatott,
            # mert ez a StepKind sosem érte el azt a frissítést (l. lejjebb,
            # _run_step()). A controller SAJÁT, élő accumulated_charge_Ah/
            # accumulated_discharge_Ah property-je (l. golden_gg_controller.py)
            # a helyes, ugyanaz a minta, mint CHARGE/DISCHARGE StepKind-nál.
            sample["accumulated_charge_Ah"] = getattr(controller, "accumulated_charge_Ah", None)
            sample["accumulated_discharge_Ah"] = getattr(controller, "accumulated_discharge_Ah", None)
            # Teljes kódbázis review 11. kör (2026-08-31), Szoftver/
            # Architektúra P3 lelet: ugyanaz a hiányzó proxy-minta, mint a
            # fenti u_psu_set_V-nél — a beágyazott ChargeController FULL_
            # CHARGE alatt ténylegesen vezeti ezt, de sosem jutott el a
            # samples.csv integration_current_source oszlopáig.
            sample["integration_current_source"] = getattr(
                controller, "last_integration_source", None
            )
        elif step.kind == StepKind.TRADITIONAL_LEARNING:
            # Release-audit 2026-08-23 (2.4/B): a TraditionalLearningController
            # belül PRECHARGE/RECHARGE-kor a PSU-t, DISCHARGE_1/DISCHARGE_2-kor
            # a Load-ot vezérli aktívan (l. traditional_learning_controller.py
            # _run_precharge/_run_recharge/_run_discharge) — korábban ez a
            # StepKind mindig a generikus else-ágba esett, ami a GOLDEN_GG-nél
            # már kijavított mintával megegyező módon, csendben 0-t adott
            # aktív energiaátadás alatt.
            learning_state = getattr(controller, "state", None)
            if learning_state in (
                LearningState.PRECHARGE, LearningState.RECHARGE, LearningState.FINAL_RECHARGE,
            ):
                sample["charge_current_A"] = i_psu
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = i_psu
            elif learning_state in (LearningState.DISCHARGE_1, LearningState.DISCHARGE_2):
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = i_load
                sample["signed_current_A"] = None if i_load is None else -i_load
            else:
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = 0.0
            # 2026-08-24 — l. a GOLDEN_GG ág fenti kommentje ugyanerről a
            # hibáról: a session_fg1_20260823_200337 traditional_learning
            # futtatás alatt a "Kapacitás integrátor" grafikon és a
            # Töltve/Kisütve mezők a TELJES session alatt 0-n álltak, mert
            # ez az ág is a befagyott self._total_charge_ah/
            # self._total_discharge_ah futó összeget adta, nem a controller
            # élő accumulated_charge_Ah/accumulated_discharge_Ah property-jét.
            sample["accumulated_charge_Ah"] = getattr(controller, "accumulated_charge_Ah", None)
            sample["accumulated_discharge_Ah"] = getattr(controller, "accumulated_discharge_Ah", None)
            # Teljes kódbázis review 11. kör (2026-08-31), Szoftver/
            # Architektúra P3 lelet — l. a GOLDEN_GG ág fenti kommentje
            # ugyanerről a hiányzó proxy-mintáról.
            sample["integration_current_source"] = getattr(
                controller, "last_integration_source", None
            )
        elif step.kind == StepKind.OCV_SOC:
            # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: az
            # OcvSocController PRECHARGE alatt a PSU-t, STEP_DISCHARGE/
            # IMPULSE_*-alatt a Load-ot vezérli aktívan (l.
            # ocv_soc_controller.py) — az almező nélküli generikus else-ág
            # korábban itt is csendben 0-t adott aktív energiaátadás alatt,
            # ugyanaz a hibaminta, mint a GOLDEN_GG/TRADITIONAL_LEARNING-nél
            # már javítva. accumulated_charge_Ah/accumulated_discharge_Ah az
            # OcvSocController saját, session-szintű integrátorából (l. ott).
            ocv_soc_state = getattr(controller, "state", None)
            if ocv_soc_state == OcvSocState.PRECHARGE:
                sample["charge_current_A"] = i_psu
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = i_psu
            elif ocv_soc_state in (
                OcvSocState.STEP_DISCHARGE,
                OcvSocState.IMPULSE_ON,
                OcvSocState.IMPULSE_ACQUIRE,
                OcvSocState.IMPULSE_FAST_ON,
                OcvSocState.IMPULSE_FAST_ACQUIRE,
            ):
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = i_load
                sample["signed_current_A"] = None if i_load is None else -i_load
            else:
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = 0.0
            sample["accumulated_charge_Ah"] = getattr(controller, "accumulated_charge_Ah", None)
            sample["accumulated_discharge_Ah"] = getattr(controller, "accumulated_discharge_Ah", None)
            # Teljes kódbázis review 11. kör (2026-08-31), Szoftver/
            # Architektúra P3 lelet — l. a GOLDEN_GG ág fenti kommentje
            # ugyanerről a hiányzó proxy-mintáról.
            sample["integration_current_source"] = getattr(
                controller, "last_integration_source", None
            )
        elif step.kind == StepKind.CHEMID:
            # Ugyanaz az elv, mint az OCV_SOC ágnál fent — a
            # ChemIdCharacterizationController FULL_CHARGE_MANUFACTURER_METHOD
            # alatt a PSU-t, C20_DISCHARGE_TO_MANUFACTURER_MINIMUM alatt a
            # Load-ot vezérli aktívan (l. chemid_controller.py).
            chemid_state = getattr(controller, "state", None)
            if chemid_state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD:
                sample["charge_current_A"] = i_psu
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = i_psu
            elif chemid_state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM:
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = i_load
                sample["signed_current_A"] = None if i_load is None else -i_load
            else:
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = 0.0
            sample["accumulated_charge_Ah"] = getattr(controller, "accumulated_charge_Ah", None)
            sample["accumulated_discharge_Ah"] = getattr(controller, "accumulated_discharge_Ah", None)
            # Teljes kódbázis review 11. kör (2026-08-31), Szoftver/
            # Architektúra P3 lelet — l. a GOLDEN_GG ág fenti kommentje
            # ugyanerről a hiányzó proxy-mintáról.
            sample["integration_current_source"] = getattr(
                controller, "last_integration_source", None
            )
        elif step.kind == StepKind.IEC_DC_RESISTANCE:
            # Ugyanaz az elv, mint az OCV_SOC/CHEMID ágaknál fent — az
            # IecDcResistanceController PRECHARGE alatt a PSU-t, LOAD_1/
            # LOAD_2 alatt a Load-ot vezérli aktívan (l.
            # iec_dc_resistance_controller.py).
            iec_state = getattr(controller, "state", None)
            if iec_state == IecDcResistanceState.PRECHARGE:
                sample["charge_current_A"] = i_psu
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = i_psu
            elif iec_state in (IecDcResistanceState.LOAD_1, IecDcResistanceState.LOAD_2):
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = i_load
                sample["signed_current_A"] = None if i_load is None else -i_load
            else:
                sample["charge_current_A"] = 0.0
                sample["discharge_current_A"] = 0.0
                sample["signed_current_A"] = 0.0
            sample["accumulated_charge_Ah"] = getattr(controller, "accumulated_charge_Ah", None)
            sample["accumulated_discharge_Ah"] = getattr(controller, "accumulated_discharge_Ah", None)
            sample["integration_current_source"] = getattr(
                controller, "last_integration_source", None
            )
        else:
            sample["charge_current_A"] = 0.0
            sample["discharge_current_A"] = 0.0
            sample["signed_current_A"] = 0.0
            sample["accumulated_charge_Ah"] = self._total_charge_ah
            sample["accumulated_discharge_Ah"] = self._total_discharge_ah
            # Teljes kódbázis review 17. kör (2026-09-05), Metrológia/
            # Adatintegritás P2 lelet: ez az ág (gyakorlatilag kizárólag
            # StepKind.RELAX éri el) az Ah-testvérpárt a session-szintű
            # futó összegből tölti — a Wh-testvérpár korábban itt NEM
            # állt be, ezért a lenti (CHARGE/DISCHARGE-et kizáró) getattr-
            # ág RÁÍRTA a RelaxController-ről olvasott (sosem létező,
            # mindig None) accumulated_charge_Wh/accumulated_discharge_Wh
            # értékkel — ugyanabban a sorban az Ah-oszlop valós, nem-null
            # futó összeget mutatott, a Wh-oszlop mindig None maradt.
            sample["accumulated_charge_Wh"] = self._total_charge_wh
            sample["accumulated_discharge_Wh"] = self._total_discharge_wh

        # P1-C: driver állapot + integrátor minőségi mezők (psu_output_
        # commanded_on fentebb, a u_drop_V gate-hez már kiolvasva)
        sample["load_input_commanded_on"] = getattr(
            self._instruments.load, "input_commanded_on", None
        )

        sample["fault_flags"] = getattr(controller, "fault_reason", None) or None
        sample["warning_flags"] = getattr(controller, "last_warning_code", None) or None

        # Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        # + Metrológia P2 lelet: korábban a controller PRIVÁT `_integrator`
        # attribútumát kereste `getattr`-rel — ez az attribútumnév KIZÁRÓLAG
        # ChargeController-en és DischargeController-en létezett, a négy
        # összetett controller (GoldenGg/TraditionalLearning/OcvSoc/ChemId)
        # más nevű integrátort tart (`_energy_integrator`/`_discharge_
        # integrator`), ezért a Wh-mezők a TELJES session alatt None
        # maradtak a samples.csv-ben ezeknél a teszttípusoknál. Mostantól a
        # publikus `accumulated_charge_Wh`/`accumulated_discharge_Wh`
        # property-t olvassuk (ugyanaz a getattr-minta, mint az Ah-párnál),
        # amit MINDEN controller-típus (a beágyazott ChargeController/
        # DischargeController is) egységesen exponál.
        #
        # Teljes kódbázis review 3. kör (2026-08-29), Metrológia/
        # Adatintegritás P2 lelet: CHARGE/DISCHARGE StepKind-nál ez a
        # feltétel nélküli getattr felülírta volna a fenti CHARGE/DISCHARGE
        # ágban már beállított, session-szintű Wh-fallback-et (self.
        # _total_charge_wh/_total_discharge_wh — az Ah-testvérpár mintája,
        # l. _flush_totals()), mert a másik irányú Wh mindig None a
        # ChargeController/DischargeController-en. Ezért itt csak azoknál a
        # StepKind-oknál futtatjuk, ahol a fenti ág MINDKÉT irányt a
        # controllerről olvassa (GOLDEN_GG/OCV_SOC/CHEMID/egyéb).
        # Teljes kódbázis review 17. kör (2026-09-05), Metrológia/
        # Adatintegritás P2 lelet: StepKind.RELAX kizárva is — a
        # RelaxController nem exponál accumulated_charge_Wh/
        # accumulated_discharge_Wh property-t (a lenti getattr default
        # None-ja felülírta volna a fenti else-ágban MÁR helyesen beállított
        # session-szintű Wh-fallback-et).
        if step.kind not in (StepKind.CHARGE, StepKind.DISCHARGE, StepKind.RELAX):
            sample["accumulated_charge_Wh"] = getattr(controller, "accumulated_charge_Wh", None)
            sample["accumulated_discharge_Wh"] = getattr(controller, "accumulated_discharge_Wh", None)

        sample["psu_set_voltage_V"] = getattr(controller, "u_psu_set_V", None)
        sample["load_set_current_A"] = getattr(controller, "i_load_set_A", None)
        # Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        # P2 lelet: a CSV_COLUMNS-ban deklarált psu_set_current_A/
        # taper_timer_s mezőket korábban sosem töltöttük ki, holott a
        # ChargeController property-je (taper_timer_s) MÁR létezett — l.
        # logger.py CSV_COLUMNS, ugyanaz a getattr-minta, mint fent.
        sample["psu_set_current_A"] = getattr(controller, "i_psu_set_A", None)
        sample["taper_timer_s"] = getattr(controller, "taper_timer_s", None)
        sample["extreme_dt_s_count"] = getattr(controller, "extreme_dt_s_count", None)
        sample["max_observed_dt_s"] = getattr(controller, "max_observed_dt_s", None)

        # Rb mérési adatok — csak kisütési lépésben kerülnek feltöltésre
        u_pre = getattr(controller, "u_batt_pre_load_V", None)
        u_curr = sample.get("battery_voltage_V")
        sample["rb_delta_v"] = (
            round(u_pre - u_curr, 4)
            if u_pre is not None and u_curr is not None
            else None
        )
        sample["rb_1s_mohm"]  = getattr(controller, "rb_1s_mohm",  None)
        sample["rb_10s_mohm"] = getattr(controller, "rb_10s_mohm", None)
        sample["rb_30s_mohm"] = getattr(controller, "rb_30s_mohm", None)
        # Release-audit 2026-08-23 (2.3): tényleges mintavételi idő +
        # degraded jelző — l. discharge_controller.py.
        sample["rb_1s_actual_s"]  = getattr(controller, "rb_1s_actual_s",  None)
        sample["rb_10s_actual_s"] = getattr(controller, "rb_10s_actual_s", None)
        sample["rb_30s_actual_s"] = getattr(controller, "rb_30s_actual_s", None)
        sample["rb_timing_degraded"] = getattr(controller, "rb_timing_degraded", None)
        # Governance-v2 review-kör 1 (CF-24/DFAM-001, BAT-RI-001 sértés,
        # batch 4.6): l. discharge_controller.py::method_origin
        # docstringje — a fenti rb_*_mohm mezőknek eddig nem volt method_
        # origin/címkéje. Más StepKind-ok controllerein (nincs method_
        # origin property) getattr(...) None-t ad, ami helyes: azoknál
        # nincs rb_*_mohm érték sem.
        sample["method_origin"] = getattr(controller, "method_origin", None)
        # MAX verifier P2-1 lelete (CF-24 utóellenőrzés, batch 4.6): l.
        # discharge_controller.py::synchronization_quality docstringje —
        # a method_origin önmagában nem jelzi a mintavételi módot
        # (software-tick-timed vs instrumentum-időzített).
        sample["synchronization_quality"] = getattr(
            controller, "synchronization_quality", None
        )

        return sample
