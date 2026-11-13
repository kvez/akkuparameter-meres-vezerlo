"""
ChargeController — DMM-kompenzált CC/CV töltési állapotgép.
Állapotok: INIT → PRECHECK → PSU_PRESET → CHARGE_CC →
           CHARGE_CV_DMM_CONTROL → TAPER_HOLD → CHARGE_DONE / FAULT
[R1] Nincs relay hívás.
[N7] Csak mód-agnosztikus PSU API hívható.
[N9] output_commanded_on nyilvántartás a PSU driveren keresztül.
"""
from __future__ import annotations
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from Prog.src.battery_profile import BatteryProfile
from Prog.src.exceptions import TestAbortedByUser
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode, WarningCode, FaultCode
from Prog.src.integrator import Integrator


_PSU_HW_MAX_CHARGE_A = {
    PsuMode.INDEPENDENT: 1.5,
    PsuMode.PARALLEL: 3.0,
    PsuMode.SERIES: 1.5,
}


class ChargeState(Enum):
    INIT = "INIT"
    PRECHECK = "PRECHECK"
    PSU_PRESET = "PSU_PRESET"
    CHARGE_CC = "CHARGE_CC"
    CHARGE_CV_DMM_CONTROL = "CHARGE_CV_DMM_CONTROL"
    TAPER_HOLD = "TAPER_HOLD"
    CHARGE_DONE = "CHARGE_DONE"
    FAULT = "FAULT"


@dataclass
class ChargeConfig:
    # CV szabályozás
    deadband_V: float = 0.010
    max_step_up_V: float = 0.050
    # Aszimmetrikus: lefelé gyors (dióda-esés csökkenésekor az akku felkúszik,
    # a 2s tick alatt 50mV lassú volt — 500mV elegendő reakció a 150mV headroomhoz)
    max_step_down_V: float = 0.500
    cv_entry_margin_V: float = 0.100
    max_expected_series_drop_V: float = 0.90
    # BQ/panel-módban (TF1 panel a töltőágban) a soros esés a fault-küszöbbel
    # (SafetyManager.fault_series_drop_V_bq = 3.50V) összhangban akár 3.5V is
    # lehet — a nem-BQ 0.90V headroom ilyenkor a PSU compliance-t a szükséges
    # alá szorítja, a töltés áram nélkül/korlátozott árammal reked meg.
    max_expected_series_drop_V_bq: float = 3.50

    # Taper [R9]
    taper_hold_s: float = 600.0
    taper_current_tolerance_factor: float = 1.05
    cv_voltage_tolerance_V_per_cell: float = 0.003

    # Safety
    max_charge_time_s: float = 86400.0
    # Felhasználói döntés (2026-08-24, session_fg1_20260823_200337 elemzése alapján): 1.20 -> 1.50.
    # A session-ben DISCHARGE_1 4.976 Ah-t sütött ki, a RECHARGE fázis 6.344 Ah-nál
    # (nominal*1.20-nál) FAULT-olt MAX_CHARGE_AH_REACHED-del, miközben a töltőáram még
    # aktívan taperelt (~0.71A, jóval a szokásos ~0.03C taper-stop küszöb felett) -- a
    # valós AGM töltési hatásfok + BQ-tanulási ciklus igényelt Ah-ja nem fért bele a
    # 20%-os tartalékba. Mérnöki feltételezés, nincs gyári forrás a pontos szorzóra --
    # l. CLAUDE.md 10.7. Ára: a runaway/vezetékezési-hiba miatti túltöltés elleni
    # detektálási tartalék 20%-ról 50%-ra nőtt (kevésbé érzékeny backstop).
    max_charge_Ah_factor: float = 1.50

    # Töltőáram felülírás (0.0 = automatikus profil alapján)
    charge_current_A_override: float = 0.0

    # 2026-08-23 (felhasználói review, 2. kör — traditional_learning OV
    # incidens): a CC->CV határ közelében/CV-ben/taperben a külső ~2s-es
    # runner-tick helyett gyorsabb, ténylegesen mért idejű bel-ső
    # résztick-ekkel hívjuk a szabályozást, hogy egy gyors dV/dt (rossz
    # csatlakozás/meleg dióda okozta ellenállás-növekedés) ne tudjon a
    # teljes külső tick-idő alatt észrevétlenül a safety-limit fölé futni.
    # Alapértelmezésben KIKAPCSOLVA (opt-in) — valós `time.sleep()`-et hív,
    # ami a meglévő, mock-alapú tesztszázakat feleslegesen lelassítaná;
    # a GUI-integráció (`main_window.py`) explicit bekapcsolja élő session-nél.
    fast_loop_enabled: bool = False
    fast_loop_period_s: float = 0.2
    fast_loop_proximity_V: float = 0.5
    fast_loop_max_substeps: int = 20

    # 2026-09-15 D10 OV incidens, 2. gyökérok: `_check_series_safety()`
    # korábban a PSU-feszültség-readback vesztését (WarningCode.
    # PSU_VOLTAGE_READBACK_LOST) határozatlan ideig csak figyelmeztetésként
    # tolerálta, miközben a mögötte álló `measure_output_voltage()` EGY
    # sikertelen hívása is a driver teljes retry-budgetjéig blokkolhat
    # (device_psu.py timeout_ms=5000 × max_retries=3 = 15s timeout-budget;
    # R13-B2: minden módban egyetlen CH1-lekérdezés, CH1+CH2 összegzés nélkül).
    #
    # A helyes korlát-levezetés (2. kör MAX verifier V2 lelete, 2026-09-15,
    # az ELSŐ kör "deadline miatt 1 résztick/külső tick" indoklása HIBÁS
    # volt -- test_runner.py:1168 a blokkoló hívás SAJÁT idejét szándékosan
    # BELESZÁMÍTJA a KÖVETKEZŐ tick dt_s-ébe [M5-01 fix], ami a `deadline`-t
    # is kitágítja, tehát TÖBB résztick futhat egy külső tick alatt, nem
    # csak egy): `self._consecutive_psu_voltage_readback_lost` MINDEN
    # `_check_series_safety()` hívásnál nő, ami `_advance_single()`-ben fut
    # -- ez résztickben IS lefut. A blokkoló hívások SZÁMA a FAULT-ig ezért
    # PONTOSAN `psu_voltage_readback_lost_after_n_checks` (=3), FÜGGETLENÜL
    # attól, hány külső/belső tick alatt oszlik el -- mérve (szimulációval,
    # fast_loop_max_substeps=20 ÉS =200 mellett is): 3 blokkoló hívás, nem
    # több. A TELJES wall-clock-idő viszont a 3 blokkoló hívás IDEJÉNEK
    # összege (nem csak 3×egy hívásnyi, mert más hívóhelyek -- a
    # TestRunner `_build_sample()`/`InstrumentManager.read_all()` saját,
    # minden ticken lefutó readback-je -- UGYANCSAK blokkolhatnak, ezeket
    # ez a számláló NEM számolja). R13-B2: a régi, kétcsatornás
    # SERIES-mérésre épülő ~150-210s becslés történeti, nem a mai
    # end-to-end időkorlát. A CV-belépés a _check_series_safety()
    # ugyanezen tickben már elvégzett olvasását használja (R2-01).
    # 3 egymást
    # követő sikertelen readback után
    # FAULT, NEM végtelen tolerancia -- ugyanaz a defektcsalád/tünet-osztály,
    # mint a BQ-transport-escalation (TraditionalLearningController._check_
    # bq_transport_escalation(), LearningConfig.bq_transport_unreachable_
    # after_n_polls=3).
    psu_voltage_readback_lost_after_n_checks: int = 3

    # CHG-004: FIAMM temperature-rise termination (battery_temp > ambient + 10°C).
    # UNKNOWN alapértelmezés mellett a feltétel NEM kényszeríthető ki — ez a
    # jelenlegi hardver (nincs ambient szenzor) tudatos, dokumentált korlátja,
    # nem hallgatólagos hiány. MANUAL/EXTERNAL_SENSOR esetén ambient_temperature_C
    # kötelező. INITIAL_BATTERY_APPROX ebben a körben NEM implementált.
    ambient_temperature_source: str = "UNKNOWN"
    ambient_temperature_C: float | None = None

    def __post_init__(self) -> None:
        if self.charge_current_A_override != 0.0:
            if not (0.0 < self.charge_current_A_override <= 100.0):
                raise ValueError(
                    f"charge_current_A_override={self.charge_current_A_override!r} "
                    "érvénytelen; elfogadott tartomány: (0.0, 100.0]"
                )
        if self.ambient_temperature_source not in (
            "MANUAL", "EXTERNAL_SENSOR", "INITIAL_BATTERY_APPROX", "UNKNOWN"
        ):
            raise ValueError(
                f"ambient_temperature_source={self.ambient_temperature_source!r} "
                "érvénytelen; elfogadott: MANUAL/EXTERNAL_SENSOR/"
                "INITIAL_BATTERY_APPROX/UNKNOWN"
            )
        if (
            self.ambient_temperature_source in ("MANUAL", "EXTERNAL_SENSOR")
            and self.ambient_temperature_C is None
        ):
            raise ValueError(
                f"ambient_temperature_source={self.ambient_temperature_source!r} "
                "esetén ambient_temperature_C kötelező"
            )
        if self.fast_loop_period_s <= 0:
            raise ValueError("fast_loop_period_s > 0 kötelező")
        if self.fast_loop_proximity_V < 0:
            raise ValueError("fast_loop_proximity_V >= 0 kötelező")
        if self.fast_loop_max_substeps < 1:
            raise ValueError("fast_loop_max_substeps >= 1 kötelező")
        if self.psu_voltage_readback_lost_after_n_checks < 1:
            raise ValueError("psu_voltage_readback_lost_after_n_checks >= 1 kötelező")


class ChargeController:
    def __init__(
        self,
        psu,
        load,
        dmm_voltage,
        dmm_temperature,
        profile: BatteryProfile,
        safety: SafetyManager,
        config: ChargeConfig,
        *,
        _traditional_learning: bool = False,
        _require_confirmed_off: bool = False,
    ):
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature
        self._profile = profile
        self._safety = safety
        self._config = config
        self._traditional_learning = _traditional_learning
        self._require_confirmed_off = _require_confirmed_off
        # R16-B12: a mérés közben érkező leállítást az indítás előtt látni kell.
        self.handoff_stop_requested: Callable[[], bool] | None = None

        self._state = ChargeState.INIT
        self._u_psu_set: float = 0.0
        self._taper_timer_s: float = 0.0
        self._elapsed_s: float = 0.0
        self._fault_reason: str = ""
        self._last_integration_source: str = "ZERO"
        self._integrator = Integrator()

        self._u_batt: float = 0.0
        self._i_charge: float = 0.0
        self._charge_current_set_A: float = 0.0
        self._dmm_valid: bool = True
        # Review-kör 3 (2026-09-09), friss Metrológia M3-01 -- l.
        # temp_dmm_valid property docstringje lent.
        self._temp_dmm_valid: bool = True
        # [Kör 19 Batch D3c, F-3 rész (a)] `_temp_dmm_valid` csak az UTOLSÓ
        # olvasás kimenetét jelzi -- ha a hőmérséklet-DMM a session ELEJÉTŐL
        # halott, ez SOHA nem válik False-ból True-vá, mert a fenti mezővel
        # nem különböztethető meg egy "mért, de stale" és egy "SOHA nem
        # mért, csak a konstrukciókori alapérték" állapot. Ez a flag
        # KIZÁRÓLAG arra a kérdésre válaszol, hogy TÖRTÉNT-e valaha
        # sikeres olvasás -- l. temp_dmm_ever_read_ok property.
        self._temp_dmm_ever_read_ok: bool = False
        self._last_warning_code: str = ""
        self._battery_temperature_C: float = 20.0
        # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        # lelet: a 20. körben bevezetett check_test_temperature_range()
        # WARNING-ja csak a per-tick _last_warning_code-ba (samples.csv)
        # jutott el — session-szintű összeg nélkül a report.json/
        # capacity_result_quality nem tudott róla, ellentétben a projekt
        # 4 másik, analóg mintájával (extreme_dt_s_count, 4×
        # relax_dmm_fault_total). Ugyanaz a delta-alapú aggregációs minta,
        # mint extreme_dt_s_count-nál — l. test_runner.py::_flush_totals().
        self._temperature_out_of_range_count: int = 0
        # Review-kör 3 (2026-09-09), MAX verifier (a306e7e764e3a717b) P2-1,
        # DFAM-001 további előfordulása -- l. temp_dmm_fault_ticks property
        # docstringje. A ChargeController-nek korábban nem volt tick-alapú
        # homerseklet-DMM-hiba-szamlaloja, csak a masodperc-alapu
        # _temp_dmm_fault_s -- az 5 osszetett szulo (chemid/golden_gg/iec_
        # dc_resistance/ocv_soc/traditional_learning) ezt nem tudta
        # atvezetni a sajat, session-szintu temp_dmm_fault_ticks
        # szamlalojaba egy beagyazott toltesi fazis alatt.
        self._temp_dmm_fault_ticks: int = 0
        # Kor 18, Batch C7 R17-10 (DFAM-001, l. safety.py::
        # check_psu_charge_current_setpoint() docstringje): a compliance-
        # emeles kimerult-e (a PSU mar a plafonon van) -- CSAK
        # _maybe_escalate_cc_compliance() irja, a metodus TETEJEN
        # explicit False-ra allitva minden ticken, hogy egy korai return
        # SOSE hagyja az ELOZO tick erteket alva (l.
        # feedback_new_tick_scoped_validity_flag_needs_explicit_reset.md).
        self._cc_compliance_headroom_exhausted: bool = False
        # Kor 18, Batch C7 R17-10: a check_psu_charge_current_setpoint()
        # figyelmeztetes tick-szamlaloja, a temp_dmm_fault_ticks/
        # load_current_setpoint_warning_ticks mar bevalt mintajat
        # koveteve (a _last_warning_code EGYETLEN, nem-felulirasos
        # slotjatol FUGGETLENUL no, l. HW4-02 MAX verifier N1 tanulsaga).
        self._psu_charge_current_setpoint_warning_ticks: int = 0
        # Review-kör 5, Metrológia M5-01 (P1, MAX verifier kötelező) -- l.
        # last_fast_loop_extra_s property docstringje.
        self._last_fast_loop_extra_s: float = 0.0
        # 2026-09-15 D10 OV incidens -- l. ChargeConfig.psu_voltage_
        # readback_lost_after_n_checks kommentje.
        self._consecutive_psu_voltage_readback_lost: int = 0
        # Review-kör 6 (2026-09-15), C-08a -- l. psu_current_readback_valid
        # property docstringje.
        self._psu_current_readback_valid: bool | None = None
        # Review-kör 6 (2026-09-15), C-08b -- l. u_psu_measured_V/
        # psu_voltage_readback_valid property docstringje.
        self._u_psu_measured_V: float | None = None
        self._psu_voltage_readback_valid: bool | None = None

    # ------------------------------------------------------------------ #
    # Nyilvános tulajdonságok                                              #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> ChargeState:
        return self._state

    @property
    def taper_timer_s(self) -> float:
        return self._taper_timer_s

    @property
    def last_fast_loop_extra_s(self) -> float:
        """Review-kör 5, Metrológia M5-01 (P1): a legutóbbi `advance(dt_s)`
        hívás alatt a `_advance_with_fast_loop()` saját, `time.sleep()`-
        pacelt résztick-jei (l. ott) MÁR ténylegesen elintegráltak ennyi
        VALÓS wall-clock időt a hívó által átadott `dt_s`-en FELÜL — a hívó
        (TestRunner) ezt az összeget vonja le a KÖVETKEZŐ tick `dt_s`-ének
        számításából, hogy ne integrálja duplán (l. test_runner.py
        `_last_tick_t` kommentje). 0.0, ha a legutóbbi hívás alatt a
        fast loop nem futott (nem `fast_loop_enabled`, vagy `_fast_loop_
        active()` False volt) -- ez a régi, dupla-integrálást megelőző
        review-14 fix ELLENTÉTES iránya: az a `dt_s`-en FELÜLI, fast-loop
        által MÁR elintegrált időt zárta ki a következő tick-ből, de emiatt
        MINDEN nem-fast-loop tick saját `advance()`-hívásának a VALÓS
        végrehajtási ideje (a DMM/PSU/Load SCPI I/O tényleges wall-clock
        időtartama) véglegesen kiesett minden `dt_s`-ből -- sem az adott
        tick, sem egyetlen későbbi tick nem integrálta soha. Ez a property
        teszi lehetővé a helyes megkülönböztetést: a hívó mostantól a
        `_tick_start_t` (az `advance()` hívás ELŐTTI időpont) + ez az
        összeg alapján számolja a következő `_last_tick_t`-et, ahelyett,
        hogy egyszerűen egy `advance()` utáni friss `time.perf_counter()`-t
        venne (ami a saját I/O-idejét is, tévesen, "már elintegráltnak"
        tekintette)."""
        return self._last_fast_loop_extra_s

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def last_integration_source(self) -> str:
        return self._last_integration_source

    @property
    def last_warning_code(self) -> str:
        return self._last_warning_code

    @property
    def accumulated_charge_Ah(self) -> float:
        return self._integrator.accumulated_charge_Ah

    @property
    def accumulated_charge_Wh(self) -> float:
        """Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        + Metrológia P2 lelet: az accumulated_charge_Ah Wh-párja — a
        TraditionalLearningController ezt a beágyazott ChargeController
        élő értékeként delegálja tovább (l. ott accumulated_charge_Wh)."""
        return self._integrator.accumulated_charge_Wh

    @property
    def extreme_dt_s_count(self) -> int:
        """Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        P2 lelet: az Integrator.extreme_dt_s_count (1. kör javítás) frissült,
        de sehol nem került kiolvasásra — most a samples.csv extreme_dt_s_
        count/max_observed_dt_s oszlopán keresztül látható."""
        return self._integrator.extreme_dt_s_count

    @property
    def max_observed_dt_s(self) -> float:
        return self._integrator.max_observed_dt_s

    @property
    def temperature_out_of_range_count(self) -> int:
        """Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        lelet — l. konstruktor kommentje ugyanerről a hiányzó session-
        szintű összesítésről."""
        return self._temperature_out_of_range_count

    @property
    def temp_dmm_fault_ticks(self) -> int:
        """Review-kör 3 (2026-09-09), MAX verifier (a306e7e764e3a717b) P2-1
        -- l. konstruktor kommentje. Az 5 összetett szülő (chemid/
        golden_gg/iec_dc_resistance/ocv_soc/traditional_learning) ezt a
        temperature_out_of_range_count-nál már bevált live-add + carry-
        over mintát követve olvassa be a beágyazott töltési fázis alatt."""
        return self._temp_dmm_fault_ticks

    @property
    def psu_charge_current_setpoint_warning_ticks(self) -> int:
        """Kor 18, Batch C7 R17-10 -- l. konstruktor kommentje es
        safety.py::check_psu_charge_current_setpoint() docstringje.
        Jelenleg nincs report.json/checkpoint fogyasztoja, a load_
        current_setpoint_warning_ticks dokumentalt 'meg nincs
        fogyasztoja' precedenset koveteve."""
        return self._psu_charge_current_setpoint_warning_ticks

    @property
    def u_psu_set_V(self) -> float:
        return self._u_psu_set

    @property
    def consecutive_psu_voltage_readback_lost(self) -> int:
        """2026-09-15 D10 OV incidens -- l. ChargeConfig.psu_voltage_
        readback_lost_after_n_checks kommentje. A jelenlegi, folyamatos
        (sikeres olvasással nullázódó) sikertelen-readback sorozat hossza.
        Jelenleg nincs samples.csv/report.json fogyasztója (MAX verifier
        V7 lelete, 2026-09-15) -- a test_runner.py-beli session-szintű
        carry-over (a temp_dmm_fault_ticks/extreme_dt_s_count mintáját
        követve) külön, e finding hatókörén kívüli munka."""
        return self._consecutive_psu_voltage_readback_lost

    @property
    def i_psu_set_A(self) -> float:
        """Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        P2 lelet: a logger.py CSV_COLUMNS-ban deklarált psu_set_current_A
        mezőt test_runner.py::_build_sample() sosem töltötte ki — a
        ténylegesen parancsolt PSU-áram setpoint (CC/CV üzemmód-átmenet
        diagnosztikájához) így sosem volt rekonstruálható a samples.csv-ből
        (CLAUDE.md §12 "Setpoint, readback és commanded state külön
        fogalom")."""
        return self._charge_current_set_A

    @property
    def i_psu_measured_A(self) -> float:
        """A jelen ticknél TÉNYLEGESEN olvasott (nem parancsolt) PSU-áram —
        l. i_psu_set_A a setpoint-testvérpárért (CLAUDE.md §12 "Setpoint,
        readback és commanded state külön fogalom").

        Teljes kódbázis review 5. kör (2026-08-29), Metrológia/
        Adatintegritás P1 lelet: a beágyazott (GOLDEN_GG/CHEMID/OCV_SOC)
        controllerek korábban EGY MÁSODIK, önálló self._psu.
        measure_output_current() hívást indítottak kizárólag a session-
        szintű Ah/Wh-összesítéshez, és egy sikertelen olvasást csendben
        0.0-ra cseréltek (CLAUDE.md §10.4 "PSU áram töltés közben" tiltott
        csendes 0-helyettesítés) — akkor is, ha a beágyazott
        ChargeController SAJÁT, ugyanebben a tickben végzett olvasása
        sikeres volt. Ez a property a ChargeController MÁR fault-kezelt
        (advance()-ban self._i_charge = self._read_psu_current(), ami
        hiba esetén emergency_stop("PSU_COMM_LOST")-ot vált ki) belső
        állapotát exponálja — a hívónak nem kell/szabad újra olvasnia."""
        return self._i_charge

    @property
    def psu_current_readback_valid(self) -> bool | None:
        """Review-kör 6 (2026-09-15), C-08a (a fix-planner korrekciója az
        eredeti finding keretezéséhez képest: ez a mező az (a) LÉPÉSHEZ
        IS kötelező előfeltétel, nem csak egy jövőbeli (b) léréshez).
        `_read_psu_current()` korábban egy kommunikációs hibát csendben
        0.0-ra cserélt (`i_psu_measured_A` emiatt hiba esetén 0.0-t adott
        vissza, MEGKÜLÖNBÖZTETHETETLENÜL egy valódi 0 A áramtól) — a
        `test_runner.py::_build_sample()` hívó csak ezt a property-t
        vizsgálva tudja eldönteni, hogy `i_psu_measured_A` egy VALÓDI
        mérés, vagy egy hiba miatti (a FAULT-ot külön jelző) helykitöltő
        érték.

        TICK-SZINTŰ, nem "session-szintű" flag: `_advance_single()` MINDEN
        hívás ELEJÉN `None`-ra nullázza (l. ott), mielőtt bármelyik
        top-of-tick safety-kapu (concurrent_psu_load/DMM_FEEDBACK_LOST/
        battery_voltage/temperature_dmm_fault/ambient_temperature_rise)
        futna. Csak akkor lesz `True`/`False` EBBEN a tickben, ha a
        vezérlési állapot ténylegesen eljutott a `_read_psu_current()`
        hívásig (CHARGE_CC/CHARGE_CV_DMM_CONTROL/TAPER_HOLD saját
        futása). `None` marad minden más esetben -- akár mert a jelen
        példányon még sosem futott `_read_psu_current()` (PRECHECK/
        PSU_PRESET előtt), akár mert EBBEN a tickben egy fenti safety-
        kapu korábban kilépett (pl. a FAULT-ot kiváltó tick maga). Ez
        MINDKÉT esetben a hívó számára ugyanazt jelenti: nincs EBBEN a
        tickben megbízható controller-oldali érték, a NYERS `read_all()`
        readback-re kell visszaesnie (l. test_runner.py) -- MAX verifier
        F-1 lelete (2026-09-15): a nullázás előtt ez a property a fault-
        kapu-ticken hamisan az ELŐZŐ tick sikeres eredményét adta volna
        vissza, elévült áramot naplózva a legfontosabb diagnosztikai
        (FAULT) sorban."""
        return self._psu_current_readback_valid

    @property
    def u_psu_measured_V(self) -> float | None:
        """Review-kör 6 (2026-09-15), C-08b -- az `i_psu_measured_A`
        feszültség-testvérpárja: a jelen ticknél `_check_series_safety()`
        által TÉNYLEGESEN olvasott (nem parancsolt) PSU-feszültség.
        `None`, ha EBBEN a tickben `_check_series_safety()` nem futott le
        (nem CC/CV/TAPER_HOLD, l. `psu_voltage_readback_valid` testvér-
        property docstringje) VAGY lefutott, de a readback hibázott --
        ez utóbbi esetet `psu_voltage_readback_valid` különbözteti meg
        (`False`, nem `None`). Tick-elején `_advance_single()` nullázza,
        ugyanazt a mintát követve, mint `psu_current_readback_valid`
        (l. ott a MAX verifier F-1 lelete miatti staleness-tanulságot)."""
        return self._u_psu_measured_V

    @property
    def psu_voltage_readback_valid(self) -> bool | None:
        """Review-kör 6 (2026-09-15), C-08b -- `psu_current_readback_valid`
        feszültség-testvérpárja: `True`/`False`, ha EBBEN a tickben
        `_check_series_safety()` (CC/CV/TAPER_HOLD saját futása) ténylegesen
        megpróbálta olvasni a PSU-feszültséget; `None`, ha az adott
        tickben `_check_series_safety()` nem futott le (l. `u_psu_
        measured_V` testvér-property docstringje a pontos tick-szintű
        nullázási mintáért)."""
        return self._psu_voltage_readback_valid

    @property
    def u_batt_V(self) -> float:
        return self._u_batt

    @property
    def battery_temperature_C(self) -> float:
        return self._battery_temperature_C

    @property
    def dmm_valid(self) -> bool:
        return self._dmm_valid

    @property
    def temp_dmm_valid(self) -> bool:
        """Review-kör 3 (2026-09-09), friss Metrológia M3-01 (P1, DFAM-001+
        DFAM-018) -- l. discharge_controller.py azonos property docstringje
        a dmm_valid (feszültség) testvér-mintájáról."""
        return self._temp_dmm_valid

    @property
    def temp_dmm_ever_read_ok(self) -> bool:
        """[Kör 19 Batch D3c, F-3 rész (a)] True, ha a hőmérséklet-DMM
        LEGALÁBB EGYSZER sikeresen olvasott ezen a controlleren -- l.
        _temp_dmm_ever_read_ok docstringje __init__-ben. Soha nem
        nullázódik vissza egy session során (monoton)."""
        return self._temp_dmm_ever_read_ok

    # ------------------------------------------------------------------ #
    # Fő advance() — egy vezérlési ciklus                                 #
    # ------------------------------------------------------------------ #

    def advance(self, dt_s: float) -> ChargeState:
        """A hívó (TestRunner) tick-jét dolgozza fel — CC->CV határ közelében/
        CV-ben/TAPER_HOLD-ban a `_config.fast_loop_*` beállítások szerint
        ugyanazon a híváson belül több, ténylegesen mért idejű bel-ső
        résztick-et is lefuttat (l. `_advance_with_fast_loop()` docstring),
        hogy egy gyors dV/dt a külső tick-idő letelte előtt is kiváltsa a
        safety-check-et — máskülönben (távol a céltól, PRECHECK/PSU_PRESET
        stb.) pontosan egy `_advance_single()` hívás, a korábbi viselkedéssel
        bit-pontosan megegyezően."""
        self._last_fast_loop_extra_s = 0.0
        state = self._advance_single(dt_s)
        if state in (ChargeState.FAULT, ChargeState.CHARGE_DONE):
            return state
        # A felismert leállítás után nincs újabb résztick vagy várakozás.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return state
        if not self._config.fast_loop_enabled or not self._fast_loop_active():
            return state
        return self._advance_with_fast_loop(dt_s)

    def _fast_loop_active(self) -> bool:
        if self._state in (ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD):
            return True
        if self._state == ChargeState.CHARGE_CC:
            target_V = self._effective_charge_target_V()
            return self._u_batt >= target_V - self._config.fast_loop_proximity_V
        return False

    def _advance_with_fast_loop(self, dt_s: float) -> ChargeState:
        """A már lefutott első `_advance_single(dt_s)` (l. `advance()`) utáni
        KIEGÉSZÍTŐ résztick-ek — mindegyik VALÓS DMM/PSU-olvasást végez,
        `time.perf_counter()`-rel mérve a saját dt_s-ét (CLAUDE.md §12:
        tényleges, nem névleges dt). A `deadline` a legutóbbi dt_s alapján
        becsüli, kb. meddig van "hely" a következő KÜLSŐ tick esedékességéig
        — nem told el érdemben a teljes tick-időt, csak finomabb felbontásban
        használja fel. `fast_loop_max_substeps` kemény felső korlát a
        vészleállítás-válaszkészség megőrzésére (worst-case időkorlát)."""
        # Review 2026-09-15 (C-07a/DFAM-027). KORREKCIÓ (MAX verifier V-1
        # lelete, a jelen fájl korábbi verziójában): egy korábbi változat
        # itt azt állította, hogy ez a cap NEM változtat megfigyelhető
        # viselkedést, mert a `substeps < fast_loop_max_substeps` feltétel
        # MÁR önmagában azonos korlátot adna. Ez HAMIS volt -- a hibás
        # levezetés a substepek SAJÁT `_advance_single()` I/O-idejét (w,
        # pl. DMM/PSU SCPI round-trip) NULLÁNAK tételezte. Ha w > 0, a
        # substep-számláló `j*period_s` helyett `j*(period_s+w)` valós idő
        # után éri el a korlátot -- a verifier FUTÓ kóddal (valódi
        # ChargeController, lassított DMM-mock, MINDKÉT DMM -- feszültség
        # ÉS hőmérséklet -- lassítva, tehát w=2*io_s) empirikusan igazolta,
        # hogy `dt_s=60.0, period_s=0.2, max_substeps=20, io_s=0.10`
        # (w=0.20) mellett a capelt kód 11 substepet futtat, a cap NÉLKÜLI
        # kód 20-at -- ELTÉRŐ megfigyelhető kimenet. Zárt alak:
        # substeps = ceil((budget_s-period_s)/(period_s+w)) + 1(kezdő
        # olvasás), a cap NÉLKÜL substeps = fast_loop_max_substeps (a
        # deadline sosem a korlátozó tényező). A helyi teszt (l.
        # test_charge_controller.py::TestFastLoop::test_fast_loop_
        # deadline_cap_bounds_blocking_time_with_slow_io) CSAK a feszültség-
        # DMM-et lassítja (w=io_s=0.10) -> 14 substep -- UGYANAZ a
        # zárt alak, MÁSIK w-vel, l. CLAIM_LEDGER.yaml FAST-LOOP-DEADLINE-
        # CAP-001 a teljes rekonciliációért (MAX verifier 2. kör W-1
        # lelete javította a korábbi, téves "nem egyezik bitre" magyarázatot).
        # A cap tehát VALÓS, korlátozó irányú viselkedésváltozás (csökkenti
        # a worst-case blokkolási időt és a naplózatlan/vészleállítás-
        # érzéketlen ablakot nagy, degradált `dt_s` ÉS nem-nulla substep
        # I/O-idő mellett egyszerre) -- diszkrimináló regressziós teszt:
        # test_charge_controller.py::TestFastLoop::
        # test_fast_loop_deadline_cap_bounds_blocking_time_with_slow_io.
        budget_s = min(dt_s, self._config.fast_loop_max_substeps * self._config.fast_loop_period_s)
        deadline = time.perf_counter() + max(budget_s - self._config.fast_loop_period_s, 0.0)
        substeps = 1
        last_t = time.perf_counter()
        state = self._state
        while (
            self._fast_loop_active()
            and substeps < self._config.fast_loop_max_substeps
            and time.perf_counter() < deadline
        ):
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return state
            time.sleep(self._config.fast_loop_period_s)
            # A várakozás alatt is érkezhetett jelzés; ne kezdjünk új mérést.
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return state
            now = time.perf_counter()
            sub_dt = now - last_t
            last_t = now
            self._last_fast_loop_extra_s += sub_dt
            state = self._advance_single(sub_dt)
            substeps += 1
            if state in (ChargeState.FAULT, ChargeState.CHARGE_DONE):
                return state
        return state

    def _advance_single(self, dt_s: float) -> ChargeState:
        self._elapsed_s += dt_s
        self._last_warning_code = ""
        # Review-kör 6 (2026-09-15), C-08a MAX verifier F-1 (P1, blokkoló):
        # psu_current_readback_valid korábban "latching" volt -- csak
        # _read_psu_current() (CC/CV/TAPER_HOLD futása) írta, tehát egy
        # top-of-tick safety-kapu (concurrent_psu_load/DMM_FEEDBACK_LOST/
        # battery_voltage/temperature_dmm_fault/ambient_temperature_rise,
        # l. lent) által kiváltott FAULT-ticken -- ahol _read_psu_current()
        # NEM fut le -- az ELŐZŐ tickről megmaradt True-t adta vissza. A
        # hívó (test_runner.py::_build_sample()) emiatt a FAULT-tick
        # LEGFONTOSABB diagnosztikai sorában egy ELÉVÜLT controller-értéket
        # naplózott a friss, a szabályos read_all() readback helyett --
        # pontosan a D10 OV-incidens hibaosztálya, amiből ez a review-kör
        # indult. Nullázás minden tick ELEJÉN (a fault-kapuk ELŐTT): egy
        # kapu-FAULT tickre a property None marad, a hívó a friss nyers
        # readbackre esik vissza (bit-pontosan a C-08a előtti viselkedés
        # ezeken a tickeken), miközben a normál (CC/CV/TAPER_HOLD-ban
        # ténylegesen olvasó) tickeken a C-08a javulás változatlan marad.
        self._psu_current_readback_valid = None
        # Review-kör 6 (2026-09-15), C-08b -- ELŐRE alkalmazva a C-08a MAX
        # verifier F-1 leletének tanulságát (l. fent): egy ÚJ tick-szintű
        # validity-flag bevezetésekor a nullázásnak a tick ELEJÉN, minden
        # fault-kapu ELŐTT kell megtörténnie, nem utólag javítva. A
        # _check_series_safety() (csak CC/CV/TAPER_HOLD saját futásában
        # hívva, l. ott) ugyanígy csak EBBEN a tickben frissülő
        # u_psu_measured_V/psu_voltage_readback_valid property-t exponál.
        self._u_psu_measured_V = None
        self._psu_voltage_readback_valid = None

        if self._state == ChargeState.FAULT:
            return self._state

        # Olvasások (hibabiztos)
        self._dmm_valid = self._read_dmm(dt_s)

        # R11-B4: csak az aktualis tick ervenyes homerseklete donthet.
        if self._state in (
            ChargeState.INIT,
            ChargeState.PRECHECK,
            ChargeState.PSU_PRESET,
            ChargeState.CHARGE_CC,
            ChargeState.CHARGE_CV_DMM_CONTROL,
            ChargeState.TAPER_HOLD,
        ):
            overtemp = self._safety.check_battery_overtemperature(
                self._battery_temperature_C, self._temp_dmm_valid,
            )
            if overtemp.fault is not None:
                self.emergency_stop(overtemp.fault.name)
                return self._state

        # R11-B1: az atadott kiesesi elozmeny mar indulaskor is ervenyes.
        if self._state in (ChargeState.INIT, ChargeState.PRECHECK):
            t_result = self._safety.check_temperature_dmm_fault(
                self._safety.temperature_dmm_fault_s,
            )
            if t_result.fault is not None:
                self.emergency_stop(t_result.fault.name)
                return self._state

        if self._state in (
            ChargeState.PSU_PRESET,
            ChargeState.CHARGE_CC,
            ChargeState.CHARGE_CV_DMM_CONTROL,
            ChargeState.TAPER_HOLD,
        ):
            # Teljes kódbázis review 14. kör (2026-09-04), Hardver/
            # Műszervezérlés P1 lelet: a kölcsönös kizárás ellenőrzést
            # (CLAUDE.md §10.2) korábban KIZÁRÓLAG a PSU-t MÁR bekapcsolt
            # állapotokban futtattuk — a PSU_PRESET (ahol _run_psu_preset()
            # ténylegesen kiadja a psu.output_on()-t, l. lent) NEM volt
            # benne. Ez az EGYETLEN hely a kódbázisban, ahol
            # ChargeController.psu.output_on() meghívódik (minden töltési
            # útvonal — CHARGE_ONLY, TRADITIONAL_LEARNING PRECHARGE/
            # RECHARGE, GOLDEN_GG, OCV_SOC PRECHARGE, CHEMID — ezen megy át)
            # — egy korábbi write-hiba miatt esetlegesen stale maradt
            # load.input_commanded_on flag esetén a PSU minden ellenőrzés
            # nélkül azonnal ON-ba kerülhetett volna, legalább egy tick
            # erejéig valós PSU+Load egyidejű aktív állapotot okozva (l. a
            # testvér DischargeController DISCHARGE_CC_SETUP-mintáját).
            concurrent_result = self._safety.check_concurrent_psu_load(
                psu_commanded_on=self._psu.output_commanded_on,
                load_commanded_on=self._load.input_commanded_on,
            )
            if concurrent_result.fault is not None:
                self.emergency_stop(concurrent_result.fault.name)
                return self._state

            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return self._state

            # Q-42: a friss, ON elotti es aktiv TLC-minta sem lehet 1.70 alatt.
            v_result = (
                self._safety.check_precheck_voltage(self._u_batt, _traditional_learning=True)
                if self._traditional_learning else self._safety.check_battery_voltage(self._u_batt)
            )
            if v_result.fault is not None:
                self.emergency_stop(v_result.fault.name)
                return self._state

            t_result = self._safety.check_temperature_dmm_fault(
                self._safety.temperature_dmm_fault_s,
            )
            if t_result.fault is not None:
                self.emergency_stop(t_result.fault.name)
                return self._state
            if t_result.warning is not None:
                self._last_warning_code = t_result.warning.name

            # Teljes kódbázis review 20. kör (2026-09-06), Metrológia/
            # Adatintegritás P2 lelet: l. safety.py::check_test_temperature_
            # range() docstringje — korábban a profil test_temperature_min_C/
            # max_C mezői betöltődtek, de semmi nem vetette össze a mért
            # hőmérséklettel.
            # Teljes kódbázis review 21. kör (2026-09-06), GUI/UX + Metrológia
            # P2 lelet (2 ágens egymástól függetlenül): a fenti feltétel
            # nélküli felülírás néma elveszíthette a súlyosabb (a mérés
            # MEGBÍZHATÓSÁGÁT kérdőjelező) TEMPERATURE_DMM_LOST warningot,
            # ha a stale _battery_temperature_C (a DMM-kiesés alatt nem
            # frissül) épp a profil tartományán kívül esett — pontosan a
            # leggyakoribb, nem-ritka forgatókönyvben (szándékosan eltérő
            # hőmérsékleten futó session). A DMM-kiesés jelzése elsőbbséget
            # élvez.
            range_result = self._safety.check_test_temperature_range(self._battery_temperature_C)
            if range_result.warning is not None:
                self._temperature_out_of_range_count += 1
                if t_result.warning is None:
                    self._last_warning_code = range_result.warning.name

            # CHG-004: FIAMM temperature-rise termination — csak akkor kényszerül
            # ki, ha van tényleges (nem UNKNOWN) ambient referencia.
            ambient_result = self._safety.check_ambient_temperature_rise(
                self._battery_temperature_C,
                self._config.ambient_temperature_source,
                self._config.ambient_temperature_C,
            )
            if ambient_result.fault is not None:
                self.emergency_stop(ambient_result.fault.name)
                return self._state

        # Állapotgép átmenetek
        if self._state == ChargeState.INIT:
            self._state = ChargeState.PRECHECK

        elif self._state == ChargeState.PRECHECK:
            self._run_precheck()

        elif self._state == ChargeState.PSU_PRESET:
            self._run_psu_preset()

        elif self._state == ChargeState.CHARGE_CC:
            self._run_charge_cc(dt_s)

        elif self._state == ChargeState.CHARGE_CV_DMM_CONTROL:
            self._run_charge_cv(dt_s)

        elif self._state == ChargeState.TAPER_HOLD:
            self._run_taper_hold(dt_s)

        elif self._state in (ChargeState.CHARGE_DONE, ChargeState.FAULT):
            pass  # terminális állapotok, nincs további átmenet

        else:
            # Review-kör 5, Session/Workflow S5-05 (P3, DFAM-004 sibling --
            # l. test_runner.py::_is_finished()/_controller_faulted() S4-07
            # kommentje ugyanerről a hibaosztályról): a fenti if/elif lánc
            # korábban hallgatólagosan (else nélkül) no-op-olt egy jövőbeli,
            # itt le nem kezelt ChargeState-re -- a controller ÖRÖKRE
            # ugyanabban az állapotban ragadt volna (a bővítés fel sem
            # tűnt volna, amíg valaki a session végtelen hosszát észre nem
            # veszi). Explicit raise -- a test_runner.py::_run_step()
            # tick-huroktól kívülről érkező kivétel-kezelés (UNHANDLED_
            # EXCEPTION FAULT + safe-off) ugyanígy már bizonyítottan
            # helyesen kezeli, mint a test_runner.py-szintű S4-07 fix.
            raise ValueError(f"Unhandled ChargeState in advance(): {self._state!r}")

        return self._state

    # ------------------------------------------------------------------ #
    # Vészleállítás [R1] — LOAD OFF → PSU OFF, relay soha                 #
    # ------------------------------------------------------------------ #

    def emergency_stop(self, reason: str) -> None:
        # Teljes kódbázis review 7. kör (2026-08-30), Hardver/Műszervezérlés
        # P2 lelet: a végső fizikai állapot mindig helyesen jelentett marad
        # (a TestRunner._emergency_stop()/InstrumentManager.safe_all_off()
        # külső rétege lefedi, 1. kör 5b088e2 óta), DE korábban az ELSŐ
        # (itteni) OFF-kísérlet hibájának ténye/szövege csendben elveszett
        # (except Exception: pass) — sosem került be a fault_reason-be/
        # report.json-ba (CLAUDE.md §13 "safe-off eredménye" + §23 "kritikus
        # kivételt üres except-tel elnyelni tilos"). Most a hibaszöveg a
        # fault_reason-höz fűződik.
        off_failures: list[str] = []
        try:
            self._load.input_off()
        except Exception as exc:
            off_failures.append(f"LOAD_OFF_FAILED:{exc}")
        try:
            self._psu.all_outputs_off()
        except Exception as exc:
            off_failures.append(f"PSU_OFF_FAILED:{exc}")
        self._fault_reason = reason if not off_failures else reason + " | " + " | ".join(off_failures)
        # NINCS relay.safe_open() — nincs relé hardware [R1]
        self._state = ChargeState.FAULT

    # ------------------------------------------------------------------ #
    # Állapot implementációk                                               #
    # ------------------------------------------------------------------ #

    def _run_precheck(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

        mode_result = self._safety.check_psu_mode_compatibility()
        if mode_result.fault is not None:
            self.emergency_stop(mode_result.fault.name)
            return
        # Teljes kódbázis review 23. kör (2026-09-07), Hardver/Műszervezérlés
        # P2 lelet: check_psu_mode_compatibility() saját docstringje szerint
        # egy 12V pack + SERIES PSU-mód kombinációra "nem tiltott, de
        # szokatlan és kezelői figyelmeztető dialógus szükséges" — a warning
        # korábban itt sosem került kiolvasásra, sem last_warning_code-ba,
        # sem eseménynaplóba/riportba nem jutott el. Mivel _run_precheck()
        # az egyetlen hívóhely, ez minden töltési útvonalra (CHARGE_ONLY,
        # TRADITIONAL_LEARNING PRECHARGE/RECHARGE, GOLDEN_GG, OCV_SOC
        # PRECHARGE, CHEMID) vonatkozott.
        if mode_result.warning is not None:
            self._last_warning_code = mode_result.warning.name

        voltage_result = self._safety.check_precheck_voltage(
            self._u_batt, _traditional_learning=self._traditional_learning,
        )
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return

        # Teljes kódbázis review 14. kör (2026-09-04), Hardver/
        # Műszervezérlés P1 lelet — a fenti PSU_PRESET-kapu melletti
        # második, proaktív védelmi réteg: a testvér
        # DischargeController._run_precheck() szimmetrikus mintáját
        # követve (psu.all_outputs_off() kisütés előtt), itt Load OFF-ot
        # kényszerítünk töltés (PSU bekapcsolása) előtt.
        # Teljes kódbázis review 18. kör (2026-09-05), Hardver/
        # Műszervezérlés P1 lelet: a fenti hívás maga védtelen maradt — a
        # 14. körben bemásolt minta a forrás (DischargeController) akkori
        # bugját is átvette. Kivétel esetén korábban a specifikus
        # diagnosztika helyett csak a TestRunner legkülső, generikus
        # UNHANDLED_EXCEPTION jutott a riportba.
        try:
            self._load.input_off()
        except Exception as exc:
            self.emergency_stop(f"CHARGE_PRECHECK_LOAD_OFF_FAILED: {exc}")
            return
        self._state = ChargeState.PSU_PRESET

    def _run_psu_preset(self) -> None:
        target_V = self._effective_charge_target_V()
        # 2026-08-23 (felhasználói review, 2. kör — traditional_learning OV
        # incidens): a PSU induló compliance-e PONTOSAN target_V, semmilyen
        # feltételezett dióda-/kábelesés-headroom nélkül. A korábbi
        # `target + max_expected_series_drop_V(_bq)` egyszeri, feltételezésen
        # alapuló ugrás volt — a 3,50V-os BQ-headroom valójában egy FAULT-
        # küszöb (l. SafetyManager.check_series_drop), nem töltési compliance-
        # tartalék. Innentől a `_maybe_escalate_cc_compliance()` MÉRT
        # áram-hiány alapján, kis lépésekben emeli a compliance-t, ha a
        # tényleges (pl. TF1 panelen át mért) esés nagyobbnak bizonyul —
        # sosem lépve túl a target_V + max_expected_series_drop_V(_bq)
        # plafont, aminek elérése önmagában fizikai bekötési hiba jele.
        # Keithley 2220-30-1 hardver limit: INDEPENDENT/SERIES → 1.5A/csatorna,
        # PARALLEL → 3.0A. Ha a kiszámított töltőáram meghaladja ezt, a PSU
        # visszautasítja a parancsot (nem clampel!) és az előző érték marad érvényes.
        psu_hw_max_A = _PSU_HW_MAX_CHARGE_A.get(self._safety.psu_mode, 1.5)

        if self._config.charge_current_A_override > 0:
            requested_charge_A = self._config.charge_current_A_override
        else:
            requested_charge_A = self._profile.effective_max_charge_A
        charge_A = min(requested_charge_A, psu_hw_max_A)
        # R9-B1: a feloldott aram a profilmaximumot sem lepheti tul.
        if charge_A > self._profile.effective_max_charge_A:
            self.emergency_stop(
                f"{FaultCode.CHARGE_CURRENT_LIMIT_EXCEEDED.name}: "
                f"{charge_A} A > profile maximum "
                f"{self._profile.effective_max_charge_A} A"
            )
            return
        # Review-kör 2 (2026-09-09), friss Szoftver/Architektúra F3 (P3,
        # DFAM-026 declared_policy_without_consumer + DFAM-017 "no silent
        # clamp" elv): a fenti min() korábban CSENDBEN vágta a kért
        # töltőáramot a PSU-mód hardverlimitjére, miközben a projektnek MÁR
        # van egy pontosan erre nevesített WarningCode-ja, aminek eddig
        # nulla producere volt.
        if charge_A < requested_charge_A:
            self._last_warning_code = WarningCode.REDUCED_CHARGE_CURRENT_DUE_TO_PSU_LIMIT.name
        # Teljes kódbázis review 14. kör (2026-09-04), Hardver/
        # Műszervezérlés P1 lelet: a fenti (tuple-bővítéses) PSU_PRESET-
        # kapu ÖNMAGÁBAN NEM elég — a check_concurrent_psu_load() a
        # JELENLEGI psu_commanded_on/load_commanded_on állapotot AND-olja,
        # psu_commanded_on pedig itt, output_on() ELŐTT MÉG False, tehát
        # egy stale Load-flag (pl. egy korábbi töltési/kisütési fázis
        # write-hibája miatt) a régi kód mellett soha nem került volna
        # elkapásra ELŐZETESEN. Itt EXPLICIT, ELŐREJELZŐ ellenőrzést
        # végzünk: "ha MOST bekapcsolnám a PSU-t, ütközne-e a Load
        # JELENLEGI állapotával" — psu_commanded_on=True-t adva át
        # szimulálja a mindjárt bekövetkező állapotot.
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=True,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        # R16-B12: a DMM-olvasás alatt érkező jelzés tiltja a PSU-konfigurációt is.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        if self._require_confirmed_off:
            try:
                if self._load.read_input_state() is not False or self._psu.read_output_state() is not False:
                    self.emergency_stop("LEARNING_CHARGE_OFF_UNCONFIRMED")
                    return
            except Exception as exc:  # noqa: BLE001 - any unconfirmed OFF blocks activation
                self.emergency_stop(f"LEARNING_CHARGE_OFF_READBACK_FAILED:{exc}")
                return
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return
        self._u_psu_set = target_V
        self._charge_current_set_A = charge_A
        # Teljes kódbázis review 18. kör (2026-09-05), Szoftver/Architektúra
        # + Hardver/Műszervezérlés P1 lelet (2 ágens egymástól függetlenül):
        # ez az EGYETLEN hely a teljes kódbázisban, ahol psu.output_on()
        # meghívódik (minden töltési útvonal ezen az egy állapotgépen megy
        # át) — a 3 PSU-hívás (set_output_voltage/set_output_current/
        # output_on) mégis védtelen maradt, szemben a testvér input_on()
        # hívásokkal, amik MIND try/except-ben vannak. Kivétel esetén
        # korábban a controller sosem állt FAULT-ra, a specifikus
        # diagnosztika elveszett.
        try:
            self._psu.set_output_voltage(self._u_psu_set)
            self._psu.set_output_current(charge_A)
        except Exception as exc:
            self.emergency_stop(f"PSU_PRESET_FAILED: {exc}")
            return
        try:
            self._psu.output_on(stop_requested=self.handoff_stop_requested)
        except TestAbortedByUser:
            # A runner az élő okot oldja fel, és egyszer kapcsolja le mindkét műszert.
            return
        except Exception as exc:
            self.emergency_stop(f"PSU_PRESET_FAILED: {exc}")
            return
        self._state = ChargeState.CHARGE_CC

    def _check_charge_limits(self) -> bool:
        """K2: Ah és idő limit ellenőrzés. True = limit elérve, emergency_stop meghívva."""
        if self._elapsed_s > self._config.max_charge_time_s:
            self.emergency_stop("MAX_CHARGE_TIME_REACHED")
            return True
        # Teljes kódbázis review 18. kör (2026-09-05), Session/Workflow +
        # Metrológia/Adatintegritás P2 lelet (2 ágens egymástól
        # függetlenül): a védendő töltőáram (_run_psu_preset() ->
        # profile.effective_max_charge_A) a kanonikus _c10_base_Ah-ból
        # (0.25×C10) származik, ez a runaway-backstop viszont a nyers
        # nominal_capacity_Ah-t (FG-sorozatnál C20-alapú, nagyobb szám)
        # használta — CLAUDE.md §11.1 "a C10 és C20 nem cserélhető fel".
        # Pontos tükörképe a kisütési oldalon már javított hibaosztálynak
        # (11. kör 5b62b0f, 13. kör 682d97e) — a töltési oldal analóg
        # backstopja eddig kimaradt a keresésből.
        if self._integrator.accumulated_charge_Ah > (
            self._profile._c10_base_Ah * self._config.max_charge_Ah_factor
        ):
            self.emergency_stop("MAX_CHARGE_AH_REACHED")
            return True
        return False

    def _run_charge_cc(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        self._integrate(dt_s, signed_current_A=self._i_charge, source="PSU_READBACK")

        # Kor 18, Batch C7 R17-10 (DFAM-001): a readback itt NEM dobott
        # kivetelt -- _read_psu_current() hiba eseten emergency_stop()-ot
        # valt ki, es a fenti FAULT-ellenorzes mar visszatert volna. Ez
        # NEM zarja ki a NaN/Inf esetet (MAX verifier P3-2 lelete, a
        # check_load_current_setpoint() testverenel is jelenlevo,
        # meglevo korlat -- kulon, jovobeli tetel). A
        # `_cc_compliance_headroom_exhausted` az ELOZO tick VEGEN a
        # `_maybe_escalate_cc_compliance()`-ben (lent, ugyanebben a
        # fuggvenyben, MEG NEM futott ezen a ticken) szamolt allapot --
        # ez szandekos: a flag TIPIKUSAN azt irja le, hogy a jelen tick
        # mert arama milyen compliance-allapot alatt keletkezett, DE a
        # hoszabalyozott celfeszultseg (target_V) valtozasakor (pl.
        # TempCompMode.ENABLED hokompenzacio) a plafon maga is elmozdulhat
        # UGYANEBBEN a tickben -- ezen az egy ticken a flag STALE lehet
        # (MAX verifier P3-3 lelete), max. 1 tuzeles hatarral,
        # onkorrigalo a kovetkezo ticken. A _last_warning_code slot csak
        # az ELOTTE (feljebb ebben a tickben) beallitott figyelmeztetest
        # nem irja felul -- a KESOBB (lent, _check_series_safety()-ben)
        # futo PSU_VOLTAGE_READBACK_LOST/DIODE_POWER_* MAR FELULIRHATJA
        # (MAX verifier P3-4 lelete, egyiranyu vedelem -- a szamlalo
        # ettol fuggetlenul mindig no, l. discharge_controller.py
        # HW4-02 azonos mintaja).
        setpoint_result = self._safety.check_psu_charge_current_setpoint(
            self._i_charge, self._charge_current_set_A,
            compliance_headroom_exhausted=self._cc_compliance_headroom_exhausted,
        )
        if setpoint_result.warning is not None:
            self._psu_charge_current_setpoint_warning_ticks += 1
            if self._last_warning_code == "":
                self._last_warning_code = setpoint_result.warning.name

        if self._check_charge_limits():
            return

        if self._check_series_safety():
            return

        if not self._maybe_escalate_cc_compliance():
            return

        target_V = self._effective_charge_target_V()
        if self._u_batt >= target_V - self._config.cv_entry_margin_V:
            # Review-kör 8 (2026-09-15), R2-01 (P0): itt korábban egy MÁSODIK,
            # független `_read_psu_voltage()` futott -- holott a fenti
            # `_check_series_safety()` (:859) UGYANEBBEN a tickben MÁR
            # elvégezte a fizikai olvasást, és a C-08b invariáns szerint
            # feltétel nélkül, MINDEN korai return ELŐTT rögzítette
            # `_u_psu_measured_V`-be (l. `_check_series_safety()` eleje). A
            # köztes `_maybe_escalate_cc_compliance()` ezen a ticken
            # bizonyíthatóan NEM ír a PSU-ra (saját cv_entry_margin_V
            # feltétele UGYANOLYAN korai True-val tér vissza, mint a fenti
            # sor), tehát a mért érték NEM stale. A második olvasás hibája
            # (None) a régi `else`-ágon CSENDBEN, mérés nélkül a MAXIMÁLIS
            # plafonra (BQ-módban target+3,50 V) ugratta a PSU-t -- FELFELÉ a
            # `_run_psu_preset()` target_V indulóértékéhez képest --,
            # megkerülve a MÁR bekötött readback-vesztés-eszkalációt
            # (PSU_VOLTAGE_READBACK_LOST + _consecutive_psu_voltage_
            # readback_lost + 3 után FAULT). CLAUDE.md §6.4: kritikus
            # readbacket tilos setpointtal csendben helyettesíteni.
            u_psu_now = self._u_psu_measured_V
            if u_psu_now is None:
                # MAX verifier F-3 pontosítás (2026-09-15): ez az ág CSAK
                # azokon a tickeken fut, ahol `_check_series_safety()` a
                # readback-hiba ELLENÉRE False-t adott (1./2. egymást
                # követő hiba) -- a warning-kód és a számláló-növelés MÁR
                # megtörtént ott, de a FAULT+safe-off NEM: a 3. hibánál
                # `_check_series_safety()` maga True-t ad, és
                # `_run_charge_cc()` a hívás helyén (fentebb) tér vissza,
                # ide SOSEM jut el. Itt CSAK annyi a dolgunk, hogy mérés
                # nélkül NE lépjünk CV-be és NE írjunk a PSU-ra: a setpoint
                # a jelenlegi (<= plafon) értékén marad, a következő tick
                # sikeres readbackje normálisan belép.
                return
            # CV belépési PSU preset = cél + aktuálisan mért esés (dióda + kábel).
            # A PSU-t AZONNAL csökkentjük — különben marad a 15.30V compliance-en
            # egy teljes tickig, ami elegendő OV-t okozni, ha u_batt már közel van
            # a határhoz. Felső korlát: target_V + várt esés — ennek elérése
            # fizikai bekötési hiba jele (l. 2026-08-23 traditional_learning OV
            # incidens elemzés, 2. kör), NEM a cél elérésének elsődleges
            # mechanizmusa; a tényleges célra a _regulate_cv() DMM-hurokja fut rá.
            u_drop = max(u_psu_now - self._u_batt, 0.0)
            voltage_V = min(
                target_V + u_drop,
                target_V + self._effective_max_expected_series_drop_V(),
            )
            if not self._set_psu_voltage_safe(voltage_V, "CC_TO_CV_PSU_SET_FAILED"):
                return
            self._state = ChargeState.CHARGE_CV_DMM_CONTROL

    def _run_charge_cv(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        regulated = self._regulate_cv()
        if self._state == ChargeState.FAULT:
            return
        # A STOP csak az írást tiltja; a mért intervallum egyszer elszámolandó.
        self._integrate(dt_s, signed_current_A=self._i_charge, source="PSU_READBACK")

        if self._check_charge_limits():
            return

        if self._check_series_safety():
            return

        # A tick safety-feloldása megelőzi a STOP miatti visszatérést.
        if not regulated:
            return
        if self._check_taper_condition():
            self._state = ChargeState.TAPER_HOLD

    def _run_taper_hold(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        # Csak lefelé szabályoz: ha u_batt > target, PSU csökkenti a feszültséget.
        # Felfelé lépés tiltott — OV-védelmi garancia a taper hold teljes idejére.
        regulated = self._regulate_cv(up_allowed=False)
        if self._state == ChargeState.FAULT:
            return
        # A STOP mellett is megmarad a már megmért töltés, kettőzés nélkül.
        self._integrate(dt_s, signed_current_A=self._i_charge, source="PSU_READBACK")

        if self._check_charge_limits():
            return

        if self._check_series_safety():
            return

        if not regulated:
            return
        if not self._check_taper_hold_condition():
            self._taper_timer_s = 0.0
            self._state = ChargeState.CHARGE_CV_DMM_CONTROL
            return

        self._taper_timer_s += dt_s
        if self._taper_timer_s >= self._config.taper_hold_s:
            # Teljes kódbázis review 17. kör (2026-09-05), Hardver/
            # Műszervezérlés P2 lelet: a sikeres töltés-befejezéskori
            # psu.output_off() korábban csupasz volt — az emergency_stop()
            # már véd hasonló OFF-hívásokat (fent), de a normál CHARGE_DONE
            # útról ez hiányzott. Kivétel esetén a specifikus diagnosztika
            # helyett csak a TestRunner legkülső UNHANDLED_EXCEPTION-je
            # jutott volna a riportba.
            try:
                self._psu.output_off()
            except Exception as exc:
                self.emergency_stop(f"CHARGE_DONE_PSU_OFF_FAILED: {exc}")
                return
            self._state = ChargeState.CHARGE_DONE

    # ------------------------------------------------------------------ #
    # CC compliance-escalation — mért áram-hiány alapú, fokozatos          #
    # ------------------------------------------------------------------ #

    def _set_psu_voltage_safe(self, voltage_V: float, fault_tag: str) -> bool:
        """Teljes kódbázis review 19. kör (2026-09-06), Hardver/
        Műszervezérlés P1 lelet: a CC/CV regulációs hurok mindhárom
        set_output_voltage() hívóhelye (CC->CV átmenet, compliance-
        escalation, _regulate_cv()) védtelen volt, szemben az egyszeri
        _run_psu_preset()-tel (14/17/18. kör) és a záró output_off()-fal
        (17. kör) — ez volt a legmagasabb hívási gyakoriságú védtelen
        hardver-write a teljes kódbázisban (fast_loop módban akár 5 Hz).
        Kivétel esetén korábban a specifikus diagnosztika helyett csak a
        TestRunner legkülső, generikus UNHANDLED_EXCEPTION jutott a
        riportba. False esetén a hívó nem folytathatja: FAULT vagy élő
        leállításkérés tiltja az írást."""
        # R16-B12: a DMM/PSU-olvasás vagy fast-loop várakozás alatti leállítás élő.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return False
        try:
            self._psu.set_output_voltage(voltage_V)
        except Exception as exc:
            self.emergency_stop(f"{fault_tag}: {exc}")
            return False
        self._u_psu_set = voltage_V
        return True

    def _maybe_escalate_cc_compliance(self) -> bool:
        """2026-08-23 (felhasználói review, 2. kör): CC fázisban a PSU
        MINDIG pontosan target_V-n indul (l. `_run_psu_preset()`) — ha a
        tényleges soros esés (pl. TF1 panel BQ/panel-módban) ezt nem engedi
        elérni a beállított áramot, a PSU compliance-limitbe fut, és a mért
        áram tartósan a setpoint alatt marad. Ez a metódus EZT a mért
        hiányt figyeli, és csak akkor emeli a compliance-t — kis,
        `max_step_up_V`-lépésekben, ugyanazzal az elvvel, mint
        `_regulate_cv()` —, ha ténylegesen szükséges, a
        `target_V + effective_max_expected_series_drop_V()` plafonig. Ennek
        a plafonnak az elérése önmagában fizikai bekötési hiba jele — a
        `check_series_drop()` (mért esésre, nem a compliance-setpointra
        épülő) fault-küszöb ezt jellemzően már ELŐBB elkapja (l.
        `_check_series_safety()`, minden tick elején lefut). Nem CV-belépés
        közelében (azt a `_regulate_cv()` DMM-hurokja kezeli) és nem akkor,
        ha a cél-áram már gyakorlatilag folyik.

        False esetén FAULT vagy élő leállításkérés tiltja a folytatást
        (l. _set_psu_voltage_safe()).

        Kor 18, Batch C7 R17-10: minden korai return ELŐTT explicit
        `_cc_compliance_headroom_exhausted = False`-ra állítunk, hogy egy
        tick-scoped flag SOSE hagyja az ELŐZŐ tick értékét állva (l.
        feedback_new_tick_scoped_validity_flag_needs_explicit_reset.md);
        CSAK a plafon-elérési ág állítja True-ra."""
        self._cc_compliance_headroom_exhausted = False
        if self._charge_current_set_A <= 0:
            return True
        target_V = self._effective_charge_target_V()
        if self._u_batt >= target_V - self._config.cv_entry_margin_V:
            return True
        if self._i_charge >= self._charge_current_set_A * 0.98:
            return True
        ceiling_V = target_V + self._effective_max_expected_series_drop_V()
        if self._u_psu_set >= ceiling_V - 1e-6:
            # R17-10: a compliance-emeles kimerult -- INNENTOL a mert aram
            # elmaradasa nem magyarazhato javithato compliance-hiannyal.
            self._cc_compliance_headroom_exhausted = True
            return True
        step = min(self._config.max_step_up_V, ceiling_V - self._u_psu_set)
        return self._set_psu_voltage_safe(
            self._u_psu_set + step, "CC_COMPLIANCE_ESCALATION_FAILED",
        )

    # ------------------------------------------------------------------ #
    # CV szabályozóhurok                                                   #
    # ------------------------------------------------------------------ #

    def _regulate_cv(self, up_allowed: bool = True) -> bool:
        """False: FAULT vagy leállítás miatt elmaradt írás. Leállításnál a
        hívó még elszámolja a mért intervallumot és feloldja a tick safety-jét.
        A korai visszatérési ágak (deadband-en
        belül, felfelé lépés tiltva) mindig True-t adnak, mert nem
        érintenek hardvert."""
        target_V = self._effective_charge_target_V()
        error = target_V - self._u_batt

        if abs(error) <= self._config.deadband_V:
            return True

        voltage_V = self._u_psu_set
        if error > 0:
            if not up_allowed:
                return True
            step = min(error, self._config.max_step_up_V)
            voltage_V += step
        else:
            step = min(abs(error), self._config.max_step_down_V)
            voltage_V -= step

        psu_mode_max = {
            PsuMode.INDEPENDENT: 30.0,
            PsuMode.PARALLEL: 30.0,
            PsuMode.SERIES: 60.0,
        }.get(self._safety.psu_mode, 30.0)

        voltage_V = min(voltage_V, psu_mode_max)
        # Backstop: ha a DMM-visszacsatolás leszakad/hibás (u_batt stuck egy
        # alacsony értéken), a szabályozó folyamatosan "fel" hibát látna és a
        # compliance-t a végtelenségig emelné — ez a felső korlát biztosítja,
        # hogy a PSU ekkor se mehessen target_V + várt esés fölé (ennek
        # elérése önmagában fizikai bekötési hiba jele, l. 2026-08-23
        # traditional_learning OV incidens elemzés, 2. kör).
        voltage_V = min(
            voltage_V,
            target_V + self._effective_max_expected_series_drop_V()
        )
        voltage_V = max(voltage_V, 0.0)

        return self._set_psu_voltage_safe(voltage_V, "CV_REGULATION_PSU_SET_FAILED")

    # ------------------------------------------------------------------ #
    # Taper feltétel [R9]                                                  #
    # Implementáld ezt a metódust (5-8 sor):                              #
    # ------------------------------------------------------------------ #

    def _check_taper_condition(self) -> bool:
        """
        [R9] Taper feltétel formális ellenőrzés — BELÉPÉSI (szűk) küszöb.

        TAPER_HOLD-ba BELÉPÉS csak akkor, ha MINDEN feltétel teljesül:
          1. U_batt_DMM >= (U_target - cv_voltage_tolerance_V)
          2. I_charge <= taper_current_A  (self._profile.effective_taper_A)
          3. dmm_voltage_valid == True    (self._dmm_valid)

        A TAPER_HOLD-ban maradáshoz (dwell-idő gyűjtése alatti kiértékelés)
        a hiszterézises, tágabb küszöböt használó _check_taper_hold_condition()
        felel (l. annak docstringje) — enélkül a küszöb körüli valós DMM/PSU
        readback-zaj minden határátlépésnél nullázná a _taper_timer_s-t, meg-
        akadályozva a taper_hold_s dwell-idő összegyűlését (Teljes kódbázis
        review 6. kör, Szoftver/Architektúra P2 lelet #2).

        Returns:
            bool — True: a szűk belépési feltétel teljesül
        """
        if not self._dmm_valid:
            return False

        cv_tolerance_V = (
            self._profile.cell_count
            * self._config.cv_voltage_tolerance_V_per_cell
        )

        voltage_ok = self._u_batt >= (
            self._effective_charge_target_V() - cv_tolerance_V
        )
        current_ok = self._i_charge <= self._profile.effective_taper_A

        return voltage_ok and current_ok

    def _check_taper_hold_condition(self) -> bool:
        """
        [R9] Taper feltétel — TAPER_HOLD-ban maradáshoz, hiszterézises (tág)
        küszöb.

        TAPER_HOLD tartható (timer nem nullázódik), ha MINDEN feltétel
        teljesül:
          1. U_batt_DMM >= (U_target - cv_voltage_tolerance_V)
          2. I_charge <= taper_current_A * taper_current_tolerance_factor
          3. dmm_voltage_valid == True

        A tolerance_factor (alapérték 1.05) szándékolt hiszterézis a
        _check_taper_condition() szűk belépési küszöbe és e metódus tág
        kilépési küszöbe között — a köztes sávban (taper_current_A < I <=
        taper_current_A * tolerance_factor) a taper_timer_s tovább gyűlik,
        így a küszöb körüli tipikus valós DMM/PSU readback-zaj nem
        akadályozza meg a dwell-idő összegyűlését.

        Returns:
            bool — True: a tág kilépési küszöbön belül vagyunk, hold tartható
        """
        if not self._dmm_valid:
            return False

        cv_tolerance_V = (
            self._profile.cell_count
            * self._config.cv_voltage_tolerance_V_per_cell
        )

        voltage_ok = self._u_batt >= (
            self._effective_charge_target_V() - cv_tolerance_V
        )
        current_ok = self._i_charge <= (
            self._profile.effective_taper_A
            * self._config.taper_current_tolerance_factor
        )

        return voltage_ok and current_ok

    # ------------------------------------------------------------------ #
    # Segédek                                                              #
    # ------------------------------------------------------------------ #

    def _read_dmm(self, dt_s: float) -> bool:
        try:
            self._u_batt = self._dmm_v.read_voltage()
            voltage_ok = True
        except Exception:
            voltage_ok = False

        try:
            self._battery_temperature_C = self._dmm_t.read_temperature()
            self._safety.record_temperature_dmm_read(valid=True, dt_s=dt_s)
            self._temp_dmm_valid = True
            self._temp_dmm_ever_read_ok = True
        except Exception:
            self._safety.record_temperature_dmm_read(valid=False, dt_s=dt_s)
            self._temp_dmm_fault_ticks += 1
            self._temp_dmm_valid = False

        return voltage_ok

    def _read_psu_current(self) -> float:
        try:
            value = self._psu.measure_output_current()
        except Exception:
            self._psu_current_readback_valid = False
            self.emergency_stop("PSU_COMM_LOST")
            return 0.0  # hívó a FAULT állapotot ellenőrzi -- l. psu_current_readback_valid
        self._psu_current_readback_valid = True
        # R14-B3 / R14-UD-PSU-PARALLEL-BAND: modfuggo readback-policy.
        # 2220S-905-01 Rev. B: Table 1 (2. o.) INDEPENDENT/SERIES
        # +/-(0.1% rdg + 5 mA); Table 4 (7. o. / PDF 17.) PARALLEL
        # +/-(0.2% + 10 mA), typical, nem garantalt hatar; 25 +/-5 C.
        # R13-B2 / SCF-010: CH1 teljes aram SERIES/PARALLEL-ben
        # (HIL 2026-11-05, 1 V / 10-50 mA); magasabb munkapont HIL_PENDING.
        # A FIAMM 0.25 C10 maximum es 5-10% gyakorlati turese
        # (SOURCE_REQUIRED) nem ad elfogadasi savot.
        # R14-UD-CHARGE-BAND-BASIS: alap = parancsolt setpoint, <= profilmax.
        # A driver set_output_current() .6g formatumu erteket kuld;
        # a mezo ezt az argumentumot orzi, CC/CV/TAPER csak feszultseget ir.
        # A sav csak a readback-kapuhoz tartozik, a setpoint-kaput nem boviti.
        maximum_A = self._profile.effective_max_charge_A
        setpoint_A = self._charge_current_set_A
        if setpoint_A is None or not math.isfinite(setpoint_A) or setpoint_A <= 0.0:
            self.emergency_stop(
                f"{FaultCode.CHARGE_CURRENT_LIMIT_EXCEEDED.name}: "
                f"invalid commanded charge current; I_set={setpoint_A!r} A; "
                f"M={maximum_A} A"
            )
            return value
        setpoint_A = float(f"{setpoint_A:.6g}")
        basis_A = min(setpoint_A, maximum_A)
        if self._safety.psu_mode == PsuMode.PARALLEL:
            readback_band_A = 0.002 * abs(value) + 0.010
        else:
            readback_band_A = 0.001 * abs(value) + 0.005
        comparison_limit_A = basis_A + readback_band_A
        if value > comparison_limit_A:
            self.emergency_stop(
                f"{FaultCode.CHARGE_CURRENT_LIMIT_EXCEEDED.name}: "
                f"I_meas={value} A > I_base+u={comparison_limit_A} A; "
                f"I_set={setpoint_A} A; M={maximum_A} A; "
                f"I_base={basis_A} A; u={readback_band_A} A"
            )
        return value

    def _effective_charge_target_V(self) -> float:
        compensated_V = self._profile.compensated_charge_voltage_V(self._battery_temperature_C)
        if self._safety.temp_comp_mode == TempCompMode.ENABLED:
            return compensated_V
        # Q-26/Q-44/Q-48: melegben MONITOR_ONLY/OFF mellett is alacsonyabb cel.
        # A T-kiesesi policy es a meglevo (utolso jo / kezdeti 20 C) ertek marad.
        return min(self._profile.charge_voltage_pack_V, compensated_V)

    def _effective_max_expected_series_drop_V(self) -> float:
        if self._safety.is_bq_mode:
            return self._config.max_expected_series_drop_V_bq
        return self._config.max_expected_series_drop_V

    def _read_psu_voltage(self) -> Optional[float]:
        try:
            return self._psu.measure_output_voltage()
        except Exception:
            return None

    def _check_series_safety(self) -> bool:
        """K3: series_drop és diode_power ellenőrzés. True = fault triggerelt."""
        u_psu = self._read_psu_voltage()
        # Review-kör 6 (2026-09-15), C-08b -- l. u_psu_measured_V/psu_
        # voltage_readback_valid property docstringje. Mindkét ágon
        # (siker ÉS hiba) rögzítve, tick-szinten, mielőtt bármelyik lenti
        # fault/warning-ág visszatérne.
        self._u_psu_measured_V = u_psu
        self._psu_voltage_readback_valid = u_psu is not None
        if u_psu is None:
            # Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: korábban
            # a PSU feszültség-readback hibája nyomtalanul (nincs fault,
            # nincs warning, nincs számláló) hagyta ki a series_drop/
            # diode_power ellenőrzést — ha a hiba tartós, a soros-esés/
            # dióda-túlterhelés védelem a hátralévő töltés alatt csendben
            # inaktív maradt volna. Most legalább last_warning_code jelzi.
            self._last_warning_code = WarningCode.PSU_VOLTAGE_READBACK_LOST.name
            self._consecutive_psu_voltage_readback_lost += 1
            # 2026-09-15 D10 OV incidens -- l. ChargeConfig.psu_voltage_
            # readback_lost_after_n_checks kommentje a pontos időkorlát-
            # levezetésért: tartós (nem tranziens) readback-vesztés FAULT,
            # nem végtelen tolerancia -- egyrészt mert a series_drop/
            # diode_power védelem eddig is inaktív maradt, másrészt mert
            # minden egyes sikertelen readback a driver teljes retry-
            # budgetjéig (alapértékkel 15s timeout-budget) blokkol.
            if (
                self._consecutive_psu_voltage_readback_lost
                >= self._config.psu_voltage_readback_lost_after_n_checks
            ):
                self.emergency_stop(
                    "PSU_VOLTAGE_READBACK_LOST_PERSISTENT: "
                    f"{self._consecutive_psu_voltage_readback_lost} egymást "
                    "követő sikertelen PSU-feszültség-readback"
                )
                return True
            return False
        self._consecutive_psu_voltage_readback_lost = 0
        if self._u_batt <= 0:
            return False
        u_drop_V = u_psu - self._u_batt
        if u_drop_V < 0:
            return False  # PSU feszültség akkuszint alatt — skip

        drop_result = self._safety.check_series_drop(u_drop_V)
        if drop_result.fault is not None:
            self.emergency_stop(drop_result.fault.name)
            return True

        power_result = self._safety.check_diode_power(self._i_charge, u_drop_V)
        if power_result.warning is not None:
            self._last_warning_code = power_result.warning.name

        return False

    def _integrate(self, dt_s: float, signed_current_A: float, source: str) -> None:
        self._last_integration_source = source
        v = self._u_batt if self._u_batt > 0 else self._profile.charge_voltage_pack_V
        self._integrator.add_sample(signed_current_A, v, dt_s)
