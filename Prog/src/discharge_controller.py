"""
DischargeController — CC kisütési állapotgép.
Állapotok: INIT → PRECHECK → DISCHARGE_CC_SETUP → DISCHARGE_CC_RUN →
           DISCHARGE_DONE / FAULT
[R1] Nincs relay hívás.
[R8] Integráció forrása: LOAD_READBACK.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from math import isfinite

from Prog.src.battery_profile import (
    UNKNOWN_DISCHARGE_RATE_ORIGIN,
    BatteryProfile,
    DischargeRateOrigin,
)
from Prog.src.exceptions import TestAbortedByUser
from Prog.src.integrator import Integrator
from Prog.src.pulse_measurement_service import SYNC_QUALITY_SOFTWARE_TICK_TIMED
from Prog.src.safety import SafetyManager

# Release-audit 2026-08-23 (2.3): ugyanaz a konvenció, mint a SUP01
# PulseMeasurementService pulse_time_tolerance_s=0.5 (l.
# pulse_measurement_service.py) — CLAUDE.md #11.5: "csak akkor R_1s, ha a
# tényleges mintavételi idő megfelelő pontossággal 1s".
_RB_TIME_TOLERANCE_S = 0.5


class DischargeState(Enum):
    INIT = "INIT"
    PRECHECK = "PRECHECK"
    DISCHARGE_CC_SETUP = "DISCHARGE_CC_SETUP"
    DISCHARGE_CC_RUN = "DISCHARGE_CC_RUN"
    DISCHARGE_DONE = "DISCHARGE_DONE"
    FAULT = "FAULT"


@dataclass
class DischargeConfig:
    discharge_current_A: float = 0.0    # 0 = auto C/5
    terminate_voltage_V_override: float = 0.0   # 0 = profil default (1.80V/cella)
    # Teljes kódbázis review 8. kör (2026-08-30), Szoftver/Architektúra P2
    # lelet: max_discharge_time_s/max_discharge_Ah_factor korábban itt ÉS
    # 3 beágyazott controllerben (chemid/golden_gg/traditional_learning)
    # is külön, privát, hardkódolt modul-konstansként létezett — a limit
    # mostantól KIZÁRÓLAG a SafetyManager-en (l. _run_cc()) él, ugyanaz a
    # minta, mint a max_load_power_W konszolidációja (5. kör, lentebb).
    # Teljes kódbázis review 5. kör (2026-08-29), Szoftver/Architektúra +
    # Hardver/Műszervezérlés P2 lelet (dedup): max_load_power_W korábban
    # itt ÉS safety.py::SafetyManager.max_load_power_W-ban is külön
    # forrásként létezett (számszerűen egyezve, de két, egymástól
    # független helyről) — a beágyazott (chemid/golden_gg/traditional_
    # learning/ocv_soc) kisütési fázisok csak a SafetyManager oldali
    # értéket látták, ez a mező csendben figyelmen kívül maradt volna, ha
    # valaki itt szigorít. A limit mostantól KIZÁRÓLAG a SafetyManager-en
    # (l. _run_cc()::self._safety.check_load_power()) él — a hívó a
    # SafetyManager konstruálásakor adja meg, ha a 250W alapértéktől
    # eltérőt akar.

    # A GUI feloldott eredete; a nulla áram alapértelmezése ettől még C/5.
    rate_origin: DischargeRateOrigin = UNKNOWN_DISCHARGE_RATE_ORIGIN

    def __post_init__(self) -> None:
        if not isfinite(self.discharge_current_A) or self.discharge_current_A < 0.0:
            raise ValueError("GOV-029: érvénytelen discharge_current_A")
        if self.terminate_voltage_V_override != 0.0:
            if not (1.0 <= self.terminate_voltage_V_override <= 100.0):
                raise ValueError(
                    f"terminate_voltage_V_override={self.terminate_voltage_V_override!r} "
                    "out of sane range [1.0, 100.0] V"
                )


class DischargeController:
    def __init__(self, psu, load, dmm_voltage, dmm_temperature,
                 profile: BatteryProfile, safety: SafetyManager,
                 config: DischargeConfig,
                 time_source: Callable[[], float] = time.monotonic):
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature
        self._profile = profile
        self._safety = safety
        self._config = config
        self.on_event: Callable[[dict], None] | None = None
        # R16-B12: a mérés közben érkező leállítást az indítás előtt látni kell.
        self.handoff_stop_requested: Callable[[], bool] | None = None
        self._time_source = time_source

        self._state = DischargeState.INIT
        self._elapsed_s: float = 0.0
        self._fault_reason: str = ""
        self._normal_discharge_cutoff_seen = False
        self._u_batt: float = 0.0
        self._dmm_valid: bool = True
        self._last_integration_source: str = "ZERO"
        self._battery_temperature_C: float = 20.0
        # Review-kör 3 (2026-09-09), friss Metrológia M3-01 (P1, DFAM-001+
        # DFAM-018): a "dmm_temperature_valid" testvér-mezőjéhez (l. lent a
        # temp_dmm_valid property docstringje) -- ugyanaz a hiányzó
        # proxy-minta, mint a fenti _dmm_valid-nál volt, mielőtt a 6. kör
        # bevezette.
        self._temp_dmm_valid: bool = True
        self._last_warning_code: str = ""
        # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        # lelet — l. charge_controller.py azonos konstruktor-kommentje
        # ugyanerről a hiányzó session-szintű összesítésről.
        self._temperature_out_of_range_count: int = 0
        # Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb) P2,
        # DFAM-001 4. előfordulása -- l. charge_controller.py azonos
        # property docstringje. A standalone StepKind.DISCHARGE útnak
        # (ellentétben a beágyazott ChargeController-rel bíró 5 összetett
        # controllerrel) korábban ez a testvér-számláló egyáltalán
        # hiányzott.
        self._temp_dmm_fault_ticks: int = 0
        # MAX verifier (review-kör 4, HW4-02, commit 329c1a3) N1 lelete: a
        # LOAD_CURRENT_SETPOINT_NOT_REACHED figyelmeztetés a _last_warning_
        # code EGYETLEN, tickenkénti nem-felülíró csatornáján ment keresztül
        # -- ha egy magasabb prioritású (pl. hőmérséklet-) figyelmeztetés
        # MINDEN tickben aktív a session teljes ideje alatt, ez a jelzés
        # SOHA nem jutna el sehova, pontosan azt a hibaosztályt reprodukálva,
        # amit a HW4-02 fix maga célzott. Ugyanaz a megoldás, mint a fenti
        # temp_dmm_fault_ticks-nél: önálló, a _last_warning_code prioritási
        # láncától FÜGGETLEN, tick-alapú számláló. Report/checkpoint-oldali
        # bekötés (session-szintű aggregáció, resume-seed) külön, dedikált
        # következő lépés -- l. DEFECT_FAMILY_REGISTRY.yaml DFAM-001.
        self._load_current_setpoint_warning_ticks: int = 0
        self._integrator = Integrator()

        # Rb mérés: terhelésbekapcsolás előtti U_batt + időzített mintavétel.
        # METRO-R20-1 (Kör 20 Batch E1, DFAM-001) -- a `_load_on_elapsed_s`
        # KÉT monoton időbélyeg (`_load_on_t0_s` az `input_on()` után,
        # `_t_voltage_sample_s` a mintavételi `read_voltage()` után)
        # KÜLÖNBSÉGE, NEM a hívó `dt_s`-einek összege. L. részletesen
        # iec_dc_resistance_controller.py::_load_on_elapsed_s __init__
        # kommentje ugyanerről a mechanizmusról (ugyanaz a hiba volt itt
        # is jelen a fix előtt).
        self._i_discharge_set: float = 0.0
        self._u_batt_pre_load: float | None = None
        # R1 B8b, F-27 (P3, DFAM-008): a publikus u_batt_pre_load_V csak
        # akkor ad értéket, ha már van TERHELT feszültségminta (a _run_cc()
        # elején True: a tick-eleji DMM-olvasás az előző tickbeli
        # input_on() UTÁN futott). A SETUP-tickben a minta battery_voltage_V-
        # je és a pre-load referencia UGYANAZ a terhelés előtti olvasás --
        # a runner rb_delta_v-je ott hamis 0.000 lenne. A belső Rb-képlet a
        # privát _u_batt_pre_load-ot olvassa, ez a jelző nem érinti.
        self._loaded_voltage_sample_available: bool = False
        self._load_on_elapsed_s: float = 0.0
        self._load_on_t0_s: float = 0.0
        self._t_voltage_sample_s: float = 0.0
        self._rb_1s_mohm: float | None = None
        self._rb_10s_mohm: float | None = None
        self._rb_30s_mohm: float | None = None
        # Release-audit 2026-08-23 (2.3): tényleges mintavételi idő
        # (traceability, CLAUDE.md #12 "első és extrém dt_s kapjon helyes
        # kezelést/quality flaget") + degraded jelző, ha bármelyik elért
        # pont a névleges célidőponttól (1s/10s/30s) a toleranciánál jobban
        # eltért — korábban ez csendben, jelzés nélkül veszett el.
        self._rb_1s_actual_s: float | None = None
        self._rb_10s_actual_s: float | None = None
        self._rb_30s_actual_s: float | None = None
        self._rb_timing_degraded: bool = False
        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # l. i_load_measured_A/load_current_readback_valid property
        # docstringje -- ugyanaz a tick-scoped validity-flag minta, mint
        # charge_controller.py::i_psu_measured_A/psu_current_readback_valid.
        self._i_load_measured_A: float | None = None
        self._load_current_readback_valid: bool | None = None

    @property
    def state(self) -> DischargeState:
        return self._state

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def u_batt_V(self) -> float:
        """Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        P2 lelet: a testvér ChargeController/TraditionalLearningController/
        GoldenGgController már exponálja ezt a property-t — a DischargeController
        (a sima StepKind.DISCHARGE lépésnél, CHARACTERIZATION/BQ_LEARNING_
        PHYSICAL/DISCHARGE_ONLY teszttípusoknál a leggyakoribb út) hiányzott,
        emiatt test_runner.py::_build_sample() getattr(controller, "u_batt_V",
        None)-je mindig None-t adott vissza, és InstrumentManager.read_all()
        MÁSODSZOR is lekérdezte ugyanazt a fizikai feszültség-DMM-et minden
        ticken — CLAUDE.md §9 "egy fizikai DMM-et egy tickben egy kijelölt
        komponens olvasson" megsértése."""
        return self._u_batt

    @property
    def battery_temperature_C(self) -> float:
        """L. u_batt_V docstringje ugyanerről a hiányzó proxy-mintáról,
        ugyanaz a dupla-olvasás vonatkozik a hőmérséklet-DMM-re is."""
        return self._battery_temperature_C

    @property
    def dmm_valid(self) -> bool:
        """Teljes kódbázis review 6. kör (2026-08-29), Session/Workflow P2
        lelet #4: a ChargeController már exponálja ezt a property-t, ezt
        test_runner.py::_build_sample() dmm_voltage_valid mezőhöz olvassa
        (793-796. sor) — a testvér controllereknél (ez is köztük) hiányzott,
        emiatt getattr(controller, "dmm_valid", None) mindig None-t adott,
        és a fallback `u_batt is not None` gyakorlatilag MINDIG True-t írt,
        akkor is, ha a DMM-olvasás ténylegesen hibázott (u_batt csak az
        utolsó sikeres mérésen/alapértéken ragad, sosem esik None-ra)."""
        return self._dmm_valid

    @property
    def temp_dmm_valid(self) -> bool:
        """Review-kör 3 (2026-09-09), friss Metrológia M3-01 (P1, DFAM-001+
        DFAM-018): a fenti dmm_valid (feszültség) 1:1 testvére a
        hőmérséklet-DMM-re -- test_runner.py::_build_sample()
        "dmm_temperature_valid" mezője korábban KIZÁRÓLAG
        `battery_temp is not None`-t nézett, ami SOSEM None (a
        battery_temperature_C egy 20.0-ra inicializált, sikertelen
        olvasásnál VÁLTOZATLANUL hagyott mező, sosem esik None-ra) --
        tehát a mező szerkezetileg mindig True volt, egy leszakadt
        hőmérséklet-szonda mellett is."""
        return self._temp_dmm_valid

    @property
    def temp_dmm_fault_ticks(self) -> int:
        """Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb) P2,
        DFAM-001 4. előfordulása -- l. charge_controller.py azonos
        property docstringje ugyanerről a session-szintű, tick-alapú
        számlálóról. L. test_runner.py::TestResult.temp_dmm_fault_ticks
        (minden StepKind-et lefedő, delta-alapú aggregáció)."""
        return self._temp_dmm_fault_ticks

    @property
    def load_current_setpoint_warning_ticks(self) -> int:
        """MAX verifier (review-kör 4, HW4-02) N1 lelete -- l. a mező
        __init__-beli docstringje. Session-szintű, tick-alapú számláló a
        LOAD_CURRENT_SETPOINT_NOT_REACHED figyelmeztetésre, FÜGGETLENÜL
        attól, hogy az adott tickben a _last_warning_code slot-ot elnyerte-e
        (nem, ha egy magasabb prioritású figyelmeztetés már foglalta)."""
        return self._load_current_setpoint_warning_ticks

    @property
    def last_integration_source(self) -> str:
        return self._last_integration_source

    @property
    def last_warning_code(self) -> str:
        return self._last_warning_code

    @property
    def accumulated_discharge_Ah(self) -> float:
        return self._integrator.accumulated_discharge_Ah

    @property
    def accumulated_discharge_Wh(self) -> float:
        """Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        + Metrológia P2 lelet: az accumulated_discharge_Ah Wh-párja — l.
        test_runner.py::_build_sample() a controller-getattr mintáról."""
        return self._integrator.accumulated_discharge_Wh

    @property
    def extreme_dt_s_count(self) -> int:
        """Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        P2 lelet — l. charge_controller.py azonos property docstringje."""
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
    def i_load_set_A(self) -> float:
        return self._i_discharge_set

    # R1 B8c, F-25 (P2, DFAM-021, D-B8-6): I/O-mentes, a config/profil
    # alapjan feloldott kisutesi aram-SETPOINT es cutoff (NEM muszer-
    # readback) a report.json discharge_config_snapshot-jahoz. A
    # _run_cc_setup()/_run_cc() inline safety-sorai SZANDEKOSAN
    # valtozatlanok (nem ezeket hivjak) -- az egyezest karakterizalo teszt
    # orzi (test_discharge_controller.py TestF25EffectiveSetpointAndCutoff
    # Snapshot).
    @property
    def effective_discharge_current_A(self) -> float:
        return (
            self._config.discharge_current_A
            if self._config.discharge_current_A != 0.0
            else self._profile.C5_discharge_current_A
        )

    @property
    def effective_terminate_voltage_V(self) -> float:
        return (
            self._config.terminate_voltage_V_override
            if self._config.terminate_voltage_V_override != 0.0
            else self._profile.terminate_voltage_pack_V
        )

    def config_snapshot(self) -> dict:
        """R1 B8c, F-25: a DischargeConfig nyers mezoi (0.0 = auto/profil
        szentinel) + a feloldott effektiv setpoint/cutoff."""
        return {
            **asdict(self._config),
            "effective_discharge_current_set_A": self.effective_discharge_current_A,
            "effective_terminate_voltage_V": self.effective_terminate_voltage_V,
        }

    @property
    def u_batt_pre_load_V(self) -> float | None:
        """R1 B8b, F-27: None, amíg nincs terhelt feszültségminta (l.
        _loaded_voltage_sample_available kommentje az __init__-ben)."""
        if not self._loaded_voltage_sample_available:
            return None
        return self._u_batt_pre_load

    @property
    def rb_1s_mohm(self) -> float | None:
        return self._rb_1s_mohm

    @property
    def rb_10s_mohm(self) -> float | None:
        return self._rb_10s_mohm

    @property
    def rb_30s_mohm(self) -> float | None:
        return self._rb_30s_mohm

    @property
    def i_load_measured_A(self) -> float | None:
        """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        a `charge_controller.py::i_psu_measured_A` Load-oldali testvére --
        a `_read_load_current()` EBBEN a tickben végzett fizikai olvasását
        tükrözi, tick-scoped módon (nullázva `advance()` elején, MINDEN
        korai-return és safety-kapu előtt). `test_runner.py::_build_
        sample()` ezt részesíti előnyben a nyers `InstrumentManager.
        read_all()` Load-áram-csatornájával szemben, hogy a naplózott
        `discharge_current_A` UGYANAZ a fizikai minta legyen, amit az
        integrálás/Rb-számítás is használt (l. `load_current_readback_
        valid` testvér-property a hiba-eset megkülönböztetéséhez)."""
        return self._i_load_measured_A

    @property
    def load_current_readback_valid(self) -> bool | None:
        """L. `i_load_measured_A` docstringje. `True` = a controller EBBEN
        a tickben sikeresen olvasta a Load-áramot; `False` = megpróbálta,
        de a readback hibázott (a controller MÁR `emergency_stop()`-olt);
        `None` = ezen a ticken a controller nem próbált Load-áramot
        olvasni (terminális állapot vagy nem-kisütő fázis)."""
        return self._load_current_readback_valid

    @property
    def rb_1s_actual_s(self) -> float | None:
        return self._rb_1s_actual_s

    @property
    def rb_10s_actual_s(self) -> float | None:
        return self._rb_10s_actual_s

    @property
    def rb_30s_actual_s(self) -> float | None:
        return self._rb_30s_actual_s

    @property
    def rb_timing_degraded(self) -> bool:
        return self._rb_timing_degraded

    @property
    def method_origin(self) -> str:
        """Governance-v2 review-kör 1 (CF-24/DFAM-001 sibling_safety_gate_
        omission, BAT-RI-001 sértés, batch 4.6; a MAX verifier P3-4
        lelete szerint a helyes család DFAM-001, nem DFAM-003, l.
        DECISION_REGISTER.md GOV-013 kiegészítés): a samples.csv rb_1s/
        10s/30s_mohm oszlopainak korábban NEM volt method_origin/címkéje —
        szemben a helyesen címkézett ocv_soc_table.csv-vel (l.
        ocv_soc_controller.py SUP01-TERM-002/003 kommentje) — így a FIAMM
        adatlap Ri-jével összehasonlíthatónak TŰNT, holott IEC 60896-21/22
        (adatlap Ri) vs pulse-ellenállás vs 1 kHz AC külön mérési módszer,
        nem összemérhető (BAT-RI-001). Ugyanazt a "PROJECT_ENGINEERING_
        METHOD" értéket adja, mint az OcvSocController saját R_pulse_
        actual_t*_mOhm mezője — mindkettő "nem TI Ra, nem FIAMM adatlap-
        érték" a lényegi állítás.

        MAX verifier P2-1 pontosítás (batch 4.6, aca185f utóellenőrzés):
        az EREDETI docstring itt tévesen állította, hogy a `rb_timing_
        degraded`/`rb_*_actual_s` mezők már elegendően dokumentálják a
        KÉT módszer eltérő mintavételi pontosságát — ez EMPIRIKUSAN
        HAMIS volt. `runner_tick_s = 0.5` vagy `1.0` esetén (operátor
        által 0.5-30.0s tartományban, 0.1s lépésben állítható, l.
        config_panel.py) a `rb_timing_degraded` False marad ÉS
        `rb_1s_actual_s` pontosan 1.0-t mutat; a köztes 0.6-0.9s
        értékeknél szintén False marad `rb_timing_degraded`, csak nem
        pontosan 1.0 az `rb_1s_actual_s` (a MAX verifier 2. köre mérte
        ki ezt a pontosítást, l. DECISION_REGISTER.md GOV-013 4.
        kiegészítés — az EREDETI P2-1 fix kommentje itt tévesen
        `runner_tick_s<=1.0`-ra általánosított). MINDEGYIK esetben a
        mérés MÉG MINDIG a fő runner-tick alapú (nem instrumentum-
        időzített) — a samples.csv sor emiatt megkülönböztethetetlen
        lett volna egy instrumentum-időzített méréstől. A helyes,
        önálló jelző a `synchronization_quality` property (l. lent) —
        ez MINDIG a tényleges mintavételi módot jelzi, függetlenül
        attól, hogy egy adott tick véletlenül a toleranciahatáron
        belülre esett-e."""
        return "PROJECT_ENGINEERING_METHOD"

    @property
    def synchronization_quality(self) -> str:
        """MAX verifier P2-1 lelete (CF-24 utóellenőrzés, batch 4.6): a
        `method_origin` ("nem TI Ra, nem FIAMM adatlap-érték") önmagában
        NEM jelzi, hogy ez a mérés a fő ~2s runner-tick alapján mintavéte-
        lezett (nem instrumentum-időzített, SUP01-PULSE-005 C szint:
        "SOFTWARE_TICK_TIMED ... az eredmeny DEGRADED, csak actual_t
        ertekkel riportolhato") — szemben az OcvSocController saját,
        PulseMeasurementService-alapú (instrumentum-időzített) pulzus-
        mérésével. A `rb_timing_degraded` flag EZT NEM helyettesíti: az
        csak azt jelzi, ha egy KONKRÉT tick a toleranciahatáron (0.5s)
        kívülre esett, nem azt, hogy MILYEN időalapú a mérés — kis
        `runner_tick_s` mellett (pl. 0.5-1.0s) `rb_timing_degraded` False
        maradhat, miközben a mérés még mindig software-tick-timed, nem
        instrumentum-időzített. A meglévő, forrás-hű `SYNC_QUALITY_
        SOFTWARE_TICK_TIMED` konstansot adja vissza (pulse_measurement_
        service.py, RB Pulse spec §18/SUP01-PULSE-005) — NEM új literál."""
        return SYNC_QUALITY_SOFTWARE_TICK_TIMED

    def advance(self, dt_s: float) -> DischargeState:
        self._elapsed_s += dt_s
        self._last_warning_code = ""
        # R2-06 (2026-09-15): tick-scoped validity flag -- MINDEN korai-
        # return és safety-kapu ELŐTT nullázva (l. feedback_new_tick_
        # scoped_validity_flag_needs_explicit_reset.md: egy ÚJ flaget a
        # tick ELEJÉN kell nullázni, különben az előző tick értékére
        # "latch"-elne egy top-of-tick FAULT-ticken).
        self._i_load_measured_A = None
        self._load_current_readback_valid = None

        if self._state == DischargeState.FAULT:
            return self._state

        self._dmm_valid = self._read_dmm(dt_s)
        if not isfinite(self._u_batt):
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return self._state
        # A befejezés utáni terhelt minta nem kaphat újabb cutoff-kivételt.
        if (
            self._normal_discharge_cutoff_seen and self._load.input_commanded_on
            and not self._normal_discharge_allowed()
        ):
            return self._state

        # R11-B4: csak az aktualis tick ervenyes homerseklete donthet.
        if self._state in (
            DischargeState.INIT,
            DischargeState.PRECHECK,
            DischargeState.DISCHARGE_CC_SETUP,
            DischargeState.DISCHARGE_CC_RUN,
        ):
            overtemp = self._safety.check_battery_overtemperature(
                self._battery_temperature_C, self._temp_dmm_valid,
            )
            if overtemp.fault is not None:
                self.emergency_stop(overtemp.fault.name)
                return self._state

        # R11-B1: az atadott kiesesi elozmeny mar indulaskor is ervenyes.
        if self._state in (DischargeState.INIT, DischargeState.PRECHECK):
            t_result = self._safety.check_temperature_dmm_fault(
                self._safety.temperature_dmm_fault_s,
            )
            if t_result.fault is not None:
                self.emergency_stop(t_result.fault.name)
                return self._state

        if self._state in (
            DischargeState.DISCHARGE_CC_SETUP,
            DischargeState.DISCHARGE_CC_RUN,
        ):
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

            # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: a
            # check_battery_voltage() (fordított polaritás / ésszerűtlen
            # feszültség, CLAUDE.md §10.6/SAFE-009) korábban KIZÁRÓLAG
            # charge_controller.py-ban futott — aktív kisütés alatt egy
            # meglazult/hibás mérőcsatlakozás (ami VALID, de ~0V/negatív
            # olvasást ad, nem NaN/Inf/overload-ot) csendben "elértük a
            # végfeszültséget"-ként, hamis DISCHARGE_DONE-ként lett volna
            # elkönyvelve.
            v_result = self._safety.check_battery_voltage(self._u_batt)
            if v_result.fault is not None:
                self.emergency_stop(v_result.fault.name)
                return self._state

            if self._elapsed_s > self._safety.max_discharge_time_s:
                self.emergency_stop("MAX_DISCHARGE_TIME_REACHED")
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
            # Adatintegritás P2 lelet — l. charge_controller.py azonos
            # hivatkozású kommentje.
            # Teljes kódbázis review 21. kör (2026-09-06), GUI/UX + Metrológia
            # P2 lelet — l. charge_controller.py azonos hivatkozású
            # kommentje (a DMM-kiesés jelzése elsőbbséget élvez).
            range_result = self._safety.check_test_temperature_range(self._battery_temperature_C)
            if range_result.warning is not None:
                self._temperature_out_of_range_count += 1
                if t_result.warning is None:
                    self._last_warning_code = range_result.warning.name

        if self._state == DischargeState.INIT:
            self._state = DischargeState.PRECHECK

        elif self._state == DischargeState.PRECHECK:
            self._run_precheck()

        elif self._state == DischargeState.DISCHARGE_CC_SETUP:
            self._run_cc_setup()

        elif self._state == DischargeState.DISCHARGE_CC_RUN:
            self._run_cc(dt_s)

        elif self._state in (DischargeState.DISCHARGE_DONE, DischargeState.FAULT):
            pass  # terminális állapotok, nincs további átmenet

        else:
            # Review-kör 5, Session/Workflow S5-05 (P3, DFAM-004 sibling) --
            # l. charge_controller.py::advance() azonos hivatkozású
            # kommentje ugyanerről a hibaosztályról.
            raise ValueError(f"Unhandled DischargeState in advance(): {self._state!r}")

        return self._state

    def emergency_stop(self, reason: str) -> None:
        # Teljes kódbázis review 7. kör (2026-08-30), Hardver/Műszervezérlés
        # P2 lelet — l. charge_controller.py::emergency_stop() docstringje
        # ugyanerről a csendben elnyelt OFF-kísérlet-hiba mintáról.
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
        # NINCS relay.safe_open() [R1]
        self._state = DischargeState.FAULT

    def _run_precheck(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

        voltage_result = self._safety.check_precheck_voltage(self._u_batt)
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return
        # Teljes kódbázis review 18. kör (2026-09-05), Hardver/
        # Műszervezérlés P1 lelet: ez a hívás védtelen maradt, szemben a
        # 17. körben (`d108c9f`) ugyanebben a controllerben javított
        # _run_cc_setup()/_run_cc() hívásokkal. Kivétel esetén korábban a
        # controller sosem állt FAULT-ra, a specifikus diagnosztika
        # elveszett, az állapot PRECHECK-ben ragadt.
        try:
            self._psu.all_outputs_off()  # PSU biztosan OFF kisütés előtt
        except Exception as exc:
            self.emergency_stop(f"DISCHARGE_PRECHECK_PSU_OFF_FAILED: {exc}")
            return
        self._state = DischargeState.DISCHARGE_CC_SETUP

    def _normal_discharge_allowed(self, *, first_cutoff_crossing: bool = False) -> bool:
        if self._state == DischargeState.FAULT:
            return False
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return False
        return self._safety.enforce_normal_discharge(
            self._u_batt, self.effective_discharge_current_A,
            self.effective_terminate_voltage_V, self.emergency_stop,
            self.on_event, self._config.rate_origin,
            first_cutoff_crossing=first_cutoff_crossing,
        )

    def _run_cc_setup(self) -> None:
        # GOV-029: setup/retry előtt és a kért cutoff módosítása nélkül.
        if not self._normal_discharge_allowed():
            return
        i_discharge = (
            self._config.discharge_current_A
            if self._config.discharge_current_A > 0
            else self._profile.C5_discharge_current_A
        )
        # R8-B1: setpoint-alapu teljesitmenykapu a Load inditasa elott.
        power_result = self._safety.check_load_power(self._u_batt, i_discharge)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return
        # Teljes kódbázis review 17. kör (2026-09-05), Hardver/Műszervezérlés
        # P1 lelet: a testvér GoldenGgController/ChemIdCharacterizationController/
        # TraditionalLearningController/OcvSocController mindegyike explicit
        # try/except-be csomagolja a saját kisütés-indítási szekvenciáját
        # (set_mode_cc/set_current) — a DischargeController (az EREDETI,
        # önálló minta, amit a testvérek követnek) maga védtelen maradt,
        # mert a 13-16. kör keresése kizárólag a 4 beágyazott-ChargeController
        # controllerre fókuszált. Kivétel esetén korábban a specifikus
        # diagnosztika (DISCHARGE_CC_SETUP_FAILED) helyett csak a
        # TestRunner legkülső, generikus UNHANDLED_EXCEPTION jutott a
        # riportba, és a controller sosem állt FAULT-ra saját maga.
        # Teljes kódbázis review 15. kör (2026-09-04), Session/Workflow +
        # Hardver/Műszervezérlés P1 lelet (2 ágens egymástól függetlenül):
        # a fenti (advance() tetején futó) kölcsönös kizárás ellenőrzés a
        # JELENLEGI load_commanded_on-t nézi, ami EZEN a ticken, az
        # input_on() ELŐTT, garantáltan False — a check emiatt sosem tud
        # tüzelni egy PSU-oldali stale flag ellen PONT ebben a tickben. A
        # 14. kör `499fa03` fixének (ChargeController._run_psu_preset())
        # mintáját követve: explicit, előrejelző ellenőrzés input_on()
        # ELŐTT, load_commanded_on=True-t szimulálva.
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=True,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        # R16-B12: a safety-kapuk után, minden Load-konfiguráció előtt.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        self._i_discharge_set = i_discharge
        try:
            self._load.set_mode_cc()
            self._load.set_current(i_discharge)
        except Exception as exc:
            self.emergency_stop(f"DISCHARGE_CC_SETUP_FAILED: {exc}")
            return
        self._normal_discharge_cutoff_seen = False
        # Rb referencia feszültség: terhelésbekapcsolás ELŐTTI U_batt
        self._u_batt_pre_load = self._u_batt
        # R1 B8b, F-27: ebben a tickben még nincs terhelt minta.
        self._loaded_voltage_sample_available = False
        self._load_on_elapsed_s = 0.0
        self._load_on_t0_s = 0.0
        self._rb_1s_mohm = None
        self._rb_10s_mohm = None
        self._rb_30s_mohm = None
        self._rb_1s_actual_s = None
        self._rb_10s_actual_s = None
        self._rb_30s_actual_s = None
        self._rb_timing_degraded = False
        try:
            self._load.input_on(stop_requested=self.handoff_stop_requested)
        except TestAbortedByUser:
            # A runner az élő okot oldja fel, és egyszer kapcsolja le mindkét műszert.
            return
        except Exception as exc:
            self.emergency_stop(f"DISCHARGE_CC_SETUP_FAILED: {exc}")
            return
        self._load_on_t0_s = self._time_source()
        self._state = DischargeState.DISCHARGE_CC_RUN

    def _run_cc(self, dt_s: float) -> None:
        # Q-35: a cutoff első mintája rendes vég; a következő már szigorú kaput kap.
        first_crossing = (
            not self._normal_discharge_cutoff_seen and self._load.input_commanded_on
        )
        self._normal_discharge_cutoff_seen |= self._u_batt <= self.effective_terminate_voltage_V
        if not self._normal_discharge_allowed(first_cutoff_crossing=first_crossing):
            return
        # R1 B8b, F-27: a tick-eleji (advance()) DMM-olvasás sikeres volt
        # (különben DMM_FEEDBACK_LOST már FAULT-ba vitt) és az előző tick
        # input_on()-ja UTÁN futott -- terhelt feszültségminta.
        self._loaded_voltage_sample_available = True
        i_load = self._read_load_current()
        if self._state == DischargeState.FAULT:
            return
        power_result = self._safety.check_load_power(self._u_batt, i_load)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return
        # HW4-02 (review-kör 4, P1, DFAM-001): a beállított és a ténylegesen
        # mért terhelőáram közti relatív eltérés ellenőrzése -- l.
        # SafetyManager.check_load_current_setpoint() docstringje. A
        # _last_warning_code slot-ot csak akkor írja, ha ebben a tickben MÉG
        # nincs figyelmeztetés (ugyanaz a nem-felülíró elv, mint a fenti
        # hőmérséklet-figyelmeztetéseknél) -- de a tick-számláló (MAX
        # verifier N1 lelete) MINDIG nő, FÜGGETLENÜL attól, hogy a slot
        # foglalt volt-e, hogy egy tartósan aktív magasabb prioritású
        # figyelmeztetés (pl. hőmérséklet) ne nyelje el a session teljes
        # egészére nézve nyomtalanul.
        setpoint_result = self._safety.check_load_current_setpoint(
            i_load, self._i_discharge_set
        )
        if setpoint_result.warning is not None:
            self._load_current_setpoint_warning_ticks += 1
            if self._last_warning_code == "":
                self._last_warning_code = setpoint_result.warning.name
        self._integrate(dt_s, signed_current_A=-i_load, source="LOAD_READBACK")

        # Rb mintavétel 1s / 10s / 30s időpontokban.
        # Release-audit 2026-08-23 (2.3): korábban Rb = ΔV / I_SET (a
        # LOAD_SETPOINT, nem a ténylegesen mért terhelésáram) — CLAUDE.md
        # #11.5 ("lehetőleg mért deltaáramot használjon") megsértése. Ezen a
        # ponton i_load MINDIG valós mért érték (a fenti _read_load_current()
        # LOAD_COMM_LOST esetén már FAULT-ba lépett és visszatért, mielőtt
        # idáig eljutnánk), tehát nincs szükség setpoint-fallbackre.
        self._load_on_elapsed_s = self._t_voltage_sample_s - self._load_on_t0_s
        if self._u_batt_pre_load is not None and self._i_discharge_set > 0.01 and i_load > 0.01:
            delta_v = self._u_batt_pre_load - self._u_batt
            t = self._load_on_elapsed_s
            # Review-kör 4 (2026-09-09), Metrológia M4-03 (P2, DFAM-001 +
            # DFAM-018): kisütő impulzusnál a terhelés alatti feszültség
            # fizikailag NEM lehet magasabb/egyenlő a terhelés előtti
            # (baseline) feszültségnél -- delta_v<=0 csak mérési hibából
            # jöhet (meglazult Kelvin-érzékelő, DMM range-váltás a
            # mintavétel alatt). Korábban ez negatív/nulla rb_*_mohm-ot írt
            # quality-jelzés nélkül (rules/20-metrology.md "Egyetlen
            # sample-ből ne jelents valódi 0 deltát"). A testvér
            # pulse_measurement_service.py ugyanezt a védelmet kapta.
            if self._rb_1s_mohm is None and t >= 1.0 and delta_v > 0:
                self._rb_1s_mohm = delta_v / i_load * 1000.0
                self._rb_1s_actual_s = t
                if abs(t - 1.0) > _RB_TIME_TOLERANCE_S:
                    self._rb_timing_degraded = True
            if self._rb_10s_mohm is None and t >= 10.0 and delta_v > 0:
                self._rb_10s_mohm = delta_v / i_load * 1000.0
                self._rb_10s_actual_s = t
                if abs(t - 10.0) > _RB_TIME_TOLERANCE_S:
                    self._rb_timing_degraded = True
            if self._rb_30s_mohm is None and t >= 30.0 and delta_v > 0:
                self._rb_30s_mohm = delta_v / i_load * 1000.0
                self._rb_30s_actual_s = t
                if abs(t - 30.0) > _RB_TIME_TOLERANCE_S:
                    self._rb_timing_degraded = True

        terminate_V = (
            self._config.terminate_voltage_V_override
            if self._config.terminate_voltage_V_override > 0
            else self._profile.terminate_voltage_pack_V
        )
        if self._u_batt <= terminate_V:
            # Teljes kódbázis review 17. kör (2026-09-05), Session/Workflow +
            # Hardver/Műszervezérlés P1 lelet (2 ágens egymástól függetlenül):
            # a testvér GoldenGgController/ChemIdCharacterizationController/
            # TraditionalLearningController/OcvSocController mindegyike
            # explicit try/except-be csomagolja a kisütés-lezáráskori
            # load.input_off()-ot ("a hibáját nem szabad csendben elnyelni,
            # mert a relax 0A-feltételezését érvénytelenítené") — a
            # DischargeController (az EREDETI, önálló minta) maga védtelen
            # maradt.
            try:
                self._load.input_off()
            except Exception as exc:
                self.emergency_stop(f"DISCHARGE_LOAD_OFF_FAILED: {exc}")
                return
            self._state = DischargeState.DISCHARGE_DONE
            return

        # Teljes kódbázis review 13. kör (2026-08-31), Szoftver/Architektúra
        # P2 lelet: a védendő kisütési áram (self._discharge_current_A,
        # alapból profile.C5_discharge_current_A — C10-alapú, l. 11. kör
        # 5b62b0f) C10-bázisú, de ez a plafon korábban nyers
        # nominal_capacity_Ah-t (FG-sorozatnál C20-alapú) használt —
        # ellentétes bázis ugyanabban a védelmi láncban (CLAUDE.md §11.1).
        # A _c10_base_Ah a kanonikus bázis (ocv_soc_controller.py már ezt
        # használja) — BAT-002 lezárásakor (C10_capacity_Ah GUI-mező) ez a
        # plafon MÁR a helyes, konzervatívabb bázisból számol.
        if self._integrator.accumulated_discharge_Ah > (
            self._profile._c10_base_Ah * self._safety.max_discharge_Ah_factor
        ):
            self.emergency_stop("MAX_DISCHARGE_AH_REACHED")

    def _read_dmm(self, dt_s: float) -> bool:
        try:
            self._u_batt = self._dmm_v.read_voltage()
            self._t_voltage_sample_s = self._time_source()
            voltage_ok = True
        except Exception:
            voltage_ok = False

        try:
            self._battery_temperature_C = self._dmm_t.read_temperature()
            self._safety.record_temperature_dmm_read(valid=True, dt_s=dt_s)
            self._temp_dmm_valid = True
        except Exception:
            self._safety.record_temperature_dmm_read(valid=False, dt_s=dt_s)
            self._temp_dmm_fault_ticks += 1
            self._temp_dmm_valid = False

        return voltage_ok

    def _read_load_current(self) -> float:
        try:
            value = self._load.measure_current()
            self._i_load_measured_A = value
            self._load_current_readback_valid = True
            return value
        except Exception:
            self._load_current_readback_valid = False
            self.emergency_stop("LOAD_COMM_LOST")
            return 0.0

    def _integrate(self, dt_s, signed_current_A, source):
        self._last_integration_source = source
        v = self._u_batt if self._u_batt > 0 else self._profile.terminate_voltage_pack_V
        self._integrator.add_sample(signed_current_A, v, dt_s)
