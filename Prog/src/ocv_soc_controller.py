"""
OcvSocController — OCV-SOC lépcsős karakterizációs állapotgép.
Automatikus teljes töltés → 5h relax → SOC-lépések (racspont-politika szerint)
Minden lépésben: partial discharge (Ah-limit) + relax + OCV mérés + pulse impulzus.
[R1] Nincs relay hívás.

SUP01 (Kiegeszito_szabalyrendszer_01_ocv_soc_pulse_resistance_v1_0_20260723.txt)
megfelelőség — l. Riportok/sup01_ocv_soc_pulse_gap_matrix.md:
  - SUP01-GRID-001/002: TI_ALIGNED_15_POINT és UNIFORM_SOC_STEP racs, konfigurálható.
  - SUP01-GRID-003/DATA-001: capacity_basis_Ah + capacity_basis_quality.
  - SUP01-GRID-004/005/006: explicit grid-pozíció alapú célszámítás, nincs off-by-one,
    a racs UTOLSÓ (valódi 0%) pontja is ténylegesen kisütve és naplózva DONE előtt.
  - SUP01-PULSE-007/TEST-004: mért DeltaI (load.measure_current()) alapú R(t), setpoint
    csak DEGRADED_SETPOINT_CURRENT fallback.
  - SUP01-PULSE-009/TEST-002: a pulse alatt eltávolított Ah beszámít a SOC-ba.
  - SUP01-PULSE-010/TEST-005: cutoff (terminate voltage) pulse közben → azonnali Load OFF,
    CUTOFF_DURING_PULSE minősítés, nincs a teljes időtartamig kényszerítve a mérés.
  - SUP01-TERM-002/003: kimeneti mezőnevek R_pulse_actual_t*_mOhm (nem "Rb"), method_origin
    jelzi, hogy ez projekt-mérnöki módszer, nem TI Ra vagy FIAMM adatlap-érték.
  - SUP01-ARCH-001/PULSE-002/003/005/006 (WP-AK-04): a pulse mintavétel időzítését a
    PulseMeasurementService adja (Keysight DMM SAMP:SOUR TIM, instrumentum-időzített
    chunkok) — NEM a fő 2s runner tick. synchronization_quality mostantól
    INSTRUMENT_TIMED_SOFTWARE_TRIGGERED. A hardveres Keithley PULS-tranziens trigger
    (HARDWARE_SYNCHRONIZED szint) additív, be nem kötött marad — HIL validálást
    igényel (l. pulse_measurement_service.py docstring + gap-mátrix).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from Prog.src.battery_profile import BatteryProfile
from Prog.src.exceptions import TestAbortedByUser
from Prog.src.integrator import Integrator
from Prog.src.pulse_measurement_service import (
    NO_MEASURED_PULSE_ENERGY,
    MeasuredPulseEnergy,
    PulseDischargeError,
    PulseMeasurementService,
    PulseOvervoltageError,
    PulsePowerLimitError,
    require_battery_pulse_mode,
)
from Prog.src.safety import SafetyManager

if TYPE_CHECKING:
    from Prog.src.charge_controller import ChargeConfig, ChargeController


# SUP01-SRC-008 / SUP01-GRID-001-A: TI bq34z100 Ra-tábla racspontjaihoz igazodó
# külső összehasonlító mérési racs. NEM TI Ra table generálás — csak a SOC-pozíciók
# igazodnak hozzá (l. SUP01-TERM-002: tilos ezt TI-normalizált Ra-ként exportálni).
TI_ALIGNED_15_POINT_GRID: tuple[float, ...] = (
    100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 20.0,
    16.7, 13.4, 10.1, 6.8, 3.5, 0.2,
)

_KEYPOINTS = frozenset({100.0, 50.0, 20.0, 10.0, 0.0})

_GRID_MODES = ("UNIFORM_SOC_STEP", "TI_ALIGNED_15_POINT")


# Q-6 (2026-11-08): közös, rögzített impulzus-NPLC a preflighthoz és a méréshez.
STANDARD_PULSE_NPLC = 1.0
# Teljes kódbázis review 5. kör (2026-08-29), Session/Workflow P2 lelet:
# runaway-backstop a STEP_DISCHARGE fázisra — ugyanaz az érték/elv, mint a
# standalone DischargeController (discharge_controller.py) és a 4. körben
# a testvér fázisokba (chemid/golden_gg/traditional_learning) bevezetett
# időalapú backstop. A 6f21269 commit (4. kör) szándékosan kihagyta ezt a
# fázist az Ah-oldali backstopból ("implicit korlátozott a
# target_removed_Ah által") — ez az indoklás azonban nem zárja ki a kettős
# szenzorhiba esetét (a feszültség-DMM ÉS az áram-readback EGYSZERRE
# hibás/rögzült): ha mindkettő hibás, a target_removed_Ah cél sosem
# érkezik el, és korábban nem volt semmilyen tisztán idő-alapú
# (szenzorfüggetlen) felső korlát, ami a ciklust lezárná — csak ez az
# időalapú komponens hiányzott, az Ah-oldal (target_removed_Ah) továbbra is
# az elsődleges, normál leállási mechanizmus marad.
# Teljes kódbázis review 8. kör (2026-08-30), Szoftver/Architektúra P2
# lelet: az érték korábban privát, hardkódolt modul-konstansként élt itt
# is — mostantól self._safety.max_discharge_time_s a kanonikus forrás
# (l. safety.py), a standalone DischargeController mintáját követve.


def _build_uniform_grid(step_percent: float) -> tuple[float, ...]:
    """SUP01-GRID-001-B: egyenletes racs 100%-tól 0%-ig, pontos (float-drift-mentes) végponttal."""
    n_steps = max(1, round(100.0 / step_percent))
    points = [round(100.0 - i * step_percent, 6) for i in range(n_steps)]
    if not points or points[-1] != 0.0:
        points.append(0.0)
    return tuple(points)


@dataclass
class OcvSocConfig:
    grid_mode: str = "UNIFORM_SOC_STEP"               # SUP01-GRID-001/002
    step_percent: float = 5.0                         # csak UNIFORM_SOC_STEP módban
    discharge_rate_divisor: int = 10                  # C/10 alapértelmezett -- Kör 19 D8g (SW19-05): most már megegyezik config_panel.py::SessionConfig.discharge_rate_divisor alapértékével (korábban 5 volt ott, GUI-n át sosem elérhető)
    relax_default_s: float = 7200.0                   # 2h alap
    relax_keypoint_s: float = 18000.0                 # 5h kulcspontokon (100%,50%,20%,10%,0%)
    impulse_current_rate_divisor: int = 5             # C/5 pulse-hoz
    impulse_duration_s: float = 30.0                  # 30s impulzus (PulseMeasurementService méri)
    pulse_time_tolerance_s: float = 0.5               # SUP01-PULSE-006 R_1s/10s/30s névhez
    pulse_sample_interval_s: float = 0.2               # SUP01-PULSE-003: DMM SAMP:TIM intervallum
    pulse_chunk_sample_count: int = 3                  # egy advance() tick alatt lekérdezett minták (~0.6s cutoff-latencia)

    # RB Pulse Digitalizálás spec kiegészítés (Folyamatok/tervek/RB_Pulse_
    # Digitalizalas_Fejlesztesi_Spec_v1_2026-08.md, 2026-08-22) — mind
    # OPT-IN, alapértelmezett értékkel a MEGLÉVŐ standard pulse viselkedés
    # bit-pontosan változatlan marad (l. pulse_measurement_service.py
    # docstring "RB Pulse Digitalizálás spec kiegészítés" szakasza).
    pulse_trigger_mode: str = "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
    pulse_baseline_window_s: float = 0.0
    pulse_baseline_sample_interval_s: float = 0.2
    pulse_recovery_duration_s: float = 0.0
    pulse_recovery_sample_interval_s: float = 0.2
    pulse_trace_interval_s: float = 0.04
    pulse_trace_points: int = 1000

    # FAST_TRANSIENT_CHARACTERIZATION (spec §6.3/§12/§22) — KÜLÖN, opcionális
    # diagnosztikai réteg a STANDARD_PULSE (1/10/30s) mellett, alapértelmezésben
    # KIKAPCSOLVA (a standard karakterizáció marad az elsődleges baseline).
    fast_transient_enabled: bool = False
    fast_transient_target_times_s: tuple[float, ...] = (0.02, 0.1, 0.5)
    fast_transient_duration_s: float = 0.6
    fast_transient_sample_interval_s: float = 0.005
    fast_transient_chunk_sample_count: int = 20
    # A ms-tartományú célidőkhöz a standard 0.5s-os pulse_time_tolerance_s
    # HASZNÁLHATATLAN lenne (nagyobb, mint a legtöbb célidő) — külön, szűk
    # tolerancia szükséges.
    fast_transient_time_tolerance_s: float = 0.01
    # ── 2026-09-12, FAST_TRANSIENT NPLC-javítás ────────────────────────────
    # A session normál üzemi DCV integrációs ideje NPLC=10 (main_window.py::
    # _bring_up() `configure_dcv(range_V=100, nplc=10)`, ill. OCV_SOC lépésre
    # test_runner.py::_configure_dmm_for_step() `relax_ocv_nplc=10.0`), ami a
    # 34465A-n 50 Hz-en 0,2 s / 60 Hz-en 0,167 s apertúra mintánként
    # (34460-70-Manual.txt 14028-14052. sor, nyomtatott 453. o.) — a
    # `fast_transient_sample_interval_s=0.005` ezzel fizikailag
    # teljesíthetetlen (a műszer -221 "cannot meet requested timing"-ot ad és
    # ELÁLLÍTJA a sample timert, uo. 9067-9072. sor).
    #
    # 0,02 PLC = 0,4 ms (50 Hz) / 0,3 ms (60 Hz) apertúra, és ez a 34465A
    # MINDEN változatában elérhető legkisebb NPLC (uo. 11428-11433. sor,
    # nyomtatott 382. o. — Kör 20 E18/SRC-F-4: önálló `pdftotext -f 381
    # -l 382` kinyeréssel megerősítve, a `[SENSe:]VOLTage[:DC]:NPLC` blokk
    # a "--- 381. oldal ---" jelölő UTÁN, a "--- 382. oldal ---" jelölő
    # ELŐTT áll; a testvér idézetek, pulse_measurement_service.py:366 és
    # device_dmm.py:335, már helyesen 382-t írtak) — a 0,006/0,002/0,001
    # fokozatok CSAK a DIG opcióval léteznek, a laborpéldány DIG-opciója
    # pedig SOURCE_REQUIRED, ezért NEM támaszkodunk rájuk. A tényleges
    # elérhetőséget a PulseMeasurementService
    # minden impulzus előtt a műszertől (SAMPle:TIMer? MIN) kérdezi vissza.
    #
    # METROLÓGIAI KORLÁT (rules/20-metrology.md): "Only integration times of
    # 1, 10, or 100 PLC provide normal mode (line frequency noise) rejection"
    # (uo. 11441. sor) — NPLC=0,02 mellett NINCS hálózati zajelnyomás,
    # és a felbontás a 100 V tartományon 3 ppm × 100 V = 0,3 mV (uo. 14053+.
    # sor). A ms-tartományú tranziens mérésének ez elkerülhetetlen ára, ezért
    # marad ez a réteg DIAGNOSZTIKAI (a fő R_1s/10s/30s forrása továbbra is a
    # standard, Q-6 szerint 1 NPLC/AZ ON impulzus).
    fast_transient_nplc: float = 0.02
    # Visszaállítási FALLBACK, ha a driver nem tud NPLC-t visszaolvasni
    # (`get_nplc()`). Ha tud, a VISSZAOLVASOTT érték élvez elsőbbséget — a
    # DCV NPLC-nek több, egymástól független írója van (l. fent), ezért a
    # readback a megbízható forrás, nem ez a konstans. Az érték a
    # TestRunnerConfig.relax_ocv_nplc alapértelmezésével egyezik, mert az
    # OCV_SOC alapbeállítása; az impulzusok ezt ideiglenesen felülírják.
    fast_transient_restore_nplc: float = 10.0

    def __post_init__(self) -> None:
        if self.grid_mode not in _GRID_MODES:
            raise ValueError(
                f"grid_mode={self.grid_mode!r} ismeretlen — {_GRID_MODES} egyike lehet"
            )


class OcvSocState(Enum):
    INIT = "INIT"
    PRECHARGE = "PRECHARGE"                   # teljes töltés (belső ChargeController)
    PRECHARGE_RELAX = "PRECHARGE_RELAX"       # 5h relax 100%-on
    STEP_DISCHARGE = "STEP_DISCHARGE"         # partial kisütés a következő racspontig
    STEP_RELAX = "STEP_RELAX"                 # relax
    IMPULSE_PREP = "IMPULSE_PREP"             # OCV mérés relax végén
    IMPULSE_ON = "IMPULSE_ON"                 # impulzus start (PulseMeasurementService.begin_pulse)
    IMPULSE_ACQUIRE = "IMPULSE_ACQUIRE"       # instrumentum-időzített chunkonkénti mintavétel
    # RB Pulse Digitalizálás spec §6.3/§12/§22: opcionális, config.fast_transient_enabled
    # esetén a standard (1/10/30s) pulse UTÁN, KÜLÖN impulzussal futó ms-tartományú
    # diagnosztikai réteg. Alapértelmezésben nem használt állapotok.
    IMPULSE_FAST_ON = "IMPULSE_FAST_ON"
    IMPULSE_FAST_ACQUIRE = "IMPULSE_FAST_ACQUIRE"
    LOG_SOC_POINT = "LOG_SOC_POINT"           # SOC pont rögzítése
    DONE = "DONE"
    FAULT = "FAULT"


# R1 B8b, F-23 (DFAM-001): azok az állapotok, amelyekben a feszültség-DMM
# EGYETLEN olvasási tulajdonosa a PulseMeasurementService (SUP01-ARCH-002),
# a controller saját _read_dmm()-je NEM fut. Egyetlen forrás az advance()
# DMM-olvasási kizárási listájához és a publikus dmm_valid tick-scoped
# érvénytelenítéséhez (l. _dmm_owned_by_pulse_service_this_tick).
_PULSE_SERVICE_DMM_OWNER_STATES: frozenset[OcvSocState] = frozenset({
    OcvSocState.IMPULSE_ON, OcvSocState.IMPULSE_ACQUIRE,
    OcvSocState.IMPULSE_FAST_ON, OcvSocState.IMPULSE_FAST_ACQUIRE,
})


class OcvSocController:
    """OCV-SOC lépcsős karakterizáció állapotgép."""

    def __init__(
        self,
        psu,
        load,
        dmm_voltage,
        dmm_temperature,
        profile: BatteryProfile,
        safety: SafetyManager,
        config: OcvSocConfig,
        measured_capacity_Ah: Optional[float] = None,
        pulse_time_source: Optional[Callable[[], float]] = None,
        charge_config: ChargeConfig | None = None,
    ):
        require_battery_pulse_mode(config.pulse_trigger_mode)
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature
        self._profile = profile
        self._safety = safety
        self._config = config
        # R16-B12: a mérés közben érkező leállítást az indítás előtt látni kell.
        self.handoff_stop_requested: Callable[[], bool] | None = None
        # Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P1
        # lelet: a TraditionalLearningController/GoldenGgController MÁR
        # megkapja a session-szintű charge_config-ot (2026-08-23 OV-incidens
        # javítás, fast_loop_enabled=True a CC→CV határon) — az OcvSocController
        # eddig strukturálisan nem tudta fogadni, ezért a beágyazott
        # ChargeController mindig a ChargeConfig() alapértéket kapta
        # PRECHARGE alatt, holott ugyanazon a fizikai (TF1/soros dióda)
        # útvonalon fut, mint a másik két controller — l. _enter_precharge().
        from Prog.src.charge_controller import ChargeConfig
        self._charge_config = charge_config or ChargeConfig()

        # SUP01-GRID-003: mért kapacitás elsődleges, névleges csak explicit
        # minősítéssel (NOMINAL_CAPACITY_BASED_SOC) fogadható el.
        # Teljes kódbázis review 8. kör (2026-08-30), Metrológia/
        # Adatintegritás P2 lelet: a fallback korábban profile.
        # nominal_capacity_Ah-t használt — a normatív forrás (SUP01-GRID-003,
        # kiegeszito_szabalyok_01_ocv_soc_pulse_resistance_v1_0_20260723.txt
        # 338-350. sor) explicit nominal_C10_capacity_Ah-t ír elő. CLAUDE.md
        # §11.1 "C10 és C20 nem cserélhető fel" — ugyanaz a mintakövetés,
        # mint battery_profile.py::C10_discharge_current_A (release-audit
        # 2.9) és traditional_learning_controller.py már helyesen C10-alapú
        # kisütőáram-számítása. FGH/FGHL-nél (nominal=C10) numerikusan
        # egyezik a régivel; FG-sorozatnál (C20-nominal), ha C10_capacity_Ah
        # explicit meg van adva, a helyes, alacsonyabb bázist adja.
        if measured_capacity_Ah is not None and measured_capacity_Ah > 0:
            self._total_capacity_Ah = measured_capacity_Ah
            self._capacity_basis_quality = "MEASURED_CAPACITY_BASED"
        else:
            self._total_capacity_Ah = profile._c10_base_Ah
            self._capacity_basis_quality = "NOMINAL_CAPACITY_BASED_SOC"

        # SUP01-GRID-001: racspont-lista a konfigurált mód szerint.
        if config.grid_mode == "TI_ALIGNED_15_POINT":
            self._grid: tuple[float, ...] = TI_ALIGNED_15_POINT_GRID
        else:
            self._grid = _build_uniform_grid(config.step_percent)

        self._state = OcvSocState.INIT
        # CF-19 (governance-v2 kör 2, DFAM-023 opaque_composite_step_hides_
        # progress): mennyi ideje van a controller a JELENLEGI belső
        # állapotban -- a `_enter()` helper minden állapotváltáskor
        # nullázza, az `advance()` minden tickben növeli. Tisztán olvasó-
        # oldali bookkeeping, semmilyen dispatch/safety-logikát nem érint.
        self._phase_elapsed_s: float = 0.0
        self._fault_reason: str = ""
        self._normal_discharge_cutoff_seen = False

        self._charge_ctrl: Optional["ChargeController"] = None

        self._step_discharge_started = False
        self._step_integrator = Integrator()
        # Teljes kódbázis review 5. kör (2026-08-29), Session/Workflow P2
        # lelet: a jelenlegi STEP_DISCHARGE lépés saját eltelt ideje — l.
        # _run_step_discharge() runaway-backstop ellenőrzése.
        self._step_discharge_phase_elapsed_s: float = 0.0
        # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: a TestRunner
        # _flush_totals()-je (test_runner.py) getattr(controller,
        # "accumulated_charge_Ah"/"accumulated_discharge_Ah", 0.0)-t olvas —
        # enélkül a property nélkül a delta MINDIG 0 volt, ezért egy teljes
        # OCV-SOC session report.json/checkpoint total_charge_ah/
        # total_discharge_ah mezője 0-án maradt (ugyanaz a hibaminta, mint
        # a GOLDEN_GG/TRADITIONAL_LEARNING-nél már javítva, l.
        # golden_gg_controller.py::_energy_integrator). R2-B4 (DFAM-043
        # korrekció; a korábbi "az impulzus-alfázisokat NEM" állítás hamis
        # volt): forrásai a PRECHARGE (a beágyazott ChargeController
        # deltája), a STEP_DISCHARGE (tickenkénti mért áram), a relax-tickek
        # (csak dt-diagnosztika, energia nincs) ÉS az impulzus-alfázisok: a
        # mért rész minden sikeres chunk után (_book_measured_pulse_
        # energy()), a terminális OFF-farok a normál lezáráskor (_finish_*).
        # Megszakított impulzusnál az emergency_stop() a már rögzített
        # leolvasásokig könyvel; az utolsó könyvelt leolvasás utáni szakasz
        # nincs benne, nincs becsülve (OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY
        # esemény).
        self._energy_integrator = Integrator()
        self._last_integration_source: str = "ZERO"  # R6-B3, l. last_integration_source
        self._pulse_observed_extra_s = 0.0
        self._pulse_block_warnings: dict[str, int] = {}

        # SUP01-GRID-005: grid-pozíció-alapú, egyértelmű állapot — nincs egyetlen
        # túlterhelt "soc_index" változó, ami egyszerre "aktuális" és "következő" pont.
        # _grid_pos: a LEGUTÓBB elért/naplózott pont indexe a rácsban (0 = 100%, induláskor).
        self._grid_pos: int = 0
        self._target_removed_Ah: float = 0.0

        # Összesített eltávolított Ah
        self._removed_Ah_total: float = 0.0

        # Kor 18, Batch C12 (METRO-P2-1, DFAM-021, R17-12 mintajat koveteve):
        # a _determine_point_quality() minden ponton kiszamolodik es az
        # ocv_soc_table.csv-be (measurement_quality oszlop) is eljut, DE
        # session-szintu osszesites nelkul a report.json/befejezes-popup
        # sosem latta -- egy tobb pontot is DEGRADED/CUTOFF_DURING_PULSE-kent
        # minositett session "Kesz"-kent jelent meg minosites nelkul. Ez a
        # dict quality_string->count szamlal, a warning_code_counts mar
        # bevalt mintajat koveteve.
        self._point_quality_counts: dict[str, int] = {}

        # Relax időzítő
        self._relax_elapsed_s: float = 0.0
        self._current_relax_s: float = config.relax_keypoint_s  # első: 5h (100%)

        # DMM olvasás
        self._u_batt: float = 0.0
        self._dmm_valid: bool = True
        # BATCH 4.2b F-2 (2026-09-13, DECISION_REGISTER.md GOV-024, "5.
        # csoport"): `_run_relax_phase()` korábban MINDEN ticken tüzelte
        # az OCV_SOC_RELAX_DMM_FAULT eseményt, amíg `self._dmm_valid`
        # False maradt -- egy hosszú (2-5 órás) relax alatti tartós DMM-
        # kiesés akár ezres nagyságrendű azonos eseményt generálhatott,
        # kiürítve az EventLogWidget 500-soros gyűrűjéből a korábbi
        # kontextust. Ez az "előző tick érvényessége" jelző teszi
        # lehetővé, hogy csak az ÁTMENETKOR (érvényes -> érvénytelen)
        # tüzeljünk, a számlálók (relax_dmm_fault_total/
        # dmm_fault_during_relax) továbbra is MINDEN ticken nőnek.
        self._dmm_valid_before_tick: bool = True
        # R1 B8b, F-23 (P2, DFAM-001; D-B8-7): tick-scoped jelző -- True,
        # ha EZ a tick egy IMPULSE_* állapotban INDULT, azaz a feszültség-
        # DMM-et a PulseMeasurementService birtokolta és a controller nem
        # olvasott. Csak a PUBLIKUS dmm_valid property-t érvényteleníti
        # (a runner mintája ne jelölje a nem e tickben mért u_batt-ot érvényes
        # friss mérésnek); a privát _dmm_valid és a belső kapuk érintetlenek.
        # advance() tetején, a terminális return ELŐTT nullázva.
        self._dmm_owned_by_pulse_service_this_tick: bool = False
        self._battery_temperature_C: float = 20.0
        # Review-kör 3 (2026-09-09), friss Metrológia M3-01 -- l.
        # temp_dmm_valid property docstringje.
        self._temp_dmm_valid: bool = True
        # Review-kör 3 (2026-09-09), friss Metrológia M3-02 / Szoftver/
        # Architektúra F2 (P2) -- l. chemid_controller.py azonos property
        # docstringje ugyanerről a session-szintű, tick-alapú számlálóról.
        self._temp_dmm_fault_ticks: int = 0
        # MAX verifier (review-kör 4, HW4-02, 329c1a3) N1 lelete -- l.
        # discharge_controller.py azonos hivatkozású mező docstringje.
        self._load_current_setpoint_warning_ticks: int = 0
        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # l. discharge_controller.py::i_load_measured_A/load_current_
        # readback_valid property docstringje -- ugyanaz a tick-scoped
        # validity-flag minta, a STEP_DISCHARGE fázisra vonatkozik (az
        # IMPULSE_* sub-tick pulzus-fázisok SZÁNDÉKOSAN kimaradnak, l.
        # pulse_measurement_service.py -- nincs "a tick mintája", amit
        # exponálni lehetne, halasztott döntés).
        self._i_load_measured_A: float | None = None
        self._load_current_readback_valid: bool | None = None
        self._dmm_fault_during_relax: bool = False
        # Teljes kódbázis review 14. kör (2026-09-04), Szoftver/
        # Architektúra + Session/Workflow/Állapotgép P2 lelet (két
        # domain-ágens is önállóan megtalálta): a testvér GoldenGgController/
        # TraditionalLearningController/ChemIdCharacterizationController
        # mindegyike gyűjt egy SESSION-SZINTŰ, fázisváltáskor NEM nullázódó
        # relax_dmm_fault_total számlálót — az OcvSocController-en (a
        # negyedik testvéren) ez hiányzott, csak a per-SOC-pont
        # _dmm_fault_during_relax (fázisváltáskor mindig resetelt) létezett,
        # ami kizárólag a nyers soc_points.csv-be jutott el, a session-
        # szintű capacity_result_quality DEGRADED-kapuba soha.
        self._relax_dmm_fault_total: int = 0
        # Teljes kódbázis review 9. kör (2026-08-30), Szoftver/Architektúra
        # P2 lelet: a hőmérséklet-DMM MONITOR_ONLY warning-ja (a fault előtti
        # figyelmeztető ablakban) korábban sehol nem került tárolásra — a
        # last_warning_code property teljesen hiányzott, test_runner.py
        # warning_flags mezője mindig None maradt OCV_SOC session alatt.
        self._last_warning_code: str = ""
        # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        # lelet — l. charge_controller.py azonos konstruktor-kommentje
        # ugyanerről a hiányzó session-szintű összesítésről.
        self._temperature_out_of_range_count: int = 0

        # SUP01-ARCH-001 (WP-AK-04): a pulse mintavétel időzítését és a
        # cutoff/degraded döntéseket a PulseMeasurementService adja —
        # az OcvSocController csak a workflow/SOC logikát tartja.
        self._pulse_service = PulseMeasurementService(
            load,
            dmm_voltage,
            profile.terminate_voltage_pack_V,
            chunk_sample_count=config.pulse_chunk_sample_count,
            sample_interval_s=config.pulse_sample_interval_s,
            time_tolerance_s=config.pulse_time_tolerance_s,
            time_source=pulse_time_source or time.monotonic,
            trigger_mode=config.pulse_trigger_mode,
            baseline_window_s=config.pulse_baseline_window_s,
            baseline_sample_interval_s=config.pulse_baseline_sample_interval_s,
            recovery_duration_s=config.pulse_recovery_duration_s,
            recovery_sample_interval_s=config.pulse_recovery_sample_interval_s,
            trace_interval_s=config.pulse_trace_interval_s,
            trace_points=config.pulse_trace_points,
            # Q-6 (2026-11-08): standard impulzus 1 NPLC, AZ ON, 0,2 s.
            acquisition_nplc=STANDARD_PULSE_NPLC,
            require_autozero_on=True,
            controller_managed_cleanup=True,
            safety=safety,
            max_voltage_V=profile.batt_absolute_max_V,
            max_load_power_W=safety.max_load_power_W,
            handoff_stop_requested=lambda: (
                self.handoff_stop_requested is not None and self.handoff_stop_requested()
            ),
        )

        # RB Pulse Digitalizálás spec: FAST_TRANSIENT_CHARACTERIZATION KÜLÖN
        # PulseMeasurementService példány, saját (ms-tartományú) célidőkkel és
        # toleranciával — a standard 1/10/30s pulse-tól függetlenül konfigurálható,
        # csak akkor jön létre, ha explicit be van kapcsolva (opt-in).
        self._fast_pulse_service: Optional[PulseMeasurementService] = None
        if config.fast_transient_enabled:
            self._fast_pulse_service = PulseMeasurementService(
                load,
                dmm_voltage,
                profile.terminate_voltage_pack_V,
                chunk_sample_count=config.fast_transient_chunk_sample_count,
                sample_interval_s=config.fast_transient_sample_interval_s,
                time_tolerance_s=config.fast_transient_time_tolerance_s,
                time_source=pulse_time_source or time.monotonic,
                target_times_s=config.fast_transient_target_times_s,
                trigger_mode=config.pulse_trigger_mode,
                # A fast-transient külön 0,02 NPLC-t használ; a standard
                # impulzus Q-6 szerint ideiglenesen 1 NPLC-re vált.
                acquisition_nplc=config.fast_transient_nplc,
                restore_nplc=config.fast_transient_restore_nplc,
                controller_managed_cleanup=True,
                safety=safety,
                max_voltage_V=profile.batt_absolute_max_V,
                max_load_power_W=safety.max_load_power_W,
                handoff_stop_requested=lambda: (
                    self.handoff_stop_requested is not None and self.handoff_stop_requested()
                ),
            )

        # SUP01-PULSE-009: pulse alatt eltávolított Ah — LOG_SOC_POINT-kor a
        # removed_Ah_total-ba számítva.
        self._impulse_removed_Ah: float = 0.0
        # R2-B4 (R2-metrology-01, DFAM-033): az aktív (standard vagy fast)
        # impulzusból az _energy_integrator-ba MÁR könyvelt rész pillanat-
        # képe. Impulzusonként EGYETLEN helyen, közvetlenül a begin_pulse()
        # ELŐTT nullázódik; a normál lezárás csak a removed - booked
        # különbséget adja hozzá.
        self._pulse_energy_booked: MeasuredPulseEnergy = NO_MEASURED_PULSE_ENERGY
        self._finalized_pulse_services: set[PulseMeasurementService] = set()
        # SUP01-PULSE-007/008: mért áram degraded-fallback jelző.
        self._pulse_current_degraded: bool = False
        # Teljes kódbázis review 20. kör (2026-09-06), Metrológia/
        # Adatintegritás P2 lelet: a PulseMeasurementService.
        # baseline_current_degraded jelzőjét (ha a baseline I mérése
        # hibázik, a feltételezett, nem mért bázisáram marad érvényben az
        # R(t) számításban) korábban _finish_impulse_acquisition() sosem
        # olvasta ki, ezért _determine_point_quality() ezt az ágat
        # kihagyta — "OK" minősítést adhatott egy olyan R_1s/10s/30s
        # pontra, aminek a ΔI-je feltételezett, nem mért bázisáramot
        # használt (CLAUDE.md §11.5/§12).
        self._baseline_current_degraded: bool = False
        # SUP01-PULSE-010/TEST-005: cutoff pulse közben; alacsony feszültségnél
        # a pulse el sem indul (SKIPPED_LOW_VOLTAGE).
        self._cutoff_during_pulse: bool = False
        self._skipped_low_voltage: bool = False
        # Review-lelet: legalább egy elért ponton a mért DeltaI <= 0 volt,
        # ezért a resistance_ohm nem számítható — ne csendben 0.0-ra essen,
        # jelezze a minőség.
        self._invalid_pulse_resistance: bool = False
        # Review-kör 2 (2026-09-08), Session/Workflow/Állapotgép S-5 lelet:
        # ha a végfeszültség a racs szerinti köztes cél ELŐTT érkezik,
        # _finish_discharge_step(forced_by_terminate_voltage=True) a
        # _grid_pos-t egyenesen a racs UTOLSÓ elemére ugratja -- a köztes
        # racspontok kimaradnak, de ez korábban semmilyen quality/
        # limitáció-jelzést nem kapott: az utolsó, DONE-t kiváltó
        # on_soc_point() ugyanúgy "OK"-nak tűnt, mint egy teljes racsot
        # bejáró session. Ez a 2 mező explicit jelzi, ha/mennyi racspont
        # maradt ki emiatt.
        self._grid_truncated_by_terminate_voltage: bool = False
        self._grid_points_skipped: int = 0
        # Release-audit 2026-08-23 (2.8): a PulseMeasurementService saját
        # within_tolerance jelzője (a reached-től FÜGGETLEN, tényleges
        # időzítési tolerancia-ellenőrzés) korábban sosem folyt be a
        # minőség-döntésbe — egy R_1s, ami jóval a célidőpont után
        # mintavételeződött (pl. 2.141s a beállított 1.0s helyett), "OK"
        # minősítést kaphatott, mert csak a `reached` (alsó korlát) számított.
        self._pulse_timing_out_of_tolerance: bool = False
        # SUP01-PULSE-006: tényleges idopontok.
        self._t1_actual_s: Optional[float] = None
        self._t10_actual_s: Optional[float] = None
        self._t30_actual_s: Optional[float] = None
        self._t1_time_error_s: Optional[float] = None
        self._t10_time_error_s: Optional[float] = None
        self._t30_time_error_s: Optional[float] = None
        self._synchronization_quality: str = "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
        # Review-kör 5, Metrológia M5-03 (P2, DFAM-021 computed_but_never_
        # persisted): a PulseMeasurementService MÁR kiszámolja a tényleges
        # impulzus-terhelési időt (pulse_duration_actual_s), de ez a
        # controller korábban sosem olvasta ki a PulseAcquisitionResult-ból
        # -- a `on_soc_point`/CSV sosem tudta meg, ha a tényleges terhelési
        # idő jelentősen eltért a konfigurált `impulse_duration_s`-től
        # (pl. instrumentum-időzítési overhead miatt).
        self._pulse_duration_set_s: Optional[float] = None
        self._pulse_duration_actual_s: Optional[float] = None

        # RB Pulse Digitalizálás spec: HARDWARE_PULSE módban post-hoc, a fetch-elt
        # trace-ből derül ki, ha a mintavétel a pulse teljes idejét nem fedte le.
        self._current_trace_incomplete: bool = False
        self._voltage_trace_incomplete: bool = False
        self._pulse_trigger_mode_result: str = config.pulse_trigger_mode
        self._measured_baseline_current_A: Optional[float] = None
        self._recovery_sample_count: int = 0

        # Impulzus eredmények (Ohm egységben belül; kimeneten mOhm — l. _run_log_soc_point)
        self._last_ocv_V: float = 0.0
        # Governance-v2 review-kör 1 (CF-23/MET-R1-06, DFAM-024): Optional —
        # nem-elért/nem-számítható pontnál None marad, nem 0.0 sentinel (l.
        # _run_log_soc_point()).
        self._rb_1s: Optional[float] = None
        self._rb_10s: Optional[float] = None
        self._rb_30s: Optional[float] = None
        # [Kör 19 Batch D3a, F-1/DFAM-021 7. előfordulás] a R_pulse_actual_
        # t*_mOhm = dU/dI hányados KORÁBBAN sosem tárolta a saját dU/dI
        # SZÁMLÁLÓJÁT/NEVEZŐJÉT -- a session csak a végeredményt naplózta,
        # az ellenőrizhetőséghez szükséges nyers bemenetek elvesztek. A
        # testvér Rb-út (test_runner.py::_build_sample(), a discharge_
        # controller.py::u_batt_pre_load_V-ből számolva) ezt már helyesen
        # teszi (rb_delta_v persistált oszlop) -- ez a hiányzó pár. `ocv_V`
        # NEM helyettesítheti dU-t (más mintavételi időpont, l. a lenti
        # _run_log_soc_point() kommentje), és `pulse_current_measured_
        # mean_A` NEM helyettesíti dI-t (a hányados a legközelebbi EGYETLEN
        # mintából, nem az átlagból számol) -- l. PulseResultPoint.delta_
        # voltage_V/delta_current_A docstringje.
        # [MAX verifier P3-1 lelet] a hányados NEM rekonstruálható sor-
        # szinten MINDIG a dU/dI-ből -- egy INVALID_PULSE_RESISTANCE
        # ponton (delta_i<=0 vagy delta_v<=0) a dU/dI mezők KITÖLTŐDNEK
        # (l. lent, a `reached` kapu ugyanaz, mint a hányadosé, de a
        # hányados MAGA emellett is None marad, l. pulse_measurement_
        # service.py::PulseResultPoint.resistance_ohm számítása) -- ez
        # SZÁNDÉKOS (a dU/dI ekkor a DIAGNÓZIS forrása, miért None a
        # hányados), nem hiba, de egy vak "R=dU/dI*1000" utólagos
        # rekonstrukció ilyen soron fizikailag lehetetlen/negatív
        # eredményt adna -- l. `measurement_quality` mező ugyanabban a
        # sorban.
        self._rb_1s_dU_V: Optional[float] = None
        self._rb_10s_dU_V: Optional[float] = None
        self._rb_30s_dU_V: Optional[float] = None
        self._rb_1s_dI_A: Optional[float] = None
        self._rb_10s_dI_A: Optional[float] = None
        self._rb_30s_dI_A: Optional[float] = None
        self._pulse_v_baseline_V: Optional[float] = None
        self._impulse_current_A: float | None = 0.0

        # FAST_TRANSIENT_CHARACTERIZATION eredmények (target_time_s -> érték),
        # üres marad, ha config.fast_transient_enabled=False.
        self._fast_R_mOhm: dict[float, float] = {}
        self._fast_actual_s: dict[float, float] = {}
        self._fast_time_error_s: dict[float, float] = {}
        self._fast_synchronization_quality: Optional[str] = None
        # Review-lelet: KÜLÖN mező a fast-transient réteg cutoff/degraded
        # jelzésére — sosem írható a standard pulse minősítését adó
        # self._cutoff_during_pulse / self._pulse_current_degraded mezőkbe
        # (l. _finish_fast_impulse_acquisition() kommentje).
        self._fast_cutoff_during_pulse: bool = False
        self._fast_pulse_current_degraded: bool = False
        # Teljes kódbázis review 21. kör (2026-09-07), Metrológia/
        # Adatintegritás P2 lelet: a fast-transient réteg saját
        # has_invalid_resistance eredménye korábban sehol nem jutott el
        # (sem külön mezőbe, sem az on_soc_point-ba) — ugyanaz a "külön
        # mező" minta, mint a fenti cutoff/degraded jelzőknél.
        self._fast_invalid_pulse_resistance: bool = False
        # Teljes kódbázis review 22. kör (2026-09-07), Metrológia/
        # Adatintegritás P2 lelet: a standard pulse-nál már bekötött
        # current_trace_incomplete/voltage_trace_incomplete (csak
        # TRIGGER_MODE_HARDWARE_PULSE módban fordulhat elő) a fast-
        # transient rétegen sehol nem jutott el — ugyanaz a "külön mező"
        # minta, mint a fenti cutoff/degraded/invalid_resistance jelzőknél.
        self._fast_current_trace_incomplete: bool = False
        self._fast_voltage_trace_incomplete: bool = False
        # Teljes kódbázis review 23. kör (2026-09-07), Metrológia/
        # Adatintegritás P2 lelet: a fast-transient réteg saját
        # skipped_low_voltage eredménye (a _run_impulse_fast_on() saját,
        # friss DMM-olvasásán alapuló előzetes ellenőrzésén akadhat el,
        # FÜGGETLENÜL a standard pulse-tól) korábban sehol nem jutott el
        # — a többi mező (cutoff/degraded/invalid_resistance/trace_
        # incomplete×2) mind bekötött, ez az egyetlen PulseAcquisitionResult
        # mező, ami kimaradt. Enélkül egy elakadt fast-transient impulzus
        # "sikeresen lezajlott" benyomást keltett (pl. fast_transient_
        # synchronization_quality kitöltve), holott a Load be sem kapcsolt.
        self._fast_skipped_low_voltage: bool = False
        # 2026-09-12, FAST_TRANSIENT NPLC-javítás: a réteg saját, explicit
        # időzítés-megvalósíthatósági minősítése. True, ha a műszer SAJÁT
        # visszaolvasott képessége (SAMPle:TIMer? MIN) szerint a kért 5 ms-os
        # mintavételi köz nem teljesíthető, vagy a képesség nem volt
        # ellenőrizhető — ilyenkor az impulzus el sem indult (a Load nem
        # kapcsolt be), és NINCS fast_transient_R_mOhm adat. Ugyanaz a
        # "külön mező, sosem a standard pulse minősítésébe" minta, mint a
        # fenti jelzőknél: a standard R_1s/10s/30s érvényes marad.
        self._fast_timing_infeasible: bool = False
        self._fast_timing_infeasible_reason: str = ""
        # True, ha az ideiglenes NPLC visszaállítása hibázott — ez viszont a
        # session HÁTRALÉVŐ (normál sebességű) méréseit is érinti, ezért
        # önálló, látható jelző (rules/20-metrology.md).
        self._fast_nplc_restore_failed: bool = False

        # Callback
        self.on_soc_point: Optional[Callable[[dict], None]] = None
        # Governance-v2 review-kör 1 (CF-07, DFAM-011, batch 4.2): a
        # testvér RelaxController-rel ellentétben ennek a controllernek
        # korábban EGYÁLTALÁN NEM volt on_event-je — a beágyazott relax-
        # fázisok (PRECHARGE_RELAX/STEP_RELAX) DMM-kiesése csak a session-
        # végi relax_dmm_fault_total számlálóban jelent meg, élőben
        # láthatatlanul (l. _run_relax_phase()).
        self.on_event: Optional[Callable[[dict], None]] = None

        # Teljes kódbázis review 8. kör (2026-08-30), Metrológia/
        # Adatintegritás P2 lelet: l. a fenti _total_capacity_Ah fallback
        # docstringje ugyanerről a C10 vs. nominal kapacitásbázis-hibáról —
        # ez a két setpoint ugyanúgy a C10-bázist kell, hogy használja.
        self._discharge_current_A: float = (
            profile._c10_base_Ah / config.discharge_rate_divisor
        )

        self._impulse_current_A_set: float = (
            profile._c10_base_Ah / config.impulse_current_rate_divisor
        )

    # ------------------------------------------------------------------ #
    # Nyilvános tulajdonságok                                              #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> OcvSocState:
        return self._state

    @property
    def phase_elapsed_s(self) -> float:
        """CF-19 (governance-v2 kör 2, DFAM-023): mennyi ideje van a
        controller a JELENLEGI belső állapotban (self._state) -- a
        `_enter()` minden átmenetkor nullázza, `advance()` minden ticken
        növeli. Cél: a GUI/report.json "Lépés idő"/aktív fázis kijelzése
        ne a teljes, akár több tíz órás opak session-időt mutassa egy
        belső állapotváltás után, hanem a ténylegesen aktuális fázisban
        eltöltött időt. L. sibling docstring: chemid_controller.py,
        golden_gg_controller.py, traditional_learning_controller.py."""
        return self._phase_elapsed_s

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def soc_index(self) -> float:
        """A legutóbb elért/naplózott SOC racspont (current_logged_soc_percent)."""
        return self._grid[self._grid_pos]

    @property
    def next_target_soc_percent(self) -> Optional[float]:
        """SUP01-GRID-005: a következő célpont — külön fogalom a soc_index-től."""
        if self._grid_pos + 1 < len(self._grid):
            return self._grid[self._grid_pos + 1]
        return None

    @property
    def capacity_basis_Ah(self) -> float:
        return self._total_capacity_Ah

    @property
    def capacity_basis_quality(self) -> str:
        return self._capacity_basis_quality

    @property
    def removed_Ah_total(self) -> float:
        return self._removed_Ah_total

    @property
    def charge_config(self) -> ChargeConfig:
        """Teljes kódbázis review 16. kör (2026-09-04), Session/Workflow
        P2 lelet: a testvér GoldenGgController/TraditionalLearningController/
        ChemIdCharacterizationController már exponálja ezt a property-t
        (DATA-006) — az OcvSocController-ről hiányzott, holott a PRECHARGE
        fázisban ténylegesen ugyanezt a self._charge_config-ot használja a
        beágyazott ChargeController felépítésekor. Enélkül
        main_window.py::_write_report() charge_config_snapshot mezője
        OCV_SOC session-nél sosem töltődött ki."""
        return self._charge_config

    @property
    def accumulated_charge_Ah(self) -> float:
        """l. _energy_integrator docstring-jét __init__-ben — a TestRunner
        _flush_totals()-je ezt olvassa a report.json/checkpoint
        total_charge_ah mezőjéhez."""
        return self._energy_integrator.accumulated_charge_Ah

    @property
    def accumulated_discharge_Ah(self) -> float:
        return self._energy_integrator.accumulated_discharge_Ah

    @property
    def i_load_measured_A(self) -> float | None:
        """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        l. discharge_controller.py::i_load_measured_A property docstringje.
        Kizárólag STEP_DISCHARGE fázisban íródik -- az IMPULSE_* fázisokban
        None marad (l. __init__ kommentje)."""
        return self._i_load_measured_A

    @property
    def load_current_readback_valid(self) -> bool | None:
        """L. i_load_measured_A docstringje."""
        return self._load_current_readback_valid

    @property
    def accumulated_charge_Wh(self) -> float:
        """Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        + Metrológia P2 lelet: az accumulated_charge_Ah Wh-párja — l.
        golden_gg_controller.py azonos property docstringje ugyanerről a
        hiányzó mintáról."""
        return self._energy_integrator.accumulated_charge_Wh

    @property
    def accumulated_discharge_Wh(self) -> float:
        return self._energy_integrator.accumulated_discharge_Wh

    @property
    def extreme_dt_s_count(self) -> int:
        """Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        P2 lelet — l. charge_controller.py azonos property docstringje."""
        return self._energy_integrator.extreme_dt_s_count

    @property
    def max_observed_dt_s(self) -> float:
        return self._energy_integrator.max_observed_dt_s

    @property
    def relax_dmm_fault_total(self) -> int:
        """Teljes kódbázis review 14. kör (2026-09-04), Szoftver/
        Architektúra + Session/Workflow/Állapotgép P2 lelet: a relax-
        fázisok (PRECHARGE_RELAX/STEP_RELAX) alatti feszültség-DMM-
        olvasási hibák session-szintű, fázisváltáskor NEM nullázódó
        összege — l. chemid_controller.py/golden_gg_controller.py/
        traditional_learning_controller.py azonos property docstringje.
        L. main_window.py::_write_report() ocv_soc_relax_dmm_fault_total
        mezője."""
        return self._relax_dmm_fault_total

    @property
    def point_quality_counts(self) -> dict[str, int]:
        """Kor 18, Batch C12 (METRO-P2-1, DFAM-021, R17-12 mintajat
        koveteve) -- l. __init__ mezokommentje. Session-szintu osszesites
        a per-pont measurement_quality-rol (quality-string -> darabszam),
        amit main_window.py session_meta-ba, majd report_generator.py a
        report.json-ba tovabbit."""
        return dict(self._point_quality_counts)

    @property
    def u_psu_set_V(self) -> float:
        """Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        P2 lelet: a GoldenGgController/TraditionalLearningController már
        kapott ilyen delegáló property-t (2026-08-23) — az OcvSocController
        (PRECHARGE alatt a PSU-t a beágyazott ChargeController vezérli)
        hiányzott, emiatt a ténylegesen parancsolt PSU-feszültség setpoint
        sosem volt rekonstruálható a samples.csv-ből OCV_SOC session-öknél."""
        return self._charge_ctrl.u_psu_set_V if self._charge_ctrl is not None else 0.0

    @property
    def last_integration_source(self) -> str:
        """Teljes kódbázis review 11. kör (2026-08-31), Szoftver/Architektúra
        P3 lelet — l. u_psu_set_V docstringje ugyanerről a hiányzó proxy-
        mintáról. test_runner.py::_build_sample() sample["integration_
        current_source"]-je enélkül mindig None maradt PRECHARGE alatt."""
        # R6-B3 (R6-metrology-01, DFAM-001): a kompozit SAJAT, tick-scoped
        # rogzitett erteke, nem a gyermek-ChargeController elo proxyja.
        # Szerzodes: (1) advance() eleje: "ZERO" (kompozit tick eleji
        # reset); (2) kisutesi integralasi pont: "LOAD_READBACK" (mert
        # nulla aramnal is); (3) toltesi ag: a gyermek MEGORZOTT (sticky)
        # forrasanak atvetele a gyermek advance()-a UTAN, az elengedese
        # ELOTT. A (3) altal masolt "PSU_READBACK" onmagaban NEM bizonyit
        # friss integralast vagy nem nulla deltat ezen a ticken.
        return self._last_integration_source

    @property
    def taper_timer_s(self) -> float:
        """Teljes kódbázis review 12. kör (2026-08-31), Szoftver/Architektúra
        P3 lelet — ugyanaz a hiányzó proxy-property minta, mint fent a
        last_integration_source-nál (11. kör). test_runner.py::_build_sample()
        sample["taper_timer_s"]-je enélkül mindig None maradt PRECHARGE
        alatt, holott a beágyazott ChargeController ténylegesen futtatja a
        taper-hold hiszterézis logikát (6. kör, b73bd78)."""
        return self._charge_ctrl.taper_timer_s if self._charge_ctrl is not None else 0.0

    @property
    def last_fast_loop_extra_s(self) -> float:
        """Review-kör 5, Metrológia M5-01 (P1) -- ugyanaz a hiányzó proxy-
        property minta, mint fent a taper_timer_s-nél. A TestRunner ezt a
        beágyazott ChargeController fast-loop mechanizmusa (l.
        charge_controller.py azonos property docstringje) által MÁR
        elintegrált extra időt olvassa ki, hogy a KÖVETKEZŐ tick dt_s-éből
        levonja (elkerülve a duplán-integrálást a PRECHARGE fázisban)."""
        return self._charge_ctrl.last_fast_loop_extra_s if self._charge_ctrl is not None else 0.0

    @property
    def i_psu_set_A(self) -> float:
        """Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        P2 lelet — l. u_psu_set_V docstringje ugyanerről a delegáló mintáról,
        most a PSU áram-setpointra (psu_set_current_A naplómező)."""
        return self._charge_ctrl.i_psu_set_A if self._charge_ctrl is not None else 0.0

    @property
    def i_psu_measured_A(self) -> float | None:
        """Review-kör 6 (2026-09-15), C-08a — l. ChargeController.
        i_psu_measured_A/psu_current_readback_valid docstringje. `None`,
        ha nincs beágyazott ChargeController (test_runner.py ekkor a
        nyers read_all() readbackre esik vissza)."""
        return self._charge_ctrl.i_psu_measured_A if self._charge_ctrl is not None else None

    @property
    def psu_current_readback_valid(self) -> bool | None:
        """Review-kör 6 (2026-09-15), C-08a — l. i_psu_measured_A fenti
        testvér-property docstringje."""
        return self._charge_ctrl.psu_current_readback_valid if self._charge_ctrl is not None else None

    @property
    def u_psu_measured_V(self) -> float | None:
        """Review-kör 6 (2026-09-15), C-08b — l. ChargeController.
        u_psu_measured_V/psu_voltage_readback_valid docstringje."""
        return self._charge_ctrl.u_psu_measured_V if self._charge_ctrl is not None else None

    @property
    def psu_voltage_readback_valid(self) -> bool | None:
        """Review-kör 6 (2026-09-15), C-08b — l. u_psu_measured_V fenti
        testvér-property docstringje."""
        return self._charge_ctrl.psu_voltage_readback_valid if self._charge_ctrl is not None else None

    @property
    def u_batt_V(self) -> float:
        """Teljes kódbázis review 6. kör (2026-08-29), Session/Workflow P1
        lelet: a testvér ChargeController/DischargeController/GoldenGgController/
        TraditionalLearningController már exponálja ezt a property-t — az
        OcvSocController hiányzott, emiatt test_runner.py::_build_sample()
        getattr(controller, "u_batt_V", None)-je mindig None-t adott vissza,
        és InstrumentManager.read_all() MÁSODSZOR is lekérdezte ugyanazt a
        fizikai feszültség-DMM-et minden ticken — CLAUDE.md §9 "egy fizikai
        DMM-et egy tickben egy kijelölt komponens olvasson" megsértése,
        pontosan a 2026-08-23 traditional_learning OV-incidens után a
        testvér controllereknél már javított hibaosztály."""
        return self._u_batt

    @property
    def battery_temperature_C(self) -> float:
        """L. u_batt_V docstringje ugyanerről a hiányzó proxy-mintáról,
        ugyanaz a dupla-olvasás vonatkozik a hőmérséklet-DMM-re is."""
        return self._battery_temperature_C

    @property
    def dmm_valid(self) -> bool:
        """Teljes kódbázis review 6. kör (2026-08-29), Session/Workflow P2
        lelet #4: l. DischargeController.dmm_valid docstringje ugyanerről
        a hiányzó property-mintáról — test_runner.py::_build_sample()
        dmm_voltage_valid mezője enélkül a gyakorlatilag mindig True
        fallback-ra (`u_batt is not None`) esett vissza.

        R1 B8b, F-23 (P2, DFAM-001; D-B8-7): egy IMPULSE_* állapotban
        INDULT tickben a controller nem olvasta a DMM-et (a
        PulseMeasurementService a tulajdonos), az u_batt_V nem e tick saját
        olvasása (a pulzus előtti utolsó érték, ill. a befejező tickben a
        _finish_impulse_acquisition() által beírt utolsó, terhelt
        pulzusminta) -- ekkor konkrét False (nem None: a runner
        `u_batt is not None` fallbackje True-t adna). A befejező átmeneti
        tick (IMPULSE_* -> LOG_SOC_POINT) is False (konzervatív); a
        következő normál tick saját olvasása újra érvényesít. A privát
        _dmm_valid (belső safety-kapuk) érintetlen."""
        if self._dmm_owned_by_pulse_service_this_tick:
            return False
        return self._dmm_valid

    @property
    def temp_dmm_valid(self) -> bool:
        """Review-kör 3 (2026-09-09), friss Metrológia M3-01 (P1, DFAM-001+
        DFAM-018) -- l. discharge_controller.py azonos property docstringje
        a dmm_valid (feszültség) testvér-mintájáról."""
        return self._temp_dmm_valid

    @property
    def temp_dmm_fault_ticks(self) -> int:
        """Review-kör 3 (2026-09-09), friss Metrológia M3-02 / Szoftver/
        Architektúra F2 (P2) -- l. chemid_controller.py azonos property
        docstringje ugyanerről a session-szintű, tick-alapú számlálóról.
        L. main_window.py::_write_report() ocv_soc_temp_dmm_fault_ticks
        mezője. MAX verifier (a306e7e764e3a717b) P2-1, DFAM-001 további
        előfordulása: a beágyazott ChargeController (PRECHARGE alatt)
        saját összegét is hozzáadja, a temperature_out_of_range_count
        property mintáját követve."""
        return self._temp_dmm_fault_ticks + (
            self._charge_ctrl.temp_dmm_fault_ticks
            if self._charge_ctrl is not None else 0
        )

    @property
    def load_current_setpoint_warning_ticks(self) -> int:
        """MAX verifier (review-kör 4, HW4-02) N1 lelete -- l.
        discharge_controller.py azonos property docstringje."""
        return self._load_current_setpoint_warning_ticks

    @property
    def i_load_set_A(self) -> float:
        """Q-40/B: impulzusnál sikeres ON-parancsvisszatérés után, OFF-ig.

        Ez aktív parancsablakhoz kötött setpoint, nem mért áram vagy fizikai
        ON-readback. A saját blokk közben nincs külső runner-mintasor; az
        ON/OFF-időket az OCV_SOC_PULSE_WINDOW esemény őrzi.
        """
        if self._state == OcvSocState.STEP_DISCHARGE:
            # Függő setupnál még nem történt Load-áramparancs.
            return self._discharge_current_A if self._step_discharge_started else 0.0
        if self._state in (
            OcvSocState.IMPULSE_ON, OcvSocState.IMPULSE_ACQUIRE,
            OcvSocState.IMPULSE_FAST_ON, OcvSocState.IMPULSE_FAST_ACQUIRE,
        ):
            service = (
                self._fast_pulse_service
                if self._state in (OcvSocState.IMPULSE_FAST_ON, OcvSocState.IMPULSE_FAST_ACQUIRE)
                else self._pulse_service
            )
            if (
                service is not None and service._on_return_at is not None
                and service._off_return_at is None and service._load_off_real_s is None
            ):
                return self._impulse_current_A_set
        return 0.0

    @property
    def last_warning_code(self) -> str:
        """Teljes kódbázis review 9. kör (2026-08-30), Szoftver/Architektúra
        P2 lelet — l. _last_warning_code docstringje az __init__-ben. A
        PRECHARGE alatt a beágyazott ChargeController saját warning-ja
        élvez elsőbbséget (pl. dióda-disszipáció), egyébként a saját szintű
        hőmérséklet-DMM warning."""
        if self._charge_ctrl is not None and self._charge_ctrl.last_warning_code:
            return self._charge_ctrl.last_warning_code
        return self._last_warning_code

    @property
    def temperature_out_of_range_count(self) -> int:
        """Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        lelet — l. charge_controller.py azonos property docstringje. A
        beágyazott ChargeController (PRECHARGE alatt) saját összegét is
        hozzáadja — a számláló, ellentétben a last_warning_code-dal,
        ADDITÍV a session teljes hosszán."""
        return self._temperature_out_of_range_count + (
            self._charge_ctrl.temperature_out_of_range_count
            if self._charge_ctrl is not None else 0
        )

    # ------------------------------------------------------------------ #
    # Fő advance() — egy vezérlési ciklus                                  #
    # ------------------------------------------------------------------ #

    def advance(self, dt_s: float) -> OcvSocState:
        # R2-06 (2026-09-15): tick-scoped validity flag -- MINDEN korai-
        # return és safety-kapu ELŐTT nullázva (l. feedback_new_tick_
        # scoped_validity_flag_needs_explicit_reset.md).
        self._i_load_measured_A = None
        self._load_current_readback_valid = None
        # R6-B3: kompozit tick eleji reset (l. last_integration_source);
        # a kisutesi integralasi pont, ill. a toltesi ag atvetele irja felul.
        self._last_integration_source = "ZERO"
        self._pulse_observed_extra_s = 0.0
        # R1 B8b, F-23: ugyanez a tick-scoped nullázás (terminális return
        # ELŐTT), utána a tick KEZDŐ állapota dönt.
        self._dmm_owned_by_pulse_service_this_tick = False

        if self._state in (OcvSocState.DONE, OcvSocState.FAULT):
            return self._state

        self._dmm_owned_by_pulse_service_this_tick = (
            self._state in _PULSE_SERVICE_DMM_OWNER_STATES
        )

        # CF-19: mennyi ideje van a controller a JELENLEGI belső állapotban
        # -- l. self._enter()/self._phase_elapsed_s docstringje.
        #
        # MAX verifier V-4 megjegyzése (2026-09-15, C-06/DFAM-035 után):
        # ez a `dt_s` a NYERS, outer értéket kapja -- a beágyazott
        # ChargeController fast-loop extra idejét (last_fast_loop_extra_s)
        # NEM kapja meg, ellentétben a lent, `_run_precharge()`-ben a
        # self._energy_integrator-nak átadott dt_s+extra-val (C-06 fix).
        # Emiatt a samples.csv-ben `phase_elapsed_s` és `accumulated_
        # charge_Ah` oszlopa charge-fázisban EGYMÁSTÓL ELTÉRŐ időalapot
        # tükröz (a mért áram / Δphase_elapsed_s hányados a fast-loop
        # aktív tickjein a tényleges áramnál MAGASABBNAK látszik). TUDATOS
        # döntés, NEM javítva ebben a körben: egy szimmetrikus fix itt (a
        # metódus ELEJÉN, a beágyazott charge_ctrl.advance() hívása ELŐTT)
        # egy ELAVULT last_fast_loop_extra_s értéket olvasna (az EZEN a
        # ticken frissül, csak a charge_ctrl.advance() hívása UTÁN) --
        # helyes javításhoz a hívási sorrend átrendezése kellene, ami
        # nagyobb, önálló mérlegelést igényel. Nem safety-kritikus (egyetlen
        # `_phase_elapsed_s`-alapú kapu sincs charge-állapotban, l. C-06
        # CLAIM_LEDGER bejegyzés), csak riport-pontossági korlátozás.
        self._phase_elapsed_s += dt_s

        self._last_warning_code = ""

        # PRECHARGE alatt a belső ChargeController olvassa a DMM-et — ha itt is
        # olvasnánk, egy ticken belül két DMM-mérés futna, ami a TF1 panel FET
        # átmeneti nyitása esetén hamis feszültségugrást (DMM_FEEDBACK_LOST)
        # okozott (l. dmm_feedback_lost_hibaelemzes_20260628.txt). Ugyanezen okból
        # IMPULSE_* alatt a PulseMeasurementService a feszultseg-DMM
        # egyetlen olvasoja (SUP01-ARCH-002); a homersekletet itt olvassuk.
        # R11-B4: a pulzusszolgaltatas csak a feszultseg-DMM tulajdonosa.
        if (
            self._state in _PULSE_SERVICE_DMM_OWNER_STATES
            and not self._check_pulse_temperature(dt_s)
        ):
            return self._state

        if (
            self._state != OcvSocState.PRECHARGE
            and self._state not in _PULSE_SERVICE_DMM_OWNER_STATES
        ):
            self._dmm_valid_before_tick = self._dmm_valid
            self._dmm_valid = self._read_dmm(dt_s)
            # A rendes vég után fennmaradó terhelés nem kap újabb felmentést.
            if self._normal_discharge_cutoff_seen and self._load.input_commanded_on:
                if not self._dmm_valid:
                    self.emergency_stop("DMM_FEEDBACK_LOST")
                    return self._state
                if not self._safety.enforce_normal_discharge(
                    self._u_batt, self._discharge_current_A, self._profile.terminate_voltage_pack_V,
                    self.emergency_stop, self.on_event,
                ):
                    return self._state
            # Teljes kódbázis review 2. kör (2026-08-28), Session/Workflow P1
            # lelet: a kiesesi ido _read_dmm()-ben MÁR nőtt, de
            # sosem került kiértékelésre — egy hőmérséklet-DMM kommunikációs
            # kiesés a lépcsőzött kisütés/relax alatt teljesen észrevétlen
            # maradt volna (CLAUDE.md §10.4). Ugyanaz a minta, mint
            # golden_gg_controller.py::advance() (release-audit 2026-08-23 2.12)
            # — ez az ág a _read_dmm()-et futtató állapotokhoz tartozik.
            # PRECHARGE alatt a gyermek olvas; IMPULSE_* alatt a feszültség-DMM
            # egyetlen olvasója a PulseMeasurementService.
            t_result = self._safety.check_temperature_dmm_fault(
                self._safety.temperature_dmm_fault_s,
            )
            if t_result.fault is not None:
                self.emergency_stop(t_result.fault.name)
                return self._state
            if t_result.warning is not None:
                self._last_warning_code = t_result.warning.name

            # Teljes kódbázis review 20. kör (2026-09-06), Metrológia/
            # Adatintegritás P2 lelet — l. charge_controller.py azonos
            # hivatkozású kommentje.
            # Teljes kódbázis review 21. kör (2026-09-06), GUI/UX + Metrológia
            # P2 lelet — l. charge_controller.py azonos hivatkozású
            # kommentje (a DMM-kiesés jelzése elsőbbséget élvez).
            # R11-B4: az aktiv kisutes ugyanazt a kozos kaput hasznalja.
            if self._state == OcvSocState.STEP_DISCHARGE:
                overtemp = self._safety.check_battery_overtemperature(
                    self._battery_temperature_C, self._temp_dmm_valid,
                )
                if overtemp.fault is not None:
                    self.emergency_stop(overtemp.fault.name)
                    return self._state

            range_result = self._safety.check_test_temperature_range(self._battery_temperature_C)
            if range_result.warning is not None:
                self._temperature_out_of_range_count += 1
                if t_result.warning is None:
                    self._last_warning_code = range_result.warning.name

        if self._state == OcvSocState.INIT:
            self._enter_precharge()

        elif self._state == OcvSocState.PRECHARGE:
            self._run_precharge(dt_s)

        elif self._state in (OcvSocState.PRECHARGE_RELAX, OcvSocState.STEP_RELAX):
            self._run_relax_phase(dt_s)

        elif self._state == OcvSocState.STEP_DISCHARGE:
            self._run_step_discharge(dt_s)

        elif self._state == OcvSocState.IMPULSE_PREP:
            self._run_impulse_prep()

        elif self._state == OcvSocState.IMPULSE_ON:
            self._run_impulse_on()

        elif self._state == OcvSocState.IMPULSE_ACQUIRE:
            self._run_impulse_acquire()

        elif self._state == OcvSocState.IMPULSE_FAST_ON:
            self._run_impulse_fast_on()

        elif self._state == OcvSocState.IMPULSE_FAST_ACQUIRE:
            self._run_impulse_fast_acquire()

        elif self._state == OcvSocState.LOG_SOC_POINT:
            self._run_log_soc_point()

        else:
            # Review-kör 5, Session/Workflow S5-05 (P3, DFAM-004 sibling) --
            # l. charge_controller.py::advance() azonos hivatkozású
            # kommentje ugyanerről a hibaosztályról. A DONE/FAULT terminális
            # állapotokat a függvény ELEJÉN lévő korai return már lefedi.
            raise ValueError(f"Unhandled OcvSocState in advance(): {self._state!r}")

        return self._state

    def _check_pulse_temperature(self, dt_s: float) -> bool:
        self._read_temperature_only(dt_s)
        t_result = self._safety.check_temperature_dmm_fault(
            self._safety.temperature_dmm_fault_s,
        )
        if t_result.fault is not None:
            self.emergency_stop(t_result.fault.name)
            return False
        if t_result.warning is not None:
            self._last_warning_code = t_result.warning.name
        overtemp = self._safety.check_battery_overtemperature(
            self._battery_temperature_C, self._temp_dmm_valid,
        )
        if overtemp.fault is not None:
            self.emergency_stop(overtemp.fault.name)
            return False
        # R12-B1: a friss pulzushomerseklet is kap tartomanyjelzest.
        if self._temp_dmm_valid:
            range_result = self._safety.check_test_temperature_range(
                self._battery_temperature_C,
            )
            if range_result.warning is not None:
                self._temperature_out_of_range_count += 1
                if t_result.warning is None:
                    self._last_warning_code = range_result.warning.name
        return True

    def prepare_pulse_block(self) -> None:
        service = None
        if self._state == OcvSocState.IMPULSE_ON:
            service = self._pulse_service
        elif self._state == OcvSocState.IMPULSE_FAST_ON:
            service = self._fast_pulse_service
        if service is not None:
            self._pulse_observation_origin_s = service._time_source()
            service.prepare_owned_acquisition()

    @property
    def last_pulse_observed_extra_s(self) -> float:
        """A belső T-felügyelet által már megfigyelt idő, a tail/cleanup nélkül."""
        return self._pulse_observed_extra_s

    def take_pulse_warnings(self) -> dict[str, int]:
        """Kivételes lezáráskor is egyszer átvehető blokk-warningok, mint IEC-n."""
        warnings, self._pulse_block_warnings = self._pulse_block_warnings, {}
        return warnings

    def run_pulse_block(
        self, poll_fault: Callable[[bool, Callable[[], bool]], str | None],
    ) -> dict[str, int] | None:
        """Szűk saját ciklus a runner szálán; a külső tick itt nem időalap."""
        service: PulseMeasurementService | None
        if self._state == OcvSocState.IMPULSE_ACQUIRE:
            service = self._pulse_service
            acquire = self._run_impulse_acquire
        elif self._state == OcvSocState.IMPULSE_FAST_ACQUIRE:
            service = self._fast_pulse_service
            acquire = self._run_impulse_fast_acquire
        else:
            return None
        assert service is not None
        if service._done or not service._started:
            # Az ON-visszatérés alatti stop még a régi, blokk nélküli tick útja.
            return None
        state = self._state
        block_started = service._time_source()
        observation_origin = getattr(self, "_pulse_observation_origin_s", block_started)
        last_temperature = observation_origin
        temperature_observed = False
        warnings = self._pulse_block_warnings = (
            {self._last_warning_code: 1} if self._last_warning_code else {}
        )
        first_poll = True
        last_temperature_status: tuple[bool, str, str] | None = None

        def supervise() -> bool:
            nonlocal first_poll, last_temperature, temperature_observed, last_temperature_status
            if self._state == OcvSocState.FAULT:
                return False
            concurrent = self._safety.check_concurrent_psu_load(
                psu_commanded_on=self._psu.output_commanded_on,
                load_commanded_on=self._load.input_commanded_on,
            )
            if concurrent.fault is not None:
                self.emergency_stop(concurrent.fault.name)
                return False
            # A PMS az élő stopot és a határidőt minden callback előtt vizsgálja.
            now = service._time_source()
            if now - last_temperature >= 0.1:
                dt_s = now - last_temperature
                self._last_warning_code = ""
                if not service.allow_owned_io():
                    return False
                last_temperature = now
                temperature_observed = True
                temperature_ok = self._check_pulse_temperature(dt_s)
                if self._last_warning_code:
                    warnings[self._last_warning_code] = warnings.get(self._last_warning_code, 0) + 1
                temperature_status = (
                    self._temp_dmm_valid, self._last_warning_code, self._fault_reason,
                )
                if self.on_event is not None and temperature_status != last_temperature_status:
                    last_temperature_status = temperature_status
                    self.on_event({
                        "event_code": "OCV_SOC_PULSE_TEMPERATURE",
                        "event_message": (
                            f"T={self._battery_temperature_C} C; valid={self._temp_dmm_valid}; "
                            f"warning={self._last_warning_code}; fault={self._fault_reason}"
                        ),
                    })
                if not temperature_ok:
                    return False
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return True
            if not service.allow_owned_io():
                return False
            reason = poll_fault(first_poll, service.allow_owned_io)
            first_poll = False
            if self._state == OcvSocState.FAULT or not service.allow_owned_io():
                return False
            if reason is not None:
                self.emergency_stop(reason)
                return False
            return True

        service.begin_owned_acquisition(
            supervise, lambda: self._cleanup_pulse_nplc(service),
        )
        try:
            while self._state == state:
                acquire()
                if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                    break
        finally:
            self._pulse_observed_extra_s = (
                max(0.0, last_temperature - observation_origin) if temperature_observed else 0.0
            )
        return warnings

    def _emit_pulse_window(self, service: PulseMeasurementService, label: str) -> None:
        diagnostic = service.load_window_diagnostic
        if diagnostic is not None and self.on_event is not None:
            self.on_event({
                "event_code": "OCV_SOC_PULSE_WINDOW",
                "event_message": f"{label}: {diagnostic}",
            })

    # ------------------------------------------------------------------ #
    # CF-19 (governance-v2 kör 2, DFAM-023): egységes állapotváltás-helper #
    # ------------------------------------------------------------------ #

    def _enter(self, state: OcvSocState) -> None:
        """Minden `self._state = X` átmenet EZEN keresztül megy -- a
        `_phase_elapsed_s`-t nullázza, hogy az `advance()`-ban minden
        ticken végzett `+= dt_s` mindig a JELENLEGI belső fázisban eltöltött
        időt adja, ne a teljes (akár több tíz órás) opak session-idő
        maradékát. Tisztán bookkeeping -- nem hív semmilyen egyéb
        mellékhatást (l. sibling docstring: chemid_controller.py,
        golden_gg_controller.py, traditional_learning_controller.py)."""
        self._state = state
        self._phase_elapsed_s = 0.0

    # ------------------------------------------------------------------ #
    # Vészleállítás [R1] — LOAD OFF → PSU OFF, relay soha                  #
    # ------------------------------------------------------------------ #

    def emergency_stop(self, reason: str) -> None:
        # Teljes kódbázis review 7. kör (2026-08-30), Hardver/Műszervezérlés
        # P2 lelet — l. charge_controller.py::emergency_stop() docstringje
        # ugyanerről a csendben elnyelt OFF-kísérlet-hiba mintáról.
        # R2-B4: az OFF-ok ELŐTT csak tiszta állapotolvasás, I/O nincs.
        pulse_floor_event = self._state in _PULSE_SERVICE_DMM_OWNER_STATES
        aborted_pulse_state = (
            self._state
            if self._state in (OcvSocState.IMPULSE_ACQUIRE, OcvSocState.IMPULSE_FAST_ACQUIRE)
            else None
        )
        aborted_start_state = (
            self._state
            if self._state in (OcvSocState.IMPULSE_ON, OcvSocState.IMPULSE_FAST_ON) else None
        )
        off_failures: list[str] = []
        pulse_floor_reason = (
            reason if reason.startswith((
                "BATTERY_UNDERVOLTAGE", "DISCHARGE_CONFIG_INVALID", "HARDWARE_PULSE_HIL_PENDING",
            )) else None
        )
        for service in (self._pulse_service, self._fast_pulse_service):
            if service is not None:
                service.cancel_acquisition()
                service.note_load_off_command()
        try:
            self._load.input_off()
        except Exception as exc:
            off_failures.append(f"LOAD_OFF_FAILED:{exc}")
        else:
            for service in (self._pulse_service, self._fast_pulse_service):
                if service is not None:
                    service.note_load_off_return()
        try:
            self._psu.all_outputs_off()
        except Exception as exc:
            off_failures.append(f"PSU_OFF_FAILED:{exc}")
        # 2026-09-12, FAST_TRANSIENT NPLC-javítás (rules/10-safety.md 9.:
        # "Cleanup/restore minden terminális úton vizsgálandó"): a
        # fast-transient impulzus TÖBB advance() ticken át tart, és a
        # session a step_chunk()/finalize() cikluson KÍVÜLI okból is
        # megszakadhat (PSU/Load kölcsönös kizárás, hőmérséklet-fault, vagy
        # a TestRunner felőli külső emergency_stop) — ilyenkor a
        # PulseMeasurementService soha nem kapna több hívást, és a DMM az
        # ideiglenes, gyors (zajos, hálózati zajelnyomás NÉLKÜLI)
        # integrációs időn maradna a session hátralévő részére.
        # SZÁNDÉKOSAN a két safe-off UTÁN fut: az energiaátadás megszüntetése
        # elsőbbséget élvez, és egy DMM-hiba itt nem akadályozhatja azt
        # (CLAUDE.md §6: "Egy OFF-hiba ne akadályozza a másik
        # megkísérlését"). Idempotens, no-op, ha nem volt átállítás.
        self._fault_reason = reason if not off_failures else reason + " | " + " | ".join(off_failures)
        self._enter(OcvSocState.FAULT)
        cleanup_error: BaseException | None = None
        try:
            # Mindkét service cleanupja a két OFF és a FAULT után fut.
            for service, label in (
                (self._pulse_service, "STANDARD_PULSE"),
                (self._fast_pulse_service, "FAST_TRANSIENT"),
            ):
                if service is None:
                    continue
                try:
                    service.restore_acquisition_nplc()
                except PulseDischargeError as exc:
                    self._fault_reason += f" | {label}_PULSE_DRAIN_FAILED:{exc}"
                except Exception as exc:
                    self._fault_reason += f" | {label}_DMM_CLEANUP_FAILED:{exc}"
                finally:
                    if service.nplc_restore_failed:
                        self._fault_reason += f" | {label}_NPLC_RESTORE_FAILED"
                    # A drain/első OFF hibája sem fedheti el a GOV-029 nyers bizonyítékát.
                    if service._discharge_fault is not None:
                        floor_reason = str(service._discharge_fault)
                        if floor_reason not in self._fault_reason:
                            self._fault_reason += f" | {label}_PULSE_DRAIN_FAILED:{floor_reason}"
                        if pulse_floor_reason is None and floor_reason.startswith((
                            "BATTERY_UNDERVOLTAGE", "DISCHARGE_CONFIG_INVALID", "HARDWARE_PULSE_HIL_PENDING",
                        )):
                            pulse_floor_reason = floor_reason
                    if service.acquisition_cleanup_diagnostic:
                        self._fault_reason += f" | {label}_DMM_CLEANUP:{service.acquisition_cleanup_diagnostic}"
                    ack = service.activation_ack_diagnostic
                    if ack is not None and ack not in self._fault_reason:
                        self._fault_reason += f" | {ack}"
        except BaseException as exc:
            cleanup_error = exc
            raise
        finally:
            try:
                if (
                    pulse_floor_event
                    and pulse_floor_reason is not None
                    and self.on_event is not None
                ):
                    self.on_event({
                        "event_code": pulse_floor_reason.split(":", 1)[0],
                        "event_message": self._fault_reason,
                        "severity": "CRITICAL",
                    })
            except BaseException as event_error:
                if cleanup_error is None:
                    raise
                cleanup_error.add_note(f"PULSE_FLOOR_EVENT_FAILED:{event_error}")
            finally:
                try:
                    # A cleanup BaseException-je sem veszítheti el a mért prefixet.
                    if aborted_pulse_state is not None:
                        self._book_aborted_pulse_prefix(aborted_pulse_state)
                    elif aborted_start_state is not None:
                        service = (
                            self._fast_pulse_service
                            if aborted_start_state == OcvSocState.IMPULSE_FAST_ON else self._pulse_service
                        )
                        # A begin előtti kapu hibája nem az előző impulzus auditja.
                        if service is not None and not service._owned_requested:
                            self._emit_pulse_window(service, aborted_start_state.value)
                except BaseException as booking_error:
                    if cleanup_error is None:
                        raise
                    cleanup_error.add_note(f"PULSE_PREFIX_BOOKING_FAILED:{booking_error}")

    def _book_aborted_pulse_prefix(self, aborted_pulse_state: OcvSocState) -> None:
        """R2-B4 (R2-metrology-01, DFAM-033), csak az emergency_stop()-ból, a
        két OFF és a FAULT UTÁN. A PMS a cutoff-/mintaszám-ági terminális
        input_off() ELŐTT rögzíti a leolvasást -- ha ez az OFF (vagy a chunk
        bármely későbbi lépése) hibázik, a már rögzített leolvasás eddig
        könyveletlen maradt (Codex 7. pont, C1). Itt az aktív szolgáltatás
        érvényesen rögzített prefixe idempotensen átkerül (ismételt hívásnál
        a delta 0); finalize() és farokbecslés NINCS. Ezután egyszeri
        OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY esemény -- ha a callback dob, az
        OFF-ok, a FAULT és a könyvelés már megtörtént.
        Mid-pulse checkpointból resume NINCS (safe_boundary=False, a
        session_recovery blokkolja) -- ez a könyvelés ezen nem változtat."""
        if aborted_pulse_state == OcvSocState.IMPULSE_FAST_ACQUIRE:
            service = self._fast_pulse_service
        else:
            service = self._pulse_service
        if service is None:
            return
        self._book_measured_pulse_energy(service)
        if self.on_event is None:
            return
        booked = self._pulse_energy_booked
        self._emit_pulse_window(service, aborted_pulse_state.value)
        self.on_event({
            "event_code": "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY",
            "severity": "WARNING",  # R3-B3 (DFAM-018): semleges kód, producer minősít
            "event_message": (
                f"Megszakított impulzus ({aborted_pulse_state.value}): a "
                f"session-összegben csak a rögzített leolvasásokig mért rész "
                f"szerepel: {booked.removed_Ah:.6f} Ah / {booked.removed_Wh:.6f} Wh "
                f"({booked.reading_count} áramleolvasás, utolsó "
                f"t={booked.last_reading_t_s:.3f} s). Az utolsó könyvelt "
                "leolvasás utáni szakasz (ha volt terhelés) NINCS benne, "
                "nincs becsülve."
                + (
                    " A könyvelt rész setpoint-áram fallbacket tartalmaz "
                    "(pulse_current_degraded)."
                    if booked.current_degraded else ""
                )
            ),
        })

    # ------------------------------------------------------------------ #
    # Állapot implementációk                                               #
    # ------------------------------------------------------------------ #

    def _enter_precharge(self) -> None:
        """INIT → PRECHARGE: belső ChargeController létrehozása."""
        from Prog.src.charge_controller import ChargeController
        self._charge_ctrl = ChargeController(
            self._psu,
            self._load,
            self._dmm_v,
            self._dmm_t,
            self._profile,
            self._safety,
            self._charge_config,
        )
        self._enter(OcvSocState.PRECHARGE)

    def _run_precharge(self, dt_s: float) -> None:
        from Prog.src.charge_controller import ChargeState
        assert self._charge_ctrl is not None
        # R1 B8a, F-20 (DFAM-035): a gyermek integrátorának állása az
        # advance() ELŐTT -- l. lent az add_charge_delta() kommentjét.
        charge_Ah_before = self._charge_ctrl.accumulated_charge_Ah
        charge_Wh_before = self._charge_ctrl.accumulated_charge_Wh
        try:
            # R16-B12: a beágyazott töltő is a runner élő leállításjelét figyeli.
            self._charge_ctrl.handoff_stop_requested = self.handoff_stop_requested
            charge_state = self._charge_ctrl.advance(dt_s)
        except BaseException:
            # R2-B5 (R2-session-02, DFAM-033): kivételes kilépéskor (pl.
            # KeyboardInterrupt a fast-loop alvásában) a gyermek által MÁR
            # elintegrált delta és a dt-diagnosztika egyszer itt kerül át --
            # csak property-olvasás és aritmetika, I/O nincs; az eredeti
            # kivétel csupasz raise-zel változatlanul továbbmegy. NEM
            # finally: a normál út lent adja át, finally-val duplázna.
            self._energy_integrator.add_charge_delta(
                self._charge_ctrl.accumulated_charge_Ah - charge_Ah_before,
                self._charge_ctrl.accumulated_charge_Wh - charge_Wh_before,
                dt_s + self._charge_ctrl.last_fast_loop_extra_s,
            )
            raise

        # Szinkronizálás: a ChargeController már olvasta a DMM-et ebben a tickben.
        self._dmm_valid = self._charge_ctrl.dmm_valid
        # Review-kör 3 (2026-09-09), MAX verifier (a3f0e6dcf93588360) P2-1:
        # a feszültség-ikret (fenti sor) mindig átvezettük a beágyazott
        # ChargeControllerből, a hőmérsékletit nem -- emiatt a
        # temp_dmm_valid property PRECHARGE alatt (ahol a saját
        # _read_dmm() ki van kapcsolva) hamisan True maradt egy
        # ténylegesen halott hőmérséklet-DMM mellett is. Az IMPULSE_* alatti
        # szándékos staleness csak a feszültség-DMM-re vonatkozik, l. MAX
        # verifier a306e7e764e3a717b P3-1 + _run_impulse_acquire() docstringje.
        self._temp_dmm_valid = self._charge_ctrl.temp_dmm_valid
        self._u_batt = self._charge_ctrl.u_batt_V
        self._battery_temperature_C = self._charge_ctrl.battery_temperature_C

        # R1 B8a, F-20 (DFAM-035, RECURRING; a C-06 fix utódja): a
        # beágyazott ChargeController fast-loop-ja (l. last_fast_loop_
        # extra_s property docstringje) ugyanabban az advance()-hívásban
        # TÖBB résztickben, résztickenként friss PSU-áram- és DMM-feszült-
        # ségolvasással integrál a SAJÁT integrátorába (MAX verifier V-7:
        # CV/TAPER_HOLD és a késői CHARGE_CC alatt, l. _fast_loop_active()).
        # A C-06 fix ehhez csak az IDŐT adta vissza (dt_s + extra), az áramot
        # és feszültséget viszont a tick UTOLSÓ résztickjéből vette -- változó
        # áramnál a session-szintű Ah/Wh ezért eltért a ténylegesen
        # elintegrálttól. Mostantól a gyermek EZEN a ticken elintegrált
        # Ah/Wh-deltája kerül át: a belső integrál az EGYETLEN numerikus
        # forrás (a TestRunner M5-01 extra-idő-kizárása így továbbra is
        # helyes, mert a kizárt időt a gyermek már elintegrálta). A delta a
        # FAULT- és a CHARGE_DONE-ág (gyermek eldobása) ELŐTT kerül át, így
        # egy részleges siker utáni FAULT-tick résztickjei sem vesznek el.
        # Egy kapu-FAULT ticken (R2-18) a gyermek nem integrál -> nulla
        # delta; a dt-diagnosztika (dt_s + extra) ekkor is vezetődik.
        # Ismert, dokumentált korlát: a CV/TAPER_HOLD-ban PSU-írási hibára
        # FAULT-oló résztick a gyermekben (és így itt is) nem integrálódik.
        self._energy_integrator.add_charge_delta(
            self._charge_ctrl.accumulated_charge_Ah - charge_Ah_before,
            self._charge_ctrl.accumulated_charge_Wh - charge_Wh_before,
            dt_s + self._charge_ctrl.last_fast_loop_extra_s,
        )
        # R6-B3: a gyermek megorzott forrasanak atvetele (nem friss-integralas-
        # bizonyitek), a gyermek elengedese ELOTT.
        self._last_integration_source = self._charge_ctrl.last_integration_source

        if charge_state == ChargeState.FAULT:
            self.emergency_stop(
                self._charge_ctrl.fault_reason or "PRECHARGE_FAULT"
            )
        elif charge_state == ChargeState.CHARGE_DONE:
            self._relax_elapsed_s = 0.0
            self._current_relax_s = self._config.relax_keypoint_s
            self._dmm_fault_during_relax = False
            self._enter(OcvSocState.PRECHARGE_RELAX)
            # Teljes kódbázis review 13. kör (2026-08-31), Metrológia/
            # Adatintegritás P2 lelet: a golden_gg_controller.py/
            # traditional_learning_controller.py mintáját követve a
            # beágyazott ChargeController-t itt is el KELL dobni — a
            # proxy-property-k (u_psu_set_V/i_psu_set_A/taper_timer_s)
            # mind `if self._charge_ctrl is not
            # None: ...` mintát követnek, tehát ha a referencia itt nem
            # nullázódik, a session hátralévő részében (STEP_DISCHARGE/
            # STEP_RELAX/IMPULSE_*/LOG_SOC_POINT) a PRECHARGE utolsó
            # tickjén befagyott, elévült értéket adnák vissza, holott a
            # PSU ténylegesen ki van kapcsolva.
            # R6-B3 (R6-metrology-01, DFAM-043 pontositas): a
            # last_integration_source R6-B3 ota NEM ilyen proxy, hanem a
            # kompozit sajat, tick-scoped attributuma (l. a property
            # kommentjet) -- ezen az elengedo ticken a gyermek fent
            # atvett, megorzott forrasat adja, a kovetkezo advance() eleji
            # reset utan "ZERO"; a lenti None-nullazas nem erinti.
            # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow
            # P2 lelet — l. chemid_controller.py azonos hivatkozású
            # kommentje: a fenti None-nullázás ELŐTT át kell menteni a
            # beágyazott számlálót, különben elveszne.
            self._temperature_out_of_range_count += (
                self._charge_ctrl.temperature_out_of_range_count
            )
            # Review-kör 3 (2026-09-09), MAX verifier (a306e7e764e3a717b)
            # P2-1 -- l. temp_dmm_fault_ticks property docstringje, ugyanaz
            # a carry-over minta, mint a fenti temperature_out_of_range_
            # count-nál.
            self._temp_dmm_fault_ticks += self._charge_ctrl.temp_dmm_fault_ticks
            self._charge_ctrl = None

    def _run_relax_phase(self, dt_s: float) -> None:
        """Közös relax logika PRECHARGE_RELAX és STEP_RELAX számára."""
        if not self._dmm_valid:
            self._dmm_fault_during_relax = True
            # Teljes kódbázis review 14. kör (2026-09-04) — session-szintű,
            # fázisváltáskor NEM nullázódó számláló (l. __init__), a
            # testvér controllerek relax_dmm_fault_total mintáját követve.
            self._relax_dmm_fault_total += 1
            # Governance-v2 review-kör 1 (CF-07, DFAM-011, batch 4.2): a
            # RelaxController RELAX_DMM_FAULT eseményének megfelelő, itt
            # korábban hiányzó élő jelzés — l. __init__ on_event kommentje.
            # BATCH 4.2b F-2 (2026-09-13): csak az ÁTMENETKOR (érvényes ->
            # érvénytelen) tüzelünk, l. _dmm_valid_before_tick docstringje
            # -- a számláló (relax_dmm_fault_total) fent változatlanul
            # minden ticken nő, csak az esemény-spam szűnik meg.
            if self._dmm_valid_before_tick and self.on_event is not None:
                self.on_event({
                    "event_code": "OCV_SOC_RELAX_DMM_FAULT",
                    "event_message": (
                        "DMM olvasási hiba relaxáció közben (OCV/SOC ciklus, "
                        f"hiba #{self._relax_dmm_fault_total})"
                    ),
                })
        # Teljes kódbázis review 13. kör (2026-08-31), Metrológia/
        # Adatintegritás P2 lelet: a relax-tick-ek dt_s-e korábban sosem
        # jutott el a session-szintű _energy_integrator-ba (0 áram mellett
        # is), ezért egy extrém dt_s-tüske relax alatt (a leghosszabb
        # fázisok — 2h/5h/18h relax, statisztikailag valószínűbb, mint egy
        # rövid töltési/kisütési ticken) sosem jelent meg az extreme_dt_s_
        # count/max_observed_dt_s session-szintű minőségi jelzőben — a 9.
        # kör (6e0c68f) capacity_result_quality kapuja emiatt csendben
        # "OK"-ot jelenthetett egy valójában degradált mérésnél. A
        # golden_gg_controller.py::_run_relax() mintáját követve.
        self._energy_integrator.add_sample(0.0, self._u_batt, dt_s)
        self._relax_elapsed_s += dt_s

        if self._relax_elapsed_s >= self._current_relax_s:
            # Mindkét relax állapotból → OCV mérés (IMPULSE_PREP)
            # PRECHARGE_RELAX-ból is: a 100%-os OCV adatpont rögzítendő
            self._enter(OcvSocState.IMPULSE_PREP)

    def _begin_step_discharge(self) -> None:
        """Új kisütési lépés előkészítése: integrátor reset, cél Ah, terhelés beállítás."""
        # R11-B4: a friss szuloi meres kapuja minden Load ON elott.
        overtemp = self._safety.check_battery_overtemperature(
            self._battery_temperature_C, self._temp_dmm_valid,
        )
        if overtemp.fault is not None:
            self.emergency_stop(overtemp.fault.name)
            return
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        # R1 B3b (NS-02, DFAM-001): akkufeszultseg-kapu az energizalas ELOTT.
        # A meglevo safety-limit (SafetyManager.check_battery_voltage(), a
        # per-tick kapuval azonos fault-koddal) alkalmazasa mar az inditoban;
        # sorrend: DMM-or -> feszultsegkapu -> concurrent-kapu (a
        # _run_rebound_check() es az OCV IMPULSE_PREP meglevo sorrendje).
        if not self._safety.enforce_normal_discharge(
            self._u_batt, self._discharge_current_A, self._profile.terminate_voltage_pack_V,
            self.emergency_stop, self.on_event,
        ):
            return
        # Kör 20 Batch E13a (F20-S3, DFAM-001, a discharge_controller.py/
        # traditional_learning_controller.py/iec_dc_resistance_controller.py
        # mar meglevo mintajat koveteve): a lenti, per-tick kapu (l.
        # _run_step_discharge() self._safety.check_concurrent_psu_load()
        # hivasa) a KOVETKEZO ticken futna, a MAR eletbe lepett input_
        # commanded_on-t olvasva -- es mivel device_psu.py::all_outputs_off()
        # a sajat finally-jaban FELTETEL NELKUL output_commanded_on=False-ra
        # allitja, egy csendben sikertelen PSU-kikapcsolas emiatt LATHATATLAN
        # maradna ennek a per-tick kapunak. A kapunak ezert MIELOTT all_
        # outputs_off() fut, elorejelzo modon (load_commanded_on=True
        # szimulalva) kell futnia, nem utana.
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=True,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        # R8-B1: setpoint-alapu teljesitmenykapu a Load inditasa elott.
        power_result = self._safety.check_load_power(self._u_batt, self._discharge_current_A)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return
        # R16-B12: a safety-kapuk után, minden Load-konfiguráció előtt.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        try:
            self._psu.all_outputs_off()
            self._load.set_mode_cc()
            self._load.set_current(self._discharge_current_A)
            self._normal_discharge_cutoff_seen = False
            self._load.input_on(stop_requested=self.handoff_stop_requested)
        except TestAbortedByUser:
            # A runner az élő okot oldja fel, és egyszer kapcsolja le mindkét műszert.
            return
        except Exception as exc:
            self.emergency_stop(f"STEP_DISCHARGE_SETUP_FAILED: {exc}")
            return
        self._step_integrator = Integrator()
        self._step_discharge_phase_elapsed_s = 0.0
        next_target_percent = self._grid[self._grid_pos + 1]
        # SUP01-GRID-004: a következő pont kumulált eltávolított töltéscélja.
        self._target_removed_Ah = (
            self._total_capacity_Ah * (100.0 - next_target_percent) / 100.0
        )
        self._step_discharge_started = True

    def _run_step_discharge(self, dt_s: float) -> None:
        """Részleges kisütés a következő racspont Ah-céljáig."""
        if not self._step_discharge_started:
            # R16-B12: a STOP miatt függő setup ismételhető, pontduplikálás nélkül.
            self._begin_step_discharge()
            return
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

        # Q-35: a cutoff-kivétel csak egyszer fogyasztható el, az egyenlőség is végpont.
        first_crossing = (
            not self._normal_discharge_cutoff_seen and self._load.input_commanded_on
        )
        self._normal_discharge_cutoff_seen |= self._u_batt <= self._profile.terminate_voltage_pack_V
        if not self._safety.enforce_normal_discharge(
            self._u_batt, self._discharge_current_A, self._profile.terminate_voltage_pack_V,
            self.emergency_stop, self.on_event,
            first_cutoff_crossing=first_crossing,
        ):
            return

        try:
            i_load = self._load.measure_current()
            self._i_load_measured_A = i_load
            self._load_current_readback_valid = True
        except Exception:
            self._load_current_readback_valid = False
            self.emergency_stop("LOAD_COMM_LOST")
            return

        # Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: PSU+Load
        # egyidejű aktivitás ellenőrzés (CLAUDE.md §10.2) — l.
        # chemid_controller.py-ban ugyanez a javítás/indoklás.
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return

        # Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P1
        # lelet: l. chemid_controller.py-ban ugyanez a javítás/indoklás +
        # safety.py::check_load_power() docstringje.
        power_result = self._safety.check_load_power(self._u_batt, i_load)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return

        # HW4-02 (review-kör 4, P1, DFAM-001) -- l. discharge_controller.py::
        # _run_cc() azonos hivatkozású kommentje/SafetyManager.check_load_
        # current_setpoint() docstringje. A STEP_DISCHARGE (C/10) fő
        # kisütési fázisra vonatkozik, NEM az impulzus-fázisokra (azok
        # PulseMeasurementService-en át futnak, önálló sibling). MAX
        # verifier N1 lelete: a tick-számláló a _last_warning_code
        # slot-tól FÜGGETLENÜL nő.
        setpoint_result = self._safety.check_load_current_setpoint(
            i_load, self._discharge_current_A
        )
        if setpoint_result.warning is not None:
            self._load_current_setpoint_warning_ticks += 1
            if self._last_warning_code == "":
                self._last_warning_code = setpoint_result.warning.name

        # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: fordított
        # polaritás / ésszerűtlen feszültség ellenőrzés (CLAUDE.md §10.6/
        # SAFE-009) — a lenti "v = ... else terminate_voltage_pack_V" fallback
        # korábban csendben elfedte volna, ha self._u_batt <= 0 valódi
        # bekötési/mérőcsatlakozási hiba, nem csak egy pillanatnyi 0-nak
        # olvasott érvényes mérés.
        v_result = self._safety.check_battery_voltage(self._u_batt)
        if v_result.fault is not None:
            self.emergency_stop(v_result.fault.name)
            return

        self._step_discharge_phase_elapsed_s += dt_s
        if self._step_discharge_phase_elapsed_s > self._safety.max_discharge_time_s:
            self.emergency_stop("MAX_DISCHARGE_TIME_REACHED")
            return

        v = self._u_batt if self._u_batt > 0 else self._profile.terminate_voltage_pack_V
        self._step_integrator.add_sample(-i_load, v, dt_s)
        self._energy_integrator.add_sample(-i_load, v, dt_s)
        self._last_integration_source = "LOAD_READBACK"
        step_removed = abs(self._step_integrator.accumulated_discharge_Ah)
        projected_total_removed_Ah = self._removed_Ah_total + step_removed

        terminate_V = self._profile.terminate_voltage_pack_V
        if self._u_batt <= terminate_V:
            # Végfeszültség a racs szerinti cél előtt jött — a teszt-definíció szerinti
            # végpontot (a racs UTOLSÓ eleme) érjük el, nem egy köztes racspontot.
            self._removed_Ah_total = projected_total_removed_Ah
            # Teljes kódbázis review 16. kör (2026-09-04), Hardver/
            # Műszervezérlés P1 lelet: a testvér GoldenGgController/
            # ChemIdCharacterizationController/TraditionalLearningController
            # mindegyike explicit try/except-be csomagolja ugyanezt a
            # kisütés-lezáráskori load.input_off()-ot ("a hibáját nem szabad
            # csendben elnyelni, mert a relax 0A-feltételezését
            # érvénytelenítené") — az OcvSocController volt az egyetlen,
            # ahol ez a védelem hiányzott, egy csupasz kivétel a
            # TestRunner legkülső UNHANDLED_EXCEPTION ágáig szállt volna,
            # elvesztve a specifikus diagnosztikát és az adott tick már
            # kiszámolt Ah-adatát a report.json-ból.
            try:
                self._load.input_off()
            except Exception as exc:
                self.emergency_stop(f"STEP_DISCHARGE_LOAD_OFF_FAILED: {exc}")
                return
            self._finish_discharge_step(forced_by_terminate_voltage=True)
            return

        if projected_total_removed_Ah >= self._target_removed_Ah:
            self._removed_Ah_total = projected_total_removed_Ah
            try:
                self._load.input_off()
            except Exception as exc:
                self.emergency_stop(f"STEP_DISCHARGE_LOAD_OFF_FAILED: {exc}")
                return
            self._finish_discharge_step(forced_by_terminate_voltage=False)

    def _finish_discharge_step(self, forced_by_terminate_voltage: bool) -> None:
        """Egy kisütési lépés lezárása: grid-pozíció léptetés + relax-időzítés + STEP_RELAX."""
        if forced_by_terminate_voltage:
            # S-5: a szándékolt köztes cél (self._grid_pos + 1) és a
            # ténylegesen elért utolsó racsindex közötti pontok maradnak ki.
            last_index = len(self._grid) - 1
            self._grid_points_skipped = max(0, last_index - (self._grid_pos + 1))
            self._grid_truncated_by_terminate_voltage = self._grid_points_skipped > 0
            self._grid_pos = last_index
        else:
            self._grid_pos += 1

        is_keypoint = (
            self._grid_pos == 0
            or self._grid_pos == len(self._grid) - 1
            or any(abs(self._grid[self._grid_pos] - kp) < 0.05 for kp in _KEYPOINTS)
        )
        self._current_relax_s = (
            self._config.relax_keypoint_s if is_keypoint else self._config.relax_default_s
        )
        self._relax_elapsed_s = 0.0
        self._dmm_fault_during_relax = False
        self._enter(OcvSocState.STEP_RELAX)

    def _run_impulse_prep(self) -> None:
        """OCV mérés a relax végén.

        Teljes kódbázis review 7. kör (2026-08-30), Session/Workflow P2
        lelet #1: advance() MÁR olvasta a feszültség-DMM-et ebben a
        tickben (IMPULSE_PREP nincs a PRECHARGE/IMPULSE_*-kizárási
        listán, l. advance() 492-495. sor) — ez a metódus korábban egy
        MÁSODIK, önálló self._dmm_v.read_voltage() hívást indított,
        holott self._u_batt/self._dmm_valid MÁR a friss, ebben a tickben
        olvasott értéket tartalmazza (CLAUDE.md §9 "egy fizikai DMM-et
        egy tickben egy kijelölt komponens olvasson")."""
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        self._last_ocv_V = self._u_batt

        # Teljes kódbázis review 2. kör (2026-08-28), Session/Workflow P2
        # lelet: az IMPULSE_PREP/ON/ACQUIRE fázisok korábban nem hívtak
        # check_battery_voltage()/check_concurrent_psu_load()-ot — ellentétben
        # a testvér _run_step_discharge()-dzsel (1. kör javítás). A
        # begin_pulse() (lentebb, _run_impulse_on()) egy negatív/fordított-
        # polaritású feszültséget is csak "SKIPPED_LOW_VOLTAGE"-ként fogadott
        # el FAULT helyett — itt, a friss OCV-olvasás UTÁN, MIELŐTT a Load
        # ténylegesen bekapcsolna, kell ellenőrizni (CLAUDE.md §10.6/SAFE-009).
        v_result = self._safety.check_battery_voltage(self._last_ocv_V)
        if v_result.fault is not None:
            self.emergency_stop(v_result.fault.name)
            return
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return

        self._enter(OcvSocState.IMPULSE_ON)

    def _run_impulse_on(self) -> None:
        """Impulzus indítás: SUP01-PULSE-010 előzetes feszültség-ellenőrzés +
        terhelés bekapcsolása a PulseMeasurementService-en keresztül. A
        tényleges mintavétel a következő tickekben, instrumentum-időzített
        chunkokban történik (IMPULSE_ACQUIRE, SUP01-ARCH-001)."""
        # Teljes kódbázis review 5. kör (2026-08-29), Session/Workflow P1
        # lelet: a STEP_DISCHARGE (C/10) már megkapta a check_load_power()-t
        # (4. kör), de az impulzus-fázisok (itt és _run_impulse_fast_on())
        # kimaradtak — holott az impulzusáram C/5 (a STEP_DISCHARGE DUPLÁJA),
        # nagyobb teljesítménykorlát-túllépési kockázattal. A
        # PulseMeasurementService nem ismeri a SafetyManager-t; R8-B2 óta
        # a friss feszültséget, R9-B2 óta a friss baseline/chunk teljesítményt
        # is kapuzza az átadott max_load_power_W alapján.
        # Az itteni kapu a korábbi PREP-kori _u_batt és a tervezett áram
        # alapján végzett előzetes ellenőrzés a begin_pulse() előtt.
        power_result = self._safety.check_load_power(self._u_batt, self._impulse_current_A_set)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return
        # R2-B4: az impulzusonkénti könyvelt-pillanatkép EGYETLEN nullázása.
        self._pulse_energy_booked = NO_MEASURED_PULSE_ENERGY
        self._finalized_pulse_services.discard(self._pulse_service)
        try:
            started = self._pulse_service.begin_pulse(
                pulse_current_A=self._impulse_current_A_set,
                duration_s=self._config.impulse_duration_s,
            )
        except PulseOvervoltageError as exc:
            self.emergency_stop(str(exc))
            return
        except PulsePowerLimitError as exc:
            self.emergency_stop(str(exc))
            return
        except PulseDischargeError as exc:
            self.emergency_stop(str(exc))
            return
        except Exception as exc:
            self.emergency_stop(f"IMPULSE_START_FAILED: {exc}")
            return

        ack = self._pulse_service.activation_ack_diagnostic
        if ack is not None:
            self.emergency_stop(f"SCPI_CRITICAL_ERROR[Load]: {ack}")
            return
        if (
            not started
            and self.handoff_stop_requested is not None
            and self.handoff_stop_requested()
        ):
            return
        self._impulse_current_A = self._impulse_current_A_set
        if started:
            self._enter(OcvSocState.IMPULSE_ACQUIRE)
        else:
            self._finish_impulse_acquisition()

    def _run_impulse_acquire(self) -> None:
        """Egy instrumentum-időzített mintavételi chunk lekérdezése (SUP01-
        ARCH-001/PULSE-003) — egy advance() tick alatt egy chunk, így a
        TestRunner stop/emergency-stop ellenőrzése és élő mintanaplózása a
        pulse teljes ideje alatt is működik."""
        # Teljes kódbázis review 3. kör (2026-08-29), Session/Workflow P2
        # lelet: az IMPULSE_PREP egyszer ellenőrzi a check_concurrent_psu_
        # load()-ot a Load bekapcsolása ELŐTT (2. kör javítás), de az akár
        # 30s+ aktív pulse alatt (step_chunk() ismételt hívásai) korábban
        # egyszer sem futott újra, szemben a testvér _run_step_discharge()-
        # dzsel, ami minden ticken ellenőrzi (CLAUDE.md §10.2).
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        try:
            more = self._pulse_service.step_chunk()
            if self._pulse_service._owned_window and not more:
                self._cleanup_pulse_nplc(self._pulse_service)
        except BaseException as exc:
            if self._state == OcvSocState.FAULT:
                # A felügyelet már döntött; a callback/cleanup csak másodlagos.
                self._fault_reason += f" | PULSE_SECONDARY_ERROR:{exc}"
                if not isinstance(exc, Exception):
                    raise
                return
            # R2-B4-V1 (DFAM-033): a PMS a terminális input_off() ELŐTT
            # rögzíti a leolvasást -- ha ez az OFF KeyboardInterrupt/
            # SystemExit-et dob, a prefixnek (OFF/FAULT után, az
            # emergency_stop()-ban) MÉG a runner finally-_flush_totals()-a
            # ELŐTT át kell kerülnie. Exception: változatlan út (FAULT +
            # return); más BaseException: az EREDETI kivételobjektum megy
            # tovább, dobó on_event mellett is.
            try:
                # R9-B3: a túlfeszültség értékei és a láncolt OFF-hiba is megmarad.
                reason = (
                    str(exc)
                    if isinstance(exc, PulseOvervoltageError)
                    else f"{exc.__cause__} | {exc}"
                    if isinstance(exc.__cause__, PulseOvervoltageError)
                    else str(exc)
                    if isinstance(exc, PulsePowerLimitError)
                    else f"{exc.__cause__} | {exc}"
                    if isinstance(exc.__cause__, PulsePowerLimitError)
                    else str(exc)
                    if isinstance(exc, PulseDischargeError)
                    else f"{exc.__cause__} | {exc}"
                    if isinstance(exc.__cause__, PulseDischargeError)
                    else f"IMPULSE_ACQUISITION_FAILED: {exc}"
                )
                self.emergency_stop(reason)
            finally:
                if not isinstance(exc, Exception):
                    raise exc  # noqa: TRY201 - az EREDETI objektum, nem a dobó on_event kivétele
            return
        if self._state == OcvSocState.FAULT:
            return
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        self._book_measured_pulse_energy(self._pulse_service)
        if not more:
            self._finish_impulse_acquisition()

    def _book_measured_pulse_energy(self, service: PulseMeasurementService) -> None:
        """R2-B4 (R2-metrology-01, DFAM-033): az aktív impulzus még nem
        könyvelt, MÉRT részének átvétele az _energy_integrator-ba -- tiszta
        számítás, I/O nincs. Hívóhelyek: minden sikeres step_chunk() után
        (a runner Stop/E-stop-ja az advance() hívások KÖZÖTT jön, így a
        _flush_totals() már a könyvelt részt látja), illetve az
        emergency_stop() megszakított impulzusnál. Teleszkópos: ismételt
        hívás új leolvasás nélkül nulla deltát ad."""
        if service in self._finalized_pulse_services:
            return
        measured = service.measured_energy_so_far()
        booked = self._pulse_energy_booked
        self._energy_integrator.accumulated_discharge_Ah += measured.removed_Ah - booked.removed_Ah
        self._energy_integrator.accumulated_discharge_Wh += measured.removed_Wh - booked.removed_Wh
        self._pulse_energy_booked = measured

    def _cleanup_pulse_nplc(self, service: PulseMeasurementService) -> bool:
        """Terminális DMM-cleanup csak a két OFF-kísérlet után."""
        if self._state == OcvSocState.FAULT:
            return False
        failures: list[str] = []
        try:
            self._load.input_off()
        except Exception as exc:  # noqa: BLE001 - a másik OFF-kísérlet kötelező
            failures.append(f"LOAD_OFF_FAILED:{exc}")
        try:
            self._psu.all_outputs_off()
        except Exception as exc:  # noqa: BLE001 - a másik OFF-kísérlet kötelező
            failures.append(f"PSU_OFF_FAILED:{exc}")
        if failures:
            self.emergency_stop(" | ".join(failures))
            return False
        try:
            service.finish_owned_acquisition()
            ack = service.activation_ack_diagnostic
            if ack is not None:
                self.emergency_stop(f"SCPI_CRITICAL_ERROR[Load]: {ack}")
                return False
            if self._state != OcvSocState.FAULT:
                service.capture_owned_recovery()
            service.restore_acquisition_nplc()
        except (PulseOvervoltageError, PulsePowerLimitError, PulseDischargeError):
            raise
        except BaseException as exc:
            if self._state == OcvSocState.FAULT:
                raise
            try:
                self.emergency_stop("PULSE_NPLC_RESTORE_INTERRUPTED")
            finally:
                raise exc
        # A halasztott cleanup alatt érkező jelzés is tiltja a véglegesítést.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            if service.nplc_restore_failed:
                # A runner elsődleges FAULT-ként kapja meg, finalize nélkül.
                self.emergency_stop("PULSE_NPLC_RESTORE_FAILED")
            return False
        return True

    def _finish_impulse_acquisition(self) -> None:
        """A PulseMeasurementService eredményének átvétele — cutoff/degraded/
        skipped jelzők, R_pulse_actual_t*_mOhm, tényleges időpontok, pulse Ah."""
        if self._pulse_service in self._finalized_pulse_services:
            return
        if not self._cleanup_pulse_nplc(self._pulse_service):
            return
        result = self._pulse_service.finalize()
        self._emit_pulse_window(self._pulse_service, "STANDARD_PULSE")

        # Konfigurációs elutasításkor még nincs mért baseline (DFAM-024).
        # A service 0,0-s alapértéke nem írhatja felül az utolsó valós mérést.
        if result.timing_infeasible:
            self.emergency_stop(f"IMPULSE_CONFIG_REJECTED: {result.timing_infeasible_reason}")
            return

        self._cutoff_during_pulse = result.cutoff_during_pulse
        self._skipped_low_voltage = result.skipped_low_voltage
        self._pulse_current_degraded = result.pulse_current_degraded
        self._invalid_pulse_resistance = result.has_invalid_resistance
        self._impulse_removed_Ah = result.removed_Ah
        # Teljes kódbázis review 15. kör (2026-09-04), Metrológia/
        # Adatintegritás P2 lelet: az impulzus-fázisok alatt ténylegesen
        # mért Ah-fogyasztás (SUP01-PULSE-009 mintájára a rács-SOC
        # kumulátorba MÁR bekerül, l. _run_log_soc_point()) korábban NEM
        # jutott el a session-szintű accumulated_discharge_Ah-ig (az
        # _energy_integrator csak PRECHARGE/STEP_DISCHARGE fázisban
        # táplálkozik) — a report.json total_discharge_ah mezője
        # ezért szisztematikusan kevesebbet mutatott, mint amennyi
        # töltés ténylegesen távozott az akkuból, belsőleg inkonzisztens
        # módon a rács-utolsó-ponthoz (removed_Ah_total) képest.
        # R2-B4: a mért részt a chunkonkénti könyvelés már átadta -- itt csak
        # a maradék (a normál lezárás OFF-farka) kerül be. Az _impulse_
        # removed_Ah (SOC-rács, fent) továbbra is a TELJES értéket kapja.
        self._energy_integrator.accumulated_discharge_Ah += (
            result.removed_Ah - self._pulse_energy_booked.removed_Ah
        )
        # Teljes kódbázis review 16. kör (2026-09-04), Metrológia/
        # Adatintegritás P2 lelet: a fenti Ah-fix testvérpárja — a
        # PulseAcquisitionResult korábban nem exponált Wh-adatot, ezért
        # az accumulated_discharge_Wh csak PRECHARGE/STEP_DISCHARGE
        # fázisból táplálkozott, belsőleg inkonzisztens módon az immár
        # helyes accumulated_discharge_Ah-hoz képest.
        self._energy_integrator.accumulated_discharge_Wh += (
            result.removed_Wh - self._pulse_energy_booked.removed_Wh
        )
        self._finalized_pulse_services.add(self._pulse_service)
        self._impulse_current_A = result.pulse_current_measured_mean_A
        self._synchronization_quality = result.synchronization_quality
        # Review-kör 5, Metrológia M5-03 -- l. __init__ mező kommentje.
        self._pulse_duration_set_s = result.pulse_duration_set_s
        self._pulse_duration_actual_s = result.pulse_duration_actual_s
        self._u_batt = (
            result.samples[-1].voltage_V if result.samples else result.v_baseline_V
        )

        r1 = result.results.get(1.0)
        r10 = result.results.get(10.0)
        r30 = result.results.get(30.0)
        # Governance-v2 review-kör 1 (CF-23/MET-R1-06, DFAM-024): a korábbi
        # `or 0.0` sentinel egy nem-elért/nem-számítható (pl. delta_i<=0)
        # pontnál fizikailag lehetetlen, de valós mérésnek látszó 0.0-t írt
        # — ugyanabban a sorban, ahol a testvér `t*_actual_s` helyesen None
        # marad. Most mindkettő None, ha a pont nem érte el a célidőt vagy
        # az ellenállás nem számítható.
        self._rb_1s = r1.resistance_ohm if (r1 and r1.reached) else None
        self._rb_10s = r10.resistance_ohm if (r10 and r10.reached) else None
        self._rb_30s = r30.resistance_ohm if (r30 and r30.reached) else None
        # [Kör 19 Batch D3a, F-1/DFAM-021] a fenti hányados nyers dU/dI-je,
        # l. __init__ mezőik kommentje -- ugyanazzal a `reached` kapuval,
        # mint a hányados maga, hogy a None-állapot konzisztens maradjon.
        self._rb_1s_dU_V = r1.delta_voltage_V if (r1 and r1.reached) else None
        self._rb_10s_dU_V = r10.delta_voltage_V if (r10 and r10.reached) else None
        self._rb_30s_dU_V = r30.delta_voltage_V if (r30 and r30.reached) else None
        self._rb_1s_dI_A = r1.delta_current_A if (r1 and r1.reached) else None
        self._rb_10s_dI_A = r10.delta_current_A if (r10 and r10.reached) else None
        self._rb_30s_dI_A = r30.delta_current_A if (r30 and r30.reached) else None
        # A konfigurációs elutasítás fent, a mezők átvétele előtt FAULT-ra fut.
        # Itt a baseline már mért adat; a skipped_low_voltage ág is csak a
        # baseline leolvasása után tér vissza, így nem naplózunk kitalált nullát.
        self._pulse_v_baseline_V = result.v_baseline_V
        self._t1_actual_s = r1.actual_time_s if (r1 and r1.reached) else None
        self._t10_actual_s = r10.actual_time_s if (r10 and r10.reached) else None
        self._t30_actual_s = r30.actual_time_s if (r30 and r30.reached) else None
        # SUP01-PULSE-006: a valós (falióra-alapú) eltérés a célidőponttól —
        # traceability, nem csendben eltűntetett diagnosztika (review-lelet).
        self._t1_time_error_s = r1.time_error_s if (r1 and r1.reached) else None
        self._t10_time_error_s = r10.time_error_s if (r10 and r10.reached) else None
        self._t30_time_error_s = r30.time_error_s if (r30 and r30.reached) else None
        # Release-audit 2026-08-23 (2.8): within_tolerance bekötése a
        # minőség-döntésbe — l. _determine_point_quality() és a
        # _pulse_timing_out_of_tolerance mező docstringje.
        self._pulse_timing_out_of_tolerance = any(
            p is not None and not p.within_tolerance
            for p in (r1, r10, r30)
        )

        # RB Pulse Digitalizálás spec: trigger-mód, mért baseline-áram, trace-
        # teljesség és recovery-minták száma — csak akkor tér el az alapértéktől,
        # ha a config a megfelelő opt-in réteget bekapcsolta.
        self._pulse_trigger_mode_result = result.trigger_mode
        self._measured_baseline_current_A = result.measured_baseline_current_A
        self._baseline_current_degraded = result.baseline_current_degraded
        self._current_trace_incomplete = result.current_trace_incomplete
        self._voltage_trace_incomplete = result.voltage_trace_incomplete
        self._recovery_sample_count = len(result.recovery_samples)

        # A kötelező standard konfiguráció hibája nem opcionális diagnosztika.
        # Az eddig mért energia fent már átkerült, a Load már OFF.
        if result.nplc_restore_failed:
            if result.started and result.nplc_restore_failed:
                # A mérés kész: a teljes, már könyvelt OFF-farok is megmarad.
                # A FAULT ne keletkeztessen hamis megszakított-prefix eseményt.
                self._enter(OcvSocState.LOG_SOC_POINT)
            reason = result.timing_infeasible_reason or "ACQUISITION_NPLC_RESTORE_FAILED"
            self.emergency_stop(f"IMPULSE_CONFIG_REJECTED: {reason}")
            return

        # A cutoff/skipped a standard pulse-ból már eldőlt — a fast-transient
        # réteget csak akkor futtatjuk, ha a standard pulse rendben lezajlott,
        # különben a FAULT/CUTOFF útvonal a szokásos LOG_SOC_POINT-hoz megy.
        if (
            self._fast_pulse_service is not None
            and not self._cutoff_during_pulse
            and not self._skipped_low_voltage
        ):
            self._enter(OcvSocState.IMPULSE_FAST_ON)
        else:
            self._enter(OcvSocState.LOG_SOC_POINT)

    def _run_impulse_fast_on(self) -> None:
        """FAST_TRANSIENT_CHARACTERIZATION indítása — a standard pulse UTÁN,
        külön, ms-tartományú impulzus (spec §6.3/§12/§22, opt-in)."""
        assert self._fast_pulse_service is not None
        # Teljes kódbázis review 5. kör (2026-08-29), Session/Workflow P1
        # lelet — l. _run_impulse_on() ugyanezen ellenőrzés indoklása.
        power_result = self._safety.check_load_power(self._u_batt, self._impulse_current_A_set)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return
        # Teljes kódbázis review 6. kör (2026-08-29), Session/Workflow P2
        # lelet #5: a standard pulse _run_impulse_prep()-je (fentebb) MINDIG
        # ellenőrzi a check_battery_voltage()-t (fordított polaritás,
        # CLAUDE.md §10.6) és a check_concurrent_psu_load()-ot (kölcsönös
        # kizárás, §10.2) a Load bekapcsolása ELŐTT — de ez a PREP-ellenőrzés
        # akár 30s+-mal (a teljes standard pulse + acquire ideje) ELŐBB
        # történt, mint a fast-transient réteg saját, KÜLÖN Load-
        # bekapcsolása itt. Az elavult PREP-ellenőrzésre hagyatkozás helyett
        # itt is friss ellenőrzés kell, ugyanúgy, mint _run_impulse_prep()-ben.
        v_result = self._safety.check_battery_voltage(self._u_batt)
        if v_result.fault is not None:
            self.emergency_stop(v_result.fault.name)
            return
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        # R2-B4: l. _run_impulse_on() azonos nullázását.
        self._pulse_energy_booked = NO_MEASURED_PULSE_ENERGY
        self._finalized_pulse_services.discard(self._fast_pulse_service)
        try:
            started = self._fast_pulse_service.begin_pulse(
                pulse_current_A=self._impulse_current_A_set,
                duration_s=self._config.fast_transient_duration_s,
            )
        except PulseOvervoltageError as exc:
            self.emergency_stop(str(exc))
            return
        except PulsePowerLimitError as exc:
            self.emergency_stop(str(exc))
            return
        except PulseDischargeError as exc:
            self.emergency_stop(str(exc))
            return
        except Exception as exc:
            self.emergency_stop(f"FAST_TRANSIENT_START_FAILED: {exc}")
            return

        ack = self._fast_pulse_service.activation_ack_diagnostic
        if ack is not None:
            self.emergency_stop(f"SCPI_CRITICAL_ERROR[Load]: {ack}")
            return
        if (
            not started
            and self.handoff_stop_requested is not None
            and self.handoff_stop_requested()
        ):
            return
        if started:
            self._enter(OcvSocState.IMPULSE_FAST_ACQUIRE)
        else:
            self._finish_fast_impulse_acquisition()

    def _run_impulse_fast_acquire(self) -> None:
        """FAST_TRANSIENT_CHARACTERIZATION mintavételi chunk lekérdezése."""
        # Teljes kódbázis review 3. kör (2026-08-29), Session/Workflow P2
        # lelet — l. _run_impulse_acquire() docstringje ugyanerről a hiányzó
        # mintáról.
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        assert self._fast_pulse_service is not None
        try:
            more = self._fast_pulse_service.step_chunk()
            if self._fast_pulse_service._owned_window and not more:
                self._cleanup_pulse_nplc(self._fast_pulse_service)
        except BaseException as exc:
            if self._state == OcvSocState.FAULT:
                # A felügyelet már döntött; a callback/cleanup csak másodlagos.
                self._fault_reason += f" | PULSE_SECONDARY_ERROR:{exc}"
                if not isinstance(exc, Exception):
                    raise
                return
            # R2-B4-V1 -- l. _run_impulse_acquire() azonos kezelőjét.
            try:
                # R9-B3: a túlfeszültség értékei és a láncolt OFF-hiba is megmarad.
                reason = (
                    str(exc)
                    if isinstance(exc, PulseOvervoltageError)
                    else f"{exc.__cause__} | {exc}"
                    if isinstance(exc.__cause__, PulseOvervoltageError)
                    else str(exc)
                    if isinstance(exc, PulsePowerLimitError)
                    else f"{exc.__cause__} | {exc}"
                    if isinstance(exc.__cause__, PulsePowerLimitError)
                    else str(exc)
                    if isinstance(exc, PulseDischargeError)
                    else f"{exc.__cause__} | {exc}"
                    if isinstance(exc.__cause__, PulseDischargeError)
                    else f"FAST_TRANSIENT_ACQUISITION_FAILED: {exc}"
                )
                self.emergency_stop(reason)
            finally:
                if not isinstance(exc, Exception):
                    raise exc  # noqa: TRY201 - az EREDETI objektum, nem a dobó on_event kivétele
            return
        if self._state == OcvSocState.FAULT:
            return
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        self._book_measured_pulse_energy(self._fast_pulse_service)
        if not more:
            self._finish_fast_impulse_acquisition()

    def _finish_fast_impulse_acquisition(self) -> None:
        """FAST_TRANSIENT eredmény átvétele — a standard pulse minősítését
        (cutoff/degraded/invalid) NEM írja felül, csak kiegészíti a diagnosztikai
        eredményeket (a fő R_pulse_actual_t1/10/30 mezők forrása mindig a
        standard pulse marad)."""
        assert self._fast_pulse_service is not None
        if self._fast_pulse_service in self._finalized_pulse_services:
            return
        if not self._cleanup_pulse_nplc(self._fast_pulse_service):
            return
        result = self._fast_pulse_service.finalize()
        self._emit_pulse_window(self._fast_pulse_service, "FAST_TRANSIENT")

        self._fast_R_mOhm = {}
        self._fast_actual_s = {}
        self._fast_time_error_s = {}
        for target_s, point in result.results.items():
            if (
                point.reached
                and point.resistance_ohm is not None
                and point.actual_time_s is not None
                and point.time_error_s is not None
            ):
                self._fast_R_mOhm[target_s] = point.resistance_ohm * 1000.0
                self._fast_actual_s[target_s] = point.actual_time_s
                self._fast_time_error_s[target_s] = point.time_error_s
        self._fast_synchronization_quality = result.synchronization_quality

        # A fast-transient impulzus is fogyaszt Ah-t — ugyanúgy be kell számítani
        # a SOC-kumulátorba, mint a standard pulse-ét (SUP01-PULSE-009 mintájára).
        self._impulse_removed_Ah += result.removed_Ah
        # Teljes kódbázis review 15. kör (2026-09-04), Metrológia/
        # Adatintegritás P2 lelet — l. _finish_impulse_acquisition()
        # azonos komment: a session-szintű accumulated_discharge_Ah-nak a
        # fast-transient impulzus Ah-ját is tartalmaznia kell. R2-B4: csak a
        # chunkonként még nem könyvelt maradék (a rács fent TELJES értéket kap).
        self._energy_integrator.accumulated_discharge_Ah += (
            result.removed_Ah - self._pulse_energy_booked.removed_Ah
        )
        # Teljes kódbázis review 16. kör (2026-09-04), Metrológia/
        # Adatintegritás P2 lelet — l. _finish_impulse_acquisition() azonos
        # komment: a fast-transient impulzus Wh-ját is hozzá kell adni.
        self._energy_integrator.accumulated_discharge_Wh += (
            result.removed_Wh - self._pulse_energy_booked.removed_Wh
        )
        self._finalized_pulse_services.add(self._fast_pulse_service)
        # Review-lelet: TILOS a standard pulse-t minősítő self._cutoff_during_pulse /
        # self._pulse_current_degraded mezőkbe írni — azokat _determine_point_quality()
        # a STANDARD (1/10/30s) R-tábla megbízhatóságának jelzésére használja, és a
        # cutoff a legmagasabb prioritású minősítés. Ha ide írnánk, egy pusztán a
        # ms-tartományú diagnosztikai impulzus alatti (a standard pulse-tól teljesen
        # független) cutoff hamisan azt jelezné, hogy a már érvényesen lezárt
        # standard R_1s/10s/30s adatok megbízhatatlanok — ezért KÜLÖN mezőben.
        self._fast_cutoff_during_pulse = result.cutoff_during_pulse
        self._fast_pulse_current_degraded = result.pulse_current_degraded
        self._fast_invalid_pulse_resistance = result.has_invalid_resistance
        self._fast_current_trace_incomplete = result.current_trace_incomplete
        self._fast_voltage_trace_incomplete = result.voltage_trace_incomplete
        self._fast_skipped_low_voltage = result.skipped_low_voltage
        # 2026-09-12, FAST_TRANSIENT NPLC-javítás — l. a mezők docstringjét
        # az __init__-ben.
        self._fast_timing_infeasible = result.timing_infeasible
        self._fast_timing_infeasible_reason = result.timing_infeasible_reason
        self._fast_nplc_restore_failed = result.nplc_restore_failed

        if result.nplc_restore_failed:
            # A teljes mért eredmény már könyvelt; ez hardverállapot-hiba.
            self._enter(OcvSocState.LOG_SOC_POINT)
            self.emergency_stop("FAST_TRANSIENT_NPLC_RESTORE_FAILED")
            return
        self._enter(OcvSocState.LOG_SOC_POINT)

    def _run_log_soc_point(self) -> None:
        """SOC pont rögzítése és döntés a következő lépésről (racs-pozíció alapján)."""
        # SUP01-PULSE-009: a pulse alatt eltávolított töltés most kerül be a kumulált
        # összegbe — enélkül a racs fokozatosan elcsúszna a valós SOC-hoz képest.
        self._removed_Ah_total += self._impulse_removed_Ah
        self._impulse_removed_Ah = 0.0

        quality = self._determine_point_quality()
        # Kor 18, Batch C12 -- l. __init__ mezokommentje.
        self._point_quality_counts[quality] = self._point_quality_counts.get(quality, 0) + 1
        reached_percent = self._grid[self._grid_pos]
        mean_pulse_current_A = self._impulse_current_A

        if self.on_soc_point is not None:
            self.on_soc_point({
                "soc_percent": reached_percent,
                "grid_mode": self._config.grid_mode,
                "capacity_basis_Ah": self._total_capacity_Ah,
                "capacity_basis_quality": self._capacity_basis_quality,
                "removed_Ah_total": self._removed_Ah_total,
                "ocv_V": self._last_ocv_V,
                # GAP-OLD-007 (OCV szemantika): a projekt 2h/5h relax-ideje SEM
                # a FIAMM 24 órás equilibrium/reference OCV-t, SEM BQ-alapú
                # relax-OCV-t nem valósítja meg — mindig gyorsított labor-OCV.
                # Explicit, konstans jelölés, hogy GUI/CSV/riport sose keverje
                # össze a 24 órás gyári referenciával.
                "ocv_semantic_type": "ACCELERATED_LAB_OCV",
                # SUP01-TERM-003: nem egyetlen általános "Rb" mező, hanem tényleges
                # idopontra utaló, mOhm egységű mezők; SUP01-TERM-001/002: ez a
                # LAB_PULSE_RESISTANCE_TABLE fogalma — nem TI Ra és nem FIAMM Ri.
                "R_pulse_actual_t1_mOhm": self._rb_1s * 1000.0 if self._rb_1s is not None else None,
                "R_pulse_actual_t10_mOhm": self._rb_10s * 1000.0 if self._rb_10s is not None else None,
                "R_pulse_actual_t30_mOhm": self._rb_30s * 1000.0 if self._rb_30s is not None else None,
                "t1_actual_s": self._t1_actual_s,
                "t10_actual_s": self._t10_actual_s,
                "t30_actual_s": self._t30_actual_s,
                "t1_time_error_s": self._t1_time_error_s,
                "t10_time_error_s": self._t10_time_error_s,
                "t30_time_error_s": self._t30_time_error_s,
                "pulse_current_set_A": self._impulse_current_A_set,
                "pulse_current_measured_mean_A": mean_pulse_current_A,
                # Kör 17 MAX-effort review, R17-13 (DFAM-021+DFAM-003+
                # DFAM-001): korábban ez a jelző (amely azt jelzi, hogy a
                # fenti pulse_current_measured_mean_A valójában a SETPOINT-
                # ra esett vissza, nem mért érték) sosem jutott el az
                # ocv_soc_table.csv-be -- a "fast_transient_current_degraded"
                # testvér (lent) MÁR exportálva volt, ez az aszimmetrikus
                # kivétel volt.
                "pulse_current_degraded": self._pulse_current_degraded,
                # Review-kör 5, Metrológia M5-03 (P2, DFAM-021) -- l.
                # __init__ mező kommentje: korábban kiszámolva, de sosem
                # kiolvasva/perzisztálva.
                "pulse_duration_set_s": self._pulse_duration_set_s,
                "pulse_duration_actual_s": self._pulse_duration_actual_s,
                # SUP01-PULSE-005 (WP-AK-04): a PulseMeasurementService instrumentum-
                # időzített DMM chunkokkal mintavételez — HARDWARE_SYNCHRONIZED-hoz
                # (közös hardvertrigger) továbbra is HIL validálás szükséges.
                "synchronization_quality": self._synchronization_quality,
                "method_origin": "PROJECT_ENGINEERING_METHOD",
                "temperature_C": self._battery_temperature_C,
                # Review-kör 5, Metrológia M5-05 (P3): korábban a SETPOINT
                # (self._current_relax_s, a relax CÉL-időtartama) került ide,
                # nem a ténylegesen mért relax-idő. self._relax_elapsed_s a
                # JUST lezárult relax-fázis (PRECHARGE_RELAX vagy STEP_RELAX)
                # tényleges, dt_s-ekből összegzett időtartamát tartalmazza,
                # és csak KÉSŐBB, a _run_step_discharge()-ban (a KÖVETKEZŐ
                # relax előkészítésekor) nullázódik — ezen a ponton még
                # pontosan a most lezárult relax valódi hosszát tükrözi.
                "relax_duration_s": self._relax_elapsed_s,
                "measurement_quality": quality,
                # RB Pulse Digitalizálás spec kiegészítő mezők — alapértelmezett
                # (INSTRUMENT_TIMED, fast_transient_enabled=False) konfigurációban
                # ezek üresek/None-ok maradnak, a meglévő fogyasztók változatlanul
                # figyelmen kívül hagyhatják őket.
                "pulse_trigger_mode": self._pulse_trigger_mode_result,
                "measured_baseline_current_A": self._measured_baseline_current_A,
                "baseline_current_degraded": self._baseline_current_degraded,
                "current_trace_incomplete": self._current_trace_incomplete,
                "voltage_trace_incomplete": self._voltage_trace_incomplete,
                "recovery_sample_count": self._recovery_sample_count,
                "fast_transient_R_mOhm": dict(self._fast_R_mOhm),
                "fast_transient_actual_s": dict(self._fast_actual_s),
                "fast_transient_time_error_s": dict(self._fast_time_error_s),
                "fast_transient_synchronization_quality": self._fast_synchronization_quality,
                "fast_transient_cutoff_during_pulse": self._fast_cutoff_during_pulse,
                "fast_transient_current_degraded": self._fast_pulse_current_degraded,
                "fast_transient_invalid_resistance": self._fast_invalid_pulse_resistance,
                "fast_transient_current_trace_incomplete": self._fast_current_trace_incomplete,
                "fast_transient_voltage_trace_incomplete": self._fast_voltage_trace_incomplete,
                "fast_transient_skipped_low_voltage": self._fast_skipped_low_voltage,
                # 2026-09-12, FAST_TRANSIENT NPLC-javítás — l. a mezők
                # __init__-beli docstringje.
                "fast_transient_timing_infeasible": self._fast_timing_infeasible,
                "fast_transient_timing_infeasible_reason": self._fast_timing_infeasible_reason,
                "fast_transient_nplc_restore_failed": self._fast_nplc_restore_failed,
                # S-5: l. a mezők __init__-beli docstringje.
                "grid_truncated_by_terminate_voltage": self._grid_truncated_by_terminate_voltage,
                "grid_points_skipped": self._grid_points_skipped,
                # M4-04 (review-kor-4): a temperature_C sticky (utolsó sikeres
                # olvasásból ragadt) lehet, ha a hőmérséklet-DMM épp ezen a
                # ticken hibázott — ez a mező jelzi pontonként, hogy a fenti
                # temperature_C ekkor frissen olvasott volt-e (l. az IEC
                # testvér-mintáját, iec_dc_resistance_controller.py).
                "temperature_valid": self._temp_dmm_valid,
                # [Kör 19 Batch D3a, F-1/DFAM-021 7. előfordulás] a fenti
                # R_pulse_actual_t*_mOhm = dU/dI hányados nyers bemenetei --
                # l. __init__ mezőik kommentje. A sor VÉGÉRE illesztve
                # (nem a hányados mezői mellé), hogy egy esetleges
                # pozíció-alapú olvasó (nincs ilyen a kódbázisban ma, l.
                # OCV_SOC_COLUMNS CF-21 kommentje) a legkisebb kockázatot
                # fusson. `pulse_v_baseline_V` FONTOS: NEM azonos `ocv_V`-
                # vel -- külön mintavételi időpontból származik (l.
                # _run_impulse_prep() vs. begin_pulse()).
                "dU_t1_V": self._rb_1s_dU_V,
                "dU_t10_V": self._rb_10s_dU_V,
                "dU_t30_V": self._rb_30s_dU_V,
                "dI_t1_A": self._rb_1s_dI_A,
                "dI_t10_A": self._rb_10s_dI_A,
                "dI_t30_A": self._rb_30s_dI_A,
                "pulse_v_baseline_V": self._pulse_v_baseline_V,
            })

        self._grid_truncated_by_terminate_voltage = False
        self._grid_points_skipped = 0
        self._dmm_fault_during_relax = False
        self._cutoff_during_pulse = False
        self._skipped_low_voltage = False
        self._invalid_pulse_resistance = False
        self._baseline_current_degraded = False
        self._pulse_timing_out_of_tolerance = False
        # [Kör 19 Batch D3a, F-1] a fenti _rb_1s/10s/30s mintáját követve --
        # ezek a mezők is per-pont adatok, a következő impulzus előtt
        # nullázandók (bár _finish_impulse_acquisition() úgyis felülírja
        # minden ponton, ez a reset a többi flag konzisztens mintáját
        # követi és védelmet ad, ha valaha egy pont impulzus nélkül
        # naplózódna).
        self._rb_1s_dU_V = None
        self._rb_10s_dU_V = None
        self._rb_30s_dU_V = None
        self._rb_1s_dI_A = None
        self._rb_10s_dI_A = None
        self._rb_30s_dI_A = None
        self._pulse_v_baseline_V = None
        self._current_trace_incomplete = False
        self._voltage_trace_incomplete = False
        self._fast_R_mOhm = {}
        self._fast_actual_s = {}
        self._fast_time_error_s = {}
        self._fast_synchronization_quality = None
        self._fast_cutoff_during_pulse = False
        self._fast_pulse_current_degraded = False
        self._fast_invalid_pulse_resistance = False
        self._fast_current_trace_incomplete = False
        self._fast_voltage_trace_incomplete = False
        self._fast_skipped_low_voltage = False
        self._fast_timing_infeasible = False
        self._fast_timing_infeasible_reason = ""
        self._fast_nplc_restore_failed = False

        # SUP01-GRID-005/006: a racs UTOLSÓ pontja (a valódi teszt-0%) csak akkor
        # tekinthető elértnek, ha ide is eljutottunk és naplóztuk — nincs "korai" DONE.
        if self._grid_pos >= len(self._grid) - 1:
            self._enter(OcvSocState.DONE)
        else:
            self._step_discharge_started = False
            self._enter(OcvSocState.STEP_DISCHARGE)
            self._begin_step_discharge()

    def _determine_point_quality(self) -> str:
        if self._cutoff_during_pulse:
            return "CUTOFF_DURING_PULSE"
        if self._skipped_low_voltage:
            return "SKIPPED_LOW_VOLTAGE"
        if self._dmm_fault_during_relax:
            return "DEGRADED_DMM_LOSS_DURING_RELAX"
        if self._pulse_current_degraded:
            return "DEGRADED_SETPOINT_CURRENT"
        if self._baseline_current_degraded:
            return "DEGRADED_BASELINE_CURRENT_ASSUMED"
        if self._invalid_pulse_resistance:
            return "INVALID_PULSE_RESISTANCE"
        # Release-audit 2026-08-23 (2.8): egy elért (reached=True) pont
        # időzítése jóval a névleges célidőponton (1s/10s/30s) kívül eshet —
        # a resistance_ohm ekkor is kiszámítható, de a CLAUDE.md #11.5
        # ("csak akkor R_1s, ha a tényleges mintavételi idő ~1s") megköveteli
        # a degraded jelzést.
        if self._pulse_timing_out_of_tolerance:
            return "DEGRADED_TIMING_TOLERANCE"
        # A CURRENT_TRACE_INCOMPLETE a hardveres trace hiányát jelzi.
        if self._current_trace_incomplete:
            return "CURRENT_TRACE_INCOMPLETE"
        # B21b: a parancs-határidőre záró standard/fast INSTRUMENT_TIMED
        # ablakban is lehet a tervezettnél kevesebb feszültségminta.
        # A VOLTAGE_TRACE_INCOMPLETE megmarad; diszkriminatív ereje HIL_PENDING.
        if self._voltage_trace_incomplete:
            return "VOLTAGE_TRACE_INCOMPLETE"
        # Governance-v2 review-kör 1 (CF-22/MET-R1-07, DFAM-012, batch
        # 4.2; MAX verifier V-2 pontosítás): a FAST_TRANSIENT (opcionális,
        # ms-tartományú diagnosztikai kiegészítő) réteg állapota KORÁBBAN
        # itt is szerepelt — a `.claude/rules/20-metrology.md` explicit
        # projekt-szabálya szerint ("Diagnosztikai/optional réteg quality
        # flagje ne szennyezze automatikusan a fő safety/minőségi
        # állapotot") ez HELYTELEN volt: ha a standard pulse tökéletesen
        # OK volt, de a KIEGÉSZÍTŐ fast-transient mérés degradált, ez a
        # függvény a FŐ `measurement_quality` mezőt is degradáltra
        # állította — egy downstream fogyasztó tévesen a TELJES pontot, a
        # valóban érvényes standard R_1s/10s/30s értékekkel együtt,
        # elvethette volna. FONTOS PONTOSÍTÁS (a korábbi, `a0d015c`
        # commit-üzenet tévesen állította ennek ellenkezőjét): ez a
        # döntés TUDATOSAN FELÜLÍRJA a 21/22/23. review-kör korábbi,
        # kifejezett követelményét (l. a testvér tesztek eredeti
        # docstringjei), miszerint a fast-transient eredménynek EL KELL
        # jutnia a `_determine_point_quality()`-be is — az `ae21efd`
        # commit ÉPPEN EZT a bekötést szállította, nem "ugyanezt az
        # irányt kezdte meg". A fast-transient rétegnek VAN saját,
        # elkülönített minőségjelzése az `on_soc_point` dict-ben (l.
        # lent), és ez EL IS JUT az `ocv_soc_table.csv`-be — a
        # `logger.py::OCV_SOC_COLUMNS` a CF-21/MET-R1-19 fix
        # (batch 4.6, előrehozva) óta 7 skalár `fast_transient_*`
        # minőségjelzőt tartalmaz. A 3, per-célidőpont DICT-értékű
        # fast-transient mező (`fast_transient_R_mOhm`/`_actual_s`/
        # `_time_error_s`) TUDATOSAN kimaradt — flatten-elésük egy
        # önálló CSV-séma döntést igényel a konfigurálható
        # `fast_transient_target_times_s` miatt (l. logger.py komment és
        # CLAIM_LEDGER.yaml nyitott rekordja). CF-22 ezzel VÉGLEGESEN
        # LEZÁRVA: a fő mező téves degradálása megszűnt, ÉS a
        # fast-transient minőségjelzők (a 3 kizárt dict-mező kivételével)
        # perzisztálódnak.
        return "OK"

    # ------------------------------------------------------------------ #
    # Segédek                                                              #
    # ------------------------------------------------------------------ #

    def _read_dmm(self, dt_s: float) -> bool:
        try:
            self._u_batt = self._dmm_v.read_voltage()
            voltage_ok = True
        except Exception:
            voltage_ok = False

        self._read_temperature_only(dt_s)
        return voltage_ok

    def _read_temperature_only(self, dt_s: float) -> None:
        # R11-B4: pulzus alatt sincs masodik feszultseg-DMM olvasas.
        try:
            self._battery_temperature_C = self._dmm_t.read_temperature()
            self._safety.record_temperature_dmm_read(valid=True, dt_s=dt_s)
            self._temp_dmm_valid = True
        except Exception:
            self._safety.record_temperature_dmm_read(valid=False, dt_s=dt_s)
            self._temp_dmm_fault_ticks += 1
            self._temp_dmm_valid = False
