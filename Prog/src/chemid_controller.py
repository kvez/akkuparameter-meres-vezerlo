"""
ChemIdCharacterizationController — SUP03-CHEMID-*: TI GPCCHEM PbA ChemID
karakterizációs állapotgép (kiegeszito_szabalyok_03 5. fejezet).

Állapotsorrend (SUP03-CHEMID-011, változatlanul követve):
  1. PRECHECK
  2. ROOM_TEMPERATURE_CONDITIONING
  3. FULL_CHARGE_MANUFACTURER_METHOD  (belső ChargeController, mint OcvSocController)
  4. CHARGE_TERMINATION_VERIFIED
  5. RELAX_5H_FULL
  6. C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
  7. RELAX_5H_EMPTY
  8. EXPORT_GPCCHEM_PACKAGE
  9. RECHARGE_REQUIRED  (terminális — a forrásdokumentum 9 lépése végpontja)

[R1] Nincs relay hívás. [SAFE-001] C/20 a kizárólagos kisütési ráta — sosem
keveredik a BQ-learning C/10-ével (SUP03-SAFE-001).

A folyamatos U/I/T görbe (SUP03-CHEMID-016: "A teljes relax gorbe resze a TI
bemenetnek") a RELAX_5H_FULL belépésétől a RELAX_5H_EMPTY végéig EGY
folytonos mintasorozatként gyűjtött — a töltési fázis csak metaadatként
kerül rögzítésre (SUP03-CHEMID-013), a GPCCHEM exportba nem.
"""
from __future__ import annotations

from dataclasses import dataclass, replace as dataclass_replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from Prog.src.battery_profile import BatteryProfile
from Prog.src.exceptions import TestAbortedByUser
from Prog.src.integrator import Integrator
from Prog.src.safety import SafetyManager
from Prog.src.gpcchem_export import (
    ChemIdSample,
    GpcChemConfig,
    build_gpcchem_submission_zip,
    evaluate_ti_compatible_quality,
)

if TYPE_CHECKING:
    from Prog.src.charge_controller import ChargeConfig, ChargeController


class ChemIdState(Enum):
    INIT = "INIT"
    PRECHECK = "PRECHECK"
    ROOM_TEMPERATURE_CONDITIONING = "ROOM_TEMPERATURE_CONDITIONING"
    FULL_CHARGE_MANUFACTURER_METHOD = "FULL_CHARGE_MANUFACTURER_METHOD"
    CHARGE_TERMINATION_VERIFIED = "CHARGE_TERMINATION_VERIFIED"
    RELAX_5H_FULL = "RELAX_5H_FULL"
    C20_DISCHARGE_TO_MANUFACTURER_MINIMUM = "C20_DISCHARGE_TO_MANUFACTURER_MINIMUM"
    RELAX_5H_EMPTY = "RELAX_5H_EMPTY"
    EXPORT_GPCCHEM_PACKAGE = "EXPORT_GPCCHEM_PACKAGE"
    RECHARGE_REQUIRED = "RECHARGE_REQUIRED"
    FAULT = "FAULT"


# SUP03-CHEMID-015: C/20 — kizárólagos, hardcoded ráta ebben a controllerben,
# hogy a BQ-learning C/10-ével véletlenül se keveredhessen (SUP03-SAFE-001).
_CHEMID_DISCHARGE_RATE_DIVISOR = 20

# Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P2 lelet:
# runaway-backstop a C20_DISCHARGE fázisra — ugyanaz az érték/elv, mint a
# standalone DischargeController (discharge_controller.py) — a beágyazott
# fázis eddig KIZÁRÓLAG a feszültség-alapú (u_batt <= terminate_V)
# leállási feltételre támaszkodott, idő-/Ah-alapú felső korlát nélkül.
# 86400s (24h) bőven meghaladja a C/20 ütem várt ~20h időtartamát, tehát
# normál üzemben nem vált ki.
# Teljes kódbázis review 8. kör (2026-08-30), Szoftver/Architektúra P2
# lelet: a fenti értékpár korábban privát, hardkódolt modul-konstansként
# élt itt is — mostantól self._safety.max_discharge_time_s/
# max_discharge_Ah_factor a kanonikus forrás (l. safety.py), a standalone
# DischargeController mintáját követve (5-8. kör konszolidáció).


@dataclass
class ChemIdConfig:
    conditioning_required: bool = True
    conditioning_min_s: float = 7200.0          # 2h (SUP03-CHEMID-012)
    relax_full_min_s: float = 18000.0           # 5h (SUP03-CHEMID-014)
    relax_empty_min_s: float = 18000.0          # 5h (SUP03-CHEMID-016)
    sample_interval_s: float = 30.0             # export resampling — 5..100s (SUP03-CHEMID-017)

    def __post_init__(self) -> None:
        if not (5.0 <= self.sample_interval_s <= 100.0):
            raise ValueError(
                f"sample_interval_s={self.sample_interval_s} a TI-kompatibilis "
                f"5-100 s tartományon kívül esik."
            )


class ChemIdCharacterizationController:
    """CHEMID_CHARACTERIZATION_PBA állapotgép."""

    def __init__(
        self,
        psu,
        load,
        dmm_voltage,
        dmm_temperature,
        profile: BatteryProfile,
        safety: SafetyManager,
        config: ChemIdConfig,
        export_output_dir: Optional[Path] = None,
        export_config: Optional[GpcChemConfig] = None,
        source_session_reference: str = "",
        missing_identifier_fields: Optional[list[str]] = None,
        charge_config: ChargeConfig | None = None,
    ) -> None:
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
        # lelet: l. ocv_soc_controller.py azonos kiegészítésének indoklása —
        # a TraditionalLearningController/GoldenGgController MÁR megkapja a
        # session-szintű charge_config-ot (2026-08-23 OV-incidens javítás,
        # fast_loop_enabled=True), a ChemIdCharacterizationController eddig
        # strukturálisan nem tudta fogadni, holott ugyanazon a fizikai
        # (TF1/soros dióda) útvonalon fut, mint a másik két controller.
        from Prog.src.charge_controller import ChargeConfig
        self._charge_config = charge_config or ChargeConfig()

        # WP-AK-05: SUP03-CHEMID-019 "Kotelezo export" — ha a hívó (session/GUI
        # réteg) megadja ezeket, a RECHARGE_REQUIRED előtt automatikusan
        # létrejön a teljes GPCCHEM csomag. Ha None (pl. unit tesztek), az
        # export kimarad, csak az állapotátmenet történik meg (backward
        # kompatibilis a meglévő tesztekkel).
        self._export_output_dir = export_output_dir
        self._export_config = export_config
        self._source_session_reference = source_session_reference
        # Review-lelet (WP-AK-05): SUP03-DATA-001 az AppVersion/GitCommit/
        # Operator mezőket kötelező azonosítóként sorolja fel — ha a hívó nem
        # tudja kitölteni (nincs forrásadat), ezt explicit limitációként kell
        # rögzíteni a measurement_quality.json-ban, nem csendben üresen hagyni.
        self._missing_identifier_fields: list[str] = missing_identifier_fields or []

        self._state = ChemIdState.INIT
        # CF-19 (governance-v2 kör 2, DFAM-023) -- l. ocv_soc_controller.py
        # azonos hivatkozású phase_elapsed_s/_enter() docstringje.
        self._phase_elapsed_s: float = 0.0
        self._fault_reason: str = ""
        self._normal_discharge_cutoff_seen = False

        self._charge_ctrl: Optional["ChargeController"] = None
        self.on_event: Optional[Callable[[dict], None]] = None

        self._u_batt: float = 0.0
        self._dmm_valid: bool = True
        # Kor 18, Batch C7 R17-14 (DFAM-001 + DFAM-011): l. _run_relax()
        # CHEMID_RELAX_DMM_FAULT emitjének kommentje. A 28ba01e/BATCH-4.2b-
        # F-2 tanulsag KOTELEZO: CSAK az ATMENETKOR (ervenyes -> ervenytelen)
        # tuzelunk, mert egy tartos DMM-kiesés tickenkenti emittel ~9000
        # azonos esemenyt generalna, kiuritve az EventLogWidget 500-soros
        # gyuruJET. A _relax_dmm_fault_total szamlalo VALTOZATLANUL MINDEN
        # ticken no -- csak az esemeny atmenet-only.
        self._dmm_valid_before_tick: bool = True
        self._battery_temperature_C: float = 20.0
        # [Kör 19 Batch D3c, F-3 rész (a)] l. charge_controller.py azonos
        # nevű mezőjének docstringje -- ez a session-szintű flag jelzi,
        # hogy TÖRTÉNT-e valaha sikeres hőmérséklet-DMM olvasás (saját
        # vagy a beágyazott ChargeController FULL_CHARGE-beli olvasása
        # révén), megkülönböztetve a "sosem mért, csak a konstrukciókori
        # 20.0°C alapérték" állapotot a "mért, de esetleg stale" állapottól.
        self._temp_dmm_ever_read_ok: bool = False
        # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-2
        # (DFAM-024 2. előfordulás + DFAM-021): a fenti _battery_
        # temperature_C egy sikertelen hőmérséklet-DMM-olvasásnál a
        # KORÁBBI (vagy ez a konstruktor-default 20.0) értéken marad,
        # CSENDBEN, és ez az érték quality=OK-ként kerül exportra
        # (_record_sample() -> roomtemp_rel_dis_rel.csv). Az egyetlen
        # jelző (SafetyManager.temperature_dmm_fault_s, MÁSODPERC-alapú) a session
        # végi _relax_dmm_fault_total-mintát követő, TICK-alapú, session-
        # szintű összegzéshez nem alkalmas (nullázódik minden sikeres
        # olvasásnál). Ez a számláló a SIKERTELEN olvasási TICKEK teljes
        # számát gyűjti a session alatt -- l. temp_dmm_fault_ticks property
        # és main_window.py::_write_report() "chemid_temp_dmm_fault_ticks"
        # mezője.
        self._temp_dmm_fault_ticks: int = 0
        # MAX verifier (review-kör 4, HW4-02, 329c1a3) N1 lelete -- l.
        # discharge_controller.py azonos hivatkozású mező docstringje.
        self._load_current_setpoint_warning_ticks: int = 0
        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # l. discharge_controller.py::i_load_measured_A/load_current_
        # readback_valid property docstringje -- ugyanaz a tick-scoped
        # validity-flag minta, a C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        # fázisra vonatkozik.
        self._i_load_measured_A: float | None = None
        self._load_current_readback_valid: bool | None = None
        # Review-kör 3 (2026-09-09), friss Metrológia M3-01 -- l.
        # temp_dmm_valid property docstringje.
        self._temp_dmm_valid: bool = True
        # Teljes kódbázis review 9. kör (2026-08-30), Szoftver/Architektúra
        # P2 lelet: a hőmérséklet-DMM MONITOR_ONLY warning-ja (a fault előtti
        # figyelmeztető ablakban) korábban sehol nem került tárolásra — a
        # last_warning_code property teljesen hiányzott, test_runner.py
        # warning_flags mezője mindig None maradt CHEMID session alatt.
        self._last_warning_code: str = ""
        # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        # lelet — l. charge_controller.py azonos konstruktor-kommentje
        # ugyanerről a hiányzó session-szintű összesítésről.
        self._temperature_out_of_range_count: int = 0

        self._conditioning_elapsed_s: float = 0.0
        self._ambient_temperature_C: Optional[float] = None

        self._charge_completed: bool = False
        self._charge_termination_verified: bool = False

        self._relax_elapsed_s: float = 0.0
        # Kör 17 MAX-effort review, R17-24a (DFAM-021+DFAM-026): korábban
        # float=0.0 init-értékkel deklarált, SOHA nem olvasott mező -- a
        # RELAX_5H_FULL fázis végi feszültség (a teljes ChemID karakteri-
        # záció legjelentősebb OCV-adata) kiszámolódott, de sehova nem
        # jutott el. float | None/None-ra váltva (nem 0.0-ra, ami R17-
        # 11a mintájú fabrikált-érték kockázatot hordozna, ha a relax
        # sosem érné el a RELAX_5H_FULL véget) + külön _valid companion,
        # ami a beállítás PILLANATÁBAN érvényes self._dmm_valid-ot rögzíti
        # (nem a végleges self._u_batt sticky-last-good értékét minősíti
        # újra, csak jelzi, FRISS volt-e EBBEN a tickben az olvasás).
        self._relaxed_full_voltage_V: float | None = None
        self._relaxed_full_voltage_valid: bool = False
        self._last_voltage_for_dvdt: float = 0.0
        self._dvdt_uV_per_s: float = 0.0
        self._dmm_fault_during_relax: bool = False
        # Teljes kódbázis review 3. kör (2026-08-29), Metrológia/
        # Adatintegritás P2 lelet: a fenti _dmm_fault_during_relax fázisonként
        # (RELAX_5H_FULL/RELAX_5H_EMPTY) resetelődik — a TELJES session alatti
        # relax-fault-ok száma korábban sehol nem került exportba/naplóba, a
        # TI-nek beküldött csomag stale (nem friss mérésből származó)
        # feszültségpontot tartalmazhatott quality-jelzés nélkül. Ez a
        # számláló NEM resetelődik fázisváltáskor, session-szintű összeg.
        self._relax_dmm_fault_total: int = 0

        # Governance-v2 review-kör 1 (CF-16/MET-R1-04+16+18, DFAM-001 6.
        # előfordulás + DFAM-017, batch 4.4): ugyanaz a minta, mint
        # ocv_soc_controller.py::_discharge_current_A — a kanonikus
        # `_c10_base_Ah`-t kell használni nyers `nominal_capacity_Ah`
        # helyett, függetlenül a konkrét rate-osztó (itt 20) értékétől.
        # Éles session-ben ma nulla viselkedésváltozás.
        self._discharge_current_A: float = (
            profile._c10_base_Ah / _CHEMID_DISCHARGE_RATE_DIVISOR
        )

        # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: a TestRunner
        # _flush_totals()-je (test_runner.py) getattr(controller,
        # "accumulated_charge_Ah"/"accumulated_discharge_Ah", 0.0)-t olvas —
        # enélkül a property nélkül a delta MINDIG 0 volt, ezért egy teljes
        # CHEMID session report.json/checkpoint total_charge_ah/
        # total_discharge_ah mezője 0-án maradt (ugyanaz a hibaminta, mint
        # a GOLDEN_GG/TRADITIONAL_LEARNING-nél már javítva). Csak a két
        # "bulk" fázist (FULL_CHARGE_MANUFACTURER_METHOD/
        # C20_DISCHARGE_TO_MANUFACTURER_MINIMUM) integrálja.
        self._energy_integrator = Integrator()
        self._last_integration_source: str = "ZERO"  # R6-B3, l. last_integration_source

        # SUP03-CHEMID-016: EGY folytonos log — RELAX_5H_FULL kezdetétől
        # RELAX_5H_EMPTY végéig, a kisütést is beleértve.
        self._log_elapsed_s: float = 0.0
        self._samples: list[ChemIdSample] = []
        # Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P2
        # lelet: CSAK a C20_DISCHARGE fázis saját (nem a teljes RELAX+
        # DISCHARGE+RELAX log-ot átfogó _log_elapsed_s) eltelt ideje — l.
        # _run_c20_discharge() runaway-backstop ellenőrzése.
        self._discharge_phase_elapsed_s: float = 0.0

        self.export_dir: Optional[str] = None
        self.export_zip_path: Optional[str] = None
        self.recharge_required: bool = True

    # ------------------------------------------------------------------ #
    # Nyilvános tulajdonságok                                              #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> ChemIdState:
        return self._state

    @property
    def phase_elapsed_s(self) -> float:
        """CF-19 -- l. ocv_soc_controller.py::phase_elapsed_s azonos
        hivatkozású docstringje."""
        return self._phase_elapsed_s

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def samples(self) -> list[ChemIdSample]:
        return list(self._samples)

    @property
    def discharge_current_A(self) -> float:
        return self._discharge_current_A

    @property
    def charge_config(self) -> ChargeConfig:
        """Teljes kódbázis review 16. kör (2026-09-04), Session/Workflow
        P2 lelet: a testvér GoldenGgController/TraditionalLearningController
        már exponálja ezt a property-t (DATA-006) — a
        ChemIdCharacterizationController-ről hiányzott, holott a
        FULL_CHARGE_MANUFACTURER_METHOD fázisban ténylegesen ugyanezt a
        self._charge_config-ot használja a beágyazott ChargeController
        felépítésekor. Enélkül main_window.py::_write_report()
        charge_config_snapshot mezője CHEMID session-nél sosem töltődött
        ki."""
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
        l. discharge_controller.py::i_load_measured_A property docstringje."""
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
        """Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
        Adatintegritás P2 lelet: a relax-fázisok (RELAX_5H_FULL/
        RELAX_5H_EMPTY) alatti feszültség-DMM-olvasási hibák session-
        szintű összege korábban csak _run_export()-ban (a GPCCHEM
        export-zip belsejében, csak ha a session ténylegesen eléri az
        EXPORT_GPCCHEM_PACKAGE állapotot) jelent meg — a fő session
        report.json-jában sosem, és FAULT-tal végződő session esetén
        nyomtalanul elveszett. L. main_window.py::_write_report()
        chemid_relax_dmm_fault_total mezője."""
        return self._relax_dmm_fault_total

    @property
    def relaxed_full_voltage_V(self) -> float | None:
        """Kör 17 MAX-effort review, R17-24a (DFAM-021+DFAM-026): a
        RELAX_5H_FULL fázis VÉGI feszültsége -- a teljes ChemID karakteri-
        záció (feltöltött állapotú, min. 5 órás relax utáni) legjelentő-
        sebb OCV-adata. `None`, ha a session sosem érte el a RELAX_5H_FULL
        véget (FAULT/STOP közben, vagy még folyamatban van)."""
        return self._relaxed_full_voltage_V

    @property
    def relaxed_full_voltage_valid(self) -> bool:
        """L. relaxed_full_voltage_V docstringje. `False`, ha a fenti
        érték beállításának PILLANATÁBAN a feszültség-DMM olvasása
        sikertelen volt (self._u_batt ekkor egy KORÁBBI, sticky-last-good
        tick értékét hordozza, nem a RELAX_5H_FULL vég friss mintáját)."""
        return self._relaxed_full_voltage_valid

    @property
    def temp_dmm_fault_ticks(self) -> int:
        """Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-2
        -- l. self._temp_dmm_fault_ticks docstringje __init__-ben. L.
        main_window.py::_write_report() chemid_temp_dmm_fault_ticks mezője.
        Review-kör 3 (2026-09-09), MAX verifier (a306e7e764e3a717b) P2-1,
        DFAM-001 további előfordulása: a beágyazott ChargeController
        (FULL_CHARGE_MANUFACTURER_METHOD alatt) saját összegét is
        hozzáadja, a temperature_out_of_range_count property mintáját
        követve (l. ott)."""
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
    def u_psu_set_V(self) -> float:
        """L. ocv_soc_controller.py azonos property docstringje ugyanerről
        a hiányzó proxy-mintáról — FULL_CHARGE_MANUFACTURER_METHOD alatt a
        PSU-t a beágyazott ChargeController vezérli."""
        return self._charge_ctrl.u_psu_set_V if self._charge_ctrl is not None else 0.0

    @property
    def last_integration_source(self) -> str:
        """Teljes kódbázis review 11. kör (2026-08-31), Szoftver/Architektúra
        P3 lelet — l. u_psu_set_V docstringje ugyanerről a hiányzó proxy-
        mintáról. test_runner.py::_build_sample() sample["integration_
        current_source"]-je enélkül mindig None maradt FULL_CHARGE_
        MANUFACTURER_METHOD alatt."""
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
        sample["taper_timer_s"]-je enélkül mindig None maradt FULL_CHARGE_
        MANUFACTURER_METHOD alatt, holott a beágyazott ChargeController
        ténylegesen futtatja a taper-hold hiszterézis logikát (6. kör,
        b73bd78)."""
        return self._charge_ctrl.taper_timer_s if self._charge_ctrl is not None else 0.0

    @property
    def last_fast_loop_extra_s(self) -> float:
        """Review-kör 5, Metrológia M5-01 (P1) -- ugyanaz a hiányzó proxy-
        property minta, mint fent a taper_timer_s-nél. A TestRunner ezt a
        beágyazott ChargeController fast-loop mechanizmusa (l.
        charge_controller.py azonos property docstringje) által MÁR
        elintegrált extra időt olvassa ki, hogy a KÖVETKEZŐ tick dt_s-éből
        levonja (elkerülve a duplán-integrálást a FULL_CHARGE_MANUFACTURER_
        METHOD alatti töltési fázisban)."""
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
        TraditionalLearningController már exponálja ezt a property-t — a
        ChemIdCharacterizationController hiányzott, emiatt test_runner.py::
        _build_sample() getattr(controller, "u_batt_V", None)-je mindig
        None-t adott vissza, és InstrumentManager.read_all() MÁSODSZOR is
        lekérdezte ugyanazt a fizikai feszültség-DMM-et minden ticken —
        CLAUDE.md §9 "egy fizikai DMM-et egy tickben egy kijelölt komponens
        olvasson" megsértése, pontosan a 2026-08-23 traditional_learning
        OV-incidens után a testvér controllereknél már javított hibaosztály."""
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
        fallback-ra (`u_batt is not None`) esett vissza."""
        return self._dmm_valid

    @property
    def temp_dmm_valid(self) -> bool:
        """Review-kör 3 (2026-09-09), friss Metrológia M3-01 (P1, DFAM-001+
        DFAM-018) -- l. discharge_controller.py azonos property docstringje
        a dmm_valid (feszültség) testvér-mintájáról."""
        return self._temp_dmm_valid

    @property
    def i_load_set_A(self) -> float:
        """L. u_psu_set_V docstringje ugyanerről a hiányzó mintáról —
        C20_DISCHARGE_TO_MANUFACTURER_MINIMUM alatt ténylegesen parancsolt
        Load-áram."""
        if self._state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM:
            return self._discharge_current_A
        return 0.0

    @property
    def last_warning_code(self) -> str:
        """Teljes kódbázis review 9. kör (2026-08-30), Szoftver/Architektúra
        P2 lelet — l. _last_warning_code docstringje az __init__-ben. A
        FULL_CHARGE_MANUFACTURER_METHOD alatt a beágyazott ChargeController
        saját warning-ja élvez elsőbbséget (pl. dióda-disszipáció), egyébként
        a saját szintű hőmérséklet-DMM warning."""
        if self._charge_ctrl is not None and self._charge_ctrl.last_warning_code:
            return self._charge_ctrl.last_warning_code
        return self._last_warning_code

    @property
    def temperature_out_of_range_count(self) -> int:
        """Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        lelet — l. charge_controller.py azonos property docstringje. A
        beágyazott ChargeController (FULL_CHARGE_MANUFACTURER_METHOD alatt)
        saját összegét is hozzáadja — a számláló, ellentétben a
        last_warning_code-dal, ADDITÍV a session teljes hosszán."""
        return self._temperature_out_of_range_count + (
            self._charge_ctrl.temperature_out_of_range_count
            if self._charge_ctrl is not None else 0
        )

    # ------------------------------------------------------------------ #
    # Fő advance()                                                         #
    # ------------------------------------------------------------------ #

    def advance(self, dt_s: float) -> ChemIdState:
        # R2-06 (2026-09-15): tick-scoped validity flag -- MINDEN korai-
        # return és safety-kapu ELŐTT nullázva (l. feedback_new_tick_
        # scoped_validity_flag_needs_explicit_reset.md).
        self._i_load_measured_A = None
        self._load_current_readback_valid = None
        # R6-B3: kompozit tick eleji reset (l. last_integration_source);
        # a kisutesi integralasi pont, ill. a toltesi ag atvetele irja felul.
        self._last_integration_source = "ZERO"

        if self._state in (ChemIdState.FAULT, ChemIdState.RECHARGE_REQUIRED):
            return self._state

        # CF-19 -- l. ocv_soc_controller.py::advance() azonos hivatkozású
        # kommentje.
        self._phase_elapsed_s += dt_s

        self._last_warning_code = ""

        if self._state != ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD:
            # R17-14 -- l. self._dmm_valid_before_tick kommentje: a FRISS
            # olvasas ELOTTI ervenyesseg rogzitese adja az atmenet-
            # detekciot a _run_relax() emitjenek.
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
            # lelet: a kiesesi ido _read_dmm()-ben MÁR nőtt,
            # de sosem került kiértékelésre — egy hőmérséklet-DMM kommunikációs
            # kiesés a több órás C/20 kisütés/relax alatt teljesen észrevétlen
            # maradt volna (CLAUDE.md §10.4). Ugyanaz a minta, mint
            # golden_gg_controller.py::advance() (release-audit 2026-08-23 2.12).
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
            if self._state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM:
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

        if self._state == ChemIdState.INIT:
            self._enter(ChemIdState.PRECHECK)

        elif self._state == ChemIdState.PRECHECK:
            self._run_precheck()

        elif self._state == ChemIdState.ROOM_TEMPERATURE_CONDITIONING:
            self._run_conditioning(dt_s)

        elif self._state == ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD:
            self._run_full_charge(dt_s)

        elif self._state == ChemIdState.CHARGE_TERMINATION_VERIFIED:
            self._run_charge_termination_verified()

        elif self._state == ChemIdState.RELAX_5H_FULL:
            self._run_relax(dt_s, is_full=True)

        elif self._state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM:
            self._run_c20_discharge(dt_s)

        elif self._state == ChemIdState.RELAX_5H_EMPTY:
            self._run_relax(dt_s, is_full=False)

        elif self._state == ChemIdState.EXPORT_GPCCHEM_PACKAGE:
            self._run_export()

        else:
            # Review-kör 5, Session/Workflow S5-05 (P3, DFAM-004 sibling) --
            # l. charge_controller.py::advance() azonos hivatkozású
            # kommentje ugyanerről a hibaosztályról. A FAULT/RECHARGE_
            # REQUIRED terminális állapotokat a függvény ELEJÉN lévő korai
            # return már lefedi.
            raise ValueError(f"Unhandled ChemIdState in advance(): {self._state!r}")

        return self._state

    # ------------------------------------------------------------------ #
    # CF-19 (governance-v2 kör 2, DFAM-023): egységes állapotváltás-helper #
    # ------------------------------------------------------------------ #

    def _enter(self, state: ChemIdState) -> None:
        """L. ocv_soc_controller.py::_enter() azonos hivatkozású docstringje."""
        self._state = state
        self._phase_elapsed_s = 0.0

    # ------------------------------------------------------------------ #
    # Vészleállítás [R1]                                                   #
    # ------------------------------------------------------------------ #

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
        self._enter(ChemIdState.FAULT)

    # ------------------------------------------------------------------ #
    # Állapot implementációk                                               #
    # ------------------------------------------------------------------ #

    def _run_precheck(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        voltage_result = self._safety.check_precheck_voltage(self._u_batt)
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return
        self._conditioning_elapsed_s = 0.0
        if self._config.conditioning_required:
            self._enter(ChemIdState.ROOM_TEMPERATURE_CONDITIONING)
        else:
            self._enter_full_charge()

    def _run_conditioning(self, dt_s: float) -> None:
        """SUP03-CHEMID-012: min. 2h akklimatizáció, ha az akku korábban más
        hőmérsékleten volt."""
        self._conditioning_elapsed_s += dt_s
        self._ambient_temperature_C = self._battery_temperature_C
        if self._conditioning_elapsed_s >= self._config.conditioning_min_s:
            self._enter_full_charge()

    def _enter_full_charge(self) -> None:
        from Prog.src.charge_controller import ChargeController
        self._charge_ctrl = ChargeController(
            self._psu, self._load, self._dmm_v, self._dmm_t,
            self._profile, self._safety, self._charge_config,
        )
        self._enter(ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD)

    def _run_full_charge(self, dt_s: float) -> None:
        """SUP03-CHEMID-013: a gyártói CC/CV töltés ténye/paraméterei
        metaadatként rögzítendők — a GPCCHEM CSV-exportba NEM kerülnek be."""
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

        self._dmm_valid = self._charge_ctrl.dmm_valid
        # Review-kör 3 (2026-09-09), MAX verifier (a3f0e6dcf93588360) P2-1:
        # a feszültség-ikret (fenti sor) mindig átvezettük a beágyazott
        # ChargeControllerből, a hőmérsékletit nem -- emiatt a
        # temp_dmm_valid property FULL_CHARGE_MANUFACTURER_METHOD alatt
        # (ahol a saját _read_dmm() ki van kapcsolva) hamisan True maradt
        # egy ténylegesen halott hőmérséklet-DMM mellett is.
        self._temp_dmm_valid = self._charge_ctrl.temp_dmm_valid
        # [Kör 19 Batch D3c, F-3 rész (a)] monoton OR -- egyszer True, mindig
        # True, a temp_dmm_valid fenti (tick-enkénti) mintájától eltérően.
        self._temp_dmm_ever_read_ok = (
            self._temp_dmm_ever_read_ok or self._charge_ctrl.temp_dmm_ever_read_ok
        )
        self._u_batt = self._charge_ctrl.u_batt_V
        self._battery_temperature_C = self._charge_ctrl.battery_temperature_C

        # R1 B8a, F-20 (DFAM-035, RECURRING; a C-06 fix utódja) -- l.
        # ocv_soc_controller.py::_run_precharge() azonos hivatkozású
        # kommentje: a session-szintű Ah/Wh a beágyazott ChargeController
        # EZEN a ticken (fast-loop résztickjeivel együtt) elintegrált
        # deltáját veszi át -- a belső integrál az EGYETLEN numerikus
        # forrás. A FAULT és a CHARGE_DONE ág (gyermek eldobása) ELŐTT.
        self._energy_integrator.add_charge_delta(
            self._charge_ctrl.accumulated_charge_Ah - charge_Ah_before,
            self._charge_ctrl.accumulated_charge_Wh - charge_Wh_before,
            dt_s + self._charge_ctrl.last_fast_loop_extra_s,
        )
        # R6-B3: a gyermek megorzott forrasanak atvetele (nem friss-integralas-
        # bizonyitek), a gyermek elengedese ELOTT.
        self._last_integration_source = self._charge_ctrl.last_integration_source

        if charge_state == ChargeState.FAULT:
            self.emergency_stop(self._charge_ctrl.fault_reason or "CHEMID_CHARGE_FAULT")
        elif charge_state == ChargeState.CHARGE_DONE:
            self._charge_completed = True
            self._enter(ChemIdState.CHARGE_TERMINATION_VERIFIED)
            # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow
            # P2 lelet: a temperature_out_of_range_count property a
            # beágyazott ChargeController-re proxyol/összegez — a lenti
            # _charge_ctrl = None ELŐTT át kell menteni a saját szintű
            # számlálóba, különben a FULL_CHARGE alatti előfordulások
            # nyomtalanul elvesznének (ellentétben accumulated_charge_Ah-
            # val, amelynek tick-enkénti deltája -- R1 B8a, F-20 -- a
            # beágyazott integrátorból már EZEN a ticken, a lenti eldobás
            # ELŐTT átkerült a saját _energy_integrator-ba).
            self._temperature_out_of_range_count += (
                self._charge_ctrl.temperature_out_of_range_count
            )
            # Review-kör 3 (2026-09-09), MAX verifier (a306e7e764e3a717b)
            # P2-1 -- l. temp_dmm_fault_ticks property docstringje, ugyanaz
            # a carry-over minta, mint a fenti temperature_out_of_range_
            # count-nál.
            self._temp_dmm_fault_ticks += self._charge_ctrl.temp_dmm_fault_ticks
            # Teljes kódbázis review 13. kör (2026-08-31), Metrológia/
            # Adatintegritás P2 lelet — l. ocv_soc_controller.py azonos
            # fixjének docstringje ugyanerről a "stale beágyazott
            # ChargeController-referencia" hibaosztályról.
            self._charge_ctrl = None

    def _run_charge_termination_verified(self) -> None:
        """Explicit verifikációs lépés (SUP03-CHEMID-011 4. állapota) —
        rögzíti a töltés-befejezési bizonyítékot, mielőtt a relax indulna."""
        self._charge_termination_verified = self._charge_completed and self._dmm_valid
        if not self._charge_termination_verified:
            self.emergency_stop("CHARGE_TERMINATION_NOT_VERIFIED")
            return

        # A folytonos log (RELAX_5H_FULL → DISCHARGE → RELAX_5H_EMPTY) itt indul.
        self._log_elapsed_s = 0.0
        self._samples = []
        self._relax_elapsed_s = 0.0
        self._last_voltage_for_dvdt = self._u_batt
        self._dmm_fault_during_relax = False
        self._enter(ChemIdState.RELAX_5H_FULL)

    def _run_relax(self, dt_s: float, is_full: bool) -> None:
        if not self._dmm_valid:
            self._dmm_fault_during_relax = True
            self._relax_dmm_fault_total += 1
            # Kor 18, Batch C7 R17-14 (DFAM-001): a HAROM relax-
            # implementacio kozul ketto (RelaxController :265-269
            # RELAX_DMM_FAULT es OcvSocController._run_relax_phase()
            # OCV_SOC_RELAX_DMM_FAULT) mar elo operatori jelzest ad egy
            # DMM-kiesesre; a ChemID inline relaxja EDDIG EGYIKET SEM adta
            # -- egy 5 oras relax alatti kieses teljesen lathatatlan volt.
            # Atmenet-only, l. _dmm_valid_before_tick kommentje.
            if self._dmm_valid_before_tick and self.on_event is not None:
                self.on_event({
                    "event_code": "CHEMID_RELAX_DMM_FAULT",
                    "event_message": (
                        "DMM olvasási hiba relaxáció közben (ChemID "
                        f"{'RELAX_5H_FULL' if is_full else 'RELAX_5H_EMPTY'}, "
                        f"hiba #{self._relax_dmm_fault_total})"
                    ),
                })
        # Teljes kódbázis review 13. kör (2026-08-31), Metrológia/
        # Adatintegritás P2 lelet — l. ocv_soc_controller.py azonos
        # fixjének docstringje ugyanerről a hiányzó relax-dt_s bekötésről.
        self._energy_integrator.add_sample(0.0, self._u_batt, dt_s)
        self._relax_elapsed_s += dt_s
        self._log_elapsed_s += dt_s

        if dt_s > 0 and self._dmm_valid:
            self._dvdt_uV_per_s = (
                (self._u_batt - self._last_voltage_for_dvdt) / dt_s * 1_000_000.0
            )
            self._last_voltage_for_dvdt = self._u_batt

        self._record_sample(current_A=0.0)

        min_s = self._config.relax_full_min_s if is_full else self._config.relax_empty_min_s
        if self._relax_elapsed_s >= min_s:
            if is_full:
                self._relaxed_full_voltage_V = self._u_batt
                self._relaxed_full_voltage_valid = self._dmm_valid
                self._begin_c20_discharge()
            else:
                self._enter(ChemIdState.EXPORT_GPCCHEM_PACKAGE)

    def _begin_c20_discharge(self) -> None:
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
        # _run_c20_discharge() self._safety.check_concurrent_psu_load()
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
            self.emergency_stop(f"C20_DISCHARGE_SETUP_FAILED: {exc}")
            return
        self._discharge_phase_elapsed_s = 0.0
        self._enter(ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM)

    def _run_c20_discharge(self, dt_s: float) -> None:
        """SUP03-CHEMID-015: állandó C/20 áram a gyártói minimum
        feszültségig — folyamatos görbe, NINCS pulse, NINCS lépcsőzés."""
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

        self._log_elapsed_s += dt_s
        self._discharge_phase_elapsed_s += dt_s
        self._record_sample(current_A=-abs(i_load))  # SUP03-CHEMID-019: negatív
        self._energy_integrator.add_sample(-abs(i_load), self._u_batt, dt_s)
        self._last_integration_source = "LOAD_READBACK"

        if self._discharge_phase_elapsed_s > self._safety.max_discharge_time_s:
            self.emergency_stop("MAX_DISCHARGE_TIME_REACHED")
            return
        # Governance-v2 review-kör 1 (CF-16/MET-R1-04+16+18, DFAM-001 6.
        # előfordulás + DFAM-017, batch 4.4; SHA-korrekció a MAX verifier
        # P3-1 leletére, l. DECISION_REGISTER.md GOV-013 kiegészítés):
        # ugyanaz a hibaosztály, amit a 13. kör (`682d97e`) egyetlen
        # commitban már javított discharge_controller.py/golden_gg_
        # controller.py/traditional_learning_controller.py-ban (mind
        # `_c10_base_Ah`-t használ azóta) — ez a védelmi plafon nyers
        # nominal_capacity_Ah-t (FG-sorozatnál C20-alapú) használt,
        # ellentétes bázissal, mint az a 3 testvér. Éles session-ben ma
        # nulla viselkedésváltozás.
        if self._energy_integrator.accumulated_discharge_Ah > (
            self._profile._c10_base_Ah * self._safety.max_discharge_Ah_factor
        ):
            self.emergency_stop("MAX_DISCHARGE_AH_REACHED")
            return

        # Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: PSU+Load
        # egyidejű aktivitás ellenőrzés (CLAUDE.md §10.2) — a standalone
        # charge_controller.py/discharge_controller.py minden tickben
        # ellenőrzi ezt, a beágyazott kisütő-ágak (itt, GOLDEN_GG,
        # TRADITIONAL_LEARNING, OCV_SOC) korábban csak a fázis INDÍTÁSAKOR
        # (_begin_c20_discharge()) állították PSU-t OFF-ba, a teljes kisütés
        # alatt sosem ellenőrizték újra — defense-in-depth hiány.
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return

        # Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P1
        # lelet: a standalone DischargeController._run_cc() ezt minden
        # ticken ellenőrzi (Keithley 2380 250W hardver max), a beágyazott
        # kisütő-ágak (itt, GOLDEN_GG, TRADITIONAL_LEARNING, OCV_SOC)
        # eddig nem — l. safety.py::check_load_power() docstringje.
        power_result = self._safety.check_load_power(self._u_batt, i_load)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return

        # HW4-02 (review-kör 4, P1, DFAM-001) -- l. discharge_controller.py::
        # _run_cc() azonos hivatkozású kommentje/SafetyManager.check_load_
        # current_setpoint() docstringje. MAX verifier N1 lelete: a
        # tick-számláló a _last_warning_code slot-tól FÜGGETLENÜL nő.
        setpoint_result = self._safety.check_load_current_setpoint(
            abs(i_load), self._discharge_current_A
        )
        if setpoint_result.warning is not None:
            self._load_current_setpoint_warning_ticks += 1
            if self._last_warning_code == "":
                self._last_warning_code = setpoint_result.warning.name

        # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: fordított
        # polaritás / ésszerűtlen feszültség ellenőrzés (CLAUDE.md §10.6/
        # SAFE-009) — l. discharge_controller.py-ban ugyanez a javítás.
        v_result = self._safety.check_battery_voltage(self._u_batt)
        if v_result.fault is not None:
            self.emergency_stop(v_result.fault.name)
            return

        terminate_V = self._profile.terminate_voltage_pack_V
        if self._u_batt <= terminate_V:
            try:
                self._load.input_off()
            except Exception as exc:
                self.emergency_stop(f"C20_DISCHARGE_LOAD_OFF_FAILED: {exc}")
                return
            self._relax_elapsed_s = 0.0
            self._dmm_fault_during_relax = False
            self._enter(ChemIdState.RELAX_5H_EMPTY)

    def _run_export(self) -> None:
        """SUP03-CHEMID-019 "Kotelezo export": ha a hívó megadta az export
        célkönyvtárat/konfigurációt (l. __init__), a config.txt/CSV/quality/zip
        csomag itt, automatikusan létrejön, MIELŐTT a RECHARGE_REQUIRED
        terminális állapotba lépnénk. Export-hiba (pl. NumCellSeries eltérés,
        lemez I/O) explicit FAULT — a kötelező export nem hagyható csendben ki."""
        if self._export_output_dir is not None and self._export_config is not None:
            try:
                export_config = self._export_config
                if self._ambient_temperature_C is not None:
                    # SUP03-CHEMID-012: a ténylegesen mért szobahőmérséklet
                    # (akklimatizáció alatt rögzítve) pontosabb, mint a
                    # konstrukciókori (feltételezett) érték.
                    export_config = dataclass_replace(
                        export_config, room_temperature_C=self._ambient_temperature_C
                    )
                quality = evaluate_ti_compatible_quality(None, None, None)
                if self._ambient_temperature_C is not None:
                    # CF-31 MAX verifier P2-2 lelete (2026-09-08, l. ugyanez
                    # golden_gg_controller.py::_run_export()-ben): a fenti
                    # "mért szobahőmérséklet" valójában az akku-felületi
                    # hőmérséklet-DMM (self._battery_temperature_C, l.
                    # _run_conditioning()), NEM önálló szoba-/ambiens
                    # hőmérséklet-szenzor -- l. charge_controller.py::
                    # ChargeConfig.ambient_temperature_source "INITIAL_
                    # BATTERY_APPROX" fogalma (ott deklarálva, de "ebben a
                    # körben NEM implementálva"). A projekt saját kanonikus
                    # állásfoglalása (CLAIM_LEDGER.yaml BAT-CHG-001) szerint
                    # egy nem-mért ambiens értéket nem szabad jelöletlenül
                    # teljes megfelelésként beküldeni -- ezért a helyettesítés
                    # MEGMARAD (a konditálás célja épp az akklimatizáció),
                    # de a provenance-t explicit limitációként rögzítjük.
                    quality.limitations.append(
                        "RoomTemperatureC az akku-felületi hőmérséklet-DMM "
                        "konditálás végi méréséből származik, NEM önálló "
                        "szoba-/ambiens hőmérséklet-szenzorból (INITIAL_"
                        "BATTERY_APPROX jellegű közelítés)."
                    )
                else:
                    # Párhuzamos eset a golden_gg_controller.py::_run_export()
                    # P2-1 javításával -- ha valamiért conditioning_required
                    # False lenne (a ChemID alapértéke True, l. ChemIdPhysical
                    # Config), a config.txt csendben a konstrukciókori
                    # alapértéket exportálná, jelöletlenül.
                    # 2. MAX verifier P3-2 lelete (2026-09-08, l. ugyanez
                    # golden_gg_controller.py-ban): a szöveg NE tartalmazzon
                    # "RoomTemperatureC=" tokent egy "kulcs=érték" formátumú
                    # limitáció-mezőben.
                    quality.limitations.append(
                        f"A RoomTemperatureC mező a GpcChemConfig "
                        f"konstrukciókori (feltételezett) "
                        f"{export_config.room_temperature_C:.2f} °C "
                        f"alapértékét exportálja -- NEM mért érték, mert a "
                        f"conditioning fázis nem futott le "
                        f"(conditioning_required=False)."
                    )
                if self._relax_dmm_fault_total > 0:
                    # Teljes kódbázis review 3. kör (2026-08-29), Metrológia/
                    # Adatintegritás P2 lelet: a relax alatti DMM-kiesés
                    # korábban nyomon volt tartva (_dmm_fault_during_relax),
                    # de sosem jutott el a TI-nek exportált csomag quality-
                    # mezőjébe — a testvér traditional_learning_controller.py
                    # ugyanezt már korrektül kezeli (_quality_flags.append).
                    # [Kör 19 Batch D3b, F-2] a szöveg korábban "Hőmérséklet-/
                    # feszültség-DMM"-ként hivatkozott erre a limitációra, de
                    # `_relax_dmm_fault_total` (l. :783, `not self._dmm_valid`
                    # gate, ami feszültség-olvasás eredménye) KIZÁRÓLAG
                    # feszültség-DMM hibákat számol -- a hőmérséklet-DMM külön
                    # számlálóval (l. lent) és külön limitációval jár, NEM
                    # ezzel.
                    quality.limitations.append(
                        f"Feszültség-DMM kiesés a relax fázis(ok) alatt "
                        f"({self._relax_dmm_fault_total} érintett tick) — "
                        f"a mért görbe stale (nem friss mérésből származó) "
                        f"pontot tartalmazhat."
                    )
                if self.temp_dmm_fault_ticks > 0:
                    # [Kör 19 Batch D3b, F-2] testvér-limitáció a fenti
                    # feszültség-DMM-hez -- korábban HIÁNYZOTT a TI export
                    # limitáció-listájából, holott a `temp_dmm_fault_ticks`
                    # session-szinten követett (report.json + DEGRADED-kapu),
                    # csak a submission-csomagba nem jutott el. A gpcchem_
                    # export.py `cell_surface_temperature_C` oszlopa emiatt
                    # stale (nem friss mérésből származó) pontot
                    # tartalmazhat, jelzés nélkül.
                    quality.limitations.append(
                        f"Hőmérséklet-DMM kiesés ({self.temp_dmm_fault_ticks} "
                        f"érintett tick a session során) — a "
                        f"cell_surface_temperature_C oszlop stale (nem friss "
                        f"mérésből származó) pontot tartalmazhat."
                    )
                if not self._temp_dmm_ever_read_ok:
                    # [Kör 19 Batch D3c, F-3 rész (a)] l. golden_gg_
                    # controller.py azonos ágának kommentje -- ez a KÜLÖN,
                    # kizáró ág explicit jelzi, hogy a cell_surface_
                    # temperature_C oszlop MINDEN sora a fabrikált
                    # (konstrukciókori 20.0°C) alapértéket hordozza, nem
                    # mérést, mert a hőmérséklet-DMM SOHA nem olvasott
                    # sikeresen a session teljes időtartama alatt.
                    quality.limitations.append(
                        "A hőmérséklet-DMM a session teljes időtartama alatt "
                        "SOHA nem olvasott sikeresen — a "
                        "cell_surface_temperature_C oszlop minden sora a "
                        "konstrukciókori (nem mért) 20.00°C alapértéket "
                        "tartalmazza."
                    )
                if self._missing_identifier_fields:
                    # Review-lelet: SUP03-DATA-001 kötelező azonosítói, amiket
                    # ez a hívó nem tudott kitölteni — explicit limitáció, nem
                    # csendes üres mező.
                    quality.limitations.append(
                        "Hiányzó, SUP03-DATA-001 által kötelezőnek jelölt "
                        "azonosító mező(k) a config.txt-ben: "
                        + ", ".join(self._missing_identifier_fields) + "."
                    )
                zip_path = build_gpcchem_submission_zip(
                    self._export_output_dir,
                    export_config,
                    self._samples,
                    quality,
                    self._source_session_reference,
                    self._profile.cell_count,
                )
                self.export_dir = str(self._export_output_dir)
                self.export_zip_path = str(zip_path)
            except Exception as exc:
                self.emergency_stop(f"GPCCHEM_EXPORT_FAILED: {exc}")
                return

        self._enter(ChemIdState.RECHARGE_REQUIRED)
        self.recharge_required = True
        if self.on_event is not None:
            self.on_event({
                "event_code": "RECHARGE_REQUIRED",
                "event_message": (
                    "ChemID karakterizáció kész — az akkumulátort a gyártói "
                    "eljárás szerint azonnal vissza kell tölteni."
                ),
            })

    def _record_sample(self, current_A: float) -> None:
        self._samples.append(ChemIdSample(
            elapsed_s=round(self._log_elapsed_s, 3),
            voltage_V=self._u_batt,
            current_A=current_A,
            temperature_C=self._battery_temperature_C,
        ))

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
