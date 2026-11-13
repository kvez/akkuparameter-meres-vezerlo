"""
GoldenGgController — WP-AK-11: TI Golden GG Maker system-rate optimalizálási
útvonal (SUP03-GG-001..012, PHASE D / ROUTE G) fizikai mérési állapotgépe.

ARCHITEKTÚRÁLIS DÖNTÉS (a WP-AK-10 `TraditionalLearningController`
mintáját követve): a fizikai töltést/kisütést az Akkuteszter SAJÁT
Keithley PSU/Load laborműszerei végzik (`ChargeController` újrafelhasználva
+ inline system-rate kisütés-logika), a BQ34Z110 gauge-állapotát a
golden-image Pico RPC rétegen (`PicoBQTransport`) keresztül csak OLVASSUK
(SUP03-GG rendszer nem ír a BQ-ba — a Golden GG Maker eredményének
programozása a BQ-ba explicit KÉSŐBBI WP hatásköre, l. WP-AK-13 "image
program" lépése).

**Két, szándékosan különálló szakasz** (a normatív szöveg saját logikáját
követve): (1) ez a controller a FIZIKAI mérési ciklust (töltés → 2h relax →
rendszerre jellemző kisütés → 5h relax) futtatja le és állítja elő a TI
Golden GG Maker eszköznek szánt csomagot (`golden_gg_export.
build_golden_gg_submission_package()`); (2) a TI eszköz VISSZAADOTT
eredményének (gg_out.csv, Qmax, Ra table, stb.) importja/nyomon-követése
(`golden_gg_export.import_ti_gg_output()`) egy KÜLÖN, e controllertől
független, aszinkron lépés — a TI feldolgozás külső, valós idejű session-en
kívüli folyamat, amit ez az állapotgép nem várhat meg (SUP03-VALID-001: "a
gg_out.csv előállítása és programozása sem elég önmagában").

**Folytonos napló a töltés kezdetétől** (SUP03-GG-005/006, ELTÉRÉS a
`ChemIdCharacterizationController` mintájától, ahol a napló csak a
RELAX_5H_FULL-tól indul): a Golden GG Maker bemenete a TELJES töltési
szakaszt is igényli (Ra0_charge számításhoz, 100% SOC modellhez) — a
`_samples` lista ezért a FULL_CHARGE állapotba lépéstől a RELAX_5H végéig
folyamatosan bővül, töltés alatt POZITÍV, kisütés alatt NEGATÍV előjelű
árammal.

**Nincs BQ-alapú relax-elfogadási kritérium** (ELTÉRÉS a
`TraditionalLearningController` SUP03-LEARN-006 mintájától): a SUP03-GG-005
lépéssorozat kizárólag időalapú relax-minimumokat ír elő (2h/5h), a
Golden GG útvonalhoz nincs forrásolt OCVTAKEN/VOK-alapú elfogadási
kritérium — a `RelaxController` saját, tiszta min-idő alapú
`RELAX_DONE`-ja az egyetlen kilépési feltétel.

**Rendszerre jellemző áram, explicit, nem C-ráta-alapú** (SUP03-GG-007):
`system_rate_current_A` a hívó (GUI/session-konfiguráció) explicit,
kötelező bemenete — a `GoldenGgPhysicalConfig.__post_init__()` blokkolja a
konstrukciót, ha ez <= 0, hogy a C/10 vagy C/20 automatikus származtatás
(mint `DischargeController`/`ChemIdCharacterizationController`-nél) itt
SOSEM történhessen meg csendben.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.battery_profile import BatteryProfile
from Prog.src.bq_consistency_gate import evaluate_provisioning_consistency
from Prog.src.bq_data_flash_service import Bq34z110DataFlashService
from Prog.src.bq_diagnostics import (
    BqDiagnosticsSnapshot,
    decode_control_status,
    format_bq_session_scale_context,
    is_snapshot_fully_invalid,
    poll_bq_diagnostics,
)
from Prog.src.charge_controller import ChargeConfig, ChargeController, ChargeState
from Prog.src.exceptions import ActivationStopHook, TestAbortedByUser
from Prog.src.golden_gg_export import (
    GoldenGgConfig,
    GoldenGgSample,
    build_golden_gg_submission_package,
)
from Prog.src.integrator import Integrator
from Prog.src.relax_controller import (
    RelaxConfig,
    RelaxController,
    RelaxSample,
    RelaxState,
)
from Prog.src.safety import SafetyManager


class GoldenGgState(Enum):
    INIT = "INIT"
    PRECOND = "PRECOND"
    CONDITIONING = "CONDITIONING"
    FULL_CHARGE = "FULL_CHARGE"
    CHARGE_TERMINATION_VERIFIED = "CHARGE_TERMINATION_VERIFIED"
    RELAX_2H = "RELAX_2H"
    SYSTEM_RATE_DISCHARGE = "SYSTEM_RATE_DISCHARGE"
    RELAX_5H = "RELAX_5H"
    EXPORT_PACKAGE = "EXPORT_PACKAGE"
    DONE = "DONE"
    FAULT = "FAULT"


_TERMINAL_STATES = (GoldenGgState.DONE, GoldenGgState.FAULT)

# Teljes kódbázis review 7. kör (2026-08-30), Szoftver/Architektúra P2
# lelet #2: relax-alállapotokban a feszültség-DMM-et a beágyazott
# RelaxController olvassa (_run_relax()) — advance() ezekre az
# állapotokra CSAK a hőmérséklet-DMM-et olvassa (_read_temperature_only()),
# hogy a temp-fault figyelés relax alatt is működjön duplikált
# feszültség-olvasás nélkül (CLAUDE.md §9).
_RELAX_STATES = (GoldenGgState.RELAX_2H, GoldenGgState.RELAX_5H)

# Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P2 lelet:
# runaway-backstop a SYSTEM_RATE_DISCHARGE fázisra — ugyanaz az érték/elv,
# mint a standalone DischargeController (discharge_controller.py) — a
# beágyazott fázis eddig KIZÁRÓLAG a feszültség-alapú (u_batt <=
# terminate_V) leállási feltételre támaszkodott, idő-/Ah-alapú felső
# korlát nélkül. A system_rate_current_A explicit, nem C-ráta-korlátozott
# bemenet, ezért ugyanez a védelem itt is indokolt.
# Teljes kódbázis review 8. kör (2026-08-30), Szoftver/Architektúra P2
# lelet: a fenti értékpár korábban privát, hardkódolt modul-konstansként
# élt itt is — mostantól self._safety.max_discharge_time_s/
# max_discharge_Ah_factor a kanonikus forrás (l. safety.py), a standalone
# DischargeController mintáját követve (5-8. kör konszolidáció).


@dataclass
class GoldenGgPhysicalConfig:
    conditioning_required: bool = False
    conditioning_min_s: float = 7200.0            # SUP03-GG-005 lépés 1 (ha szükséges)
    relax_after_charge_s: float = 7200.0          # 2h — SUP03-GG-005 lépés 4
    relax_after_discharge_s: float = 18000.0      # 5h — SUP03-GG-005 lépés 7
    system_rate_current_A: float = 0.0            # SUP03-GG-007: kötelező, explicit
    sample_interval_s: float = 30.0               # export resampling — SUP03-GG-008
    # HIL-D5 (2026-09-14, CF-10a/DFAM-027, CLAIM_LEDGER.yaml PICO-RECONNECT-001):
    # l. traditional_learning_controller.py::LearningConfig.bq_transport_
    # unreachable_after_n_polls docstringje ugyanerre a mezőre — sibling-
    # konzisztens PROJECT_POLICY érték (k=3), NEM gyártói forrás.
    bq_transport_unreachable_after_n_polls: int = 3

    def __post_init__(self) -> None:
        if self.system_rate_current_A <= 0:
            raise ValueError(
                "system_rate_current_A > 0 kötelező — SUP03-GG-007 tiltja a C/10 "
                "vagy C/20 automatikus származtatást, a rendszerre jellemző "
                "terhelést explicit meg kell adni."
            )
        if not (5.0 <= self.sample_interval_s <= 100.0):
            raise ValueError(
                f"sample_interval_s={self.sample_interval_s} a TI-kompatibilis "
                f"5-100 s tartományon kívül esik (SUP03-GG-008)."
            )


class GoldenGgController:
    """GOLDEN_GG_SYSTEM_RATE_ROOM_TEMP állapotgép — SUP03-GG-001..010."""

    def __init__(
        self,
        psu,
        load,
        dmm_voltage,
        dmm_temperature,
        bq_transport,
        profile: BatteryProfile,
        safety: SafetyManager,
        config: GoldenGgPhysicalConfig,
        expected_chem_id: Optional[int] = None,
        export_output_dir: Optional[Path] = None,
        export_config: Optional[GoldenGgConfig] = None,
        source_session_reference: str = "",
        missing_metadata_fields: Optional[list[str]] = None,
        charge_config: Optional[ChargeConfig] = None,
    ) -> None:
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature
        self._bq_transport = bq_transport
        self._bq_driver = Bq34z110ReadOnlyDriver(transport=bq_transport)
        self._profile = profile
        self._safety = safety
        self._config = config
        self._expected_chem_id = expected_chem_id
        # 2026-08-23 kiegészítés — l. TraditionalLearningController.__init__
        # docstring-kommentje ugyanerről a hiányzó ChargeConfig-öröklésről.
        self._charge_config = charge_config or ChargeConfig()

        self._export_output_dir = export_output_dir
        self._export_config = export_config
        self._source_session_reference = source_session_reference
        self._missing_metadata_fields: list[str] = missing_metadata_fields or []

        self._state = GoldenGgState.INIT
        # CF-19 (governance-v2 kör 2, DFAM-023) -- l. ocv_soc_controller.py
        # azonos hivatkozású phase_elapsed_s/_enter() docstringje.
        self._phase_elapsed_s: float = 0.0
        self._fault_reason: str = ""
        self._normal_discharge_cutoff_seen = False

        self._charge_ctrl: Optional[ChargeController] = None
        self._relax_ctrl: Optional[RelaxController] = None
        self.prepare_relax_sampling: Callable[[RelaxController], None] | None = None
        # A runner élő leállításjelzése; az okfeloldás és az OFF a runner feladata.
        self.handoff_stop_requested: Callable[[], bool] | None = None
        self._relax_exit_pending = False
        self._relax_missed_deadline_total = 0
        self._charge_completed: bool = False
        # Teljes kódbázis review 3. kör (2026-08-29), Metrológia/
        # Adatintegritás P2 lelet: a RelaxController.dmm_fault_count-ot a
        # _run_relax() minden fázisváltáskor eldobja (self._relax_ctrl =
        # None) — a session-szintű összeget itt gyűjtjük, hogy a
        # _run_export()-ban a TI-nek beküldött csomag metadata_limitations.
        # json-jában látható legyen, ne csendben vesszen el.
        self._relax_dmm_fault_total: int = 0

        self._u_batt: float = 0.0
        self._dmm_valid: bool = True
        self._battery_temperature_C: float = 20.0
        # [Kör 19 Batch D3c, F-3 rész (a)] l. charge_controller.py azonos
        # nevű mezőjének docstringje -- ez a session-szintű flag jelzi,
        # hogy TÖRTÉNT-e valaha sikeres hőmérséklet-DMM olvasás (saját
        # vagy a beágyazott ChargeController FULL_CHARGE-beli olvasása
        # révén), megkülönböztetve a "sosem mért, csak a konstrukciókori
        # 20.0°C alapérték" állapotot a "mért, de esetleg stale" állapottól.
        self._temp_dmm_ever_read_ok: bool = False
        # Release-audit 2026-08-23 (2.12, TEMP-DMM-GAP-01 kiterjesztése): a
        # hőmérséklet-DMM kiesését korábban _read_dmm() csendben elnyelte
        # (except Exception: pass), FAULT-ot sosem váltott ki a relax/
        # kisütés fázisokban (a FULL_CHARGE fázist a beágyazott
        # ChargeController saját, azonos mintájú védelme fedi).
        # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-2
        # (DFAM-024 2. előfordulás + DFAM-021) -- l. chemid_controller.py
        # azonos nevű mezőjének docstringje ugyanerről a session-szintű,
        # tick-alapú (nem másodperc-alapú) számlálóról.
        self._temp_dmm_fault_ticks: int = 0
        # MAX verifier (review-kör 4, HW4-02, 329c1a3) N1 lelete -- l.
        # discharge_controller.py azonos hivatkozású mező docstringje.
        self._load_current_setpoint_warning_ticks: int = 0
        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # l. discharge_controller.py::i_load_measured_A/load_current_
        # readback_valid property docstringje -- ugyanaz a tick-scoped
        # validity-flag minta, kiterjesztve a SYSTEM_RATE_DISCHARGE
        # fázisra.
        self._i_load_measured_A: float | None = None
        self._load_current_readback_valid: bool | None = None
        # Review-kör 3 (2026-09-09), friss Metrológia M3-01 -- l.
        # temp_dmm_valid property docstringje.
        self._temp_dmm_valid: bool = True
        # Teljes kódbázis review 9. kör (2026-08-30), Szoftver/Architektúra
        # P2 lelet: a last_warning_code property korábban KIZÁRÓLAG a
        # beágyazott ChargeController-t proxyzta (FULL_CHARGE alatt) — a
        # PRECOND/CONDITIONING/RELAX_2H/SYSTEM_RATE_DISCHARGE/RELAX_5H/
        # CHARGE_TERMINATION_VERIFIED fázisokban a saját szintű
        # check_temperature_dmm_fault() MONITOR_ONLY warning-ja ténylegesen
        # kiszámolódott (l. advance()), de sosem került tárolásra/kiolvasásra.
        self._last_warning_code: str = ""
        # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        # lelet — l. charge_controller.py azonos konstruktor-kommentje
        # ugyanerről a hiányzó session-szintű összesítésről.
        self._temperature_out_of_range_count: int = 0

        self._conditioning_elapsed_s: float = 0.0
        self._ambient_temperature_C: Optional[float] = None

        self._gg_source_snapshot: Optional[BqDiagnosticsSnapshot] = None
        # [SCF-011 újratervezés, 2026-09-11, felhasználói döntés] session-
        # szintű BQ SEALED/UNSEALED életciklus, a TraditionalLearning
        # Controller mintáját követve (l. ott self._quality_flags/
        # _ensure_bq_sealed_or_flag() docstringje ugyanerről a mintáról) --
        # itt a chip sosem íródik, de a PRECOND-beli Qmax/Ra-forrásolvasás
        # (BQ-DF-SEALED-001) csak UNSEALED állapotban megbízható.
        self._quality_flags: list[str] = []
        # HIL-D5 (2026-09-14, CF-10a/DFAM-027 escalation-küszöb) -- l.
        # traditional_learning_controller.py azonos nevű mezőinek
        # docstringje ugyanerre a mintára.
        self._consecutive_fully_invalid_bq_polls: int = 0
        self._bq_transport_unreachable_flagged: bool = False
        # R2-11b (2026-09-15, DFAM-016 terminal_cleanup_restore_omission +
        # DFAM-026, a C-12 pending_bq_df_restore cross-process mintájának
        # GG-oldali testvére) -- l. pending_bq_unsealed property docstringje.
        # `_bq_unseal_attempted` SZÁNDÉKOSAN a tényleges `unseal(confirm=
        # True)` RPC ELŐTT vált True-ra -- ugyanaz a "kísérletet a művelet
        # ELŐTT jelöld" minta, mint traditional_learning_controller.py::
        # _restore_cell_bl_if_needed() `_cell_bl_restore_done = True`-jánál
        # --, hogy egy PONT az RPC közben bekövetkező process-halál se
        # hagyjon nyom nélküli, esetleg UNSEALED chipet (CLAUDE.md §6:
        # bizonytalan állapot is safety esemény). Hamis pozitívot nem okoz:
        # ha az unseal ténylegesen nem ment végbe, a terminális
        # `_ensure_bq_sealed_or_flag()` SS-bit readbackje MEGFIGYELÉSSEL
        # törli a maradványt.
        self._bq_unseal_attempted: bool = False
        self._bq_reseal_confirmed: bool = False

        # SUP03-GG-005/006: folytonos napló a töltés kezdetétől.
        self._log_elapsed_s: float = 0.0
        self._samples: list[GoldenGgSample] = []
        # Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P2
        # lelet: CSAK a SYSTEM_RATE_DISCHARGE fázis saját (nem a teljes
        # FULL_CHARGE+SYSTEM_RATE_DISCHARGE+RELAX log-ot átfogó
        # _log_elapsed_s) eltelt ideje — l. _run_system_rate_discharge()
        # runaway-backstop ellenőrzése.
        self._discharge_phase_elapsed_s: float = 0.0
        # Release-audit 2026-08-23 (2.7): a TestRunner._run_step() a
        # generikus getattr(controller, "accumulated_charge_Ah", 0.0)
        # mintán keresztül olvassa az összesített Ah-t — enélkül a mező
        # hiánya miatt mindig 0.0-t adott (CHEMID-AH-GAP-01 mintázatának
        # kiterjesztése). Ugyanabból a _record_sample()-ből táplálva, mint a
        # GoldenGgSample napló — előjel-konvenció (töltés +, kisütés -)
        # megegyezik az Integrator elvárásával.
        self._energy_integrator = Integrator()
        self._last_integration_source: str = "ZERO"  # R6-B3, l. last_integration_source

        self.export_dir: Optional[str] = None
        self.export_zip_path: Optional[str] = None
        self.package_ready_for_ti_submission: bool = False

        self.on_event: Optional[Callable[[dict], None]] = None
        # Feature (2026-08-23, felhasználói kérés): a BQ Diagnosztika fül
        # élő, passzív nézete a session PicoBQTransport-ján keresztül — a
        # TraditionalLearningController _record_gauge_sample() mintáját
        # követve, de itt a _record_sample()-be kötve (a napló-mintavétel
        # egyetlen choke pointja FULL_CHARGE/RELAX_2H/SYSTEM_RATE_DISCHARGE/
        # RELAX_5H alatt).
        self._gauge_poll_elapsed_s: float = 0.0
        # Review 2026-09-15 (C-01/DFAM-027, P0, RECURRING, sibling a
        # traditional_learning_controller.py::_bq_poll_extra_s_to_exclude_
        # from_throttle azonos fixéhez, l. ott a teljes indoklást): a
        # throttle-ölt BQ-poll SAJÁT wall-clock idejét a KÖVETKEZŐ
        # `_record_sample()`-hívás ebből vonja le, KIZÁRÓLAG ennek a
        # küszöb-akkumulátornak (`_gauge_poll_elapsed_s`) a számára -- a
        # `dt_s` paraméter maga (Ah/Wh-integráció, napló elapsed_s) NEM
        # csökken, mert a poll ideje SOHA nincs máshol integrálva (ellentét
        # a ChargeController `last_fast_loop_extra_s`-ével, ahol a kizárt
        # idő MÁR máshol integrálva volt).
        self._bq_poll_extra_s_to_exclude_from_throttle: float = 0.0
        self.on_gauge_sample: Optional[Callable[[BqDiagnosticsSnapshot], None]] = None

    # ------------------------------------------------------------------ #
    # Nyilvános tulajdonságok                                             #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> GoldenGgState:
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
    def samples(self) -> list[GoldenGgSample]:
        return list(self._samples)

    @property
    def gg_source_snapshot(self) -> Optional[BqDiagnosticsSnapshot]:
        return self._gg_source_snapshot

    @property
    def quality_flags(self) -> tuple[str, ...]:
        """[SCF-011 újratervezés, 2026-09-11] l. TraditionalLearning
        Controller.quality_flags docstringje ugyanerről a mintáról --
        itt jelenleg kizárólag a session-végi BQ seal-visszazárás
        esetleges sikertelenségét attesztálja (l. _ensure_bq_sealed_
        or_flag())."""
        return tuple(self._quality_flags)

    @property
    def pending_bq_unsealed(self) -> dict | None:
        """R2-11b (2026-09-15, DFAM-016 + DFAM-026): a `quality_flags`
        (R2-11a) és a session-végi popup KIZÁRÓLAG a SAJÁT PROCESS-ben,
        NORMÁLISAN lezárt sessiont fedi. Ha a process a PRECOND unseal()-je
        UTÁN, de a záró `_ensure_bq_sealed_or_flag()` ELŐTT szakad meg
        (crash/kill/áramkimaradás), a BQ34Z110 fizikailag UNSEALED
        maradhat, és eddig SEMMILYEN perzisztens (checkpoint.json-beli)
        nyoma nem volt ennek: a `_quality_flags` in-memory, a report.json
        pedig ilyenkor SOSEM készül el -- `session_recovery.
        discover_incomplete_sessions()` pont a report.json HIÁNYÁRA épül.

        SZÁNDÉKOSAN KÜLÖN fogalom a TraditionalLearningController.
        pending_bq_df_restore-tól (DFAM-018 semantic-collapse-avoidance):
        az egy VISSZA-NEM-ÍRT, ideiglenes Data Flash ÉRTÉK maradványa, ez
        viszont tisztán a chip HOZZÁFÉRÉS-/biztonsági ÁLLAPOTÁNAK
        maradványa -- ez a controller a Data Flash-be SOSEM ír (l. modul-
        docstring), az unseal kizárólag a PRECOND-beli Qmax/Ra
        forrásolvasáshoz kell (BQ-DF-SEALED-001). A kettőt sem a
        checkpoint-mezőben, sem az operátori üzenetben NEM szabad egy
        mezőbe/sorba összeolvasztani.

        `None`, ha (a) unseal SOSEM lett megkísérelve (`_bq_unseal_
        attempted is False`, pl. korai PRECOND FAULT a DMM-/precheck-/
        ChemID-kapun), VAGY (b) a zárt állapot MEGFIGYELÉSSEL igazolt
        (`_bq_reseal_confirmed`, l. `_ensure_bq_sealed_or_flag()` SS-bit
        readbackje -- nem az elküldött parancs). Egyébként dict a maradvány
        leírásával."""
        if not self._bq_unseal_attempted:
            return None
        if self._bq_reseal_confirmed:
            return None
        return {
            "controller": "GOLDEN_GG",
            "reason": "SOURCE_SNAPSHOT_READ_REQUIRES_UNSEALED",
            "bq_unsealed": True,
        }

    @property
    def charge_config(self) -> ChargeConfig:
        """Teljes kódbázis review 15. kör (2026-09-04), Szoftver/
        Architektúra P2 lelet: a testvér TraditionalLearningController
        már exponálja ezt a property-t (DATA-006, 2026-08-24) — a
        GoldenGgController-ről hiányzott, holott a FULL_CHARGE fázisban
        ténylegesen ugyanezt a self._charge_config-ot használja a
        beágyazott ChargeController felépítésekor. Enélkül
        main_window.py::_write_report() charge_config_snapshot mezője
        GOLDEN_GG session-nél sosem töltődött ki (getattr(ggc,
        "charge_config", None) mindig None-t adott), holott a mező
        pontosan a max_charge_Ah_factor 1.20→1.50 incidens (2026-08-24,
        c18e525) miatti traceability-igényt hivatott kielégíteni."""
        return self._charge_config

    @property
    def accumulated_charge_Ah(self) -> float:
        return self._energy_integrator.accumulated_charge_Ah

    @property
    def accumulated_discharge_Ah(self) -> float:
        return self._energy_integrator.accumulated_discharge_Ah

    @property
    def i_load_measured_A(self) -> float | None:
        """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        l. discharge_controller.py::i_load_measured_A property docstringje
        -- ugyanaz a tick-scoped validity-flag minta, a SYSTEM_RATE_
        DISCHARGE fázisra vonatkozik."""
        return self._i_load_measured_A

    @property
    def load_current_readback_valid(self) -> bool | None:
        """L. i_load_measured_A docstringje."""
        return self._load_current_readback_valid

    @property
    def accumulated_charge_Wh(self) -> float:
        """Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        + Metrológia P2 lelet: az accumulated_charge_Ah Wh-párja — a
        test_runner.py::_build_sample() korábban a controller privát
        `_integrator` attribútumát kereste `getattr`-rel, ami ezen a
        controlleren SOSEM létezett (`_energy_integrator` a neve), ezért a
        Wh-mezők a TELJES session alatt None maradtak a samples.csv-ben."""
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
    def missed_deadline_count(self) -> int:
        """SERVICE-határidők: élő gyermek vagy egyszer átvett fázisösszeg."""
        live = self._relax_ctrl.missed_deadline_count if self._relax_ctrl is not None else 0
        return self._relax_missed_deadline_total + live

    @property
    def relax_dmm_fault_total(self) -> int:
        """Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
        Adatintegritás P2 lelet: a relax-fázisok (RELAX_2H/RELAX_5H) alatti
        feszültség-DMM-olvasási hibák session-szintű összege korábban csak
        _run_export()-ban (az export-zip belsejében, csak ha a session
        ténylegesen eléri az EXPORT_PACKAGE állapotot) jelent meg — a fő
        session report.json-jában sosem, és FAULT-tal végződő session
        esetén nyomtalanul elveszett. L. main_window.py::_write_report()
        golden_gg_relax_dmm_fault_total mezője.

        Review-kör 2 (2026-09-09), friss Szoftver/Architektúra F1 — a
        `traditional_learning_controller.py` azonos nevű property-jén a
        CF-05 2. MAX verifier P3-1 lelete (2026-09-08) már javította ezt a
        rést, de erre a testvér-példányra nem lett átvezetve: a fenti
        `_relax_dmm_fault_total` KIZÁRÓLAG a `_run_relax()` RELAX_DONE
        ágában carry-overölt érték — egy `emergency_stop()`/fault-alapú
        megszakítás a RELAX_2H/RELAX_5H fázis KÖZBEN (a RELAX_DONE elérése
        előtt) nem hívja meg azt a carry-overt, tehát a `self._relax_ctrl`
        élő, még nem carry-overölt `dmm_fault_count`-ja elveszett volna. Az
        `extreme_dt_s_count`/`max_observed_dt_s` property-k mintáját követve
        (l. fent) az ÉLŐ relax_ctrl értékét is hozzáadjuk — a carry-over és
        az élő tag konstrukció szerint kizárja egymást (a
        `self._relax_ctrl = None` mindig közvetlenül a carry-over UTÁN
        történik, l. :657-658), tehát ez sosem duplázhat."""
        live = self._relax_ctrl.dmm_fault_count if self._relax_ctrl is not None else 0
        return self._relax_dmm_fault_total + live

    @property
    def temp_dmm_fault_ticks(self) -> int:
        """Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-2
        -- l. self._temp_dmm_fault_ticks docstringje __init__-ben (a
        chemid_controller.py azonos property-jének testvére). L.
        main_window.py::_write_report() golden_gg_temp_dmm_fault_ticks
        mezője. Review-kör 3 (2026-09-09), MAX verifier
        (a306e7e764e3a717b) P2-1, DFAM-001 további előfordulása: a
        beágyazott ChargeController (FULL_CHARGE alatt) saját összegét is
        hozzáadja, a temperature_out_of_range_count property mintáját
        követve."""
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
    def u_batt_V(self) -> float:
        """2026-08-23 traditional_learning OV incidens elemzés kiterjesztése
        golden_gg-re (ugyanaz a beágyazott ChargeController mintát követi,
        FULL_CHARGE alatt): l. TraditionalLearningController.u_batt_V
        docstringje — a TestRunner ezt használja a saját, tick-desszinkron
        InstrumentManager.read_all() DMM-olvasás helyett."""
        return self._u_batt

    @property
    def battery_temperature_C(self) -> float:
        """L. TraditionalLearningController.battery_temperature_C
        docstringje ugyanerről a duplán olvasott hőmérséklet-DMM mintáról."""
        return self._battery_temperature_C

    @property
    def dmm_valid(self) -> bool:
        """L. DischargeController.dmm_valid docstringje ugyanerről a
        hiányzó property-mintáról — test_runner.py::_build_sample()
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
    def u_psu_set_V(self) -> float:
        """2026-08-23 kiegészítés — l. TraditionalLearningController.u_psu_set_V
        docstringje ugyanerről a hiányzó proxy-mintáról."""
        return self._charge_ctrl.u_psu_set_V if self._charge_ctrl is not None else 0.0

    @property
    def last_integration_source(self) -> str:
        """Teljes kódbázis review 11. kör (2026-08-31), Szoftver/Architektúra
        P3 lelet — l. u_psu_set_V docstringje ugyanerről a hiányzó proxy-
        mintáról. test_runner.py::_build_sample() sample["integration_
        current_source"]-je enélkül mindig None maradt FULL_CHARGE alatt."""
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
        sample["taper_timer_s"]-je enélkül mindig None maradt FULL_CHARGE
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
        levonja (elkerülve a duplán-integrálást a töltési fázisban)."""
        return self._charge_ctrl.last_fast_loop_extra_s if self._charge_ctrl is not None else 0.0

    @property
    def i_psu_set_A(self) -> float:
        """Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        P2 lelet — l. traditional_learning_controller.py azonos property
        docstringje ugyanerről a delegáló mintáról."""
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
    def last_warning_code(self) -> str:
        """2026-08-23 kiegészítés — l. TraditionalLearningController.
        last_warning_code docstringje ugyanerről a hiányzó proxy-mintáról
        (dióda-disszipáció warning FULL_CHARGE alatt). Teljes kódbázis
        review 9. kör (2026-08-30), Szoftver/Architektúra P2 lelet: a
        FULL_CHARGE alatt a beágyazott ChargeController saját warning-ja
        élvez elsőbbséget, egyébként a saját szintű (PRECOND/CONDITIONING/
        RELAX_2H/SYSTEM_RATE_DISCHARGE/RELAX_5H/CHARGE_TERMINATION_VERIFIED)
        hőmérséklet-DMM warning — l. _last_warning_code docstringje."""
        if self._charge_ctrl is not None and self._charge_ctrl.last_warning_code:
            return self._charge_ctrl.last_warning_code
        return self._last_warning_code

    @property
    def temperature_out_of_range_count(self) -> int:
        """Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        lelet — l. charge_controller.py azonos property docstringje. A
        beágyazott ChargeController (FULL_CHARGE alatt) saját összegét is
        hozzáadja — a számláló, ellentétben a last_warning_code-dal,
        ADDITÍV a session teljes hosszán."""
        return self._temperature_out_of_range_count + (
            self._charge_ctrl.temperature_out_of_range_count
            if self._charge_ctrl is not None else 0
        )

    @property
    def i_load_set_A(self) -> float:
        """2026-08-24 (élő panel audit, session_fg1_20260823_200337 nyomán) —
        l. TraditionalLearningController.i_load_set_A docstringje ugyanerről a
        hiányzó proxy-mintáról. SYSTEM_RATE_DISCHARGE alatt a load ténylegesen
        `system_rate_current_A`-val van vezérelve (l. _run_system_rate_discharge())."""
        if self._state == GoldenGgState.SYSTEM_RATE_DISCHARGE:
            return self._config.system_rate_current_A
        return 0.0

    # ------------------------------------------------------------------ #
    # Fő advance()                                                         #
    # ------------------------------------------------------------------ #

    def advance(self, dt_s: float) -> GoldenGgState:
        # R2-06 (2026-09-15): tick-scoped validity flag -- MINDEN korai-
        # return és safety-kapu ELŐTT nullázva (l. feedback_new_tick_
        # scoped_validity_flag_needs_explicit_reset.md).
        self._i_load_measured_A = None
        self._load_current_readback_valid = None
        # R6-B3: kompozit tick eleji reset (l. last_integration_source);
        # a kisutesi integralasi pont, ill. a toltesi ag atvetele irja felul.
        self._last_integration_source = "ZERO"

        if self._state in _TERMINAL_STATES:
            return self._state

        # CF-19 -- l. ocv_soc_controller.py::advance() azonos hivatkozású
        # kommentje.
        self._phase_elapsed_s += dt_s

        self._last_warning_code = ""

        if self._state != GoldenGgState.FULL_CHARGE:
            if self._state in _RELAX_STATES:
                self._read_temperature_only(dt_s)
            else:
                self._dmm_valid = self._read_dmm(dt_s)
            # A rendes vég után fennmaradó terhelés nem kap újabb felmentést.
            if (
                self._state not in _RELAX_STATES
                and self._normal_discharge_cutoff_seen and self._load.input_commanded_on
            ):
                if not self._dmm_valid:
                    self.emergency_stop("DMM_FEEDBACK_LOST")
                    return self._state
                if not self._safety.enforce_normal_discharge(
                    self._u_batt, self._config.system_rate_current_A, self._profile.terminate_voltage_pack_V,
                    self.emergency_stop, self.on_event,
                ):
                    return self._state
            # Release-audit 2026-08-23 (2.12): homerseklet-DMM-kapu
            # — a FULL_CHARGE fázist a beágyazott ChargeController már fedi.
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
            if self._state == GoldenGgState.SYSTEM_RATE_DISCHARGE:
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

        if self._state == GoldenGgState.INIT:
            self._enter(GoldenGgState.PRECOND)
        elif self._state == GoldenGgState.PRECOND:
            self._run_precond()
        elif self._state == GoldenGgState.CONDITIONING:
            self._run_conditioning(dt_s)
        elif self._state == GoldenGgState.FULL_CHARGE:
            self._run_full_charge(dt_s)
        elif self._state == GoldenGgState.CHARGE_TERMINATION_VERIFIED:
            self._run_charge_termination_verified()
        elif self._state == GoldenGgState.RELAX_2H:
            self._run_relax(dt_s, is_full=True)
        elif self._state == GoldenGgState.SYSTEM_RATE_DISCHARGE:
            self._run_system_rate_discharge(dt_s)
        elif self._state == GoldenGgState.RELAX_5H:
            self._run_relax(dt_s, is_full=False)
        elif self._state == GoldenGgState.EXPORT_PACKAGE:
            self._run_export()

        else:
            # Review-kör 5, Session/Workflow S5-05 (P3, DFAM-004 sibling) --
            # l. charge_controller.py::advance() azonos hivatkozású
            # kommentje ugyanerről a hibaosztályról. A _TERMINAL_STATES-t
            # (DONE/FAULT) a függvény ELEJÉN lévő korai return már lefedi.
            raise ValueError(f"Unhandled GoldenGgState in advance(): {self._state!r}")

        return self._state

    # ------------------------------------------------------------------ #
    # Vészleállítás [R1]                                                   #
    # ------------------------------------------------------------------ #

    def emergency_stop(self, reason: str) -> None:
        # Teljes kódbázis review 7. kör (2026-08-30), Hardver/Műszervezérlés
        # P2 lelet — l. charge_controller.py::emergency_stop() docstringje
        # ugyanerről a csendben elnyelt OFF-kísérlet-hiba mintáról. Itt az
        # emittált esemény is a kiegészített self._fault_reason-t kapja.
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
        # MAX verifier P2-1 (2026-09-11): a terminális állapotba lépés
        # (_enter(FAULT)) MEGELŐZI a lenti BQ-zárási kísérletet -- l.
        # traditional_learning_controller.py::emergency_stop() azonos
        # sorrendje. Ha a lenti _ensure_bq_sealed_or_flag() akár ~9s-ig
        # (3 retry × 1s timeout) blokkolna egy holt Pico-kapcsolaton, a
        # controller.state MÁR FAULT, nem egy köztes, nem-terminális
        # állapot -- egy eközben érkező megszakítás (pl. Ctrl+C) sem
        # hagyhatja a state-et FAULT-on kívül.
        self._enter(GoldenGgState.FAULT)
        # [SCF-011 újratervezés, 2026-09-11, felhasználói döntés] MINDEN
        # terminális útvonalon (itt: FAULT) explicit, visszaellenőrzött
        # zárási kísérlet -- l. _ensure_bq_sealed_or_flag() docstringje.
        # A külső eseményfogyasztó csak az összes BQ-cleanup kísérlet után futhat.
        pending_events: list[dict] = []
        self._ensure_bq_sealed_or_flag("EMERGENCY_STOP", emit=pending_events.append)
        for event in pending_events:
            self._emit(event)
        self._emit({"event_code": "GOLDEN_GG_FAULT", "event_message": self._fault_reason})

    def _emit(self, payload: dict) -> None:
        if self.on_event is not None:
            self.on_event(payload)

    def _emit_relax_event(
        self, payload: dict, *, emit: Callable[[dict], None] | None = None,
    ) -> None:
        """R2-12 (DFAM-011 + DFAM-013): a beágyazott RelaxController
        eseményét a JELENLEGI relax-alállapottal (RELAX_2H/RELAX_5H)
        kiegészítve továbbítjuk -- a generikus "RELAX_DMM_FAULT" üzenet
        önmagában megkülönböztethetetlen lenne a két relax-fázis között.
        Ugyanaz a `..._AT_{self._state.value}` kontextus-minta, mint a
        quality_flags oldalon. Az `event_code` SZÁNDÉKOSAN változatlan
        (events.csv/EventLogWidget kompatibilitás)."""
        enriched = dict(payload)
        enriched["event_message"] = (
            f"{self._state.value}: {payload.get('event_message', '')}"
        )
        (self._emit if emit is None else emit)(enriched)

    def _ensure_bq_sealed_or_flag(
        self, context: str, *, emit: Callable[[dict], None] | None = None,
    ) -> bool:
        """[SCF-011 újratervezés, 2026-09-11, felhasználói döntés] l.
        TraditionalLearningController._ensure_bq_sealed_or_flag()
        docstringje ugyanerről a mintáról -- a KÜLÖNBSÉG: ez a controller
        sosem ír a BQ-ba (a chip csak a PRECOND-beli forrásolvasáshoz lett
        unsealed, l. _run_precond()), ezért itt NINCS külön "temp-write
        restore" lépés, ami a normál záró utat adná -- ez a metódus MAGA a
        session egyetlen záró seal-lépése, minden terminális úton
        (EXPORT_PACKAGE -> DONE, emergency_stop() -> FAULT) meghívva.
        Explicit SEAL-ÁLLAPOT VISSZAELLENŐRZÉSSEL zár, nem csak "küldtük a
        parancsot, reméljük működött". Visszatér: True, ha a végállapot
        igazoltan SEALED, False egyébként (ilyenkor self._quality_flags/
        self._emit() már rögzítette a hibát -- ez a controller sosem
        blokkol emiatt semmilyen eredményt, csak attesztál)."""
        emit = self._emit if emit is None else emit
        # F19-S1 (Kör 19, DFAM-032, mirroring R17-05's TraditionalLearningController
        # fix 1:1): korábban `return False`-t adott, ha ez az ELSŐ olvasás
        # (a "már sealed?" gyors ellenőrzés) hibázott -- ez SOHA nem kísérelte
        # meg a seal()-t, miközben egy tranziens olvasási hiba (pl. Pico
        # dropout) után a rákövetkező seal() RPC ténylegesen sikeres lehetett
        # volna. JAVÍTVA: a hiba rögzítve marad, DE a végrehajtás FOLYTATÓDIK
        # a seal() megkísérlésére (ugyanaz az ág, mint amikor az olvasás
        # sikeres, de cs.ss még False).
        try:
            cs = decode_control_status(self._bq_driver.read_control_status())
            if cs.ss:
                self._bq_reseal_confirmed = True
                return True
        except Exception as exc:
            self._quality_flags.append(f"BQ_SEAL_STATE_VERIFY_FAILED_AT_{context}:{exc}")
            emit({
                "event_code": "GOLDEN_GG_SEAL_STATE_VERIFY_FAILED",
                "event_message": f"{context}: {exc}",
            })
        try:
            self._bq_transport.seal(confirm=True)
        except Exception as exc:
            self._quality_flags.append(f"BQ_SEAL_RESTORE_FAILED_AT_{context}:{exc}")
            emit({
                "event_code": "GOLDEN_GG_SEAL_RESTORE_FAILED",
                "event_message": f"{context} fallback seal: {exc}",
            })
            return False
        try:
            cs = decode_control_status(self._bq_driver.read_control_status())
        except Exception as exc:
            self._quality_flags.append(f"BQ_SEAL_STATE_VERIFY_FAILED_AT_{context}:{exc}")
            emit({
                "event_code": "GOLDEN_GG_SEAL_STATE_VERIFY_FAILED",
                "event_message": f"{context} post-seal verify: {exc}",
            })
            return False
        if not cs.ss:
            self._quality_flags.append(
                f"BQ_SEAL_RESTORE_FAILED_AT_{context}:seal command accepted but SS bit still clear"
            )
            emit({
                "event_code": "GOLDEN_GG_SEAL_RESTORE_FAILED",
                "event_message": f"{context}: seal() lefutott, de SS bit még nem SEALED-et jelez",
            })
            return False
        self._bq_reseal_confirmed = True
        return True

    # ------------------------------------------------------------------ #
    # PRECOND — SUP03-GG-002                                              #
    # ------------------------------------------------------------------ #

    def _run_precond(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        voltage_result = self._safety.check_precheck_voltage(self._u_batt)
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return
        try:
            chem_id = self._bq_driver.read_chem_id()
        except TestAbortedByUser:
            raise
        except Exception as exc:
            self.emergency_stop(f"BQ_COMM_UNAVAILABLE: {exc}")
            return
        if self._expected_chem_id is not None and chem_id != self._expected_chem_id:
            self.emergency_stop(
                f"CHEM_ID_MISMATCH: expected=0x{self._expected_chem_id:04X} "
                f"actual=0x{chem_id:04X}"
            )
            return

        # [SCF-011 újratervezés, 2026-09-11, felhasználói döntés] a lenti
        # poll_bq_diagnostics() a Qmax/Ra táblát is olvassa (data_flash_
        # block-on keresztül) -- l. TraditionalLearningController.
        # _run_precond() azonos hivatkozású kommentje ugyanerről a
        # BQ-DF-SEALED-001 forrásáról (SLUSB55B: "In SEALED mode, data
        # flash cannot be accessed through commands 0x3e and 0x3f").
        # Ez a controller a chipbe SOSEM ír (l. modul-docstring), de a
        # forrás-pillanatkép MEGBÍZHATÓ olvasásához az UNSEAL itt is
        # kötelező -- session-szintű életciklus, ugyanaz a minta, mint
        # TraditionalLearningController-nél: egyszeri unseal itt, a chip a
        # session VÉGÉIG (DONE/FAULT) UNSEALED marad, l.
        # _ensure_bq_sealed_or_flag() a záró oldalon (_run_export()/
        # emergency_stop()).
        # R2-11b (2026-09-15): a `_bq_unseal_attempted` SZÁNDÉKOSAN az RPC
        # ELŐTT vált True-ra -- l. pending_bq_unsealed docstringje.
        self._bq_unseal_attempted = True
        try:
            self._bq_transport.unseal(confirm=True)
            # [SCF-011 újratervezés, MAX verifier P1-1 utóellenőrzés,
            # 2026-09-11] l. TraditionalLearningController._run_precond()
            # azonos hivatkozású kommentje ugyanerről a szimmetria-hiányról
            # -- egy "unseal() RPC ok-kal tért vissza" NEM bizonyítja, hogy
            # az SS bit ténylegesen 0-ra váltott.
            cs = decode_control_status(self._bq_driver.read_control_status())
        except TestAbortedByUser:
            raise
        except Exception as exc:
            self.emergency_stop(f"BQ_UNSEAL_FAILED: {exc}")
            return
        if cs.ss:
            self.emergency_stop("BQ_UNSEAL_NOT_EFFECTIVE: SS bit still set after unseal()")
            return

        # SUP03-GG-002/009: a gg.csv forrása — a ChemID programozása utáni
        # gauge-paraméter pillanatkép, ugyanabból a rétegből, mint a BQ
        # Diagnosztika (WP-AK-07) és kalibráció (WP-AK-09). SZÁNDÉKOSAN itt,
        # PRECOND-ban rögzítve (a "post-ChemID-programming" állapotot
        # reprezentálja), NEM a fizikai ciklus végén — a fizikai
        # töltés/kisütés saját maga nem programozza újra a ChemID-t.
        gg_snapshot = poll_bq_diagnostics(self._bq_driver)
        self._gg_source_snapshot = gg_snapshot
        # Kor 18, Batch C7 R17-16 (DFAM-021+DFAM-001): ez a snapshot DONTO
        # (verdikt-kritikus) volt, de korabban KIZAROLAG a controller sajat
        # mezojebe kerult -- sem az elo BQ-diagnosztika nezetbe, sem a
        # bq_diagnostics.csv-be nem jutott el. L. _emit_gauge_sample_best_
        # effort() docstringje.
        self._emit_gauge_sample_best_effort(gg_snapshot)

        # [review-lelet] a qmax/Ra table érvényességét MÁR ITT ellenőrizni
        # kell, nem csak az EXPORT_PACKAGE lépésben (build_golden_gg_
        # submission_package() saját kapuja) — különben egy PRECOND-kori,
        # előre észlelhető BQ-olvasási hiba csak a TELJES, több órás
        # töltés/2h relax/kisütés/5h relax ciklus VÉGÉN derülne ki,
        # feleslegesen elpazarolva a teljes fizikai mérést.
        if not gg_snapshot.qmax.valid or not gg_snapshot.ra_table_mohm.valid:
            self.emergency_stop(
                "GG_SOURCE_SNAPSHOT_INCOMPLETE: "
                f"qmax_valid={gg_snapshot.qmax.valid} ({gg_snapshot.qmax.error}), "
                f"ra_table_valid={gg_snapshot.ra_table_mohm.valid} "
                f"({gg_snapshot.ra_table_mohm.error})"
            )
            return

        # R16-B7F1: ugyanaz a stabil DF-kapu, mint TL-ben; csak igazolt
        # UNSEALED állapotban, minden kondicionálás és töltés előtt.
        report = evaluate_provisioning_consistency(
            self._profile, Bq34z110DataFlashService(self._bq_transport),
        )
        if not report.consistent:
            self.emergency_stop(
                f"GOLDEN_GG_CONFIGURATION_BLOCKED: {report.decision.value}: {report.reason}"
            )
            return

        self._emit({
            "event_code": "GG_BQ_DF_CROSS_CHECK_PASSED",
            "event_message": f"{report.reason}; {format_bq_session_scale_context(report)}",
        })
        self._conditioning_elapsed_s = 0.0
        if self._config.conditioning_required:
            self._enter(GoldenGgState.CONDITIONING)
        else:
            self._enter_full_charge()

    # ------------------------------------------------------------------ #
    # CONDITIONING — SUP03-GG-005 lépés 1 (opcionális)                     #
    # ------------------------------------------------------------------ #

    def _run_conditioning(self, dt_s: float) -> None:
        self._conditioning_elapsed_s += dt_s
        self._ambient_temperature_C = self._battery_temperature_C
        if self._conditioning_elapsed_s >= self._config.conditioning_min_s:
            self._enter_full_charge()

    # ------------------------------------------------------------------ #
    # FULL_CHARGE — SUP03-GG-005 lépés 2/3 (napló a töltéstől kezdve)      #
    # ------------------------------------------------------------------ #

    def _enter_full_charge(self) -> None:
        self._charge_ctrl = ChargeController(
            self._psu, self._load, self._dmm_v, self._dmm_t,
            self._profile, self._safety, self._charge_config,
        )
        self._log_elapsed_s = 0.0
        self._samples = []
        self._enter(GoldenGgState.FULL_CHARGE)

    def _run_full_charge(self, dt_s: float) -> None:
        assert self._charge_ctrl is not None
        # R1 B8a, F-20 (DFAM-035): a gyermek integrátorának állása az
        # advance() ELŐTT -- l. lent a _record_sample() hívás kommentjét.
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
            # GG: közvetlenül az integrátorba, NEM a _record_sample()-lel
            # (az exportsort, _log_elapsed_s-t és BQ-pollt is végezne).
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
        # temp_dmm_valid property FULL_CHARGE alatt (ahol a saját
        # _read_dmm() ki van kapcsolva) hamisan True maradt egy
        # ténylegesen halott hőmérséklet-DMM mellett is.
        self._temp_dmm_valid = self._charge_ctrl.temp_dmm_valid
        # [Kör 19 Batch D3c, F-3 rész (a)] monoton OR -- egyszer True, mindig
        # True, a temp_dmm_valid fenti (tick-enkénti) mintájától eltérően.
        self._temp_dmm_ever_read_ok = (
            self._temp_dmm_ever_read_ok or self._charge_ctrl.temp_dmm_ever_read_ok
        )
        self._u_batt = self._charge_ctrl.u_batt_V
        self._battery_temperature_C = self._charge_ctrl.battery_temperature_C

        # Teljes kódbázis review 5. kör (2026-08-29), Metrológia/
        # Adatintegritás P1 lelet: korábban egy MÁSODIK, önálló self._psu.
        # measure_output_current() hívás történt itt, amit hiba esetén
        # csendben 0.0-val helyettesített (CLAUDE.md §10.4 sérülés) — akkor
        # is, ha a beágyazott ChargeController SAJÁT, ugyanebben a tickben
        # végzett olvasása sikeres volt. A ChargeController.i_psu_measured_A
        # a MÁR fault-kezelt (advance()-ban emergency_stop("PSU_COMM_LOST")
        # hívó) belső állapotot exponálja — nincs szükség redundáns, saját
        # hibakezelésű újraolvasásra.
        # Teljes kódbázis review 2. kör (2026-09-15, R2-18, 12. writer-kör):
        # `i_psu_measured_A` (a beágyazott ChargeController `_i_charge`
        # mezőjét tükrözi) NEM tick-scoped, csak `_read_psu_current()`
        # sikeres futásakor íródik -- egy top-of-tick safety-kapu FAULT-
        # ticken (ahol `_run_charge_cc()` nem fut) az ELŐZŐ tick ÉRTÉKÉN
        # marad, miközben `psu_current_readback_valid` helyesen None-ra
        # reset. Enélkül egy ilyen ticken elévült, nem-nulla áram
        # integrálódna a session-szintű `_energy_integrator`-ba (és a
        # GoldenGgSample TI-GPC exportsorába). `0.0`-t adunk, NEM skip-et
        # -- a `_record_sample()` a dt-statisztikákat (extreme_dt_s_count/
        # max_observed_dt_s) is vezeti, amit egy skip elveszítene minden
        # PRECHECK/PSU_PRESET/kapu-FAULT tickről. (R1 B8a, F-20 óta a
        # session-szintű Ah/Wh a gyermek deltájából jön, l. lent -- az i_psu
        # itt már csak a GoldenGgSample exportsorát táplálja.)
        i_psu = self._charge_ctrl.i_psu_measured_A if self._charge_ctrl.psu_current_readback_valid is True else 0.0
        # Review 2026-09-15 (C-06/DFAM-035, RECURRING) + R1 B8a, F-20 --
        # l. ocv_soc_controller.py::_run_precharge() azonos hivatkozású
        # kommentje a teljes indoklásért: a session-szintű Ah/Wh (a
        # _record_sample()-en belüli self._energy_integrator) a beágyazott
        # ChargeController EZEN a ticken (fast-loop résztickjeivel együtt)
        # elintegrált deltáját kapja (`energy_charge_delta`), a FAULT- és a
        # CHARGE_DONE-ág (gyermek eldobása) ELŐTT -- a belső integrál az
        # EGYETLEN numerikus forrás. A dt_s + extra idő és a fenti i_psu
        # (a tick utolsó, friss áramolvasása) VÁLTOZATLANUL a GoldenGgSample
        # exportsorába, a _log_elapsed_s időtengelybe és a BQ-poll-throttle-
        # ba megy (GG időalap, l. V-3 lent). (A _record_sample()-en belüli
        # BQ-poll-throttle SAJÁT, külön exclude-mezője -- l. Kör 2/C-01 --
        # ettől FÜGGETLENÜL, helyesen komponálódik: két KÜLÖNBÖZŐ kizárt
        # időforrás, nem ütköznek.)
        #
        # MAX verifier V-3 megjegyzése (2026-09-15): mivel a `_record_
        # sample()` EGYETLEN dt_s paramétert kap, ez a javítás NEM csak
        # a fenti self._energy_integrator-t érinti, hanem UGYANEZZEL a
        # megnövelt idővel táplálja a self._log_elapsed_s-t (a GoldenGgSample.
        # elapsed_s mező -- a TI GPC/CHEM export IDŐTENGELYE, l.
        # gpcchem_export.py resample_for_export()) ÉS a self._gauge_poll_
        # elapsed_s BQ-poll-throttle-t (l. C-01 fent) IS. Mindhárom hatás
        # a FIZIKAI valósághoz közelebb visz (a session TELJES valós idejét
        # tükrözi, nem csak a runner-tick nominális idejét) -- SZÁNDÉKOS,
        # nem mellékhatás, de dokumentálva legyen: ez eltér a 3 testvér-
        # controllertől, ahol a fix KIZÁRÓLAG a self._energy_integrator-t
        # érinti (ott az elapsed_s/throttle-akkumulátor önálló, saját
        # dt_s-t kap ugyanabban a tick-ben, nem osztja meg a hívást).
        self._record_sample(
            dt_s + self._charge_ctrl.last_fast_loop_extra_s,
            current_A=i_psu,
            energy_charge_delta=(
                self._charge_ctrl.accumulated_charge_Ah - charge_Ah_before,
                self._charge_ctrl.accumulated_charge_Wh - charge_Wh_before,
            ),
        )
        # R6-B3: a gyermek megorzott forrasanak atvetele (nem friss-integralas-
        # bizonyitek), a gyermek elengedese ELOTT.
        self._last_integration_source = self._charge_ctrl.last_integration_source

        if charge_state == ChargeState.FAULT:
            self.emergency_stop(self._charge_ctrl.fault_reason or "GOLDEN_GG_CHARGE_FAULT")
        elif charge_state == ChargeState.CHARGE_DONE:
            self._charge_completed = True
            # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow
            # P2 lelet — l. chemid_controller.py azonos hivatkozású
            # kommentje: a _charge_ctrl = None ELŐTT át kell menteni a
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
            self._enter(GoldenGgState.CHARGE_TERMINATION_VERIFIED)

    def _run_charge_termination_verified(self) -> None:
        if not (self._charge_completed and self._dmm_valid):
            self.emergency_stop("CHARGE_TERMINATION_NOT_VERIFIED")
            return
        self._enter(GoldenGgState.RELAX_2H)

    # ------------------------------------------------------------------ #
    # RELAX — tisztán idő-alapú (nincs forrásolt BQ-kritérium Route G-hez) #
    # ------------------------------------------------------------------ #

    def _check_loaded_relax_sample(self, sample: RelaxSample) -> None:
        # Q-35: a lezáró pont után egy jó rekord sem fedhet el terhelt alamintát.
        if (
            self._state == GoldenGgState.FAULT
            or not self._normal_discharge_cutoff_seen or not self._load.input_commanded_on
        ):
            return
        if sample.error is not None or sample.voltage_V is None:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        self._u_batt = sample.voltage_V
        self._safety.enforce_normal_discharge(
            sample.voltage_V, self._config.system_rate_current_A, self._profile.terminate_voltage_pack_V,
            self.emergency_stop, self.on_event,
        )

    def _run_relax(self, dt_s: float, is_full: bool) -> None:
        min_s = self._config.relax_after_charge_s if is_full else self._config.relax_after_discharge_s
        if self._relax_ctrl is None:
            self._relax_ctrl = RelaxController(self._dmm_v, RelaxConfig(min_relax_s=min_s))
            self._relax_exit_pending = False
            self._relax_ctrl.on_event = self._emit_relax_event
            self._relax_ctrl.on_sample = self._check_loaded_relax_sample
            # Runner alatt SERVICE; service nélküli közvetlen hívás explicit LEGACY_TICK.
            if self.prepare_relax_sampling is not None:
                self.prepare_relax_sampling(self._relax_ctrl)

        if not self._relax_exit_pending:
            relax_state = self._relax_ctrl.advance(dt_s)
            # A nyers rekord kapuja már leállíthatta a szülőt a drain közben.
            if self._state == GoldenGgState.FAULT:
                return
            self._dmm_valid = self._relax_ctrl.dmm_valid
            if self._dmm_valid:
                self._u_batt = self._relax_ctrl.last_voltage_V
            # A rendes vég után fennmaradó terhelés nem kap újabb felmentést.
            if self._normal_discharge_cutoff_seen and self._load.input_commanded_on:
                if not self._dmm_valid:
                    self.emergency_stop("DMM_FEEDBACK_LOST")
                    return
                if not self._safety.enforce_normal_discharge(
                    self._u_batt, self._config.system_rate_current_A, self._profile.terminate_voltage_pack_V,
                    self.emergency_stop, self.on_event,
                ):
                    return
            self._record_sample(dt_s, current_A=0.0)
            if relax_state != RelaxState.RELAX_DONE:
                return
            # GoldenGG: csak minimumidő, nincs TLC-féle BQ/OLS-elfogadási feltétel.
            self._relax_exit_pending = True

        released = self._relax_ctrl.poll_sampling_stop()
        if self._state == GoldenGgState.FAULT or not released:
            return
        self._dmm_valid = self._relax_ctrl.dmm_valid
        handoff_events: list[dict] = []
        not_fresh_event = None
        if is_full:
            # Csak az energizáló ág igényel friss kapumérést és hibadiagnózist.
            snapshot = (
                self._relax_ctrl.read_handoff_voltage(
                    emit=lambda event: self._emit_relax_event(event, emit=handoff_events.append),
                )
                if self._relax_ctrl.sampling_mode == "SERVICE"
                else self._relax_ctrl.voltage_snapshot()
            )
            self._dmm_valid = (
                snapshot.quality == "CACHE_FRESH" if self._relax_ctrl.sampling_mode == "SERVICE"
                else self._relax_ctrl.dmm_valid
            )
            if not self._dmm_valid:
                reason = (
                    "DMM_READ_ERROR" if snapshot.quality == "READ_ERROR"
                    else "STALE" if snapshot.quality == "STALE" else "INSUFFICIENT_DATA"
                )
                not_fresh_event = {
                    "event_code": "GG_RELAX_DMM_NOT_FRESH",
                    "event_message": (
                        f"{self._state.value}: {reason}; sample_age_s={snapshot.age_s}; "
                        f"read_start={snapshot.read_start}"
                    ),
                    "dmm_reason": reason, "sample_age_s": snapshot.age_s,
                    "read_start": snapshot.read_start,
                }
        if self._dmm_valid:
            self._u_batt = self._relax_ctrl.last_voltage_V
        # A végső drain mintája ugyanazt a szigorú kaput kapja.
        if self._normal_discharge_cutoff_seen and self._load.input_commanded_on:
            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return
            if not self._safety.enforce_normal_discharge(
                self._u_batt, self._config.system_rate_current_A, self._profile.terminate_voltage_pack_V,
                self.emergency_stop, self.on_event,
            ):
                return
        self._relax_dmm_fault_total += self._relax_ctrl.dmm_fault_count
        self._relax_missed_deadline_total += self._relax_ctrl.missed_deadline_count
        self._relax_ctrl = None
        self._relax_exit_pending = False
        if is_full:
            # A meglévő indítókapu őrzi a hőmérsékleti hiba elsőbbségét.
            # A diagnosztikai callback csak a totalok és a cleanup után futhat.
            self._begin_system_rate_discharge()
            # A handoff hibája is csak a totalátvétel és az OFF/seal után juthat ki.
            for event in handoff_events:
                self._emit(event)
            if not_fresh_event is not None:
                self._emit(not_fresh_event)
        else:
            self._enter(GoldenGgState.EXPORT_PACKAGE)

    # ------------------------------------------------------------------ #
    # SYSTEM_RATE_DISCHARGE — SUP03-GG-005 lépés 5/6, SUP03-GG-007         #
    # ------------------------------------------------------------------ #

    def _begin_system_rate_discharge(self) -> None:
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
            self._u_batt, self._config.system_rate_current_A, self._profile.terminate_voltage_pack_V,
            self.emergency_stop, self.on_event,
        ):
            return
        # Kör 20 Batch E13a (F20-S3, DFAM-001, a discharge_controller.py/
        # traditional_learning_controller.py/iec_dc_resistance_controller.py
        # mar meglevo mintajat koveteve): a lenti, per-tick kapu (l.
        # _run_system_rate_discharge() self._safety.check_concurrent_psu_
        # load() hivasa) a KOVETKEZO ticken futna, a MAR eletbe lepett
        # input_commanded_on-t olvasva -- es mivel device_psu.py::all_
        # outputs_off() a sajat finally-jaban FELTETEL NELKUL output_
        # commanded_on=False-ra allitja, egy csendben sikertelen PSU-
        # kikapcsolas emiatt LATHATATLAN maradna ennek a per-tick kapunak.
        # A kapunak ezert MIELOTT all_outputs_off() fut, elorejelzo modon
        # (load_commanded_on=True szimulalva) kell futnia, nem utana.
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=True,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        # R8-B1: setpoint-alapu teljesitmenykapu a Load inditasa elott.
        power_result = self._safety.check_load_power(self._u_batt, self._config.system_rate_current_A)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return
        # A mérés/várakozás alatt érkező kérést még a Load-konfiguráció előtt nézzük.
        # A safety-fault elsőbbsége megmarad; az okfeloldás és a közös OFF a runneré.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        try:
            self._psu.all_outputs_off()
            self._load.set_mode_cc()
            self._load.set_current(self._config.system_rate_current_A)
            self._normal_discharge_cutoff_seen = False
            self._load.input_on(stop_requested=self.handoff_stop_requested)
        except TestAbortedByUser:
            # A runner az élő okot oldja fel, és egyszer kapcsolja le mindkét műszert.
            return
        except Exception as exc:
            self.emergency_stop(f"SYSTEM_RATE_DISCHARGE_SETUP_FAILED: {exc}")
            return
        self._discharge_phase_elapsed_s = 0.0
        self._enter(GoldenGgState.SYSTEM_RATE_DISCHARGE)

    def _run_system_rate_discharge(self, dt_s: float) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        # Q-35: a cutoff-kivétel csak egyszer fogyasztható el, az egyenlőség is végpont.
        first_crossing = (
            not self._normal_discharge_cutoff_seen and self._load.input_commanded_on
        )
        self._normal_discharge_cutoff_seen |= self._u_batt <= self._profile.terminate_voltage_pack_V
        if not self._safety.enforce_normal_discharge(
            self._u_batt, self._config.system_rate_current_A, self._profile.terminate_voltage_pack_V,
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
        self._record_sample(dt_s, current_A=-abs(i_load))
        self._last_integration_source = "LOAD_READBACK"
        self._discharge_phase_elapsed_s += dt_s

        if self._discharge_phase_elapsed_s > self._safety.max_discharge_time_s:
            self.emergency_stop("MAX_DISCHARGE_TIME_REACHED")
            return
        # Teljes kódbázis review 13. kör (2026-08-31), Szoftver/Architektúra
        # P2 lelet — l. discharge_controller.py azonos fixjének docstringje.
        # Itt a kisütési áram (system_rate_current_A) explicit, C-rátától
        # független bemenet (SUP03-GG-007), tehát nincs közvetlen "áram-
        # bázis vs. plafon-bázis" ellentmondás — de a plafon-kapacitás maga
        # a projekt máshol mindenütt preferált, konzervatívabb C10-bázisra
        # vált a konzisztencia és a BAT-002 lezárása utáni helyes viselkedés
        # érdekében.
        if self._energy_integrator.accumulated_discharge_Ah > (
            self._profile._c10_base_Ah * self._safety.max_discharge_Ah_factor
        ):
            self.emergency_stop("MAX_DISCHARGE_AH_REACHED")
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
        # current_setpoint() docstringje. MAX verifier N1 lelete: a
        # tick-számláló a _last_warning_code slot-tól FÜGGETLENÜL nő.
        setpoint_result = self._safety.check_load_current_setpoint(
            abs(i_load), self._config.system_rate_current_A
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

        if self._u_batt <= self._profile.terminate_voltage_pack_V:
            try:
                self._load.input_off()
            except Exception as exc:
                # [ugyanaz az elv, mint TraditionalLearningController/
                # ChemIdCharacterizationController]: a load.input_off() hibáját
                # nem szabad csendben elnyelni, mert a relax 0A-feltételezését
                # érvénytelenítené.
                self.emergency_stop(f"SYSTEM_RATE_DISCHARGE_LOAD_OFF_FAILED: {exc}")
                return
            self._enter(GoldenGgState.RELAX_5H)

    # ------------------------------------------------------------------ #
    # EXPORT — SUP03-GG-009/010 (kötelező export, nem hagyható csendben ki)#
    # ------------------------------------------------------------------ #

    def _run_export(self) -> None:
        if self._export_output_dir is not None and self._export_config is not None:
            assert self._gg_source_snapshot is not None
            try:
                export_config = self._export_config
                missing_metadata_fields = list(self._missing_metadata_fields)
                if self._ambient_temperature_C is not None:
                    # Governance-v2 review-kör 1 (CF-31, DFAM-003): a
                    # chemid_controller.py::_run_export() mintáját követve
                    # (SUP03-CHEMID-012) -- a konditálás alatt rögzített,
                    # ténylegesen mért szobahőmérséklet pontosabb, mint a
                    # konstrukciókori (feltételezett) GoldenGgConfig.
                    # room_temperature_C alapérték. Korábban ez a controller
                    # sosem alkalmazta a mért értéket -- a config.txt mindig
                    # a 20.00C alapértéket exportálta, akkor is, ha a
                    # conditioning fázisban más hőmérséklet lett mérve.
                    export_config = dataclass_replace(
                        export_config, room_temperature_C=self._ambient_temperature_C
                    )
                    # CF-31 MAX verifier P2-2 lelete (2026-09-08): a
                    # konditálás alatt mért érték valójában az akku-felületi
                    # hőmérséklet-DMM (self._battery_temperature_C, l.
                    # _run_conditioning()), NEM önálló szoba-/ambiens
                    # hőmérséklet-szenzor -- l. charge_controller.py::
                    # ChargeConfig.ambient_temperature_source "INITIAL_
                    # BATTERY_APPROX" fogalma (ott deklarálva, de "ebben a
                    # körben NEM implementálva"). A projekt saját kanonikus
                    # állásfoglalása (CLAIM_LEDGER.yaml BAT-CHG-001) szerint
                    # egy nem-mért ambiens értéket nem szabad jelöletlenül
                    # teljes megfelelésként beküldeni -- ezért a helyettesítés
                    # MEGMARAD (2h+ nyugalmi akklimatizáció után védhető
                    # közelítés), de a provenance-t explicit limitációként
                    # rögzítjük.
                    missing_metadata_fields.append(
                        "RoomTemperatureC az akku-felületi hőmérséklet-DMM "
                        "konditálás végi méréséből származik, NEM önálló "
                        "szoba-/ambiens hőmérséklet-szenzorból (INITIAL_"
                        "BATTERY_APPROX jellegű közelítés)."
                    )
                else:
                    # CF-31 MAX verifier P2-1 lelete (2026-09-08): a fenti
                    # helyettesítés a produkciós útvonalon (GoldenGgPhysical
                    # Config.conditioning_required alapértéke False, l.
                    # main_window.py::_make_golden_gg_ctrl() -- sosem állítja
                    # True-ra) SOSEM fut le, mert a CONDITIONING állapot ki
                    # van hagyva és self._ambient_temperature_C None marad.
                    # A config.txt emiatt MINDEN mai GOLDEN_GG session-nél a
                    # GoldenGgConfig konstrukciókori alapértékét (20.00C)
                    # exportálja -- ezt korábban semmi nem jelezte. Új
                    # konfigurációs szabadságot (CLAUDE.md #10) NEM adunk;
                    # a meglévő limitáció-mechanizmust használjuk, hogy a
                    # hiány LÁTHATÓ legyen a TI submissionban, nem csendes.
                    # 2. MAX verifier P3-2 lelete (2026-09-08): a szöveg NE
                    # tartalmazzon "RoomTemperatureC=" tokent -- a config.txt
                    # egy dokumentáltan validálatlan (SUP03-GG-011), kulcs=
                    # érték formátumú TI-fájl, és egy második "kulcs=érték"
                    # alakú token egy komment-sorban félreértést okozhatna.
                    missing_metadata_fields.append(
                        f"A RoomTemperatureC mező a GoldenGgConfig "
                        f"konstrukciókori (feltételezett) "
                        f"{export_config.room_temperature_C:.2f} °C "
                        f"alapértékét exportálja -- NEM mért érték, mert a "
                        f"conditioning fázis nem futott le "
                        f"(conditioning_required=False)."
                    )
                if self._relax_dmm_fault_total > 0:
                    # Teljes kódbázis review 3. kör (2026-08-29), Metrológia/
                    # Adatintegritás P2 lelet — l. __init__ _relax_dmm_fault_
                    # total docstringje.
                    # [Kör 19 Batch D3b, F-2] a szöveg korábban "Hőmérséklet-/
                    # feszültség-DMM"-ként hivatkozott erre a limitációra, de
                    # `_relax_dmm_fault_total` KIZÁRÓLAG feszültség-DMM
                    # (`relax_controller.py::read_voltage()`) hibákat számol
                    # -- a hőmérséklet-DMM külön számlálóval (l. lent) és
                    # külön limitációval jár, NEM ezzel.
                    missing_metadata_fields.append(
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
                    # export.py/golden_gg_export.py `cell_surface_temperature_C`
                    # oszlopa emiatt stale (nem friss mérésből származó)
                    # pontot tartalmazhat, jelzés nélkül.
                    missing_metadata_fields.append(
                        f"Hőmérséklet-DMM kiesés ({self.temp_dmm_fault_ticks} "
                        f"érintett tick a session során) — a "
                        f"cell_surface_temperature_C oszlop stale (nem friss "
                        f"mérésből származó) pontot tartalmazhat."
                    )
                if not self._temp_dmm_ever_read_ok:
                    # [Kör 19 Batch D3c, F-3 rész (a)] a `_battery_
                    # temperature_C` konstrukciókori alapértéke (20.0°C) --
                    # ha a hőmérséklet-DMM a session ELEJÉTŐL kezdve SOHA
                    # nem olvasott sikeresen (pl. TempCompMode.OFF + halott
                    # DMM esetén a safety.py::check_temperature_dmm_fault()
                    # nem ad FAULT-ot/WARNING-ot), a fenti `temp_dmm_fault_
                    # ticks > 0` kapu is CSENDBEN marad, mert a hiba SOHA
                    # nem "tick"-ként (dt_s-alapú számlálóként), hanem
                    # állandó, kezdettől fogva fennálló állapotként
                    # jelentkezik. Ez a KÜLÖN, kizáró ág explicit jelzi,
                    # hogy a cell_surface_temperature_C oszlop MINDEN sora
                    # a fabrikált alapértéket hordozza, nem mérést.
                    missing_metadata_fields.append(
                        "A hőmérséklet-DMM a session teljes időtartama alatt "
                        "SOHA nem olvasott sikeresen — a "
                        "cell_surface_temperature_C oszlop minden sora a "
                        "konstrukciókori (nem mért) 20.00°C alapértéket "
                        "tartalmazza."
                    )
                zip_path = build_golden_gg_submission_package(
                    self._export_output_dir,
                    export_config,
                    self._samples,
                    self._gg_source_snapshot,
                    self._source_session_reference,
                    self._profile.cell_count,
                    missing_metadata_fields=missing_metadata_fields,
                )
                self.export_dir = str(self._export_output_dir)
                self.export_zip_path = str(zip_path)
                self.package_ready_for_ti_submission = True
            except Exception as exc:
                self.emergency_stop(f"GOLDEN_GG_EXPORT_FAILED: {exc}")
                return

        # [SCF-011 újratervezés, 2026-09-11, felhasználói döntés] a session
        # egyetlen sikeres (DONE) záró útja -- l. _ensure_bq_sealed_or_flag()
        # docstringje. A sibling terminális út (FAULT) az emergency_stop()-
        # ban van lefedve.
        self._ensure_bq_sealed_or_flag("EXPORT_PACKAGE")
        self._enter(GoldenGgState.DONE)
        # Governance-v2 review-kör 1 (CF-32/F8, DFAM-018): korábban ez az
        # esemény FELTÉTEL NÉLKÜL kiment, akkor is, ha export_output_dir/
        # export_config hiányában a fenti blokk teljesen kihagyta a
        # csomagépítést — hamis "beküldhető" szemantika. Mostantól csak
        # akkor, ha a csomag TÉNYLEGESEN elkészült.
        if self.package_ready_for_ti_submission:
            self._emit({
                "event_code": "GOLDEN_GG_PACKAGE_READY",
                "event_message": (
                    "Golden GG fizikai mérési ciklus kész — a csomag a TI Golden GG "
                    "Maker eszköznek beküldhető. Az optimalizálási eredmény (Qmax/Ra/"
                    "thermal parameters) importja/nyomon-követése külön, e ciklustól "
                    "független lépés (l. golden_gg_export.import_ti_gg_output())."
                ),
            })
        else:
            self._emit({
                "event_code": "GOLDEN_GG_PACKAGE_NOT_BUILT",
                "event_message": (
                    "Golden GG fizikai mérési ciklus kész, de export_output_dir/"
                    "export_config hiányában NEM készült TI-beküldhető csomag. "
                    "A mért adatok (samples) elérhetők, a csomag elkészítése "
                    "külön lépést igényel."
                ),
            })

    # ------------------------------------------------------------------ #
    # Napló — SUP03-GG-005/006/008                                        #
    # ------------------------------------------------------------------ #

    def _record_sample(
        self,
        dt_s: float,
        current_A: float,
        energy_charge_delta: tuple[float, float] | None = None,
    ) -> None:
        """`energy_charge_delta` (R1 B8a, F-20): ha megadva, a session-
        szintű Ah/Wh egy MÁSIK integrátor (a beágyazott ChargeController)
        által EZEN a ticken már elintegrált (Ah, Wh) töltési deltáját veszi
        át, `current_A`-t NEM integrálja újra; a minta (export) és az
        időtengely/throttle ettől függetlenül `dt_s`-t és `current_A`-t
        kapja. None (alapértelmezés, minden nem-töltési hívó): a régi
        add_sample(current_A, u_batt, dt_s) út, változatlanul."""
        self._log_elapsed_s += dt_s
        self._samples.append(GoldenGgSample(
            elapsed_s=round(self._log_elapsed_s, 3),
            voltage_V=self._u_batt,
            current_A=current_A,
            temperature_C=self._battery_temperature_C,
        ))
        # Release-audit 2026-08-23 (2.7) — l. accumulated_charge_Ah/
        # accumulated_discharge_Ah property docstringje.
        if energy_charge_delta is None:
            self._energy_integrator.add_sample(current_A, self._u_batt, dt_s)
        else:
            self._energy_integrator.add_charge_delta(
                energy_charge_delta[0], energy_charge_delta[1], dt_s
            )

        # Feature (2026-08-23): a BQ Diagnosztika fül élő nézete — a session
        # config.sample_interval_s kadenciájával lekérdezzük a gauge-ot, és
        # (ha van feliratkozó) továbbadjuk a friss snapshotot. Kivétel esetén
        # a poll_bq_diagnostics mezőnként minősíti a transporthibákat;
        # a vezérlési abort továbbterjed, l. bq_diagnostics.py.
        #
        # Review 2026-09-15 (C-01/DFAM-027, P0, RECURRING) -- l. __init__
        # `_bq_poll_extra_s_to_exclude_from_throttle` docstring: csak a dt_s
        # azon része kerül a küszöb-akkumulátorba, ami NEM a legutóbbi poll
        # saját ideje volt (önerősítő hurok elkerülése).
        # R16-B10F4: megszakadt nyugta utan az OFF elott nincs uj BQ-I/O.
        # A mar konyvelt mintat es energiat megorizzuk; az OFF a runnere.
        if (
            isinstance(self.handoff_stop_requested, ActivationStopHook)
            and self.handoff_stop_requested.activation_interrupted
        ):
            return

        effective_dt_s = max(dt_s - self._bq_poll_extra_s_to_exclude_from_throttle, 0.0)
        self._bq_poll_extra_s_to_exclude_from_throttle = 0.0
        self._gauge_poll_elapsed_s += effective_dt_s
        if self._gauge_poll_elapsed_s >= self._config.sample_interval_s:
            self._gauge_poll_elapsed_s = 0.0
            # HIL-D5 escalation-küszöb (2026-09-14, l. _check_bq_transport_
            # escalation() docstringje): a poll mostantól FELTÉTEL NÉLKÜL
            # lefut minden sample_interval_s-nél, még akkor is, ha
            # on_gauge_sample nincs feliratkoztatva -- korábban a poll
            # MAGA is az on_gauge_sample-feltétel ALATT volt, tehát egy
            # feliratkozó nélküli (pl. headless/teszt) konstrukció sosem
            # pollozott. A traditional_learning_controller.py testvér
            # _record_gauge_sample()-je ezt a mintát MÁR eddig is
            # követte (ott a poll sosem volt on_gauge_sample-hez kötve) --
            # ez a mostani változás a GG-t hozza szinkronba azzal, NEM egy
            # önmagában vett, e feature-től független viselkedésváltozás.
            poll_start_t = time.perf_counter()
            try:
                snapshot = self._poll_bq_diagnostics_throttled()
            finally:
                # MAX verifier V-7 lelete (2026-09-15) -- l.
                # traditional_learning_controller.py sibling kommentje.
                self._bq_poll_extra_s_to_exclude_from_throttle = time.perf_counter() - poll_start_t
            if self.on_gauge_sample is not None:
                self.on_gauge_sample(snapshot)
            self._check_bq_transport_escalation(snapshot)

    def _check_bq_transport_escalation(self, snapshot: BqDiagnosticsSnapshot) -> None:
        """HIL-D5 (2026-09-14, CF-10a/DFAM-027, CLAIM_LEDGER.yaml PICO-
        RECONNECT-001) -- l. traditional_learning_controller.py azonos
        nevű metódusának docstringje ugyanerre a mintára (sibling-
        konzisztens escalation-küszöb, NEM backoff-időzítő, mert egy valós
        Pico-kiesés HIL-mérése szerint önmagától sosem gyógyul).

        KÜLÖNBSÉG a TLC-testvérhez képest (MAX verifier 2026-09-14 F-2
        megjegyzés nyomán ellenőrizve): a GoldenGgController végső
        eredmény-klasszifikációja NEM olvassa a `self._quality_flags`-et
        egy `not self._quality_flags`-szerű kapuban (nincs ilyen mintázat
        ebben a fájlban) -- ez az esemény itt TISZTÁN diagnosztikai/
        operátori jelzés, nem befolyásolja automatikusan a GG session
        végeredményét, szemben a TLC-vel, ahol a `LEARNING_RESULT_
        VERIFIED`-et explicit lefokozza `LEARNING_RESULT_AWAITING`-re."""
        if is_snapshot_fully_invalid(snapshot):
            self._consecutive_fully_invalid_bq_polls += 1
        else:
            self._consecutive_fully_invalid_bq_polls = 0
            self._bq_transport_unreachable_flagged = False
            return

        if (
            self._consecutive_fully_invalid_bq_polls >= self._config.bq_transport_unreachable_after_n_polls
            and not self._bq_transport_unreachable_flagged
        ):
            self._bq_transport_unreachable_flagged = True
            self._quality_flags.append(
                f"BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE_AT_{self._state.value}"
                f"(consecutive_polls={self._consecutive_fully_invalid_bq_polls})"
            )
            self._emit({
                "event_code": "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE",
                "event_message": (
                    f"{self._consecutive_fully_invalid_bq_polls} egymást követő BQ-diagnosztikai "
                    f"poll TELJESEN érvénytelen ({self._state.value} fázis) — a BQ-diagnosztika "
                    f"egyetlen mezője sem olvasható. Lehetséges ok: a Pico soros kapcsolat, a "
                    f"Pico-oldali RPC-szerviz, VAGY a BQ/I2C busz elérhetetlen (a konkrét ok innen "
                    f"nem különböztethető meg). A fizikai ciklus folytatódik, de a BQ-learning "
                    f"eredmény ettől a ponttól kétséges lehet, amíg a kapcsolat helyre nem áll."
                ),
                "severity": "CRITICAL",
            })

    def _emit_gauge_sample_best_effort(self, snapshot: BqDiagnosticsSnapshot) -> None:
        """Kor 18, Batch C7 R17-16 (DFAM-021+DFAM-001): a DONTO (verdikt-
        kritikus) snapshotok -- PRECOND gg-forras / TraditionalLearning-
        Controller INITIAL_SNAPSHOT/FINAL_SNAPSHOT -- korabban kizarolag a
        controller belso mezojebe kerultek: sem az elo BQ-diagnosztika
        nezetbe, sem a bq_diagnostics.csv-be nem jutottak el, tehat a
        learning-verdikt bizonyitéka nem volt rekonstrualhato a session-
        mappabol. A periodikus poll-ut (l. lent) MAR eddig is emittalt.

        BEST-EFFORT, SZANDEKOSAN guardolt -- a periodikus uttal ELLENTETBEN:
        ez idozites-erzekeny BQ-tranzakcios pontokon (unseal/IT_ENABLE/zaro
        seal kornyeken) fut, es egy GUI-callback kivetele SOHA nem
        szakithatja meg a learning-folyamatot, sem nem maszkolhatja az
        eredeti hibat (DFAM-033). A periodikus ag guard NELKUL marad -- az
        egy laza tick-hatar, nem tranzakcios pont; ez az aszimmetria
        TUDATOS, nem elnezes."""
        if self.on_gauge_sample is None:
            return
        try:
            self.on_gauge_sample(snapshot)
        except Exception:  # noqa: BLE001 - l. a docstring DFAM-033 indoklasat
            pass

    def _poll_bq_diagnostics_throttled(self) -> BqDiagnosticsSnapshot:
        """Governance-v2 review-kör 1 (CF-10a MAX verifier P1-1 sibling,
        DFAM-027, ceiling P1, RECURRING): a `poll_bq_diagnostics()` 19
        mezőt olvas egymástól függetlenül, mindegyiket a transport saját
        retry-logikájával (PicoBQTransport.max_retries alapból 3, soros
        timeout alapból 1.0s) — egy teljes Pico-kiesésnél ez akár
        19*3*1.0s ~= 57s szinkron blokkolást okozhat EGYETLEN advance()
        tickben, méghozzá AKTÍV energiaátadás közben (_run_full_charge()/
        _run_system_rate_discharge() hívja _record_sample()-t, l. fentebb
        589./676. sor — a kisütési hívás a tick SAJÁT safety-kapui,
        pl. max_discharge_time/Ah, concurrent PSU/Load, check_load_power,
        check_battery_voltage ELŐTT fut, tehát a blokkolás nemcsak a
        KÖVETKEZŐ tick vészleállítását, hanem az AKTUÁLIS tick kisütési
        kapuit is késlelteti), miközben a TestRunner az emergency_stop_
        requested flag-jét csak a tick TETEJÉN nézi. A traditional_
        learning_controller.py::_poll_bq_diagnostics_throttled()-dzsel
        AZONOS mintát követve (ugyanaz a DFAM-027 hibaosztály, ugyanaz a
        minimális fix): a poll idejére ideiglenesen 1-re csökkentjük a
        max_retries-t, majd garantáltan (try/finally) visszaállítjuk.

        CF-10a 2. MAX verifier P3-2 pontosítás (2026-09-08): ez a hívás
        NEM csak élő GUI-kijelzést táplál — a snapshot a session
        bq_diagnostics.csv-jébe is íródik (main_window.py::
        _on_bq_snapshot_ready() -> logger.log_bq_diagnostics()), tehát
        perzisztens mérési artefaktum. A max_retries=1 csökkentés emiatt
        megnövelheti a <mező>_valid=False arányt egy zajos (de működő)
        kapcsolat mellett a CSV-ben. Ez ELFOGADHATÓ, mert a BqFieldReading.
        valid/error mező EXPLICIT jelöli a degradált mintát (CLAUDE.md §7:
        nincs csendes hamis-érték-helyettesítés) — nem elveszett adat,
        csak alacsonyabb sikerességi arányú, önmagát minősítő adat.

        SZÁNDÉKOSAN NEM érinti a PRECOND-beli, egyszeri `gg_snapshot =
        poll_bq_diagnostics(...)` hívást (l. fent): az a Qmax/Ra table
        érvényességét kapuzza (emergency_stop, ha hiányos) — a TÉNYLEGES
        TI Golden GG Maker csomag forrásadata, nem csupán diagnosztikai
        kijelzés, ezért ott a retry-robusztusság megtartása indokolt."""
        original_max_retries = getattr(self._bq_transport, "max_retries", None)
        if original_max_retries is not None:
            self._bq_transport.max_retries = 1
        try:
            return poll_bq_diagnostics(self._bq_driver)
        finally:
            if original_max_retries is not None:
                self._bq_transport.max_retries = original_max_retries

    # ------------------------------------------------------------------ #
    def _enter(self, state: GoldenGgState) -> None:
        """CF-19 (governance-v2 kör 2, DFAM-023) -- korábban ez a helper
        NEM reseteltte a phase_elapsed_s-t (degenerált átmenet-hook volt).
        L. ocv_soc_controller.py::_enter() azonos hivatkozású docstringje."""
        self._state = state
        self._phase_elapsed_s = 0.0

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

    def _read_temperature_only(self, dt_s: float) -> None:
        """Teljes kódbázis review 7. kör (2026-08-30), Szoftver/Architektúra
        P2 lelet #2: relax-alállapotokban (_RELAX_STATES) a feszültség-DMM-et
        a beágyazott RelaxController olvassa (_run_relax()) — korábban
        advance() ITT IS lefuttatta a teljes _read_dmm()-et (feszültség+
        hőmérséklet), 2 fizikai feszültség-olvasást okozva egy ticken belül
        (CLAUDE.md §9), és a naplózott self._u_batt/self._dmm_valid a MÁSODIK
        (RelaxController) olvasásból, NEM az ITT történt elsőből származott
        — tick-en belüli valid/stale desync-kockázat. Ez a metódus CSAK a
        hőmérséklet-DMM-et olvassa, hogy a temp-fault figyelés relax alatt
        is működjön, a feszültség-DMM-et pedig egyáltalán nem érinti."""
        try:
            self._battery_temperature_C = self._dmm_t.read_temperature()
            self._safety.record_temperature_dmm_read(valid=True, dt_s=dt_s)
            self._temp_dmm_valid = True
            self._temp_dmm_ever_read_ok = True
        except Exception:
            self._safety.record_temperature_dmm_read(valid=False, dt_s=dt_s)
            self._temp_dmm_fault_ticks += 1
            self._temp_dmm_valid = False
