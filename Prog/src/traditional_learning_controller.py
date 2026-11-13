"""
traditional_learning_controller.py — WP-AK-10: BQ34Z110 traditional learning
cycle, PC-vezérelt (PC = workflow/safety master), SUP03-LEARN-001..010.

ARCHITEKTÚRÁLIS DÖNTÉS (felhasználói jóváhagyással, 2026-08-22): a fizikai
töltést/kisütést az Akkuteszter SAJÁT Keithley PSU/Load laborműszerei
végzik (ChargeController-en keresztül + ebben a modulban inline kisütés-
logikával, a `ChemIdCharacterizationController` C/20 mintáját követve),
NEM a TF1 panel saját ATmega-vezérelt "BQ Learning Wizard"-ja
(Prog/TF1_basic.c:5241-5416, `bq_learn_tick()`) — az utóbbi a PANEL SAJÁT
PSU/BAT relé-jét használná, ami egy MÁSIK fizikai felépítés (deployed UPS
panel saját akkuja), nem a laborpadi karakterizációs bench. A BQ34Z110
állapotát a golden-image Pico RPC rétegen (`PicoBQTransport`) keresztül
olvassuk/írjuk — UGYANAZ az architektúra, mint a WP-AK-06..09.

**Két-cikluses szekvencia, forrás: SUP03-LEARN-004** (kiegeszito_szabalyok_
03_golden_image_teljes_meresi_program_v1_0_20260723.txt 622-636. sor) — ez
a NORMATÍV forrás, nem a TF1_basic.c wizard. **Fontos, dokumentált
eltérés-megfigyelés (audit, nem javítás — a TF1_basic.c-t ez a WP nem
módosítja)**: a TF1_basic.c "BQ Learning Wizard"-ja (BQ_LEARN_PRECOND→
CHARGE→RELAX1→DISCHARGE→RELAX2→RECHARGE→VERIFY→DONE) csak EGY teljes
kisütési ciklust kódol RECHARGE után közvetlenül VERIFY-ba lép, míg a
SUP03-LEARN-004 explicit KÉT kisütési ciklust ír elő (lépés 4 ÉS lépés 9)
— ez összhangban van a jól dokumentált BQ34Z100 Impedance Track
követelménnyel (két független Qmax-frissítő esemény kell 0x05→0x06
átmenethez). Ez a modul a normatív SUP03-LEARN-004 KÉT-cikluses
szekvenciáját követi, forrás-elsőbbség alapján (CLAUDE.md forráshierarchia:
normatív követelményrendszer > jelenlegi implementáció).

**Relax-elfogadási kritérium (SUP03-LEARN-006)**: a RelaxController a
minimumidőt a runner tényleges dt_s-eiből kezeli; SERVICE módban a worker
monoton rekordbélyegeiből becsül ablakos OLS dV/dt-t.
A TLC csak a minimumidő után fogad el: sikeres friss DMM-olvasás, érvényes
OLS-becslés és abs(dV/dt) < dvdt_threshold_V_per_s, valamint BQ OCVTAKEN=1
kell. VOK=0 is szükséges, kivéve RELAX_PRE_DISCHARGE_2-ben (CF-06), ahol a
VOK értéke nem feltétel. NaN = nincs érvényes becslés, így a kapu zárva marad.
Ha nincs elfogadás, a minimumidő után max_relax_extra_wait_s extra-várakozás
következik, majd RELAX_ACCEPTANCE_NOT_CONFIRMED quality flaggel folytatódik
a ciklus; a végső klasszifikáció ezt figyelembe veszi. Q-21: a runner a
beágyazott relaxhoz is saját órájú, 1,5 s célütemű service-t köt be.
A BQ-poll után, döntéskor legfeljebb 4 s olvasáskezdet-kor engedhető meg.
A minimumidő után, extra-várakozásban is fut a worker; tényleges kilépés
csak a végső rekordok és a release-nyugta átvétele után lehetséges.
Service nélküli közvetlen használat: explicit LEGACY_TICK kompatibilitás.
Fizikai relax-HIL: HIL_PENDING.

**SUP03-LEARN-007 tilalom betartva**: az UpdateStatus 0x05/0x06 értékét
KIZÁRÓLAG a BQ34Z110 saját belső folyamata állíthatja — ez a modul SOHA
nem ír UpdateStatus-t, csak OLVASSA és NAPLÓZZA, és a VERIFY_* állapotok
egy sikertelen olvasást NEM alakítanak át hamis sikerré (l.
`_classify_result()`).
"""
from __future__ import annotations

import logging
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import Callable, Optional

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.battery_profile import BatteryProfile
from Prog.src.bq_consistency_gate import (
    ProvisioningConsistencyReport,
    ProvisioningGateDecision,
    evaluate_provisioning_consistency,
)
from Prog.src.bq_controlled_write import ControlledBqWriteError
from Prog.src.bq_data_flash_service import (
    FIELD_REGISTRY,
    Bq34z110DataFlashService,
    encode_df_field,
)
from Prog.src.bq_diagnostics import (
    BqDiagnosticsSnapshot,
    BqFieldReading,
    decode_control_status,
    decode_flags,
    format_bq_session_scale_context,
    is_snapshot_fully_invalid,
    poll_bq_diagnostics,
)
from Prog.src.bq_learning_verification import (
    UPDATE_STATUS_FIRST_QMAX_UPDATE,
    UPDATE_STATUS_FULL_LEARNING,
)
from Prog.src.charge_controller import (
    _PSU_HW_MAX_CHARGE_A,
    ChargeConfig,
    ChargeController,
    ChargeState,
)
from Prog.src.exceptions import (
    ActivationStopHook,
    TestAbortedByUser,
    check_activation_stop,
)
from Prog.src.integrator import Integrator
from Prog.src.relax_controller import (
    RelaxConfig,
    RelaxController,
    RelaxSample,
    RelaxState,
)
from Prog.src.safety import SafetyManager


class LearningState(Enum):
    INIT = "INIT"
    PRECOND = "PRECOND"
    PRECHARGE = "PRECHARGE"
    PRECHARGE_RELAX = "PRECHARGE_RELAX"
    INITIAL_SNAPSHOT = "INITIAL_SNAPSHOT"
    IT_ENABLE_STEP = "IT_ENABLE_STEP"
    VERIFY_VOK_QEN = "VERIFY_VOK_QEN"
    DISCHARGE_1 = "DISCHARGE_1"
    REBOUND_CHECK_1 = "REBOUND_CHECK_1"
    RELAX_1 = "RELAX_1"
    VERIFY_UPDATE_STATUS_05 = "VERIFY_UPDATE_STATUS_05"
    RECHARGE = "RECHARGE"
    RELAX_PRE_DISCHARGE_2 = "RELAX_PRE_DISCHARGE_2"
    DISCHARGE_2 = "DISCHARGE_2"
    REBOUND_CHECK_2 = "REBOUND_CHECK_2"
    RELAX_2 = "RELAX_2"
    VERIFY_UPDATE_STATUS_06 = "VERIFY_UPDATE_STATUS_06"
    FINAL_SNAPSHOT = "FINAL_SNAPSHOT"
    FINAL_RECHARGE = "FINAL_RECHARGE"
    DONE = "DONE"
    FAULT = "FAULT"


# SUP03-LEARN-010 klasszifikációk — szó szerint a normatív szöveg szerint.
LEARNING_RESULT_VERIFIED = "BQ_LEARNING_VERIFIED"
LEARNING_RESULT_AWAITING = "PHYSICAL_CYCLE_COMPLETED_AWAITING_BQ_VERIFICATION"
LEARNING_RESULT_FAILED = "BQ_LEARNING_FAILED"

_TERMINAL_STATES = (LearningState.DONE, LearningState.FAULT)

# PROJECT_POLICY GOV-029/Q-32/Q-34: kizárólag a TLC saját kisütési útjához.
LEARNING_HARD_FLOOR_V_PER_CELL = 1.70
LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL = (1.75, 1.73, 1.71, 1.70, 1.70)

# Q-39 PROJECT_POLICY: a BQ-kalibráció 3 retry/70 ms mintája (nem TI
# Update Status-előírás); tickek között várunk, blokkoló sleep nélkül.
_INITIAL_UPDATE_STATUS_MAX_ATTEMPTS = 4
_INITIAL_UPDATE_STATUS_RETRY_WAIT_S = 0.07


def _qmax_within_ti_range(value: int | None) -> bool:
    """R1 B4 (F-10(b)/NS-11): a kiolvasott Qmax a TI-tartományon belül van-e.

    Tartományforrás a FIELD_REGISTRY "qmax_cell0_initial_mAh" bejegyzése --
    ugyanaz a DF-szó (SLUSB55B Table 20: Qmax Cell 0, SC82/off0, I2,
    Min 0, Max 32767), amelyet a `Bq34z110ReadOnlyDriver.read_qmax()` olvas.
    Ez CSAK tartomány-, nem plauzibilitás-ellenőrzés (pl. 1 mAh tartományon
    belüli; l. Q-3). None és hiányzó határ -> False."""
    spec = FIELD_REGISTRY["qmax_cell0_initial_mAh"]
    lo, hi = spec.min_value, spec.max_value
    return value is not None and lo is not None and hi is not None and lo <= value <= hi


# Teljes kódbázis review 7. kör (2026-08-30), Szoftver/Architektúra P2
# lelet #2: relax-alállapotokban a feszültség-DMM-et a beágyazott
# RelaxController olvassa (_run_relax()) — advance() ezekre az
# állapotokra CSAK a hőmérséklet-DMM-et olvassa (_read_temperature_only()),
# hogy a temp-fault figyelés relax alatt is működjön duplikált
# feszültség-olvasás nélkül (CLAUDE.md §9).
_RELAX_STATES = (
    LearningState.PRECHARGE_RELAX, LearningState.RELAX_1,
    LearningState.RELAX_PRE_DISCHARGE_2, LearningState.RELAX_2,
)

# Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P2 lelet:
# runaway-backstop a DISCHARGE_1/DISCHARGE_2 fázisra — ugyanaz az érték/elv,
# mint a standalone DischargeController (discharge_controller.py) — a
# beágyazott fázis eddig KIZÁRÓLAG a feszültség-alapú (u_batt <=
# _discharge_cutoff_V) leállási feltételre támaszkodott, idő-/Ah-alapú
# felső korlát nélkül.
# Teljes kódbázis review 8. kör (2026-08-30), Szoftver/Architektúra P2
# lelet: a fenti értékpár korábban privát, hardkódolt modul-konstansként
# élt itt is — mostantól self._safety.max_discharge_time_s/
# max_discharge_Ah_factor a kanonikus forrás (l. safety.py), a standalone
# DischargeController mintáját követve (5-8. kör konszolidáció).


@dataclass
class LearningConfig:
    # SUP03-LEARN-006 referencia-időtartamok — az OcvSocController/
    # ChemIdCharacterizationController-rel KONZISZTENS FIAMM/lab
    # equilibrium relax konvenció (2h/5h), NEM a TF1_basic.c ~30 perces,
    # gyorsabb, mezei/production célú wizard-időzítése (tudatos döntés: ez
    # a "master" minőségi tanulási ciklus, nem gyors mezei kalibráció).
    relax_after_charge_s: float = 7200.0
    relax_after_discharge_s: float = 18000.0
    # TI SLUSB55B 18. o.: dV/dt < 4 µV/s; STEP 8 (30. o.): BQ-visszaigazolás.
    # Q-41 PROJECT_POLICY: 3,5 µV/s pakk-DMM OLS, cellaszámmal nem osztva.
    dvdt_threshold_V_per_s: float = 3.5e-6
    # Biztonsági plusz-várakozás, ha a min. idő letelt, de OCVTAKEN/VOK
    # még nem teljesül — analóg a TF1_basic.c BQ_LEARN_TIMEOUT_CYC
    # (per-fázis timeout) szerepéhez, de itt KÜLÖN a relax minimumtól.
    max_relax_extra_wait_s: float = 3600.0
    it_enable_settle_s: float = 5.0
    verify_settle_s: float = 5.0
    sample_interval_s: float = 30.0   # SUP03-LEARN-002 gauge log mintavétel
    # HIL-D5 (2026-09-14, CF-10a/DFAM-027, CLAIM_LEDGER.yaml PICO-RECONNECT-001):
    # a Pico-kiesés HIL-mérése kimutatta, hogy egy valós USB-kiesés (power-
    # cycle) SOSEM áll helyre magától a sample_interval_s ismételt
    # próbálkozásaival — nincs "elég hosszú N várakozás", ami segítene.
    # Emiatt a helyes minta NEM egy backoff/skip-időzítő, hanem egy
    # escalation-küszöb: k egymást követő TELJESEN érvénytelen poll után
    # explicit, operátor felé látható eseményt kell adni (a fizikai ciklus
    # emellett is folytatódik — ez csak jelzés, nem automatikus leállás).
    # k=3 PROJECT_POLICY döntés (nincs gyártói forrás rá): elég gyors, hogy
    # az operátor ne maradjon sokáig tudatlanul egy tartós kiesésről, de
    # egy egyetlen tranziens poll-hiba (pl. egyetlen zajos ciklus) ne
    # váltson ki hamis riasztást. PONTOSÍTÁS (MAX verifier V-8 lelete,
    # 2026-09-15, C-01 fix után): a "~90s" becslés (k=3 × 30s) elavult --
    # egy tartósan elérhetetlen transportnál minden poll MAGA is a teljes
    # throttle-ölt retry-budgetet felhasználja (l. _poll_bq_diagnostics_
    # throttled() docstring, ~19s worst-case), és a C-01 fix óta ez az idő
    # KIZÁRÓDIK a következő poll-ablak számításából -- a TÉNYLEGES
    # poll-távolság tartós kiesésnél kb. `sample_interval_s + poll_s`
    # (~49s), tehát k=3-hoz ~147s, nem ~90s.
    bq_transport_unreachable_after_n_polls: int = 3

    # TI SLUSB55B STEP 8: C/10, rebound-ellenőrzés és szükség szerinti retry.
    # A TI 6-cellás 10,0 V példája illusztratív, nem univerzális cutoff.
    # A számértékek PROJECT_POLICY: GOV-029, Q-32/Q-34; rebound-cél Q-8.
    rebound_settle_s: float = 300.0
    rebound_tolerance_V: float = 0.1
    max_rebound_retries: int = 3  # Legfeljebb 4; a negyedik is 1,70 V/cella.
    cell_bl_learning_temp_V_per_cell: float = 1.65  # Tranzakciós BL, változatlan.
    learning_discharge_floor_V_per_cell: float = 1.75
    learning_rebound_target_V_per_cell: float = 1.80

    def __post_init__(self) -> None:
        if self.dvdt_threshold_V_per_s <= 0:
            raise ValueError("dvdt_threshold_V_per_s > 0 kötelező")
        if self.rebound_settle_s < 0:
            raise ValueError("rebound_settle_s >= 0 kötelező")
        if self.rebound_tolerance_V < 0:
            raise ValueError("rebound_tolerance_V >= 0 kötelező")
        if type(self.max_rebound_retries) is not int or not 0 <= self.max_rebound_retries <= 4:
            raise ValueError("max_rebound_retries: 0..4 egész szám kötelező")
        if self.cell_bl_learning_temp_V_per_cell <= 0:
            raise ValueError("cell_bl_learning_temp_V_per_cell > 0 kötelező")
        if (
            not isfinite(self.learning_discharge_floor_V_per_cell)
            or self.learning_discharge_floor_V_per_cell < LEARNING_HARD_FLOOR_V_PER_CELL
            or self.learning_discharge_floor_V_per_cell != LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL[0]
        ):
            raise ValueError("Q-34: a kezdeti TLC-cutoff kötött 1,75 V/cella, clamp nélkül")
        if any(
            not isfinite(value) or value < LEARNING_HARD_FLOOR_V_PER_CELL
            for value in LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL
        ):
            raise ValueError("Q-34: érvénytelen TLC cél/retry-jelölt")
        if self.learning_rebound_target_V_per_cell <= 0:
            raise ValueError("learning_rebound_target_V_per_cell > 0 kötelező")


@dataclass
class LearningResultSummary:
    """SUP03-LEARN-008/009/010 kötelező mezői — a végső, bizonyíték-alapú
    klasszifikáció és a mögötte álló nyers adat."""

    result: str
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    # R1 B4 (F-11): az INITIAL_SNAPSHOT-kori, tehát IT_ENABLE ELŐTTI
    # UpdateStatus (None, ha nem volt snapshot vagy a mező nem olvasható).
    # Q-4: a kezdeti 0x06 erre a ciklusra kizárja a VERIFIED-besorolást.
    update_status_initial: int | None = None
    update_status_at_0x05_check: Optional[int] = None
    update_status_final: Optional[int] = None
    qmax_before: Optional[int] = None
    qmax_after_first_update: Optional[int] = None
    qmax_final: Optional[int] = None
    design_capacity_mAh: Optional[float] = None
    measured_capacity_Ah: Optional[float] = None
    discharge_1_capacity_Ah: Optional[float] = None
    discharge_2_capacity_Ah: Optional[float] = None
    rebound_retries_1: int = 0
    rebound_retries_2: int = 0
    qmax_change_percent: Optional[float] = None
    ra_before: tuple[float, ...] = field(default_factory=tuple)
    ra_final: tuple[float, ...] = field(default_factory=tuple)
    vok_qen_observed_after_it_enable: Optional[dict] = None
    # Block 3 (2026-08-25) — Cell BL Set Volt Threshold tranzakciós evidence.
    cell_bl_original_mV: int | None = None
    cell_bl_restore_confirmed: bool | None = None


class TraditionalLearningController:
    """BQ34Z110 traditional learning cycle állapotgépe — SUP03-LEARN-001..010."""

    def __init__(
        self,
        psu,
        load,
        dmm_voltage,
        dmm_temperature,
        bq_transport,
        profile: BatteryProfile,
        safety: SafetyManager,
        config: LearningConfig,
        expected_chem_id: Optional[int] = None,
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
        # 2026-08-23 (felhasználói review, 2. kör — traditional_learning OV
        # incidens): korábban a beágyazott ChargeController mindig egy bare
        # ChargeConfig()-ot kapott, figyelmen kívül hagyva a session/GUI
        # töltőáram-felülírását (cfg.charge_current_A_override) és taper-
        # beállítását — a TRADITIONAL_LEARNING így NEM ugyanazt a töltési
        # konfigurációt használta, mint a CHARGE_ONLY, holott ugyanazon a
        # fizikai áramúton (TF1 panelen keresztül) tölt. Ha a hívó nem ad
        # explicit charge_config-ot, a régi, bare ChargeConfig() marad az
        # alapértelmezés (visszafelé kompatibilis).
        self._charge_config = charge_config or ChargeConfig()
        # Q-32 D8/Q-38: a zaro aram nem a GUI CUSTOM-override forrasa.
        self._final_charge_config = replace(self._charge_config, charge_current_A_override=0.67)
        self.persist_tlc_completion: Callable[[dict], None] | None = None
        self._tlc_completion: dict = {
            "learning_evidence": None,
            "effective_charge_config": asdict(self._final_charge_config),
            "final_recharge": {
                "status": "NOT_STARTED", "reason": "",
                "started_at_utc": None, "completed_at_utc": None,
                "next_campaign_not_before_utc": None,
            },
        }

        self._state = LearningState.INIT
        self._fault_reason: str = ""
        self._phase_elapsed_s: float = 0.0
        # Review 2026-09-15 (C-09/DFAM-025, 2. előfordulás az S5-01 fix után):
        # a runner KÍVÜLRŐL kezdeményezett abortja (operátori vészleállítás,
        # runner-szintű kivétel, kritikus SCPI-hiba) NEM bizonyítja a BQ-tanulás
        # kudarcát (CLAUDE.md §2/§7) -- ezt a mezőt test_runner.py::
        # _abort_active_controller() állítja be, MIELŐTT az emergency_stop()-ot
        # hívja, hogy _classify_result() különbséget tudjon tenni a controller
        # SAJÁT safety-hibája és egy runner-eredetű megszakítás között.
        self.runner_abort_origin: Optional[str] = None

        self._charge_ctrl: Optional[ChargeController] = None
        self._relax_ctrl: Optional[RelaxController] = None
        self.prepare_relax_sampling: Callable[[RelaxController], None] | None = None
        # A runner élő leállításjelzése; az okfeloldás és az OFF a runner feladata.
        self.handoff_stop_requested: Callable[[], bool] | None = None
        self._relax_exit_pending = False
        self._relax_acceptance_reason: str | None = None
        self._relax_gate_mismatch: str | None = None
        self._relax_missed_deadline_total = 0
        self._relax_extra_wait_s: float = 0.0
        self._it_enable_sent: bool = False
        # Release-audit 2026-08-23 (2.7): a PRECHARGE lépés befejezésekor
        # elveszne a ChargeController saját accumulated_charge_Ah-ja, mielőtt
        # a RECHARGE lépés egy ÚJ ChargeController-t hozna létre — ez a
        # futó összeg őrzi meg a korábbi (lezárt) töltési fázisok Ah-ját
        # (l. accumulated_charge_Ah property és _run_charge() CHARGE_DONE ág).
        self._charge_total_accumulated_Ah: float = 0.0
        # Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        # + Metrológia P2 lelet: a fenti _charge_total_accumulated_Ah Wh-párja
        # — l. accumulated_charge_Wh property.
        self._charge_total_accumulated_Wh: float = 0.0
        # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
        # Adatintegritás P3 lelet: a fenti Ah/Wh carry-over mintáját
        # követve — a 3. kör extreme_dt_s_count/max_observed_dt_s
        # property-je korábban SZÁNDÉKOSAN nem vitte át a lezáruló
        # ChargeController-fázis (PRECHARGE) extrém dt_s-adatait a
        # RECHARGE fázisba ("tisztán diagnosztikai számláló, nem
        # session-szintű összeg" — akkor még helyes feltételezés). A 9.
        # kör (`6e0c68f`) óta viszont a main_window.py::_write_report()
        # capacity_result_quality gate-je session-szintű MINŐSÍTÉSI
        # döntésként kezeli ezt a mezőt — egy dt_s-tüske a PRECHARGE
        # fázis alatt (ami sikeresen lezárul) elveszne, ha a RECHARGE-
        # fázis saját (esetleg tiszta) extreme_dt_s_count-ja fedi el a
        # végső TestResult-ban.
        self._charge_total_extreme_dt_s_count: int = 0
        self._charge_total_max_observed_dt_s: float = 0.0
        # Teljes kódbázis review 12. kör (2026-08-31), Szoftver/Architektúra
        # P2 lelet: a GoldenGgController/ChemIdCharacterizationController MÁR
        # gyűjti a RELAX-fázisok (itt: RELAX_1/RELAX_2H) alatti feszültség-
        # DMM-olvasási hibák session-szintű összegét egy relax_dmm_fault_total
        # property-n keresztül (l. golden_gg_controller.py azonos mintájú
        # docstringje) — ez a controller ugyanazt a beágyazott RelaxController-t
        # használja, de a hibát korábban KIZÁRÓLAG a szöveges quality_flags
        # bejegyzésbe írta, numerikus session-összeg nélkül. A
        # main_window.py::_write_report() capacity_result_quality DEGRADED-
        # kapuja emiatt nem látta ezt a jelzést erről a controllerről.
        self._relax_dmm_fault_total: int = 0
        # Teljes kódbázis review 13. kör (2026-08-31), Metrológia/
        # Adatintegritás P2 lelet: ugyanaz a carry-over minta, mint fent a
        # _charge_total_extreme_dt_s_count-nál, csak a RelaxController
        # saját (10. kör, 6eb395b) extreme_dt_s_count/max_observed_dt_s
        # property-jéhez — l. _run_relax() docstring-kommentje.
        self._relax_total_extreme_dt_s_count: int = 0
        self._relax_total_max_observed_dt_s: float = 0.0
        self._discharge_integrator = Integrator()
        self._last_integration_source: str = "ZERO"  # R6-B3, l. last_integration_source
        # Teljes kódbázis review 12. kör (2026-08-31), Metrológia/
        # Adatintegritás P2 lelet: ugyanaz a carry-over minta, mint a fenti
        # _charge_total_extreme_dt_s_count-nál (l. ottani docstring, fb4e031/
        # 11. kör), csak a kisütési oldalon — a DISCHARGE_1 lezárásakor a
        # _discharge_integrator ÚJ Integrator()-ra cserélődik (l.
        # _begin_discharge(DISCHARGE_2)), a DISCHARGE_1 extrém dt_s-adata
        # enélkül elveszne, holott a DISCHARGE_1/2 pontosan a mért kapacitás
        # meghatározó fázisa (nem csak kondicionáló töltés).
        self._discharge_total_extreme_dt_s_count: int = 0
        self._discharge_total_max_observed_dt_s: float = 0.0
        self._discharge_current_A = profile.bq_learning_discharge_current_A
        self._discharge_1_capacity_Ah: Optional[float] = None
        self._discharge_1_capacity_Wh: float | None = None
        # Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P2
        # lelet: a jelenlegi DISCHARGE_1/DISCHARGE_2 ciklus saját eltelt
        # ideje (nem a rebound-retry-k közti settle-várakozásokra is
        # újrahasznosított _phase_elapsed_s) — l. _run_discharge()
        # runaway-backstop ellenőrzése.
        self._discharge_phase_elapsed_s: float = 0.0
        # 2026-08-24 — rebound-kezelés (l. LearningConfig rebound_* mezők
        # docstringje): a dinamikus cutoff-küszöb és a próbaszámláló
        # DISCHARGE_1/DISCHARGE_2-önként külön (l. _begin_discharge()).
        self._discharge_cutoff_V: float = 0.0
        self._learning_cutoff_seen = False
        self._rebound_retry_count: int = 0
        self._rebound_retries_1: int = 0
        self._rebound_retries_2: int = 0

        self._u_batt: float = 0.0
        self._dmm_valid: bool = True
        self._battery_temperature_C: float = 20.0
        # Release-audit 2026-08-23 (2.12, TEMP-DMM-GAP-01 kiterjesztése): a
        # hőmérséklet-DMM kiesését korábban _read_dmm() csendben elnyelte,
        # FAULT-ot sosem váltott ki a relax/kisütés fázisokban (a PRECHARGE/
        # RECHARGE fázisokat a beágyazott ChargeController saját, azonos
        # mintájú védelme fedi).
        # Review-kör 3 (2026-09-09), friss Metrológia M3-01 (P1) + M3-02/
        # Architektúra F2 (P2, ugyanaz a lelet két domain-ágens által
        # függetlenül megtalálva -- a temp_dmm_fault_ticks/temp_dmm_valid
        # testvér-felület a golden_gg/chemid/iec_dc_resistance
        # controlleren már megvolt, a TraditionalLearningController volt
        # az egyetlen kimaradt): l. temp_dmm_valid/temp_dmm_fault_ticks
        # property docstringje.
        self._temp_dmm_valid: bool = True
        self._temp_dmm_fault_ticks: int = 0
        # MAX verifier (review-kör 4, HW4-02, 329c1a3) N1 lelete -- l.
        # discharge_controller.py azonos hivatkozású mező docstringje.
        self._load_current_setpoint_warning_ticks: int = 0
        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # l. discharge_controller.py::i_load_measured_A/load_current_
        # readback_valid property docstringje -- ugyanaz a tick-scoped
        # validity-flag minta, közös mind a DISCHARGE_1, mind a
        # DISCHARGE_2 fázisra.
        self._i_load_measured_A: float | None = None
        self._load_current_readback_valid: bool | None = None
        # Teljes kódbázis review 9. kör (2026-08-30), Szoftver/Architektúra
        # P2 lelet: a last_warning_code property korábban KIZÁRÓLAG a
        # beágyazott ChargeController-t proxyzta (PRECHARGE/RECHARGE alatt)
        # — a relax/kisütés fázisokban a saját szintű
        # check_temperature_dmm_fault() MONITOR_ONLY warning-ja ténylegesen
        # kiszámolódott (l. advance()), de sosem került tárolásra/kiolvasásra.
        self._last_warning_code: str = ""
        # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        # lelet — l. charge_controller.py azonos konstruktor-kommentje
        # ugyanerről a hiányzó session-szintű összesítésről. A "_total"
        # tag ugyanazt a carry-over mintát követi, mint
        # _charge_total_extreme_dt_s_count (a PRECHARGE ÉS a RECHARGE
        # fázis eldobott ChargeController-jének összege).
        self._temperature_out_of_range_count: int = 0
        self._charge_total_temperature_out_of_range_count: int = 0
        # Review-kör 3 (2026-09-09), MAX verifier (a306e7e764e3a717b) P2-1,
        # DFAM-001 további előfordulása -- l. temp_dmm_fault_ticks property
        # docstringje. Ugyanaz a 3-utas carry-over minta, mint
        # _charge_total_temperature_out_of_range_count-nál.
        self._charge_total_temp_dmm_fault_ticks: int = 0

        # Block 2 (2026-08-25) — PROFILE↔SESSION↔BQ Data Flash konzisztencia-gate
        # eredménye, PRECOND-nál rögzítve (l. _run_precond()).
        self._provisioning_consistency_report: ProvisioningConsistencyReport | None = None

        # Block 3 (2026-08-25) — Cell BL Set Volt Threshold tranzakciós
        # kezelés (TI SLUSB55B STEP 8, 2./10. lépés). A ténylegesen
        # kiolvasott EREDETI érték kerül ide (nem egy feltételezett "normál"
        # konstans) — l. _run_it_enable()/_restore_cell_bl_if_needed().
        self._cell_bl_original_mV: int | None = None
        self._cell_bl_restore_done: bool = False
        self._cell_bl_restore_confirmed: bool | None = None
        # Review-kör 7 (2026-09-15), C-12, MAX verifier F3 lelete -- l.
        # pending_bq_df_restore property docstringje.
        self._cell_bl_reseal_confirmed: bool = False
        # R17-08 (2026-09-16, Kor 17 MAX-effort review): l. GoldenGgController.
        # _bq_unseal_attempted / pending_bq_unsealed docstringje ugyanerről
        # a mintáról -- SZÁNDÉKOSAN az RPC ELŐTT vált True-ra.
        self._bq_unseal_attempted: bool = False

        self._initial_snapshot: Optional[BqDiagnosticsSnapshot] = None
        self._initial_update_status_attempts = 0
        self._initial_update_status_retry_at_s = 0.0
        self._final_snapshot: Optional[BqDiagnosticsSnapshot] = None
        self._update_status_at_0x05_check: Optional[int] = None
        self._update_status_final: Optional[int] = None
        self._qmax_before: Optional[int] = None
        self._qmax_after_first_update: Optional[int] = None
        self._qmax_final: Optional[int] = None
        self._ra_before: tuple[float, ...] = ()
        self._ra_final: tuple[float, ...] = ()
        self._vok_qen_observed: Optional[dict] = None
        self._quality_flags: list[str] = []
        self._gauge_log: list[dict] = []
        self._log_elapsed_s: float = 0.0
        # Review 2026-09-15 (C-01/DFAM-027, P0, RECURRING): a `_record_gauge_
        # sample()` dt_s-AKKUMULÁCIÓS kapuja korábban a throttle-ölt BQ-poll
        # SAJÁT, akár ~19s-os végrehajtási idejét IS a küszöb-akkumulátorba
        # (`_log_elapsed_s`) engedte (mert az a KÖVETKEZŐ tick `dt_s`-ében
        # jelenik meg, l. test_runner.py `_last_tick_t` kommentje — az I/O-
        # idő ONNAN kezdve helyesen BENNE van minden `dt_s`-ben). Mivel a
        # poll ideje (~19s) közel a `sample_interval_s`-hez (30s), ez a
        # felfúvódott `dt_s` MINDEN KÖVETKEZŐ ticken újra átlépte a
        # küszöböt -- önerősítő hurok (D10 HIL, 2026-09-15: ~54s/tick mért).
        # FONTOS: a fix KIZÁRÓLAG ezt a helyi throttle-akkumulátort érinti —
        # az Ah/Wh-integrátorokhoz/`_phase_elapsed_s`-hez átadott `dt_s`
        # VÁLTOZATLAN marad (azok továbbra is a teljes, valós időt
        # integrálják, a poll blokkolása alatt is folyó fizikai energia-
        # átadásnak megfelelően) — ez NEM a ChargeController `last_fast_
        # loop_extra_s`-mintája (ott a kizárt idő MÁR egy másik úton
        # integrálva volt; itt a poll ideje SOHA nincs integrálva sehol,
        # tehát kizárása a dt_s-ből Ah/Wh alul-integrálást okozna — l.
        # Metrológia M5-01, 2026-09-10, ugyanerről a hibaosztályról).
        self._bq_poll_extra_s_to_exclude_from_throttle: float = 0.0
        self._result_summary: Optional[LearningResultSummary] = None
        # HIL-D5 (2026-09-14, CF-10a/DFAM-027 escalation-küszöb, l.
        # _record_gauge_sample() docstring): egymást követő TELJESEN
        # érvénytelen BQ-poll számlálója + "már jelezve" flag, hogy a
        # BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE esemény ne ismétlődjön
        # minden további sample_interval_s-nél, amíg a transport tartósan
        # elérhetetlen marad.
        self._consecutive_fully_invalid_bq_polls: int = 0
        self._bq_transport_unreachable_flagged: bool = False

        self.on_event: Optional[Callable[[dict], None]] = None
        # Feature (2026-08-23, felhasználói kérés): a BQ Diagnosztika fül
        # élő, passzív nézete a session PicoBQTransport-ján keresztül — a
        # meglévő _record_gauge_sample() (SUP03-LEARN-002) MÁR periodikusan
        # lekérdezi a gauge-ot relax/kisütés közben; ez a callback teszi
        # elérhetővé a friss snapshotot a GUI-nak.
        self.on_gauge_sample: Optional[Callable[[BqDiagnosticsSnapshot], None]] = None
        # Block 6 (2026-08-25): a Block 2 PROFILE<->SESSION<->BQ konzisztencia-
        # gate eredménye PRECOND-nál egyszer keletkezik — ugyanaz a callback-
        # minta, mint on_gauge_sample (a GUI-nak QThread-en át, Qt signal-lel
        # kell eljutnia, nem közvetlen widget-hívással, l. main_window.py).
        self.on_provisioning_consistency: (
            Callable[[ProvisioningConsistencyReport], None] | None
        ) = None

    # ------------------------------------------------------------------ #
    # Nyilvános tulajdonságok                                             #
    # ------------------------------------------------------------------ #

    @property
    def tlc_completion(self) -> dict:
        """A toltes elotti BQ-bizonyitek es az utokezeles kulon, masolatban."""
        return deepcopy(self._tlc_completion)

    @property
    def state(self) -> LearningState:
        return self._state

    @property
    def phase_elapsed_s(self) -> float:
        """CF-19 -- l. ocv_soc_controller.py::phase_elapsed_s azonos
        hivatkozású docstringje. (Ez a controller MÁR rendelkezett
        `_phase_elapsed_s`/`_enter()` bookkeeginggel -- a CF-19 fix csak a
        növelést egységesítette `advance()`-ban, korábban csak 4/20
        handlerben történt.)"""
        return self._phase_elapsed_s

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def quality_flags(self) -> tuple[str, ...]:
        return tuple(self._quality_flags)

    @property
    def gauge_log(self) -> list[dict]:
        """[Review-kör 4, Architektúra F5, DFAM-026] session-élettartamú,
        IN-MEMORY lista -- production/teszt fogyasztó jelenleg nincs.
        A tényleges perzisztens gauge-diagnosztikai artefaktum az
        `on_gauge_sample` callback-en keresztül a GUI-oldali
        bq_diagnostics.csv-be íródik, NEM ez a property (l.
        _record_gauge_sample()/_poll_bq_diagnostics_throttled() kommentje)."""
        return list(self._gauge_log)

    @property
    def result_summary(self) -> Optional[LearningResultSummary]:
        return self._result_summary

    @property
    def pending_bq_df_restore(self) -> dict | None:
        """Review-kör 7 (2026-09-15), C-12 (DFAM-016 rokona, D10 OV
        incidens tovább-elemzés): a `_restore_cell_bl_if_needed()` egy
        IDEIGLENES Cell BL Set Volt Threshold-ot ír vissza a chipre a
        session VÉGÉN, de eddig SEMMILYEN perzisztens (checkpoint.json-
        beli) nyoma nem volt annak, hogy a chip EBBEN A PILLANATBAN
        ideiglenes learning-értéken áll ÉS unsealed -- ha a process a
        restore ELŐTT szakad meg (crash/kill/áramkimaradás), a
        checkpoint.json-ból induló resume/incomplete-session-felismerés
        (`session_recovery.py`) semmit nem tudott erről, a chip csendben
        maradt volna ideiglenes/unsealed állapotban.

        `None`, ha (a) a temp-write SOSEM történt meg (`_cell_bl_
        original_mV is None`, pl. korai PRECOND FAULT), VAGY (b) a
        restore MEGERŐSÍTETTEN sikeres volt (`_cell_bl_restore_confirmed
        is True`). Egyébként (a restore még nem futott, VAGY lefutott de
        sikertelen/kivétel volt) egy dict-et ad a maradvány leírásával --
        SZÁNDÉKOSAN `_cell_bl_restore_confirmed is True`-ra szűkít, NEM
        `_cell_bl_restore_done`-ra: az utóbbi a restore-kísérlet
        ELEJÉN (a tényleges írás ELŐTT) válik True-vá (l. `_restore_
        cell_bl_if_needed()`), tehát arra szűrve a rekord egy SIKERTELEN
        restore UTÁN is eltűnne — pont a legveszélyesebb esetben, amikor
        a figyelmeztetésre a legnagyobb szükség van.

        MAX verifier F3 lelete (2026-09-15): `bq_unsealed` korábban
        FELTÉTLENÜL `True` volt, holott egy SIKERTELEN DF-visszaírás
        (`write_report.passed is False`, NEM kivétel) UTÁN a `finally`-
        ág seal-lépése ATTÓL FÜGGETLENÜL lefut, és ha ez sikeres, a chip
        ténylegesen SEALED — a rekord ekkor tévesen `bq_unsealed: True`-t
        állított volna, ellentmondva a CLAUDE.md §7 "commanded state és
        readback külön fogalom" elvének. Most a ténylegesen megfigyelt
        `_cell_bl_reseal_confirmed` flagből származik.

        PONTOSÍTÁS (2026-09-16, Kör 17 MAX-effort review, R17-05,
        DFAM-032 5. előfordulás): a `_cell_bl_reseal_confirmed` flag
        KORÁBBAN a bare `seal(confirm=True)` RPC sikeréből lett True,
        egy kódkomment szerint azért, mert "a transport SAJÁT readback-
        ellenőrzésével tér vissza" -- ez egy a TF1 Pico firmware-re
        vonatkozó, forrás nélküli (`SOURCE_REQUIRED`) állítás volt,
        amit `pico_bq_transport.py::seal()` nem támogat (nincs Python-
        oldali readback). Most a flag az `_ensure_bq_sealed_or_flag()`
        helper TÉNYLEGES SS-bit (`ControlStatusBits.ss`) readback-
        megerősítéséből származik -- `True` csak akkor, ha a chip
        IGAZOLTAN SEALED (konzervatív alapértelmezés a bizonytalan
        esetben is: uncertain = unsealed-nek tekintve, CLAUDE.md §6
        "bizonytalan állapot is safety esemény")."""
        if self._cell_bl_original_mV is None:
            return None
        if self._cell_bl_restore_confirmed is True:
            return None
        return {
            "field": "cell_bl_set_volt_mV",
            "original_mV": self._cell_bl_original_mV,
            "restore_confirmed": self._cell_bl_restore_confirmed,
            "bq_unsealed": not self._cell_bl_reseal_confirmed,
        }

    @property
    def pending_bq_unsealed(self) -> dict | None:
        """R17-08 (2026-09-16, Kor 17 MAX-effort review, DFAM-016+DFAM-018):
        a `pending_bq_df_restore` (fent) KIZÁRÓLAG a temp-write UTÁNI
        ablakot fedi (`_cell_bl_original_mV is not None`) -- de a chip a
        PRECOND `unseal()`-je ÓTA, azaz JÓVAL a temp-write ELŐTT (PRECHARGE
        + PRECHARGE_RELAX + INITIAL_SNAPSHOT alatt is) már UNSEALED. Ha a
        process EBBEN a korábbi ablakban szakad meg (crash/kill/
        áramkimaradás), a `pending_bq_df_restore` `None`-t ad (nincs mit
        visszaírni), és eddig SEMMILYEN MÁSIK perzisztens csatorna nem
        jelezte, hogy a chip fizikailag unsealed.

        SZÁNDÉKOSAN KÜLÖN fogalom a `pending_bq_df_restore`-tól (DFAM-018
        semantic-collapse-avoidance) -- l. GoldenGgController.
        pending_bq_unsealed docstringje ugyanerről a mintáról. `None`, ha
        (a) unseal SOSEM lett megkísérelve (`_bq_unseal_attempted is
        False`, pl. korai PRECOND FAULT MIELŐTT az unseal-hívás megtörtént
        volna), VAGY (b) a Data Flash-maradvány csatorna AKTÍV
        (`pending_bq_df_restore is not None`, azaz temp-write megtörtént ÉS
        a restore nincs megerősítve) -- ilyenkor a `pending_bq_df_restore`
        a helyes csatorna (a saját `bq_unsealed` mezőjével), és a
        `MainWindow._review_incomplete_sessions_on_startup()` "BQ SEAL
        MARADVÁNY" sorának "Nyitott (vissza nem állított) Data Flash-érték-
        maradvány ezen az úton nincs" szövege HAMIS lenne, ha ez a property
        is jelezne egyszerre -- ez a feltétel biztosítja, hogy a két
        csatorna DISZJUNKT legyen. R1 F-17 (DFAM-018+DFAM-016): a tiltás
        korábban a temp-write TÉNYÉHEZ (`_cell_bl_original_mV is not None`)
        kötött -- egy MEGERŐSÍTETT restore után, igazolatlan reseal mellett
        így MINDKÉT property None volt, holott a chip UNSEALED maradhatott;
        ezt az esetet most a "CELL_BL_RESTORED_RESEAL_UNCONFIRMED" reason
        jelzi (temp-write nélkül: "PRECOND_DF_READ_REQUIRES_UNSEALED"),
        VAGY (c) a zárt állapot MEGFIGYELÉSSEL igazolt
        (`_cell_bl_reseal_confirmed is True`, l. `_ensure_bq_sealed_or_
        flag()` SS-bit readbackje -- nem az elküldött parancs; ez a
        feltétel csak R17-05 után érvényesíthető helyesen, mivel korábban
        ez a flag nem SS-bit-igazolt sikerből származott). Egyébként dict
        a maradvány leírásával."""
        if not self._bq_unseal_attempted:
            return None
        if self.pending_bq_df_restore is not None:
            return None
        if self._cell_bl_reseal_confirmed:
            return None
        return {
            "controller": "TRADITIONAL_LEARNING",
            "reason": (
                "CELL_BL_RESTORED_RESEAL_UNCONFIRMED"
                if self._cell_bl_original_mV is not None
                else "PRECOND_DF_READ_REQUIRES_UNSEALED"
            ),
            "bq_unsealed": True,
        }

    @property
    def provisioning_consistency_report(self) -> ProvisioningConsistencyReport | None:
        """Block 2 (2026-08-25): PROFILE↔SESSION↔BQ Data Flash konzisztencia-gate
        eredménye — riportba/GUI-ba íráshoz (l. Block 6)."""
        return self._provisioning_consistency_report

    @property
    def charge_config(self) -> ChargeConfig:
        """DATA-006 (2026-08-24): a beágyazott ChargeController tényleges
        session-konfigurációja — riportba/naplóba íráshoz (main_window.py
        _write_report())."""
        return self._charge_config

    @property
    def learning_config(self) -> LearningConfig:
        """DATA-006 (2026-08-24): l. charge_config docstringje."""
        return self._config

    @property
    def accumulated_charge_Ah(self) -> float:
        """Release-audit 2026-08-23 (2.7): a TestRunner._run_step() ezt a
        generikus getattr(controller, "accumulated_charge_Ah", 0.0) mintán
        keresztül olvassa — enélkül a mező hiánya miatt mindig 0.0-t adott
        (CHEMID-AH-GAP-01 mintázatának kiterjesztése). PRECHARGE + RECHARGE
        összege (a lezárt fázisok futó összegben, az aktuális fázis a live
        ChargeController-ből)."""
        live = self._charge_ctrl.accumulated_charge_Ah if self._charge_ctrl is not None else 0.0
        return self._charge_total_accumulated_Ah + live

    @property
    def accumulated_discharge_Ah(self) -> float:
        """DISCHARGE_1 (a _discharge_1_capacity_Ah-ban megőrzött snapshot,
        l. _begin_discharge()) + DISCHARGE_2 (élő _discharge_integrator)
        összege."""
        return (self._discharge_1_capacity_Ah or 0.0) + self._discharge_integrator.accumulated_discharge_Ah

    @property
    def i_load_measured_A(self) -> float | None:
        """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        l. discharge_controller.py::i_load_measured_A property docstringje.
        Közös mind a DISCHARGE_1, mind a DISCHARGE_2 fázisra."""
        return self._i_load_measured_A

    @property
    def load_current_readback_valid(self) -> bool | None:
        """L. i_load_measured_A docstringje."""
        return self._load_current_readback_valid

    @property
    def accumulated_charge_Wh(self) -> float:
        """Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        + Metrológia P2 lelet: az accumulated_charge_Ah Wh-párja, ugyanaz a
        PRECHARGE+RECHARGE összeg mintát követve."""
        live = self._charge_ctrl.accumulated_charge_Wh if self._charge_ctrl is not None else 0.0
        return self._charge_total_accumulated_Wh + live

    @property
    def accumulated_discharge_Wh(self) -> float:
        """L. accumulated_discharge_Ah docstringje ugyanerről a DISCHARGE_1+
        DISCHARGE_2 összeg mintáról."""
        return (self._discharge_1_capacity_Wh or 0.0) + self._discharge_integrator.accumulated_discharge_Wh

    @property
    def extreme_dt_s_count(self) -> int:
        """Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        P2 lelet — l. charge_controller.py azonos property docstringje.
        Összeg: a jelenleg élő _discharge_integrator + (ha épp aktív) a
        beágyazott ChargeController integrátora + a korábbi (már eldobott)
        PRECHARGE-fázis carry-over összege (l. _charge_total_extreme_dt_s_
        count docstringje az __init__-ben — 11. kör, session-szintű
        minőségi jelzővé vált a 9. kör capacity_result_quality kapuja
        óta, ezért mostantól ugyanazt az Ah/Wh carry-over mintát követi) +
        a korábbi (már eldobott) DISCHARGE_1-fázis carry-over összege (l.
        _discharge_total_extreme_dt_s_count docstringje az __init__-ben —
        12. kör, ugyanaz a minta a kisütési oldalon) + a beágyazott
        RelaxController élő értéke (CSAK amíg a self._relax_ctrl MÉG
        LÉTEZIK, azaz a RELAX_DONE-utáni BQ-elfogadási carry-over MÉG NEM
        történt meg) + a korábbi (már eldobott/lezárt) relax-fázisok
        carry-over összege (l. _relax_total_extreme_dt_s_count docstringje
        az __init__-ben — 13. kör).

        Governance-v2 review-kör 1 (CF-05, DFAM-008) javítás: korábban egy
        külön `_relax_done_fault_recorded` flag védte a duplikálás ellen,
        mert a RelaxController.extreme_dt_s_count RELAX_DONE UTÁN
        fagyott érték volt (l. relax_controller.py::advance() fix). Most,
        hogy a RelaxController RELAX_DONE után is folytatja a mérést, a
        carry-over pontosan a `self._relax_ctrl = None` pillanatában
        történik (l. _run_relax() `_finalize_relax_ctrl_totals()`), tehát
        `self._relax_ctrl is not None` ÖNMAGÁBAN elegendő és pontos guard
        — a külön flag feleslegessé (és a fix nélkül hibássá) vált."""
        charge_count = self._charge_ctrl.extreme_dt_s_count if self._charge_ctrl is not None else 0
        relax_count = (
            self._relax_ctrl.extreme_dt_s_count if self._relax_ctrl is not None else 0
        )
        return (
            self._discharge_integrator.extreme_dt_s_count
            + charge_count
            + relax_count
            + self._charge_total_extreme_dt_s_count
            + self._discharge_total_extreme_dt_s_count
            + self._relax_total_extreme_dt_s_count
        )

    @property
    def temperature_out_of_range_count(self) -> int:
        """Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow P2
        lelet — l. charge_controller.py azonos property docstringje.
        Összeg: a saját szintű (nem-PRECHARGE/RECHARGE) számláló + a
        jelenleg élő beágyazott ChargeController értéke (ha épp aktív) +
        a korábbi (már eldobott) PRECHARGE/RECHARGE-fázisok carry-over
        összege — ugyanaz a minta, mint extreme_dt_s_count-nál."""
        charge_count = (
            self._charge_ctrl.temperature_out_of_range_count
            if self._charge_ctrl is not None else 0
        )
        return (
            self._temperature_out_of_range_count
            + charge_count
            + self._charge_total_temperature_out_of_range_count
        )

    @property
    def max_observed_dt_s(self) -> float:
        """L. extreme_dt_s_count docstringje: a `self._relax_ctrl is not
        None` guard önmagában elegendő a CF-05 fix (relax_controller.py)
        óta, a külön `_relax_done_fault_recorded` flag megszűnt."""
        charge_max = self._charge_ctrl.max_observed_dt_s if self._charge_ctrl is not None else 0.0
        relax_max = (
            self._relax_ctrl.max_observed_dt_s if self._relax_ctrl is not None else 0.0
        )
        return max(
            self._discharge_integrator.max_observed_dt_s,
            self._discharge_total_max_observed_dt_s,
            charge_max,
            self._charge_total_max_observed_dt_s,
            relax_max,
            self._relax_total_max_observed_dt_s,
        )

    @property
    def missed_deadline_count(self) -> int:
        """SERVICE-határidők: élő gyermek vagy egyszer átvett fázisösszeg."""
        live = self._relax_ctrl.missed_deadline_count if self._relax_ctrl is not None else 0
        return self._relax_missed_deadline_total + live

    @property
    def relax_dmm_fault_total(self) -> int:
        """Teljes kódbázis review 12. kör (2026-08-31), Szoftver/Architektúra
        P2 lelet — l. golden_gg_controller.py azonos nevű property docstringje
        és __init__ itteni docstring-kommentje. A RELAX_1/RELAX_2H fázisok
        alatti feszültség-DMM-olvasási hibák session-szintű összege. L.
        main_window.py::_write_report() relax_dmm_fault_total összegzése.

        CF-05 2. MAX verifier P3-1 lelete (2026-09-08): a `_finalize_relax_
        ctrl_totals()` csak a KÉT normál kilépési ágon (BQ+dV/dt elfogadva /
        extra-wait timeout) fut le — egy `emergency_stop()`/fault-alapú
        megszakítás a relax-fázis alatt (RELAXING vagy az extra-wait
        közben) NEM hívja meg, tehát a `self._relax_ctrl` élő, még nem
        carry-overölt `dmm_fault_count`-ja elveszne, ha ez a property csak
        a carry-overölt `_relax_dmm_fault_total`-t adná vissza. A
        `extreme_dt_s_count`/`max_observed_dt_s` property-k mintáját
        követve az ÉLŐ relax_ctrl értékét is hozzáadjuk — ugyanaz az
        invariáns, mint azoknál: a carry-over és az élő tag konstrukció
        szerint kizárja egymást (a `_relax_ctrl = None` mindig közvetlenül
        a finalize UTÁN történik), tehát ez sosem duplázhat."""
        live = self._relax_ctrl.dmm_fault_count if self._relax_ctrl is not None else 0
        return self._relax_dmm_fault_total + live

    @property
    def u_batt_V(self) -> float:
        """2026-08-23 traditional_learning OV incidens elemzés: a TestRunner
        korábban ehelyett a saját, FÜGGETLEN InstrumentManager.read_all()
        hívásával olvasta újra a DMM-et a naplózáshoz — ez a controller
        SAJÁT, az adott tick-ben a safety-check-hez ténylegesen használt
        u_batt-jától eltérő (később mért, gyors dV/dt mellett jelentősen
        eltérő) értéket eredményezett a logban, ami megtévesztő volt a
        hibaelemzésnél (CLAUDE.md §9: egy DMM-et egy tickben egy komponens
        olvasson). Ez a property teszi lehetővé, hogy a TestRunner a
        controller MÁR kiolvasott, tick-konzisztens értékét használja
        újbóli DMM-lekérdezés helyett."""
        return self._u_batt

    @property
    def battery_temperature_C(self) -> float:
        """Ugyanaz az elv, mint u_batt_V-nél, de a hőmérséklet-DMM-re — a
        TestRunner._build_sample() korábban MINDEN tick-ben egy MÁSODIK,
        a controller saját tick-jétől deszinkron DMM_T-olvasást is végzett
        (InstrumentManager.read_all() skip_battery_temperature paraméter
        nélkül), StepKind-tól függetlenül. Ez a property teszi lehetővé,
        hogy a TestRunner a controller MÁR kiolvasott, tick-konzisztens
        értékét használja."""
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
    def temp_dmm_fault_ticks(self) -> int:
        """Review-kör 3 (2026-09-09), friss Metrológia M3-02 / Szoftver/
        Architektúra F2 (P2) -- l. chemid_controller.py azonos property
        docstringje ugyanerről a session-szintű, tick-alapú számlálóról.
        L. main_window.py::_write_report()
        traditional_learning_temp_dmm_fault_ticks mezője. MAX verifier
        (a306e7e764e3a717b) P2-1, DFAM-001 további előfordulása: 3-utas
        összeg (saját + élő beágyazott ChargeController + korábbi
        PRECHARGE/RECHARGE-fázisok carry-over összege), a
        temperature_out_of_range_count property mintáját követve."""
        charge_ticks = (
            self._charge_ctrl.temp_dmm_fault_ticks
            if self._charge_ctrl is not None else 0
        )
        return (
            self._temp_dmm_fault_ticks
            + charge_ticks
            + self._charge_total_temp_dmm_fault_ticks
        )

    @property
    def load_current_setpoint_warning_ticks(self) -> int:
        """MAX verifier (review-kör 4, HW4-02) N1 lelete -- l.
        discharge_controller.py azonos property docstringje."""
        return self._load_current_setpoint_warning_ticks

    @property
    def u_psu_set_V(self) -> float:
        """2026-08-23 (kiegészítés a fenti OV incidens-elemzéshez, felhasználói
        jelzés alapján: "A PSU SET V még mindíg nem mutat értéket"): a
        TestRunner._build_sample() getattr(controller, "u_psu_set_V", None)
        mintán olvassa a psu_set_voltage_V naplómezőt — ez korábban HIÁNYZOTT
        (csak a belső, eldobható _charge_ctrl-nek volt ilyen property-je),
        ezért a mező mindig üres maradt PRECHARGE/RECHARGE alatt is, jelentősen
        megnehezítve a hibaelemzést (nem lehetett látni, mekkora compliance-re
        állította magát a PSU a hiba pillanatában)."""
        return self._charge_ctrl.u_psu_set_V if self._charge_ctrl is not None else 0.0

    @property
    def last_integration_source(self) -> str:
        """Teljes kódbázis review 11. kör (2026-08-31), Szoftver/Architektúra
        P3 lelet — l. u_psu_set_V docstringje ugyanerről a hiányzó proxy-
        mintáról. test_runner.py::_build_sample() sample["integration_
        current_source"]-je enélkül mindig None maradt PRECHARGE/RECHARGE
        alatt."""
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
        sample["taper_timer_s"]-je enélkül mindig None maradt PRECHARGE/
        RECHARGE alatt, holott a beágyazott ChargeController ténylegesen
        futtatja a taper-hold hiszterézis logikát (6. kör, b73bd78)."""
        return self._charge_ctrl.taper_timer_s if self._charge_ctrl is not None else 0.0

    @property
    def last_fast_loop_extra_s(self) -> float:
        """Review-kör 5, Metrológia M5-01 (P1) -- ugyanaz a hiányzó proxy-
        property minta, mint fent a taper_timer_s-nél. A TestRunner ezt a
        beágyazott ChargeController fast-loop mechanizmusa (l.
        charge_controller.py azonos property docstringje) által MÁR
        elintegrált extra időt olvassa ki, hogy a KÖVETKEZŐ tick dt_s-éből
        levonja (elkerülve a duplán-integrálást a PRECHARGE/RECHARGE
        fázisban)."""
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
    def last_warning_code(self) -> str:
        """2026-08-23 (kiegészítés): a beágyazott ChargeController
        `check_diode_power()` warningje (SERIES_DROP_HIGH/DIODE_POWER_HIGH/
        DIODE_POWER_TOO_HIGH) korábban NEM jutott el a naplóig — a
        2026-08-23 18:57-es incidensben a dióda-disszipáció 4,46W-ra nőtt
        (a 3,0W kritikus figyelmeztetési küszöb fölé) MÁR a fault előtti
        tick-ben, de ez a warning_flags oszlopban sosem jelent meg, elveszve
        egy korai figyelmeztetési lehetőséget. Teljes kódbázis review 9. kör
        (2026-08-30), Szoftver/Architektúra P2 lelet: a PRECHARGE/RECHARGE
        alatt a beágyazott ChargeController saját warning-ja élvez
        elsőbbséget, egyébként a saját szintű (relax/kisütés fázisokbeli)
        hőmérséklet-DMM warning — l. _last_warning_code docstringje."""
        if self._charge_ctrl is not None and self._charge_ctrl.last_warning_code:
            return self._charge_ctrl.last_warning_code
        return self._last_warning_code

    @property
    def i_load_set_A(self) -> float:
        """2026-08-24 (élő panel audit, session_fg1_20260823_200337 nyomán):
        a TestRunner._build_sample() getattr(controller, "i_load_set_A", None)
        mintán olvassa a load_set_current_A naplómezőt (LivePanel "Terh. I set")
        — ez a controlleren eddig HIÁNYZOTT (csak DischargeController-en volt
        ilyen property), ezért DISCHARGE_1/DISCHARGE_2 alatt is mindig üres
        maradt, holott a load ekkor ténylegesen C/10 árammal van vezérelve
        (l. _begin_discharge())."""
        if self._state in (LearningState.DISCHARGE_1, LearningState.DISCHARGE_2):
            return self._discharge_current_A
        return 0.0

    # ------------------------------------------------------------------ #
    # Fő advance()                                                         #
    # ------------------------------------------------------------------ #

    def advance(self, dt_s: float) -> LearningState:
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

        # CF-19 (governance-v2 kör 2, DFAM-023): korábban a _phase_elapsed_s
        # növelését csak 4/20 handler végezte el szórtan (IT_ENABLE_STEP/
        # VERIFY_VOK_QEN/REBOUND_CHECK_1-2/VERIFY_UPDATE_STATUS_05-06) — a
        # maradék ~16 állapotban a mező mindig 0 maradt, holott a `_enter()`/
        # `_transition_to()` MINDEN átmenetkor helyesen nullázta. Mostantól
        # egy központi hely növeli minden nem-terminális ticken, a 4 szórt
        # `+= dt_s` sor törölve (a küszöb-ellenőrzések változatlanok, csak a
        # növelés forrása lett egységes — l. ocv_soc_controller.py::advance()
        # azonos hivatkozású kommentje).
        self._phase_elapsed_s += dt_s

        self._last_warning_code = ""

        if self._state not in (
            LearningState.PRECHARGE, LearningState.RECHARGE, LearningState.FINAL_RECHARGE,
            LearningState.IT_ENABLE_STEP,
            LearningState.VERIFY_UPDATE_STATUS_05,
            LearningState.VERIFY_UPDATE_STATUS_06, LearningState.INITIAL_SNAPSHOT,
            LearningState.FINAL_SNAPSHOT,
        ):
            # R11-B4: VERIFY_VOK_QEN kozvetlen kisutesinditas, friss T kell.
            if self._state in _RELAX_STATES or self._state == LearningState.VERIFY_VOK_QEN:
                self._read_temperature_only(dt_s)
            else:
                self._dmm_valid = self._read_dmm(dt_s)
            # Release-audit 2026-08-23 (2.12): homerseklet-DMM-kapu
            # — a PRECHARGE/RECHARGE fázist a beágyazott ChargeController
            # már fedi.
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
            if self._state in (
                LearningState.DISCHARGE_1, LearningState.DISCHARGE_2,
                LearningState.REBOUND_CHECK_1, LearningState.REBOUND_CHECK_2,
            ):
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

        if self._load.input_commanded_on and self._state not in (
            LearningState.PRECHARGE, LearningState.RECHARGE, LearningState.FINAL_RECHARGE,
            *_RELAX_STATES,
        ):
            concurrent = self._safety.check_concurrent_psu_load(
                psu_commanded_on=self._psu.output_commanded_on,
                load_commanded_on=True,
            )
            if concurrent.fault is not None:
                self.emergency_stop(concurrent.fault.name)
                return self._state
            if self._state in (LearningState.DISCHARGE_1, LearningState.DISCHARGE_2):
                first_crossing = not self._learning_cutoff_seen and self._u_batt <= self._discharge_cutoff_V
                self._learning_cutoff_seen |= self._u_batt <= self._discharge_cutoff_V
                if not self._check_learning_discharge_floor(
                    self._discharge_cutoff_V, first_crossing=first_crossing,
                ):
                    return self._state
            elif self._state in (LearningState.REBOUND_CHECK_1, LearningState.REBOUND_CHECK_2):
                if not self._check_learning_discharge_floor(self._discharge_cutoff_V):
                    return self._state
            elif not self._safety.enforce_normal_discharge(
                self._u_batt, self._discharge_current_A, self._profile.terminate_voltage_pack_V,
                self.emergency_stop, self.on_event,
            ):
                return self._state

        handler = {
            LearningState.INIT: lambda: self._enter(LearningState.PRECOND),
            LearningState.PRECOND: self._run_precond,
            LearningState.PRECHARGE: lambda: self._run_charge(dt_s, LearningState.PRECHARGE_RELAX),
            LearningState.PRECHARGE_RELAX: lambda: self._run_relax(dt_s, LearningState.INITIAL_SNAPSHOT, is_full=True),
            LearningState.INITIAL_SNAPSHOT: self._run_initial_snapshot,
            LearningState.IT_ENABLE_STEP: lambda: self._run_it_enable(dt_s),
            LearningState.VERIFY_VOK_QEN: lambda: self._run_verify_vok_qen(dt_s),
            LearningState.DISCHARGE_1: lambda: self._run_discharge(dt_s, LearningState.REBOUND_CHECK_1),
            LearningState.REBOUND_CHECK_1: lambda: self._run_rebound_check(
                dt_s, LearningState.DISCHARGE_1, LearningState.RELAX_1,
            ),
            LearningState.RELAX_1: lambda: self._run_relax(dt_s, LearningState.VERIFY_UPDATE_STATUS_05, is_full=False),
            LearningState.VERIFY_UPDATE_STATUS_05: lambda: self._run_verify_update_status(
                dt_s, is_final=False, next_state=LearningState.RECHARGE,
            ),
            LearningState.RECHARGE: lambda: self._run_charge(dt_s, LearningState.RELAX_PRE_DISCHARGE_2),
            # CF-06 (governance-v2 review-kör 1): require_vok_cleared=False
            # -- ez a RECHARGE utáni relax (TI-SLUSB55B/SLUA711 7./8. lépés),
            # ahol a VOK flag a forrás szerint SET marad, l. _run_relax()
            # feletti kommentet.
            LearningState.RELAX_PRE_DISCHARGE_2: lambda: self._run_relax(
                dt_s, LearningState.DISCHARGE_2, is_full=True, require_vok_cleared=False,
            ),
            LearningState.DISCHARGE_2: lambda: self._run_discharge(dt_s, LearningState.REBOUND_CHECK_2),
            LearningState.REBOUND_CHECK_2: lambda: self._run_rebound_check(
                dt_s, LearningState.DISCHARGE_2, LearningState.RELAX_2,
            ),
            LearningState.RELAX_2: lambda: self._run_relax(dt_s, LearningState.VERIFY_UPDATE_STATUS_06, is_full=False),
            LearningState.VERIFY_UPDATE_STATUS_06: lambda: self._run_verify_update_status(
                dt_s, is_final=True, next_state=LearningState.FINAL_SNAPSHOT,
            ),
            LearningState.FINAL_SNAPSHOT: self._run_final_snapshot,
            LearningState.FINAL_RECHARGE: lambda: self._run_charge(dt_s, LearningState.DONE),
        }.get(self._state)
        if handler is not None:
            handler()
        else:
            # Review-kör 5, Session/Workflow S5-05 (P3, DFAM-004 sibling) --
            # l. charge_controller.py::advance() azonos hivatkozású
            # kommentje ugyanerről a hibaosztályról: a korábbi `if handler
            # is not None: handler()` egy jövőbeli, itt le nem kezelt
            # LearningState-re CSENDBEN no-op-olt volna (a `.get()` None-t
            # ad vissza) -- explicit raise. A _TERMINAL_STATES-t (DONE/
            # FAULT) a függvény ELEJÉN lévő korai return már lefedi.
            raise ValueError(f"Unhandled LearningState in advance(): {self._state!r}")
        return self._state

    def _enter(self, state: LearningState) -> None:
        self._state = state
        self._phase_elapsed_s = 0.0

    def _transition_to(self, state: LearningState) -> None:
        """Mint `_enter()`, de a DISCHARGE_1/DISCHARGE_2 célállapotoknál a
        load-konfigurációt (`_begin_discharge()`) is elvégzi — DISCHARGE_1-be
        `_run_verify_vok_qen()`-ből explicit hívással jutunk, DISCHARGE_2-be
        viszont a RELAX_PRE_DISCHARGE_2 elfogadásán/timeoutján keresztül
        (`_run_relax()`), ahol ez a helper KÖZPONTOSÍTVA biztosítja, hogy a
        load setup soha ne maradjon el egy generikus `_enter()` mögött."""
        if state in (LearningState.DISCHARGE_1, LearningState.DISCHARGE_2):
            self._begin_discharge(state)
        else:
            self._enter(state)

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
        pending_events: list[dict] = []
        # CF-19 MAX/normál verifier P2 lelet (governance-v2 kör 2): ez a
        # terminális átmenet korábban közvetlen self._state = X maradt,
        # ELTÉRVE a másik 3 összetett controllertől (ocv_soc_controller.py/
        # chemid_controller.py/golden_gg_controller.py, ahol MINDEN átmenet,
        # a terminálisak is, _enter()-en megy át) -- emiatt egy FAULT-ba
        # lépéskor a phase_elapsed_s nem nullázódott, hanem megfagyott az
        # utolsó aktív fázis felhalmozott értékén, félrevezető report.json
        # active_internal_phase_elapsed_s-t eredményezve.
        self._enter(LearningState.FAULT)
        # Block 3 (2026-08-25, GAP #5): a Cell BL Set Volt Threshold
        # ideiglenes learning-értékét MINDEN terminális útvonalon (DONE ÉS
        # FAULT) vissza kell állítani — a _classify_result() ELŐTT, hogy egy
        # esetleges restore-hiba bekerüljön a quality_flags-be.
        if self._tlc_completion["learning_evidence"] is None:
            self._restore_cell_bl_if_needed(emit=pending_events.append)
        # [SCF-011 újratervezés, 2026-09-11] a fenti restore NO-OP, ha a
        # temp-write sosem történt meg (l. docstringje) -- pl. ha a FAULT
        # már a PRECOND gate-en belül bekövetkezett, MIELŐTT az IT_ENABLE
        # valaha unseal-elt/beállította volna ezt a mezőt. Ilyenkor a
        # PRECOND saját unseal()-je óta a chip UNSEALED maradhatott anélkül,
        # hogy bármi visszazárta volna -- ez a fallback fedezi ezt a rést,
        # explicit seal-állapot visszaellenőrzéssel (l. docstringje).
        if self._tlc_completion["learning_evidence"] is None:
            self._ensure_bq_sealed_or_flag("EMERGENCY_STOP", emit=pending_events.append)
        # 2026-08-24 (session_fg1_20260823_200337 elemzés): korábban a
        # _classify_result()/result_summary KIZÁRÓLAG sikeres DONE-nál
        # (_run_final_snapshot()) futott le — FAULT esetén a report.json
        # bq_learning_result mezője örökre null maradt, holott a
        # _classify_result() már eleve úgy készült, hogy hiányos/részleges
        # bizonyítékkal (update_status_final/qmax_final/ra_final=None) is
        # helyesen AWAITING/FAILED-re klasszifikáljon (CLAUDE.md 13. fejezet:
        # minden terminális állapot kapjon érdemi riportot, nem csak DONE).
        # Nem indít új BQ-lekérdezést itt (a vészleállítás maradjon gyors és
        # blokkolásmentes) — a már összegyűlt bizonyítékkal (_initial_
        # snapshot, _update_status_at_0x05_check, a _record_gauge_sample()
        # által időközben gyűjtött _gauge_log) klasszifikál.
        if self._tlc_completion["learning_evidence"] is None:
            self._result_summary = self._classify_result()
        final = self._tlc_completion["final_recharge"]
        if final["status"] != "COMPLETED":
            final.update(status="INCOMPLETE", reason=self._fault_reason)
            self._emit({
                "event_code": "LEARNING_FINAL_RECHARGE_INCOMPLETE",
                "event_message": self._fault_reason,
                "severity": "WARNING",
            })
        # A terminális runner-checkpoint is ezt a rekordot orzi meg.
        if self.persist_tlc_completion is not None:
            try:
                self.persist_tlc_completion(self.tlc_completion)
            except Exception as exc:  # noqa: BLE001 - terminal persistence must not mask safe-off
                self._fault_reason += f" | TLC_COMPLETION_CHECKPOINT_FAILED:{exc}"
        # A külső callback csak minden BL/seal cleanup-kísérlet után futhat.
        for event in pending_events:
            self._emit(event)
        self._emit({"event_code": "LEARNING_FAULT", "event_message": self._fault_reason})

    def _emit(self, payload: dict) -> None:
        if self.on_event is not None:
            self.on_event(payload)

    def _emit_relax_event(
        self, payload: dict, *, emit: Callable[[dict], None] | None = None,
    ) -> None:
        """R2-12 (DFAM-011 + DFAM-013): a beágyazott RelaxController
        eseményét a JELENLEGI relax-alállapottal (PRECHARGE_RELAX/RELAX_1/
        RELAX_PRE_DISCHARGE_2/RELAX_2) kiegészítve továbbítjuk -- a
        generikus "RELAX_DMM_FAULT" üzenet önmagában megkülönböztethetetlen
        lenne a 4 relax-fázis között. Ugyanaz a `..._AT_{self._state.value}`
        kontextus-minta, mint a quality_flags oldalon. Az `event_code`
        SZÁNDÉKOSAN változatlan (events.csv/EventLogWidget kompatibilitás)."""
        enriched = dict(payload)
        enriched["event_message"] = (
            f"{self._state.value}: {payload.get('event_message', '')}"
        )
        (self._emit if emit is None else emit)(enriched)

    # ------------------------------------------------------------------ #
    # PRECOND — SUP03-LEARN-001                                           #
    # ------------------------------------------------------------------ #

    def _run_precond(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        voltage_result = self._safety.check_precheck_voltage(
            self._u_batt, _traditional_learning=True,
        )
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return
        if not self._validate_final_charge_config():
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
        # Block 2 (2026-08-25, GAP #1/#6): PROFILE↔SESSION↔BQ Data Flash
        # konzisztencia-gate — a Block 1 Bq34z110DataFlashService immár
        # forrásolt DF subclass/offset térképpel rendelkezik (l.
        # bq_data_flash_service.py FIELD_REGISTRY), ezért a korábbi
        # "nincs forrásolt offset, ezért nem ellenőrizzük" indoklás (l.
        # git history LEARNING_BQ_DF_CROSS_CHECK_NOT_IMPLEMENTED) megszűnt.
        # Review-kör 4 (2026-09-09), Gyári forrás F4-1 / SCF-011 rezolúció:
        # a DF-OLVASÁS is UNSEALED állapotot igényel -- ez korábban csak
        # HIL_PENDING mérnöki feltételezés volt, de a SLUSB55B (BQ34Z110PWR.
        # pdf) explicit dokumentálja: "In SEALED mode, data flash cannot be
        # accessed through commands 0x3e and 0x3f." (2340. sor) +
        # DataFlashClass() (0x3e) leírása "SEALED Access: This command is
        # not available in SEALED mode." (2491-2496. sor) + az ACCESSING
        # DATA FLASH szakasz: SEALED-ben a DataFlashBlock() (0x3f) a
        # subclass-alapú hozzáférés helyett Manufacturer Information Block
        # A/B/C kiválasztására áll át (949-953. sor). Ez a gyökéroka a
        # BQ-DF-SEALED-001 HIL-bizonyítéknak (degenerált 21×0x00+11×0xFF
        # minta SEALED chipen) -- l. CLAIM_LEDGER.yaml BQ-DF-SEALED-001 és
        # SOURCE_CONFLICTS.md SCF-011.
        #
        # [SCF-011 újratervezés, 2026-09-11, felhasználói döntés] KORÁBBAN
        # ez a gate saját, önálló unseal->read->seal tranzakciót futtatott,
        # és a session hátralévő része SEALED állapotban maradt egészen a
        # KÖVETKEZŐ (IT_ENABLE) unseal-ig -- eközben `_run_initial_
        # snapshot()` (a PRECHARGE/PRECHARGE_RELAX UTÁN, de az IT_ENABLE
        # ELŐTT lefutó Qmax/Ra érvényesség-kapu) egy SEALED chipen futott,
        # tehát a fenti BQ-DF-SEALED-001 hibaosztályt ÉPP AZON a köztes
        # ablakon keresztül volt kitéve, amit e tranzakció maga próbált
        # elkerülni. Session-szintű életciklusra állítva: a chip a PRECOND
        # unseal()-je UTÁN UNSEALED marad a TELJES tanulási ciklus alatt
        # (a köztes lépések -- IT_ENABLE, VOK/QEN verify, a diszkrét
        # kisütési/relax fázisok periodikus `_poll_bq_diagnostics_
        # throttled()` hívásai -- innentől MIND megbízható, nem-degenerált
        # olvasást kapnak), egészen a `_restore_cell_bl_if_needed()` VALÓDI
        # session-végi seal()-jéig (l. `_run_final_snapshot()`/
        # `emergency_stop()` -- MINDEN terminális útvonalon garantált,
        # explicit fallback-kel, ha a restore maga nem futna). Ezért itt a
        # korábbi `finally: seal()` blokk TÖRÖLVE -- a PRECOND unseal()-je
        # utáni seal-felelősség a session VÉGÉRE tolódott át, nem
        # köztes lépésenként ismétlődik.
        # R17-08 (2026-09-16): a `_bq_unseal_attempted` SZÁNDÉKOSAN az RPC
        # ELŐTT vált True-ra -- l. pending_bq_unsealed docstringje.
        self._bq_unseal_attempted = True
        try:
            self._bq_transport.unseal(confirm=True)
            # [SCF-011 újratervezés, MAX verifier P1-1 utóellenőrzés,
            # 2026-09-11] a session-szintű életciklus SZIMMETRIKUS
            # visszaellenőrzést igényel mindkét oldalon (nyitó UNSEAL ÉS
            # záró SEAL, l. _ensure_bq_sealed_or_flag()) -- egy "unseal()
            # RPC ok-kal tért vissza" NEM bizonyítja, hogy az SS bit
            # ténylegesen 0-ra váltott (rossz unseal-kulcs a DF-ben,
            # SCF-012 retry-duplikáció, egyéb chip-állapot). Enélkül a
            # TELJES, több órás session egy SEALED chipen futhatna
            # végig -- pontosan az az eset, amit ez az újratervezés
            # kizárni hivatott. CLAUDE.md §7 "Setpoint, commanded state
            # és readback külön fogalom" -- nem elég a parancs elfogadását
            # nézni, a tényleges állapotot vissza kell olvasni.
            cs = decode_control_status(self._bq_driver.read_control_status())
            if cs.ss:
                self.emergency_stop(
                    "BQ_UNSEAL_NOT_EFFECTIVE: SS bit still set after unseal()"
                )
                return
            df_service = Bq34z110DataFlashService(self._bq_transport)
            gate_report = evaluate_provisioning_consistency(self._profile, df_service)
        except TestAbortedByUser:
            raise
        except Exception as exc:
            # Kör 20 Batch E13b (F20-S4, DFAM-002): korábban csak `Instrument
            # Error`-t fogott el ez az ág -- a testvér golden_gg_controller.py
            # UGYANEZEN unseal()+read_control_status() szekvencia körül `except
            # Exception`-t használ (l. ott :872). FONTOS: ez NEM a GG-minta
            # átvétele (az `emergency_stop()`-tal ÉS `return`-nel zárna) -- a
            # TLC SAJÁT, BLOCKED-gate-alapú kimenete (lent, gate_report.decision
            # != CONSISTENT ág) VÁLTOZATLAN marad, csak a kapu szélesebb
            # kivétel-osztályra is lefedi ugyanazt a BLOCKED_BQ_DF_READBACK_
            # FAILED döntést -- egy korábban `InstrumentError`-on kívüli
            # kivétel (pl. a DF-parse-oldali `Bq34z110DataFlashService`/
            # `evaluate_provisioning_consistency()` egy nem-InstrumentError
            # hibája) eddig KIVÉTELKÉNT PROPAGÁLT volna a metódusból, elkerülve
            # a szándékolt BLOCKED-gate/GUI-visszajelzési utat.
            gate_report = ProvisioningConsistencyReport(
                decision=ProvisioningGateDecision.BLOCKED_BQ_DF_READBACK_FAILED,
                reason=f"BQ Data Flash olvasás sikertelen (unseal/transport): {exc}",
                comparisons=(),
            )

        self._provisioning_consistency_report = gate_report
        if gate_report.decision != ProvisioningGateDecision.CONSISTENT:
            # Hibacallback csak a terminális BQ-cleanup után futhat.
            self.emergency_stop(f"LEARNING_CONFIGURATION_MISMATCH: {gate_report.reason}")
            if self.on_provisioning_consistency is not None:
                self.on_provisioning_consistency(gate_report)
            return
        if self.on_provisioning_consistency is not None:
            self.on_provisioning_consistency(gate_report)
        self._emit({
            "event_code": "LEARNING_BQ_DF_CROSS_CHECK_PASSED",
            "event_message": f"{gate_report.reason}; {format_bq_session_scale_context(gate_report)}",
        })

        self._charge_ctrl = ChargeController(
            self._psu, self._load, self._dmm_v, self._dmm_t,
            self._profile, self._safety, self._charge_config,
            _traditional_learning=True,
        )
        self._enter(LearningState.PRECHARGE)

    # ------------------------------------------------------------------ #
    # CHARGE — reused ChargeController (PRECHARGE lépés1 realizáció ÉS    #
    # RECHARGE lépés7 is ugyanezt hívja, külön ChargeController-t indítva)#
    # ------------------------------------------------------------------ #

    def _run_charge(self, dt_s: float, next_state: LearningState) -> None:
        final_charge = self._state == LearningState.FINAL_RECHARGE
        if final_charge and self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        if self._charge_ctrl is None:
            if final_charge:
                if not self._validate_final_charge_config() or self._confirm_final_charge_off() is None:
                    return
                self._tlc_completion["final_recharge"].update(
                    status="RUNNING", started_at_utc=datetime.now(timezone.utc).isoformat(),
                )
                if not self._persist_completion():
                    return
                self._emit({"event_code": "LEARNING_FINAL_RECHARGE_STARTED", "event_message": "Q-38: 0.67 A"})
            self._charge_ctrl = ChargeController(
                self._psu, self._load, self._dmm_v, self._dmm_t,
                self._profile, self._safety,
                self._final_charge_config if final_charge else self._charge_config,
                _traditional_learning=True, _require_confirmed_off=final_charge,
            )
        # R16-B12: a beágyazott töltő is a runner élő leállításjelét figyeli.
        self._charge_ctrl.handoff_stop_requested = self.handoff_stop_requested
        charge_state = self._charge_ctrl.advance(dt_s)
        # R6-B3: a gyermek megorzott forrasanak atvetele (nem friss-integralas-
        # bizonyitek), a gyermek elengedese ELOTT.
        self._last_integration_source = self._charge_ctrl.last_integration_source
        self._dmm_valid = self._charge_ctrl.dmm_valid
        # Review-kör 3 (2026-09-09), MAX verifier (a3f0e6dcf93588360) P2-1:
        # a feszültség-ikret (fenti sor) mindig átvezettük a beágyazott
        # ChargeControllerből, a hőmérsékletit nem -- emiatt a
        # temp_dmm_valid property PRECHARGE/RECHARGE alatt (ahol a saját
        # _read_dmm() ki van kapcsolva) hamisan True maradt egy
        # ténylegesen halott hőmérséklet-DMM mellett is.
        self._temp_dmm_valid = self._charge_ctrl.temp_dmm_valid
        self._u_batt = self._charge_ctrl.u_batt_V
        self._battery_temperature_C = self._charge_ctrl.battery_temperature_C
        # A töltési gyermekút nem örökli a TLC kisütési kivételét.
        if self._load.input_commanded_on:
            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return
            if not self._safety.enforce_normal_discharge(
                self._u_batt, self._discharge_current_A, self._profile.terminate_voltage_pack_V,
                self.emergency_stop, self.on_event,
            ):
                return
        # 2026-08-24 (session_fg1_20260823_200337 elemzés, felhasználói
        # jelzés: a BQ Diagnosztika fül régi/elévült adatot mutatott töltés
        # közben): a _run_discharge()/_run_relax() már hívta ezt (SUP03-
        # LEARN-002), de a _run_charge() (PRECHARGE/RECHARGE) NEM — emiatt a
        # BQ Diagnosztika fül élő nézete (session_owned mód) egy több órás
        # RECHARGE alatt egyszer sem frissült, a session indulása előtti
        # utolsó ismert (jóval alacsonyabb, relaxált) feszültségnél fagyva.
        if not final_charge:
            # A zaro toltes SEALED: nincs DF-poll es nincs uj UNSEAL-ablak.
            self._record_gauge_sample(dt_s)

        if charge_state == ChargeState.FAULT:
            self.emergency_stop(self._charge_ctrl.fault_reason or "LEARNING_CHARGE_FAULT")
        elif charge_state == ChargeState.CHARGE_DONE:
            if final_charge:
                off_readback = self._confirm_final_charge_off()
                if off_readback is None:
                    return
                if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                    return
                completed = datetime.now(timezone.utc)
                self._tlc_completion["final_recharge"].update(
                    status="COMPLETED", reason="TAPER_AND_OFF_CONFIRMED",
                    completed_at_utc=completed.isoformat(),
                    next_campaign_not_before_utc=(completed + timedelta(hours=48)).isoformat(),
                    dmm_voltage_V=self._u_batt, battery_temperature_C=self._battery_temperature_C,
                    psu_current_A=self._charge_ctrl.i_psu_measured_A,
                    taper_timer_s=self._charge_ctrl.taper_timer_s, off_readback=off_readback,
                )
                if not self._persist_completion():
                    return
                self._emit({"event_code": "LEARNING_FINAL_RECHARGE_COMPLETED", "event_message": completed.isoformat()})
            # Release-audit 2026-08-23 (2.7): a lezáruló fázis Ah-ját a futó
            # összegbe mentjük, MIELŐTT a ChargeController-t eldobnánk —
            # l. accumulated_charge_Ah property.
            self._charge_total_accumulated_Ah += self._charge_ctrl.accumulated_charge_Ah
            self._charge_total_accumulated_Wh += self._charge_ctrl.accumulated_charge_Wh
            # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
            # Adatintegritás P3 lelet — l. _charge_total_extreme_dt_s_count
            # docstringje az __init__-ben. Ugyanaz a "mentsd el, MIELŐTT
            # eldobnánk" minta, mint az Ah/Wh-nál fent.
            self._charge_total_extreme_dt_s_count += self._charge_ctrl.extreme_dt_s_count
            self._charge_total_max_observed_dt_s = max(
                self._charge_total_max_observed_dt_s, self._charge_ctrl.max_observed_dt_s
            )
            # Teljes kódbázis review 21. kör (2026-09-06), Session/Workflow
            # P2 lelet — ugyanaz a "mentsd el, MIELŐTT eldobnánk" minta.
            self._charge_total_temperature_out_of_range_count += (
                self._charge_ctrl.temperature_out_of_range_count
            )
            # Review-kör 3 (2026-09-09), MAX verifier (a306e7e764e3a717b)
            # P2-1 -- l. temp_dmm_fault_ticks property docstringje, ugyanaz
            # a "mentsd el, MIELŐTT eldobnánk" minta.
            self._charge_total_temp_dmm_fault_ticks += (
                self._charge_ctrl.temp_dmm_fault_ticks
            )
            self._charge_ctrl = None
            if final_charge:
                self._finish_learning()
            else:
                self._enter(next_state)

    # ------------------------------------------------------------------ #
    # RELAX — RelaxController (min idő + dV/dt) + SUP03-LEARN-006         #
    # BQ-alapú elfogadás (OCVTAKEN=1, VOK törölve). A `_run_relax()` maga #
    # konstruálja meg (lusta inicializálás) a RelaxController-t az ELSŐ   #
    # tick-nél - a hívó (`_run_charge`/`_run_discharge`) csak `_enter()`- #
    # tel lép be a megfelelő RELAX_* állapotba, semmilyen külön "begin"   #
    # lépés nem kell.                                                     #
    #                                                                      #
    # Governance-v2 review-kör 1 (CF-06, VERIFIED_MULTI_SOURCE: TI-        #
    # SLUSB55B p.30 STEP 8 + TI-SLUA711, saját pdftotext kinyeréssel is    #
    # megerősítve): a "VOK törölve" elvárás CSAK a kisütés utáni relax-    #
    # fázisokra (5./9. lépés = RELAX_1/RELAX_2) igaz -- a RECHARGE utáni   #
    # relaxban (7. lépés = RELAX_PRE_DISCHARGE_2) a forrás EXPLICIT azt    #
    # írja: "The VOK flag will remain set." A korábbi, egységes "ocvtaken  #
    # and not vok" kritérium emiatt RELAX_PRE_DISCHARGE_2-re STRUKTURÁLI-  #
    # SAN sosem teljesülhetett (VOK ott sosem törlődik) -- LEARNING_       #
    # RESULT_VERIFIED egy tankönyvi, hibátlan ciklusnál is elérhetetlen    #
    # lett volna. `require_vok_cleared=False` csak RELAX_PRE_DISCHARGE_2   #
    # hívásánál (l. lent) -- PRECHARGE_RELAX/RELAX_1/RELAX_2 alapértéken   #
    # (True) marad, viselkedésük változatlan.                             #
    # ------------------------------------------------------------------ #

    def _run_relax(
        self, dt_s: float, next_state: LearningState, is_full: bool,
        require_vok_cleared: bool = True,
    ) -> None:
        min_s = self._config.relax_after_charge_s if is_full else self._config.relax_after_discharge_s
        if self._relax_ctrl is None:
            self._relax_ctrl = RelaxController(
                self._dmm_v, RelaxConfig(min_relax_s=min_s, dvdt_threshold_V_per_s=0.0),
            )
            self._relax_extra_wait_s = 0.0
            self._relax_exit_pending = False
            self._relax_acceptance_reason = None
            self._relax_gate_mismatch = None
            self._relax_ctrl.on_event = self._emit_relax_event
            self._relax_ctrl.on_sample = self._check_loaded_relax_sample
            # Runner alatt kötelező SERVICE; közvetlen, service nélküli használat LEGACY_TICK.
            if self.prepare_relax_sampling is not None:
                self.prepare_relax_sampling(self._relax_ctrl)

        if self._relax_exit_pending:
            self._finish_relax_exit(next_state)
            return

        relax_state = self._relax_ctrl.advance(dt_s)
        if self._state == LearningState.FAULT:
            return
        self._dmm_valid = self._relax_ctrl.dmm_valid
        if self._dmm_valid:
            self._u_batt = self._relax_ctrl.last_voltage_V
        # A LEGACY_TICK út nem hív on_sample-t; a saját friss mintája is kaput kap.
        if self._load.input_commanded_on:
            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return
            if not self._safety.enforce_normal_discharge(
                self._u_batt, self._discharge_current_A, self._profile.terminate_voltage_pack_V,
                self.emergency_stop, self.on_event,
            ):
                return
        self._record_gauge_sample(dt_s)
        if relax_state != RelaxState.RELAX_DONE:
            return

        bq_ok, ocvtaken, vok = self._check_relax_bq_acceptance(require_vok_cleared)
        # A BQ-poll alatt is gyűjt a worker: utána drain és új mintakor kell.
        self._relax_ctrl.collect_sampling_records()
        if self._state == LearningState.FAULT:
            return
        snapshot = self._relax_ctrl.acceptance_snapshot(self._config.dvdt_threshold_V_per_s)
        self._dmm_valid = self._relax_ctrl.dmm_valid
        if self._dmm_valid:
            self._u_batt = self._relax_ctrl.last_voltage_V
        dvdt_ok = snapshot.reason == "VALID_BELOW_THRESHOLD"
        evidence = {
            "dmm_reason": snapshot.reason,
            "sample_age_s": snapshot.voltage.age_s,
            "read_start": snapshot.voltage.read_start,
            "decision_monotonic_s": (
                snapshot.voltage.read_start + snapshot.voltage.age_s
                if snapshot.voltage.read_start is not None and snapshot.voltage.age_s is not None
                else None
            ),
            "sample_count": snapshot.sample_count,
            "coverage_s": snapshot.coverage_s,
            "max_gap_s": snapshot.max_gap_s,
            "dvdt_V_per_s": snapshot.dvdt_V_per_s if snapshot.dvdt_valid else None,
            "epoch": snapshot.epoch,
            "ocvtaken": ocvtaken, "vok": vok, "bq_ok": bq_ok,
        }
        # Q-41: OCVTAKEN és a friss DMM-kapu eltérése mindkét irányban.
        # Csak állapotváltáskor jelzünk; az új relax-fázis külön indul.
        mismatch = (
            "BQ_OCVTAKEN_ONLY" if ocvtaken is True and not dvdt_ok
            else "DMM_ONLY" if ocvtaken is False and dvdt_ok
            else None
        )
        if mismatch != self._relax_gate_mismatch:
            self._relax_gate_mismatch = mismatch
            if mismatch is not None:
                self._emit({
                    "event_code": "LEARNING_RELAX_GATE_MISMATCH",
                    "event_message": (
                        f"{self._state.value}: {mismatch}; "
                        f"dvdt_threshold_V_per_s={self._config.dvdt_threshold_V_per_s}; "
                        f"{evidence}"
                    ),
                    "phase": self._state.value,
                    "direction": mismatch,
                    "dvdt_threshold_V_per_s": self._config.dvdt_threshold_V_per_s,
                    **evidence,
                })
        if snapshot.reason != self._relax_acceptance_reason:
            self._relax_acceptance_reason = snapshot.reason
            self._emit({
                "event_code": "LEARNING_RELAX_ACCEPTANCE_STATE",
                "event_message": f"{self._state.value}: {snapshot.reason}",
                **evidence,
            })
        if bq_ok and dvdt_ok:
            self._relax_exit_pending = True
            self._emit({
                "event_code": "LEARNING_RELAX_ACCEPTED",
                "event_message": f"{self._state.value}: BQ és friss OLS elfogadva; {evidence}",
                **evidence,
            })
            self._finish_relax_exit(next_state)
            return

        self._relax_extra_wait_s += dt_s
        if self._relax_extra_wait_s >= self._config.max_relax_extra_wait_s:
            self._quality_flags.append(
                f"RELAX_ACCEPTANCE_NOT_CONFIRMED_AT_{self._state.value}"
                f"(ocvtaken={ocvtaken},vok={vok},dvdt_ok={dvdt_ok})"
            )
            self._relax_exit_pending = True
            self._emit({
                "event_code": "LEARNING_RELAX_ACCEPTANCE_TIMEOUT",
                "event_message": (
                    f"{self._state.value}: relax min. idő letelt, de OCVTAKEN/VOK/dV-dt "
                    f"kritérium {self._config.max_relax_extra_wait_s:.0f}s után sem teljesült "
                    f"— folytatás a fizikai ciklussal; {evidence}"
                ),
                **evidence,
            })
            self._finish_relax_exit(next_state)

    def _finish_relax_exit(self, next_state: LearningState) -> None:
        assert self._relax_ctrl is not None
        if not self._relax_ctrl.poll_sampling_stop() or self._state == LearningState.FAULT:
            return
        # A végső drain hibái is egyszer kerülnek át, közvetlenül az eldobás előtt.
        self._dmm_valid = self._relax_ctrl.dmm_valid
        if self._dmm_valid:
            self._u_batt = self._relax_ctrl.last_voltage_V
        handoff_events: list[dict] = []
        not_fresh_event = None
        if next_state == LearningState.DISCHARGE_2 and self._relax_ctrl.sampling_mode == "SERVICE":
            # A nyugta utáni friss mérés védi a közvetlen Load ON kapuját.
            snapshot = self._relax_ctrl.read_handoff_voltage(
                emit=lambda event: self._emit_relax_event(event, emit=handoff_events.append),
            )
            self._dmm_valid = snapshot.quality == "CACHE_FRESH"
            if self._dmm_valid:
                self._u_batt = self._relax_ctrl.last_voltage_V
            else:
                not_fresh_event = {
                    "event_code": "LEARNING_RELAX_DMM_NOT_FRESH",
                    "event_message": (
                        f"{self._state.value}: {snapshot.quality}; "
                        f"sample_age_s={snapshot.age_s}; read_start={snapshot.read_start}"
                    ),
                    "dmm_reason": snapshot.quality, "sample_age_s": snapshot.age_s,
                    "read_start": snapshot.read_start,
                }
        self._finalize_relax_ctrl_totals()
        self._relax_ctrl = None
        self._relax_exit_pending = False
        if not_fresh_event is not None:
            # A diagnosztikai callback csak az összes cleanup-kísérlet után futhat.
            self._transition_to(next_state)
            for event in handoff_events:
                self._emit(event)
            self._emit(not_fresh_event)
            return
        self._transition_to(next_state)

    def _finalize_relax_ctrl_totals(self) -> None:
        """Governance-v2 review-kör 1 (CF-05, DFAM-008): a session-szintű
        relax_dmm_fault_total/extreme_dt_s_count/max_observed_dt_s carry-
        over-t PONTOSAN itt, a self._relax_ctrl eldobása ELŐTT kell
        elvégezni — ez az EGYETLEN hívási pont mindkét kilépési ágon
        (BQ+dV/dt elfogadva / extra-wait timeout), tehát garantáltan
        pontosan egyszer fut le relax-fázisonként, nem igényel külön
        "one-shot" flaget.

        Korábban (13. kör) ez a blokk a RELAX_DONE ELSŐ észlelésekor futott
        le, egy `_relax_done_fault_recorded` flaggel védve az ismétlődéstől
        — az akkori RelaxController implementáció mellett ez helyes volt,
        mert dmm_fault_count/extreme_dt_s_count RELAX_DONE UTÁN fagyott
        érték maradt. A relax_controller.py::advance() CF-05 fixe óta
        viszont ezek az értékek a BQ-elfogadási extra-wait hurok TELJES
        ideje alatt tovább nőhetnek — ha a carry-over az ELSŐ RELAX_DONE-
        tick pillanatában (a réginek megfelelő helyen) futna, az extra-wait
        alatt bekövetkező további DMM-kiesések/dt_s-tüskék csendben
        elvesznének (sosem jutnának a session-szintű összegbe). Az
        áthelyezés a self._relax_ctrl eldobásának pillanatára ezt zárja
        ki: mindig a VÉGSŐ, teljes értéket olvassa ki."""
        assert self._relax_ctrl is not None
        if self._relax_ctrl.dmm_fault_count > 0:
            self._quality_flags.append(
                f"RELAX_DMM_FAULTS_AT_{self._state.value}"
                f"(count={self._relax_ctrl.dmm_fault_count})"
            )
        self._relax_dmm_fault_total += self._relax_ctrl.dmm_fault_count
        self._relax_missed_deadline_total += self._relax_ctrl.missed_deadline_count
        self._relax_total_extreme_dt_s_count += self._relax_ctrl.extreme_dt_s_count
        self._relax_total_max_observed_dt_s = max(
            self._relax_total_max_observed_dt_s, self._relax_ctrl.max_observed_dt_s
        )

    def _check_relax_bq_acceptance(
        self, require_vok_cleared: bool = True,
    ) -> tuple[bool, Optional[bool], Optional[bool]]:
        """CF-06 (governance-v2 review-kör 1, VERIFIED_MULTI_SOURCE: TI-
        SLUSB55B p.30 STEP 8, 5./7./9. lépés + TI-SLUA711, 5./8./10.
        lépés -- Kör 20 E18/SRC-F-8: az első SLUA711-szám korábban
        helytelenül 6 volt, a "Calibration Process" számozott listája
        szerint a 5. lépés az OCVTAKEN relax, a 6. lépés csak egy
        státusz-ellenőrzés): a VOK flag CSAK a kisütés utáni relax-fázisokban (5./9.
        lépés) törlődik a sikeres OCV-elfogadáshoz -- a RECHARGE utáni
        relaxban (7. lépés) a forrás szerint "The VOK flag will remain
        set." `require_vok_cleared=False` hívónál (RELAX_PRE_DISCHARGE_2)
        az elfogadás kizárólag `ocvtaken`-en múlik, a `vok` értékét nem
        követeli meg egyik irányban sem -- l. _run_relax() feletti
        kommentet a hívóhelyekért."""
        try:
            flags = decode_flags(self._bq_driver.read_flags())
            cs = decode_control_status(self._bq_driver.read_control_status())
        except TestAbortedByUser:
            raise
        except Exception:
            return False, None, None
        ocvtaken = bool(flags.ocvtaken)
        vok = bool(cs.vok)
        ok = ocvtaken and (not vok if require_vok_cleared else True)
        return ok, ocvtaken, vok

    # ------------------------------------------------------------------ #
    # SNAPSHOT — SUP03-LEARN-003 (kiindulási) / final                     #
    # ------------------------------------------------------------------ #

    def _initial_snapshot_should_stop(self) -> bool:
        # Q-39: a callbackből kért Stop után is az OFF legyen a következő I/O.
        if self._state == LearningState.FAULT:
            return True
        try:
            check_activation_stop(self.handoff_stop_requested)
        except TestAbortedByUser:
            return True
        return False

    def _run_initial_snapshot(self) -> None:
        if self._initial_snapshot_should_stop():
            return
        if self._initial_snapshot is None:
            snapshot = poll_bq_diagnostics(self._bq_driver)
            self._initial_snapshot = snapshot
            self._initial_update_status_attempts = 1
        else:
            # Q-39: csak az Update Status ismétlődik, tickenként legfeljebb egyszer.
            if time.monotonic() < self._initial_update_status_retry_at_s:
                return
            snapshot = self._initial_snapshot
            self._initial_update_status_attempts += 1
            ts = time.monotonic()
            try:
                value = self._bq_driver.read_update_status()
                update_status = BqFieldReading(
                    value=value, valid=value is not None,
                    error=None if value is not None else "Update Status value=None",
                    timestamp_monotonic=ts,
                )
            except TestAbortedByUser:
                raise
            except Exception as exc:  # noqa: BLE001 -- a readback-hiba retry/FAULT-ra kerül
                update_status = BqFieldReading(
                    value=None, valid=False, error=str(exc), timestamp_monotonic=ts,
                )
            # A már publikált snapshotot a GUI sorba állított callbackje is őrizheti.
            snapshot = replace(snapshot, update_status=update_status)
            self._initial_snapshot = snapshot
        if self._initial_snapshot_should_stop():
            return
        # Kor 18, Batch C7 R17-16 (DFAM-021+DFAM-001): ez a snapshot DONTO
        # (verdikt-kritikus) volt, de korabban KIZAROLAG a controller sajat
        # mezojebe kerult. L. _emit_gauge_sample_best_effort() docstringje.
        self._emit_gauge_sample_best_effort(snapshot)
        if self._initial_snapshot_should_stop():
            return
        # Review-kör 2 (2026-09-08), Session/Workflow/Állapotgép S-4 lelet
        # (sibling: golden_gg_controller.py::_run_precond() ugyanezt a
        # kaput MÁR alkalmazza a saját PRECOND állapotában, l. ott a
        # GG_SOURCE_SNAPSHOT_INCOMPLETE kommentje -- azzal a KÜLÖNBSÉGGEL,
        # hogy a GG saját kapuja a TELJES fizikai ciklus (kondicionálás/
        # töltés/kisütés) ELŐTT fut, itt viszont a PRECHARGE (teljes
        # töltés) + PRECHARGE_RELAX MÁR lezajlott, mire ide érünk -- a
        # pontosítás a MAX verifier eaf9221-utóellenőrzés V2 leletéből):
        # a Qmax/Ra tábla érvényességét MÁR ITT, az IT-ENABLE + a két
        # kisütési/relax fázis ELŐTT ellenőrizni kell -- különben egy itt
        # előre észlelhető BQ-olvasási hiba csak a TELJES, ~30 órás
        # Traditional Learning ciklus VÉGÉN, a _run_final_snapshot()/
        # _classify_result()-ban derülne ki, feleslegesen elpazarolva az
        # IT-enable+kisütés+relax szakaszokat (a PRECHARGE-ot már nem).
        #
        # [SCF-011 újratervezés, 2026-09-11, felhasználói döntés -- FRISSÍTVE,
        # l. CLAIM_LEDGER.yaml BQ-DF-SEALED-001 2. KORREKCIÓ] a lenti
        # megállapítás (MAX verifier eaf9221-utóellenőrzés V1, 2026-09-08)
        # MÁR NEM ÉRVÉNYES: (1) a `_run_precond()` MOST session-szintű
        # unseal()-t végez, visszaellenőrzéssel, tehát mire ez a metódus
        # lefut, a chip UNSEALED (kivéve, ha az unseal maga hibázott, ami
        # már _run_precond()-ban BQ_UNSEAL_NOT_EFFECTIVE/BQ_COMM_UNAVAILABLE
        # fault-tal megáll); (2) a driver-szinten (`Bq34z110ReadOnlyDriver.
        # _reject_degenerate_sealed_df_block()`) is bevezetésre került egy
        # defense-in-depth kontroll, ami a HIL-igazolt 21×0x00+11×0xFF
        # degenerált mintát MOST explicit `InstrumentInvalidReading`-gel
        # elutasítja `read_qmax()`/`read_update_status()`/`read_ra_table()`-
        # ban -- ez a kapu tehát MÁR NEM csak a "hard error" esetet fedi
        # le, hanem a konkrét, HIL-igazolt degenerált-minta hibamódot is,
        # ha (1) ellenére valamiért mégis SEALED állapotban futna. HIL_
        # PENDING MARAD, hogy egyetlen unseal(confirm=True) (FULL_ACCESS
        # nélkül) valós chipen ténylegesen törli-e az SS bitet -- l.
        # SOURCE_CONFLICTS.md SCF-011 "HIL_PENDING, nevesített tesztesetek"
        # szakasza.
        if not snapshot.qmax.valid or not snapshot.ra_table_mohm.valid:
            self.emergency_stop(
                "LEARNING_SOURCE_SNAPSHOT_INCOMPLETE: "
                f"qmax_valid={snapshot.qmax.valid} ({snapshot.qmax.error}), "
                f"ra_table_valid={snapshot.ra_table_mohm.valid} "
                f"({snapshot.ra_table_mohm.error})"
            )
            return
        # Q-39: ismeretlen induló státusszal az IT_ENABLE nem érhető el.
        us = snapshot.update_status
        if not us.valid or us.value is None:
            if self._initial_update_status_attempts >= _INITIAL_UPDATE_STATUS_MAX_ATTEMPTS:
                self.emergency_stop(
                    "LEARNING_INITIAL_UPDATE_STATUS_UNREADABLE: "
                    f"attempts={self._initial_update_status_attempts}; {us.error}"
                )
            else:
                self._initial_update_status_retry_at_s = (
                    time.monotonic() + _INITIAL_UPDATE_STATUS_RETRY_WAIT_S
                )
                self._emit({
                    "event_code": "LEARNING_INITIAL_UPDATE_STATUS_RETRY",
                    "event_message": (
                        f"Update Status nem olvasható; próbák="
                        f"{self._initial_update_status_attempts}/"
                        f"{_INITIAL_UPDATE_STATUS_MAX_ATTEMPTS}; {us.error}"
                    ),
                    "attempts": self._initial_update_status_attempts,
                    "max_attempts": _INITIAL_UPDATE_STATUS_MAX_ATTEMPTS,
                    "retry_wait_s": _INITIAL_UPDATE_STATUS_RETRY_WAIT_S,
                })
                self._initial_snapshot_should_stop()
            return
        self._qmax_before = snapshot.qmax.value
        # R1 B4 (F-10(b)): olvasott, de TI-tartományon kívüli Qmax -> quality
        # flag (a Qmax-bizonyítékot érvényteleníti, a ciklust nem; FAULT nincs).
        if not _qmax_within_ti_range(self._qmax_before):
            self._quality_flags.append(
                f"QMAX_OUT_OF_TI_RANGE_AT_INITIAL_SNAPSHOT(value={self._qmax_before})"
            )
        self._ra_before = tuple(snapshot.ra_table_mohm.value or [])
        # R1 B4 (F-11): az IT_ENABLE ELŐTTI UpdateStatus jelzése (nincs új
        # BQ-I/O, a fenti snapshotból). A Q-4 besorolását a klasszifikáló védi.
        us = snapshot.update_status
        if us.valid and us.value is not None:
            us_text = f"UpdateStatus=0x{us.value:02X} (IT_ENABLE előtti érték)"
            if us.value == UPDATE_STATUS_FULL_LEARNING:
                us_text += " — már a ciklus előtt 0x06; besorolás: Q-4 szerint AWAITING"
        else:
            us_text = f"UpdateStatus nem olvasható ({us.error})"
        self._emit({
            "event_code": "LEARNING_INITIAL_SNAPSHOT",
            "event_message": (
                f"kiindulási BQ snapshot rögzítve; {us_text}; "
                f"Update Status próbák={self._initial_update_status_attempts}"
            ),
            "initial_update_status_attempts": self._initial_update_status_attempts,
        })
        if self._initial_snapshot_should_stop():
            return
        self._enter(LearningState.IT_ENABLE_STEP)

    def _run_final_snapshot(self) -> None:
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        # Az OFF-hiba sem dobhatja el a még olvasható BQ-bizonyítékot.
        # Mindkét OFF-parancs most fut; a FAULT-döntés csak a mentés után.
        off_failures = self._command_final_charge_off()
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        # [SCF-011 újratervezés, 2026-09-11, felhasználói döntés] SORREND
        # SZÁNDÉKOSAN megfordítva a korábbihoz képest: a záró
        # poll_bq_diagnostics() ELŐSZÖR fut, MÉG a chip UNSEALED állapota
        # alatt (a PRECOND unseal()-je óta nem volt köztes seal, l.
        # _run_precond()/_run_verify_vok_qen() azonos hivatkozású
        # kommentjei) -- KORÁBBAN ez a hívás a _restore_cell_bl_if_needed()
        # UTÁN futott, ami MÁR lezárta (seal) a chipet, tehát ez a záró
        # snapshot pontosan ugyanabba a BQ-DF-SEALED-001 hibaosztályba
        # esett (degenerált, hibajelzés nélküli olvasás), amit a session-
        # szintű életciklus egyébként megszüntetni hivatott. A Cell BL
        # restore -- Block 3 (2026-08-25, GAP #5, TI SLUSB55B STEP 8 10.
        # lépés) -- marad az UTOLSÓ BQ-módosító művelet, aminek saját
        # seal()-je a session VALÓDI záró seal-je.
        snapshot = poll_bq_diagnostics(self._bq_driver)
        self._final_snapshot = snapshot
        # Kor 18, Batch C7 R17-16 -- l. _emit_gauge_sample_best_effort()
        # docstringje. Idozites-erzekeny pont (a chip meg UNSEALED, l.
        # feljebb a SORREND-kommentet), ezert best-effort/guardolt emit.
        self._emit_gauge_sample_best_effort(snapshot)
        if snapshot.qmax.valid:
            self._qmax_final = snapshot.qmax.value
            # R1 B4 (F-10(b)): a nyers érték megmarad, de explicit quality
            # flag jelzi, ha a TI-tartományon kívül esik.
            if not _qmax_within_ti_range(self._qmax_final):
                self._quality_flags.append(
                    f"QMAX_OUT_OF_TI_RANGE_AT_FINAL_SNAPSHOT(value={self._qmax_final})"
                )
        if snapshot.ra_table_mohm.valid:
            self._ra_final = tuple(snapshot.ra_table_mohm.value or [])
        # R2-B1 (R2-source-01, DFAM-001): a VERIFIED Qmax/Ra-bizonyítékot EZ a
        # snapshot adja, ezért a saját UpdateStatus-ának is pontosan 0x06-nak
        # kell lennie (SLUSB55B rev. B Table 11, 18. o.; Table 20, 41. o.).
        # Csak quality flag (-> AWAITING): a restore/seal alább feltétel
        # nélkül fut, a _update_status_final (VERIFY-olvasás) nem íródik felül.
        final_update_status = snapshot.update_status
        if not final_update_status.valid:
            self._quality_flags.append(
                f"UPDATE_STATUS_READ_FAILED_AT_FINAL_SNAPSHOT:{final_update_status.error}"
            )
        elif final_update_status.value != UPDATE_STATUS_FULL_LEARNING:
            self._quality_flags.append(
                f"UPDATE_STATUS_NOT_0X06_AT_FINAL_SNAPSHOT(got={final_update_status.value})"
            )
        pending_events: list[dict] = []
        self._restore_cell_bl_if_needed(emit=pending_events.append)
        # Fallback -- l. _ensure_bq_sealed_or_flag() docstringje: a normál
        # DONE-úton `_cell_bl_original_mV` mindig be van állítva (az
        # IT_ENABLE már lefutott, különben FAULT-tal álltunk volna meg),
        # tehát ez itt tipikusan egy olcsó, már-SEALED no-op -- de védelmet
        # ad egy jövőbeli, ma nem létező DONE-útvonalra is, ahol a restore
        # esetleg no-op lenne. PASS csak igazolt, végleges SEALED állapot
        # UTÁN adható (_classify_result() ez után fut).
        sealed_confirmed = self._ensure_bq_sealed_or_flag("FINAL_SNAPSHOT", emit=pending_events.append)
        if not sealed_confirmed:
            self._cell_bl_reseal_confirmed = False
        self._result_summary = self._classify_result()
        self._tlc_completion["learning_evidence"] = {
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "result_summary": asdict(self._result_summary),
            "final_snapshot": asdict(snapshot),
        }
        for event in pending_events:
            self._emit(event)
        if not self._persist_completion():
            return
        self._emit({
            "event_code": "LEARNING_EVIDENCE_CAPTURED",
            "event_message": f"eredmény: {self._result_summary.result}",
        })
        if self._confirm_final_charge_off(off_failures=off_failures) is None:
            return
        if self.pending_bq_df_restore is not None or not sealed_confirmed:
            self.emergency_stop("LEARNING_FINAL_RECHARGE_BQ_CLEANUP_UNCONFIRMED")
            return
        self._enter(LearningState.FINAL_RECHARGE)

    def _finish_learning(self) -> None:
        assert self._result_summary is not None
        done_event = {
            "event_code": "LEARNING_DONE",
            "event_message": f"eredmény: {self._result_summary.result}",
        }
        # R3-B3 (R3-GUI-1, DFAM-018): a kód semleges, a negatív jelentés az
        # eredményben van -- a producer minősít (GOV-024); VERIFIED: INFO marad.
        if self._result_summary.result != LEARNING_RESULT_VERIFIED:
            done_event["severity"] = "WARNING"
        self._emit(done_event)
        # CF-19 verifier P2 lelet -- l. emergency_stop() azonos hivatkozású
        # kommentje ugyanerről a hiányzó _enter() útvonalról.
        self._enter(LearningState.DONE)

    def _validate_final_charge_config(self) -> bool:
        max_current = self._profile.effective_max_charge_A
        hw_limit = _PSU_HW_MAX_CHARGE_A.get(self._safety.psu_mode, 0.0)
        if (
            not self._temp_dmm_valid or not isfinite(self._battery_temperature_C)
            or not isfinite(max_current) or min(max_current, hw_limit) < 0.67
            or self.persist_tlc_completion is None
        ):
            self.emergency_stop(
                "LEARNING_FINAL_RECHARGE_CONFIG_INVALID: fresh finite T, 0.67 A profile/PSU and checkpoint required",
            )
            return False
        return True

    def _persist_completion(self) -> bool:
        try:
            if self.persist_tlc_completion is None:
                raise RuntimeError("TLC completion checkpoint is not configured")
            self.persist_tlc_completion(self.tlc_completion)
        except Exception as exc:  # noqa: BLE001 - every failed checkpoint must block ON
            self.emergency_stop(f"TLC_COMPLETION_CHECKPOINT_FAILED:{exc}")
            return False
        return True

    def _command_final_charge_off(self) -> list[str]:
        """Load OFF -> PSU ALL OFF; az egyik hiba nem gátolja a másik kísérletet."""
        failures = []
        for name, off in (("Load", self._load.input_off), ("PSU", self._psu.all_outputs_off)):
            try:
                off()
            except Exception as exc:  # noqa: BLE001 - attempt both energy outputs
                failures.append(f"{name}_OFF:{exc}")
        return failures

    def _confirm_final_charge_off(self, *, off_failures: list[str] | None = None) -> dict | None:
        """Mindkét OFF-parancs és readback kötelező; mentés után is dönthet."""
        failures = self._command_final_charge_off() if off_failures is None else off_failures
        readings = {}
        for name, read, commanded in (
            ("Load", self._load.read_input_state, self._load.input_commanded_on),
            ("PSU", self._psu.read_output_state, self._psu.output_commanded_on),
        ):
            try:
                value = read()
                readings[name] = value
                if value is not False or commanded is not False:
                    failures.append(f"{name}_OFF_UNCONFIRMED:{value!r}/{commanded!r}")
            except Exception as exc:  # noqa: BLE001 - unknown output state blocks charge
                failures.append(f"{name}_READBACK:{exc}")
        if failures:
            self.emergency_stop("LEARNING_FINAL_RECHARGE_OFF_UNCONFIRMED: " + " | ".join(failures))
            return None
        return readings

    # ------------------------------------------------------------------ #
    # IT_ENABLE — SUP03-LEARN-004 2. lépés                                #
    # ------------------------------------------------------------------ #

    def _run_it_enable(self, _dt_s: float) -> None:
        # CF-19 verifier P3 lelet: a dt_s paraméter itt sem használt már
        # (a _phase_elapsed_s növelése advance()-ban központilag történik),
        # _ előtaggal jelölve -- a _run_verify_vok_qen()/_run_verify_
        # update_status() mintáját követve.
        """Az UNSEAL+[Cell BL temp-write]+IT_ENABLE pontosan EGYSZER, az
        állapotba lépés utáni ELSŐ tick-en megy ki (`_it_enable_sent`
        explicit flag, nem implicit időzítés-összehasonlítás) - utána a
        hívó csak `it_enable_settle_s`-et vár, mielőtt a VOK/QEN
        ellenőrzésre lép.

        Block 3 (2026-08-25, TI SLUSB55B STEP 8, 2. lépés — "Temporarily
        adjust the Cell BL Set Volt Threshold so as not to affect a full
        discharge" — a 3. lépés, IT_ENABLE ELŐTT): a Cell BL Set Volt
        Threshold eredeti értéke kiolvasásra, majd a konfigurált learning-
        temp értékre íródik, MÉG az IT_ENABLE előtt, ugyanabban az UNSEAL
        ablakban. A restore (STEP 10) a _restore_cell_bl_if_needed()-ben
        történik, minden terminális útvonalon (l. emergency_stop()/
        _run_final_snapshot())."""
        if not self._it_enable_sent:
            self._it_enable_sent = True
            try:
                self._bq_transport.unseal(confirm=True)
                df_service = Bq34z110DataFlashService(self._bq_transport)
                read_result = df_service.read_field("cell_bl_set_volt_mV")
                original_mV = read_result.value
                # Review-kör 2 (2026-09-08), Metrológia F-03 lelet (CF-40
                # P2-3 gyökér-megelőzése): a chipről olvasott EREDETI érték
                # visszaírhatóságát MÉG A TEMP-WRITE ELŐTT ellenőrizzük,
                # ugyanazzal az encode_df_field()-del, amit a restore is
                # használ (bq_data_flash_service.py::write_fields()) --
                # ha egy törölt/0xFF-fel teli blokk pl. -1-re dekódolódik
                # (ami a CF-40 Min=0 korlátját sérti), a restore a ciklus
                # VÉGÉN garantáltan ValueError-ral hiúsulna, a chip pedig
                # VÉGLEGESEN az ideiglenes learning-küszöbön ragadna. A
                # temp-write EL SEM INDUL, ha előre látható, hogy nem
                # állítható vissza -- `self._cell_bl_original_mV` NEM
                # íródik be, így `_restore_cell_bl_if_needed()` no-op
                # marad (nem próbál egy sosem-elvégzett temp-write-ot
                # visszaállítani).
                spec = df_service.get_field_spec("cell_bl_set_volt_mV")
                try:
                    encode_df_field(spec, original_mV)
                except ValueError as exc:
                    raise ControlledBqWriteError(
                        f"CELL_BL_ORIGINAL_VALUE_UNRESTORABLE: olvasott={original_mV} "
                        f"({exc})"
                    ) from exc
                self._cell_bl_original_mV = original_mV
                temp_mV = round(self._config.cell_bl_learning_temp_V_per_cell * 1000)
                write_report = df_service.write_fields({"cell_bl_set_volt_mV": temp_mV}, confirm=True)
                if not write_report.passed:
                    raise ControlledBqWriteError(
                        f"CELL_BL_TEMP_WRITE_FAILED: stopped_at={write_report.stopped_at}"
                    )
                self._bq_transport.it_enable(confirm=True)
            except TestAbortedByUser:
                raise
            except Exception as exc:
                # Best-effort visszazárás, mielőtt FAULT-ba lépünk - ugyanaz
                # az elv, mint a WP-AK-09 kalibrációs hiba-ágaiban (a BQ ne
                # maradjon UNSEALED állapotban egy sikertelen próbálkozás után).
                # emergency_stop() ezután _restore_cell_bl_if_needed()-en
                # keresztül külön megpróbálja visszaírni az eredeti Cell BL
                # értéket is, ha az már ki lett olvasva.
                # Review-kör 5, Architektúra A-1 (P1) -- l. PRECOND gate
                # azonos hivatkozású kommentje ugyanerről a csendben
                # elnyelt seal-hibáról.
                seal_event: dict | None = None
                try:
                    self._bq_transport.seal(confirm=True)
                except Exception as seal_exc:
                    self._quality_flags.append(
                        f"BQ_SEAL_RESTORE_FAILED_AT_IT_ENABLE:{seal_exc}"
                    )
                    seal_event = {
                        "event_code": "LEARNING_SEAL_RESTORE_FAILED",
                        "event_message": f"IT_ENABLE hiba után: {seal_exc}",
                    }
                self.emergency_stop(f"IT_ENABLE_FAILED: {exc}")
                if seal_event is not None:
                    self._emit(seal_event)
                return
        # CF-19: a _phase_elapsed_s növelése mostantól advance()-ban
        # központilag történik (l. ott a kommentet).
        if self._phase_elapsed_s >= self._config.it_enable_settle_s:
            self._it_enable_sent = False
            self._enter(LearningState.VERIFY_VOK_QEN)

    # ------------------------------------------------------------------ #
    # Cell BL restore — Block 3 (2026-08-25), TI SLUSB55B STEP 8 10. lépés #
    # ("Restore the Cell BL Set Volt Threshold to the desired value").    #
    # ------------------------------------------------------------------ #

    def _restore_cell_bl_if_needed(self, *, emit: Callable[[dict], None] | None = None) -> None:
        """A ténylegesen kiolvasott EREDETI Cell BL Set Volt Threshold
        értéket írja vissza (NEM egy feltételezett "normál" konstanst) —
        biztonságosabb, mint egy hardkódolt production-érték, mert nem
        tesz feltételezést arról, mi volt a chipen a learning indulása
        előtt. Idempotens (`_cell_bl_restore_done`), no-op, ha a temp-write
        sosem történt meg (`_cell_bl_original_mV is None`, pl. korai PRECOND
        FAULT esetén)."""
        emit_event = emit if emit is not None else self._emit
        if self._cell_bl_original_mV is None or self._cell_bl_restore_done:
            return
        self._cell_bl_restore_done = True
        try:
            self._bq_transport.unseal(confirm=True)
            df_service = Bq34z110DataFlashService(self._bq_transport)
            write_report = df_service.write_fields(
                {"cell_bl_set_volt_mV": self._cell_bl_original_mV}, confirm=True,
            )
            self._cell_bl_restore_confirmed = write_report.passed
            if not write_report.passed:
                self._quality_flags.append(
                    f"BL_THRESHOLD_RESTORE_FAILED(stopped_at={write_report.stopped_at})"
                )
                emit_event({
                    "event_code": "LEARNING_BL_THRESHOLD_RESTORE_FAILED",
                    "event_message": f"stopped_at={write_report.stopped_at}",
                })
        except Exception as exc:
            self._cell_bl_restore_confirmed = False
            self._quality_flags.append(f"BL_THRESHOLD_RESTORE_FAILED:{exc}")
            emit_event({
                "event_code": "LEARNING_BL_THRESHOLD_RESTORE_FAILED",
                "event_message": str(exc),
            })
        finally:
            # Review-kör 5, Architektúra A-1 (P1) -- l. PRECOND gate azonos
            # hivatkozású kommentje ugyanerről a csendben elnyelt
            # seal-hibáról.
            #
            # R17-05 (Kor 17 MAX-effort review, DFAM-032, 5. elofordulas):
            # KORABBAN itt bare self._bq_transport.seal(confirm=True) allt,
            # es _cell_bl_reseal_confirmed a nyers RPC SIKEREBOL (nem
            # megfigyelt SS bitbol) lett True -- a kommentje azt allitotta,
            # hogy a transport "sajat readback-ellenorzesevel ter vissza",
            # ami a Pico firmware egy SOURCE_REQUIRED, forrassal nem
            # igazolt allitasa volt (pico_bq_transport.py::seal() NEM
            # olvas vissza). Az UGYANEZEN OSZTALYON belul mar letezo,
            # SS-bit-tel tenylegesen ellenorzo _ensure_bq_sealed_or_flag()
            # helperre valtva -- ugyanaz a mintat koveti, mint az
            # EMERGENCY_STOP/FINAL_SNAPSHOT hivohelyek.
            self._ensure_bq_sealed_or_flag("CELL_BL_RESTORE", emit=emit_event)

    def _ensure_bq_sealed_or_flag(
        self, context: str, *, emit: Callable[[dict], None] | None = None,
    ) -> bool:
        """[SCF-011 újratervezés, 2026-09-11, felhasználói döntés]
        FALLBACK zárás -- a session-szintű életciklusban a chip a PRECOND
        unseal()-je óta UNSEALED, és a `_restore_cell_bl_if_needed()`
        session-végi seal()-je a NORMÁL út a visszazáráshoz. Ez a metódus
        azt a rést fedi, amikor a restore NEM fut (`_cell_bl_original_mV
        is None`, pl. FAULT a PRECOND gate-en belül, MIELŐTT az IT_ENABLE
        valaha beállította volna ezt a mezőt) -- ilyenkor a fenti restore
        no-op, tehát semmi nem próbálná visszazárni a chipet.

        Explicit SEAL-ÁLLAPOT VISSZAELLENŐRZÉSSEL zár (nem csak "küldtük a
        parancsot, reméljük működött"): `read_control_status()`-ból
        dekódolt SS bit (`ControlStatusBits.ss`, l. bq_diagnostics.py) --
        ha a chip MÁR SEALED (pl. a restore mégis lefutott, vagy sosem
        volt ténylegesen unsealed), ez egy ártalmatlan, gyors no-op.
        Visszatér: True, ha a végállapot igazoltan SEALED, False egyébként
        (ilyenkor `quality_flags`/`on_event` már rögzítette a hibát --
        a hívó felelőssége eldönteni, blokkolja-e emiatt a PASS-t).

        R17-05 (2026-09-16): ez a metódus MOSTANTÓL nem csak a fallback
        (restore-nem-futott) útra, hanem `_restore_cell_bl_if_needed()`
        NORMÁL, session-végi reseal-lépésére IS ez a kanonikus SS-bit-
        ellenőrző zárás -- MINDKÉT sikeres visszatérési ág (már-sealed
        korai-return, ÉS a seal()+utóellenőrzés sikere) beállítja
        `self._cell_bl_reseal_confirmed = True`-t, minden hibás ág
        hagyja `False`-on."""
        # MAX verifier P2 lelete (2026-09-16, R17-05): az ELŐZŐ verzió itt
        # `return False`-t adott, ha ez az ELSŐ olvasás (a "már sealed?"
        # gyors ellenőrzés) hibázott -- ez SOHA nem kísérelte meg a seal()-t,
        # miközben az EREDETI (Cell BL finally) kód FELTÉTEL NÉLKÜL próbálta
        # a seal()-t. Egy tranziens olvasási hiba (pl. Pico dropout) így a
        # chipet UNSEALED állapotban hagyta volna egy olyan esetben, amikor
        # a rákövetkező seal() RPC ténylegesen sikeres lett volna. JAVÍTVA:
        # a hiba rögzítve marad, DE a végrehajtás FOLYTATÓDIK a seal()
        # megkísérlésére (ugyanaz az ág, mint amikor az olvasás sikeres, de
        # cs.ss még False).
        emit_event = emit if emit is not None else self._emit
        try:
            cs = decode_control_status(self._bq_driver.read_control_status())
            if cs.ss:
                # R17-05 (Kor 17 MAX-effort review, DFAM-032): a hivo
                # (_restore_cell_bl_if_needed() finally-je) ezt a
                # SS-bit-tel MEGEROSITETT allapotot hasznalja
                # _cell_bl_reseal_confirmed-hez, NEM a nyers seal() RPC
                # sikeret.
                self._cell_bl_reseal_confirmed = True
                return True
        except Exception as exc:
            self._quality_flags.append(f"BQ_SEAL_STATE_VERIFY_FAILED_AT_{context}:{exc}")
            emit_event({
                "event_code": "LEARNING_SEAL_STATE_VERIFY_FAILED",
                "event_message": f"{context}: {exc}",
            })
        try:
            self._bq_transport.seal(confirm=True)
        except Exception as exc:
            self._quality_flags.append(f"BQ_SEAL_RESTORE_FAILED_AT_{context}:{exc}")
            emit_event({
                "event_code": "LEARNING_SEAL_RESTORE_FAILED",
                "event_message": f"{context} fallback seal: {exc}",
            })
            return False
        try:
            cs = decode_control_status(self._bq_driver.read_control_status())
        except Exception as exc:
            self._quality_flags.append(f"BQ_SEAL_STATE_VERIFY_FAILED_AT_{context}:{exc}")
            emit_event({
                "event_code": "LEARNING_SEAL_STATE_VERIFY_FAILED",
                "event_message": f"{context} post-seal verify: {exc}",
            })
            return False
        if not cs.ss:
            self._quality_flags.append(f"BQ_SEAL_RESTORE_FAILED_AT_{context}:seal command accepted but SS bit still clear")
            emit_event({
                "event_code": "LEARNING_SEAL_RESTORE_FAILED",
                "event_message": f"{context}: seal() lefutott, de SS bit még nem SEALED-et jelez",
            })
            return False
        self._cell_bl_reseal_confirmed = True
        return True

    def _run_verify_vok_qen(self, _dt_s: float) -> None:
        # CF-19: a _phase_elapsed_s növelése mostantól advance()-ban
        # központilag történik (l. ott a kommentet) -- a dt_s paraméter
        # itt már nem használt, _ előtaggal jelölve.
        if self._phase_elapsed_s < self._config.verify_settle_s:
            return
        try:
            cs = decode_control_status(self._bq_driver.read_control_status())
            self._vok_qen_observed = {"vok": bool(cs.vok), "qen": bool(cs.qen)}
            if not cs.qen:
                self._quality_flags.append("QEN_NOT_CONFIRMED_AFTER_IT_ENABLE")
                self._emit({
                    "event_code": "LEARNING_QEN_NOT_CONFIRMED",
                    "event_message": (
                        "QEN nem 1 az IT_ENABLE után - Qmax-frissítés nem lesz "
                        "engedélyezve, amíg ez nem változik"
                    ),
                })
            # Governance-v2 review-kör 1 (CF-42, VERIFIED_PRIMARY: TI-
            # SLUSB55B "IT ENABLE: 0x0021" -- "sets bit 2 of UpdateStatus
            # and causes the [VOK] and [QEN] flags to be set... [VOK] is
            # cleared if the voltages are not suitable for a Qmax
            # update"): a `self._vok_qen_observed` MÁR rögzíti a vok
            # értékét, de korábban semmilyen minőségi jelzés nem épült rá
            # -- ha a gauge AZONNAL, IT_ENABLE után törli a VOK-ot, ez
            # korai, MEGBÍZHATÓ előjelzés, hogy a több órás fizikai ciklus
            # végén NEM lesz Qmax-frissítés (a hiba korábban csak órákkal
            # később, a VERIFY_UPDATE_STATUS_05 kiértékelésénél derült ki).
            # Ugyanaz a konzervatív minta, mint a QEN-ellenőrzésnél --
            # NEM blokkol, csak explicit jelzést ad, l. CLAUDE.md §7
            # (quality kötelező, ha degradált/gyanús adat van).
            if not cs.vok:
                self._quality_flags.append("VOK_NOT_SET_AFTER_IT_ENABLE")
                self._emit({
                    "event_code": "LEARNING_VOK_NOT_SET_AFTER_IT_ENABLE",
                    "severity": "WARNING",  # R3-B3: semleges kód, producer minősít
                    "event_message": (
                        "VOK nem 1 közvetlenül az IT_ENABLE után - a gauge "
                        "szerint a jelenlegi feszültségek nem alkalmasak "
                        "Qmax-frissítésre, a fizikai ciklus végén valószínűleg "
                        "nem történik Qmax-tanulás"
                    ),
                })
        except TestAbortedByUser:
            raise
        except Exception as exc:
            self._vok_qen_observed = None
            self._quality_flags.append(f"VOK_QEN_READ_FAILED:{exc}")
        # [SCF-011 újratervezés, 2026-09-11] KORÁBBAN itt egy köztes seal()
        # zárta le az IT_ENABLE óta nyitott UNSEAL-ablakot -- session-
        # szintű életciklusra állítva a chip UNSEALED marad innentől is,
        # egészen a `_restore_cell_bl_if_needed()` session-végi seal()-jéig
        # (l. `_run_precond()` azonos hivatkozású kommentje). A DISCHARGE/
        # RELAX fázisok periodikus `_poll_bq_diagnostics_throttled()`
        # hívásai innentől megbízhatóan, UNSEALED állapotban futnak.
        self._begin_discharge(LearningState.DISCHARGE_1)

    # ------------------------------------------------------------------ #
    # DISCHARGE — inline C/10, `ChemIdCharacterizationController` C/20    #
    # mintáját követve (SUP03-LEARN-004 lépés 4/9). Az "üres" állapot     #
    # (SUP03-LEARN-005) itt: Q-34 kötött TLC-lépcsők, Q-35 egyszeri     #
    # lezáró minta; 1,70 V/cella pakkszintű alsó kapu.                    #
    # SCF-019 forrásszinten OPEN; projektszinten GOV-029 kezeli.           #
    # ------------------------------------------------------------------ #

    def _check_learning_discharge_floor(self, cutoff_V: float, *, first_crossing: bool = False) -> bool:
        # A kivétel lokális: a közös normál safety-primitív nem változik.
        if self._state in _TERMINAL_STATES:
            return False
        try:
            self._config.__post_init__()
        except ValueError:
            self.emergency_stop("DISCHARGE_CONFIG_INVALID")
            return False
        minimum_V = LEARNING_HARD_FLOOR_V_PER_CELL * self._profile.cell_count
        if not isfinite(cutoff_V) or cutoff_V < minimum_V:
            self.emergency_stop("DISCHARGE_CONFIG_INVALID")
            return False
        if not self._dmm_valid or not isfinite(self._u_batt):
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return False
        if self._u_batt < minimum_V and not first_crossing:
            message = (
                f"Q-34/Q-35: U_batt={self._u_batt:.6f} V; minimum={minimum_V:.6f} V; "
                f"cutoff={cutoff_V:.6f} V"
            )
            self.emergency_stop("BATTERY_UNDERVOLTAGE")
            # A minta diagnózisa is csak a BL/seal cleanup után kerülhet ki.
            self._emit({
                "event_code": "BATTERY_UNDERVOLTAGE", "event_message": message,
                "severity": "CRITICAL", "measured_V": self._u_batt,
                "minimum_V": minimum_V, "cutoff_V": cutoff_V,
            })
            return False
        # Minden lépcső első lezáró mintája kampánybizonyíték (Q-7/Q-32/Q-35).
        if first_crossing:
            below_hard_floor = self._u_batt < minimum_V
            message = (
                f"Q-34/Q-35: U_batt={self._u_batt:.6f} V; minimum={minimum_V:.6f} V; "
                f"cutoff={cutoff_V:.6f} V; overshoot={cutoff_V - self._u_batt:.6f} V; "
                f"below_hard_floor={below_hard_floor}"
            )
            try:
                logging.getLogger(__name__).warning(message)
                self._emit({
                    "event_code": "LEARNING_DISCHARGE_CUTOFF_OVERSHOOT",
                    "event_message": message, "severity": "WARNING",
                    "measured_V": self._u_batt, "minimum_V": minimum_V,
                    "cutoff_V": cutoff_V, "overshoot_V": cutoff_V - self._u_batt,
                    "below_hard_floor": below_hard_floor,
                })
            except BaseException:
                self.emergency_stop("DISCHARGE_OVERSHOOT_EVENT_FAILED")
                raise
        return True

    def _check_loaded_relax_sample(self, sample: RelaxSample) -> None:
        # A cutoff után fennmaradó terhelés minden nyers relaxmintája számít.
        if self._state == LearningState.FAULT or not self._load.input_commanded_on:
            return
        if sample.error is not None or sample.voltage_V is None:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        self._u_batt = sample.voltage_V
        self._safety.enforce_normal_discharge(
            sample.voltage_V, self._discharge_current_A, self._profile.terminate_voltage_pack_V,
            self.emergency_stop, self.on_event,
        )

    def _begin_discharge(self, state: LearningState) -> None:
        # R11-B4: a friss szuloi meres kapuja minden Load ON elott.
        overtemp = self._safety.check_battery_overtemperature(
            self._battery_temperature_C, self._temp_dmm_valid,
        )
        if overtemp.fault is not None:
            self.emergency_stop(overtemp.fault.name)
            return
        if state == LearningState.DISCHARGE_1:
            # Az IT/BQ-lépések után az első kisütéshez is új, időbélyegzett mérés kell.
            read_start = time.monotonic()
            reason = "CACHE_FRESH"
            try:
                voltage = self._dmm_v.read_voltage()
                if not isfinite(voltage):
                    raise ValueError("Nem véges DMM-feszültség")
            except Exception:  # noqa: BLE001 -- diagnosztizált kapumérési hiba
                reason = "READ_ERROR"
                self._relax_dmm_fault_total += 1
            age = max(0.0, time.monotonic() - read_start)
            if reason == "CACHE_FRESH" and age > 4.0:
                reason = "STALE"
            self._dmm_valid = reason == "CACHE_FRESH"
            if self._dmm_valid:
                self._u_batt = voltage
            if not self._dmm_valid:
                not_fresh_event = {
                    "event_code": "LEARNING_RELAX_DMM_NOT_FRESH",
                    "event_message": (
                        f"{self._state.value}: {reason}; "
                        f"sample_age_s={age}; read_start={read_start}"
                    ),
                    "dmm_reason": reason, "sample_age_s": age,
                    "read_start": read_start,
                }
                # A dobó callback sem előzheti meg a BL/seal visszaállítását.
                self.emergency_stop("DMM_FEEDBACK_LOST")
                self._emit(not_fresh_event)
                return
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        # R1 B3b (NS-02, DFAM-001): akkufeszultseg-kapu az energizalas ELOTT.
        # A meglevo safety-limit (SafetyManager.check_battery_voltage(), a
        # per-tick kapuval azonos fault-koddal) alkalmazasa mar az inditoban;
        # sorrend: DMM-or -> feszultsegkapu -> concurrent-kapu (a
        # _run_rebound_check() es az OCV IMPULSE_PREP meglevo sorrendje).
        v_result = self._safety.check_battery_voltage(self._u_batt)
        if v_result.fault is not None:
            self.emergency_stop(v_result.fault.name)
            return
        # Kör 20 Batch E13a (F20-S3, DFAM-001, a discharge_controller.py:550-556
        # es a lenti _run_rebound_check() (2203-2209. sor) mar meglevo mintajat
        # koveteve): a testver per-tick kapuk (l. _run_discharge() self._safety.
        # check_concurrent_psu_load() hivasa) a KOVETKEZO ticken futnak, a MAR
        # eletbe lepett input_commanded_on-t olvasva -- es mivel device_psu.py::
        # all_outputs_off() a sajat finally-jaban FELTETEL NELKUL output_
        # commanded_on=False-ra allitja, egy csendben sikertelen PSU-kikapcsolas
        # emiatt LATHATATLAN maradna ezeknek a per-tick kapuknak. A kapunak ezert
        # MIELOTT all_outputs_off() fut, elorejelzo modon (load_commanded_on=True
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
        cutoff_V = self._config.learning_discharge_floor_V_per_cell * self._profile.cell_count
        if not self._check_learning_discharge_floor(cutoff_V):
            return
        # A mérés/várakozás alatt érkező kérést még a Load-konfiguráció előtt nézzük.
        # A safety-fault elsőbbsége megmarad; az okfeloldás és a közös OFF a runneré.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return
        try:
            self._psu.all_outputs_off()
            self._load.set_mode_cc()
            self._load.set_current(self._discharge_current_A)
            self._load.input_on(stop_requested=self.handoff_stop_requested)
        except TestAbortedByUser:
            # A runneré az OFF és az ok; a kapacitásmérleg még nem nullázódhat.
            return
        except Exception as exc:
            self.emergency_stop(f"DISCHARGE_SETUP_FAILED: {exc}")
            return
        if state == LearningState.DISCHARGE_2:
            # DISCHARGE_1 kapacitása külön megőrizve, mielőtt az
            # integrátor a 2. ciklushoz nullázódik (SUP03-LEARN-008
            # nyomon-követhetőség — mindkét ciklus mért kapacitása
            # rögzített, nem csak az utolsó).
            # R1 B3 F-41 (DFAM-028/017): a másolás a sikeres input_on() UTÁN,
            # közvetlenül a lenti integrátor-reset ELŐTT fut, hogy egy korai
            # kilépés (DMM-őr, concurrent-kapu, setup-kivétel) ne számolja
            # kétszer a D1 Ah/Wh/extrém-dt adatait.
            self._discharge_1_capacity_Ah = self._discharge_integrator.accumulated_discharge_Ah
            self._discharge_1_capacity_Wh = self._discharge_integrator.accumulated_discharge_Wh
            # Teljes kódbázis review 12. kör (2026-08-31), Metrológia/
            # Adatintegritás P2 lelet — ugyanaz a "mentsd el, MIELŐTT
            # eldobnánk" minta, mint fent az Ah/Wh-nál, és mint a fb4e031
            # (11. kör) a PRECHARGE->RECHARGE töltési oldalon: DISCHARGE_1
            # extrém dt_s-adata elveszne, ha a DISCHARGE_2 saját (esetleg
            # tiszta) extreme_dt_s_count-ja fedi el a végső TestResult-ban
            # — l. _discharge_total_extreme_dt_s_count docstringje az
            # __init__-ben.
            self._discharge_total_extreme_dt_s_count += (
                self._discharge_integrator.extreme_dt_s_count
            )
            self._discharge_total_max_observed_dt_s = max(
                self._discharge_total_max_observed_dt_s,
                self._discharge_integrator.max_observed_dt_s,
            )
        self._discharge_integrator = Integrator()
        self._discharge_phase_elapsed_s = 0.0
        # 2026-08-24 — csak IGAZI (első) belépéskor reset: a rebound-retry
        # utáni folytatás _run_rebound_check()-ből közvetlenül self._enter()-
        # nel lép vissza ide, NEM ezen a helperen keresztül, hogy a cutoff/
        # próbaszámláló a ciklus egészére (nem próbánként) érvényes maradjon.
        # Block 3 (2026-08-25, felhasználói döntés): a kisütési floor a
        # LearningConfig.learning_discharge_floor_V_per_cell-ből származik
        # (a Cell BL ideiglenes csökkentéséhez igazított, mélyebb célérték),
        # NEM a profil normál FIAMM terminate_voltage_pack_V-jából — l.
        # LearningConfig docstring.
        self._discharge_cutoff_V = (
            self._config.learning_discharge_floor_V_per_cell * self._profile.cell_count
        )
        self._rebound_retry_count = 0
        self._learning_cutoff_seen = False
        self._enter(state)

    def _run_discharge(self, dt_s: float, next_state: LearningState) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        try:
            i_load = self._load.measure_current()
            self._i_load_measured_A = i_load
            self._load_current_readback_valid = True
        except Exception:
            self._load_current_readback_valid = False
            self.emergency_stop("LOAD_COMM_LOST")
            return
        self._discharge_integrator.add_sample(-abs(i_load), self._u_batt, dt_s)
        self._last_integration_source = "LOAD_READBACK"
        self._record_gauge_sample(dt_s)
        self._discharge_phase_elapsed_s += dt_s

        if self._discharge_phase_elapsed_s > self._safety.max_discharge_time_s:
            self.emergency_stop("MAX_DISCHARGE_TIME_REACHED")
            return
        # Teljes kódbázis review 13. kör (2026-08-31), Szoftver/Architektúra
        # P2 lelet — l. discharge_controller.py azonos fixjének docstringje:
        # a védendő C10-bázisú kisütési áram mellett ez a plafon korábban
        # nyers (C20-bázisú) nominal_capacity_Ah-t használt.
        if self._discharge_integrator.accumulated_discharge_Ah > (
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
            i_load, self._discharge_current_A
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

        if self._u_batt <= self._discharge_cutoff_V:
            try:
                self._load.input_off()
            except Exception as exc:
                # [review] a load.input_off() hibáját NEM szabad csendben
                # elnyelni és mégis REBOUND_CHECK-be lépni - a rebound-mérés
                # 0A feltételezést a lehetséges bekapcsolva-ragadt load
                # meghiúsítaná, és a rebound-érték hamis lenne. Ugyanaz az
                # elv, mint ChemIdCharacterizationController._run_c20_discharge().
                self.emergency_stop(f"DISCHARGE_LOAD_OFF_FAILED: {exc}")
                return
            self._enter(next_state)

    # ------------------------------------------------------------------ #
    # REBOUND CHECK — Folyamatok/tervek/BQ34Z110_learning_rebound_        #
    # kisutes.txt, forrás: TI SLUSB55B "STEP 8: Run an Optimization       #
    # Cycle". A terhelés levétele után NEM azonnal RELAX-ba lépünk: meg   #
    # kell várni a feszültség-visszapattanást, és ha az túl magasra áll   #
    # vissza a rebound-célhoz (target_V) képest, a terhelést vissza kell  #
    # kapcsolni és tovább kell süteni, alacsonyabb cutoff-küszöbbel —     #
    # addig ismételve, amíg a rebound a felső korlát alá nem esik         #
    # (`rebound_V <= target_V + rebound_tolerance_V`: egyoldalú felső     #
    # korlát, NEM ± sáv -- a cél alatti rebound is elfogadott), vagy a    #
    # próbaszám el nem éri max_rebound_retries-t (utóbbi esetben NEM      #
    # safety-fault — csak degraded quality_flags).                        #
    # R1 F-33 (csak komment-pontosítás; a működés Q-8 nyitott): target_V  #
    # = LearningConfig.learning_rebound_target_V_per_cell × cellaszám     #
    # (felhasználói számpolicy, D10) -- NEM a profil/DF Cell Termination  #
    # Voltage-ából olvasva. A konfigurációs mező profil- és DF-független  #
    # ALAPÉRTÉKE 1.80 V/cella; a TI SLUSB55B STEP 8 példa CTV-jével és a  #
    # két gyári FIAMM profil-YAML terminate_voltage_per_cell_V-jével      #
    # (1.80) való számegyezés csak erre az alapértékre igaz -- eltérő     #
    # profilnál vagy felülírt mezőnél a kettő szétválhat.                 #
    # Q-34/Q-35: a retry a kötött lépcsősorból választ; nincs clamp.      #
    # A lezáró minta egyszeri engedélye csak sikeres új Load ON-nál újul.  #
    # ------------------------------------------------------------------ #

    def _run_rebound_check(
        self, dt_s: float, discharge_state: LearningState, final_state: LearningState,
    ) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        # Teljes kódbázis review 2. kör (2026-08-28), Metrológia/
        # Adatintegritás P1 lelet: a testvér _run_discharge() (fentebb) MÁR
        # kapott check_battery_voltage() (fordított polaritás/ésszerűtlen
        # feszültség, CLAUDE.md §10.6/SAFE-009) ellenőrzést az 1. körben —
        # a REBOUND_CHECK fázis kimaradt. Egy csendben félreolvasott (nem
        # kivételt dobó) negatív/ésszerűtlen feszültség itt hamis
        # REBOUND_ACCEPTED-et VAGY indokolatlan Load input_on()-t (további
        # kisütési kör) eredményezhetett volna egy megbízhatatlan mérésre
        # alapozva — pont azon az egyetlen ponton, ahol a Load újra-
        # aktiválásáról döntünk.
        v_result = self._safety.check_battery_voltage(self._u_batt)
        if v_result.fault is not None:
            self.emergency_stop(v_result.fault.name)
            return
        # Teljes kódbázis review 3. kör (2026-08-29), Session/Workflow P2
        # lelet: a testvér _run_discharge() (fentebb) MÁR kapott
        # check_concurrent_psu_load()-ot (CLAUDE.md §10.2) — a REBOUND_CHECK
        # fázisból ez kimaradt. A REBOUND_CHECK a Load-ot retry esetén
        # (lentebb) ÚJRA bekapcsolja, és a settle-várakozás (akár
        # rebound_settle_s, retry-nkénti) teljes ideje alatt korábban nem
        # ellenőriztük újra a PSU/Load egyidejű aktivitást.
        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        # CF-19: a _phase_elapsed_s növelése mostantól advance()-ban
        # központilag történik (l. ott a kommentet).
        self._record_gauge_sample(dt_s)
        if self._phase_elapsed_s < self._config.rebound_settle_s:
            return

        rebound_V = self._u_batt
        # Block 3 (2026-08-25) + D10 (2026-09-15), felhasználói döntések;
        # R1 F-33 (csak komment-pontosítás): a rebound-cél a
        # LearningConfig.learning_rebound_target_V_per_cell × cellaszám
        # (a mező profil- és DF-független ALAPÉRTÉKE 1.80 V/cella) -- NEM
        # a Cell BL-hez igazított, és NEM a profil/DF Cell Termination
        # Voltage-ából olvasott érték (Q-8 nyitott). Az elfogadás egyoldalú
        # felső korlát (rebound_V <= target_V + tol_V), nem ± sáv -- l. a
        # REBOUND CHECK fejlécet.
        target_V = self._config.learning_rebound_target_V_per_cell * self._profile.cell_count
        tol_V = self._config.rebound_tolerance_V

        if rebound_V <= target_V + tol_V:
            if self._rebound_retry_count > 0:
                # Csak akkor quality_flags-bejegyzés, ha volt legalább 1
                # sikertelen próba — a tiszta, első próbára elfogadott
                # rebound (retries=0) NOMINÁLIS eredmény, nem degradáció
                # (l. _classify_result(): LEARNING_RESULT_VERIFIED üres
                # quality_flags-et követel meg).
                self._quality_flags.append(
                    f"REBOUND_ACCEPTED_AFTER_RETRY_AT_{discharge_state.value}"
                    f"(target={target_V:.3f}V,tol={tol_V:.3f}V,measured={rebound_V:.3f}V,"
                    f"retries={self._rebound_retry_count})"
                )
            accepted_event = {
                "event_code": "LEARNING_REBOUND_ACCEPTED",
                "event_message": (
                    f"{discharge_state.value}: rebound {rebound_V:.3f}V <= cél "
                    f"{target_V:.3f}V+{tol_V:.3f}V felső korlát ({self._rebound_retry_count} "
                    f"próba után) — RELAX következik."
                ),
            }
            # R4-B1 (DFAM-018): a kód semleges, retry > 0 esetén a fenti quality
            # flag miatt nem lehet VERIFIED -- a producer minősít (GOV-024);
            # retry == 0: INFO marad.
            if self._rebound_retry_count > 0:
                accepted_event["severity"] = "WARNING"
            self._emit(accepted_event)
            if discharge_state == LearningState.DISCHARGE_1:
                self._rebound_retries_1 = self._rebound_retry_count
            else:
                self._rebound_retries_2 = self._rebound_retry_count
            self._enter(final_state)
            return

        try:
            self._config.__post_init__()
        except ValueError:
            self.emergency_stop("DISCHARGE_CONFIG_INVALID")
            return
        self._rebound_retry_count += 1
        if self._rebound_retry_count > self._config.max_rebound_retries:
            self._quality_flags.append(
                f"REBOUND_MAX_RETRIES_EXCEEDED_AT_{discharge_state.value}"
                f"(target={target_V:.3f}V,tol={tol_V:.3f}V,measured={rebound_V:.3f}V,"
                f"retries={self._rebound_retry_count - 1})"
            )
            self._emit({
                "event_code": "LEARNING_REBOUND_MAX_RETRIES_EXCEEDED",
                "event_message": (
                    f"{discharge_state.value}: rebound {rebound_V:.3f}V a "
                    f"{self._config.max_rebound_retries}. próba után is > cél "
                    f"{target_V:.3f}V+{tol_V:.3f}V felső korlát — folytatás "
                    f"degraded minőségi jelzővel, végső klasszifikáció ezt tükrözni fogja."
                ),
            })
            if discharge_state == LearningState.DISCHARGE_1:
                self._rebound_retries_1 = self._rebound_retry_count - 1
            else:
                self._rebound_retries_2 = self._rebound_retry_count - 1
            self._enter(final_state)
            return

        previous_cutoff_V = self._discharge_cutoff_V
        self._discharge_cutoff_V = (
            LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL[self._rebound_retry_count] * self._profile.cell_count
        )
        if not self._check_learning_discharge_floor(self._discharge_cutoff_V):
            return
        # Teljes kódbázis review 15. kör (2026-09-04), Hardver/Műszervezérlés
        # P1 lelet: a fenti (a függvény ELEJÉN futó) kölcsönös kizárás
        # ellenőrzés a JELENLEGI load_commanded_on-t nézi, ami EBBEN a
        # tickben, az input_on() ELŐTT (a Load a cutoff-detekció óta ki
        # van kapcsolva) garantáltan False — a check emiatt sosem tud
        # tüzelni egy PSU-oldali stale flag ellen PONT ebben a retry-
        # ágban. A 14. kör `499fa03` fixének mintáját követve: explicit,
        # előrejelző ellenőrzés input_on() ELŐTT, load_commanded_on=
        # True-t szimulálva.
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
        # R16-B12: STOP esetén a függő retry nem fogyhat el.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            self._rebound_retry_count -= 1
            self._discharge_cutoff_V = previous_cutoff_V
            return
        try:
            self._load.input_on(stop_requested=self.handoff_stop_requested)
        except TestAbortedByUser:
            # A megszakított folytatás nem fogyaszt rebound-próbát.
            self._rebound_retry_count -= 1
            self._discharge_cutoff_V = previous_cutoff_V
            return
        except Exception as exc:
            self.emergency_stop(f"DISCHARGE_RESUME_AFTER_REBOUND_FAILED: {exc}")
            return
        self._learning_cutoff_seen = False
        # Csak a sikeresen parancsolt folytatást naplózzuk.
        self._emit({
            "event_code": "LEARNING_REBOUND_TOO_HIGH",
            "event_message": (
                f"{discharge_state.value}: rebound {rebound_V:.3f}V > cél "
                f"{target_V:.3f}V+{tol_V:.3f}V — folytatás, új cutoff "
                f"{self._discharge_cutoff_V:.3f}V ({self._rebound_retry_count}/"
                f"{self._config.max_rebound_retries}. próba)"
            ),
        })
        # Explicit self._enter() (NEM _begin_discharge()/_transition_to()):
        # a cutoff/próbaszámláló és a folyamatban lévő _discharge_integrator
        # a ciklus egészére érvényes marad, nem nullázódik újra.
        self._enter(discharge_state)

    # ------------------------------------------------------------------ #
    # VERIFY UPDATE STATUS — SUP03-LEARN-004 lépés 6/11,                  #
    # SUP03-LEARN-007 (nem kézi, nem feltételezett)                       #
    # ------------------------------------------------------------------ #

    def _run_verify_update_status(
        self, _dt_s: float, is_final: bool, next_state: LearningState,
    ) -> None:
        # CF-19: a _phase_elapsed_s növelése mostantól advance()-ban
        # központilag történik -- a dt_s paraméter itt már nem használt,
        # _ előtaggal jelölve.
        if self._phase_elapsed_s < self._config.verify_settle_s:
            return
        try:
            status = self._bq_driver.read_update_status()
        except TestAbortedByUser:
            raise
        except Exception as exc:
            status = None
            self._quality_flags.append(
                f"UPDATE_STATUS_READ_FAILED_AT_{'FINAL' if is_final else '0X05'}:{exc}"
            )
        # R1 B4 (F-10(a)): pontos egyezés a SLUSB55B Table 11/20 szerinti
        # 0x05/0x06 értékekkel -- a korábbi `<` kapun 0x07..0xFF is átjutott.
        if is_final:
            self._update_status_final = status
            if status is None or status != UPDATE_STATUS_FULL_LEARNING:
                self._quality_flags.append(f"UPDATE_STATUS_NOT_0X06_AT_FINAL_CHECK(got={status})")
        else:
            self._update_status_at_0x05_check = status
            if status is None or status not in (UPDATE_STATUS_FIRST_QMAX_UPDATE, UPDATE_STATUS_FULL_LEARNING):
                self._quality_flags.append(f"UPDATE_STATUS_NOT_0X05_AT_CHECKPOINT(got={status})")
            try:
                self._qmax_after_first_update = self._bq_driver.read_qmax()
            except TestAbortedByUser:
                raise
            except Exception as exc:
                # Kör 20 Batch E13b (SW20-4, DFAM-001+DFAM-018): korábban ez
                # a Qmax-olvasás CSENDBEN nyelte el a hibát, mikozben a 11
                # sorral feljebb lévő testvér (read_update_status()) UGYANEZT
                # a 0x05-checkpointot egy explicit quality_flag-gel (l.
                # UPDATE_STATUS_READ_FAILED_AT_0X05) jelzi. A fenti
                # _classify_result() (2377. sor: `and not self._quality_flags`)
                # miatt ez a flag legrosszabb esetben VERIFIED->AWAITING
                # atmenetet okoz (NEM FAILED-et) -- pontosan ugyanaz a
                # "bizonytalan != bizonyítottan sikertelen" elv, amit a
                # sibling UPDATE_STATUS_READ_FAILED_AT_* flag mar kovet.
                self._quality_flags.append(
                    f"QMAX_READ_FAILED_AT_0X05_CHECKPOINT:{exc}"
                )
        self._enter(next_state)

    # ------------------------------------------------------------------ #
    # SUP03-LEARN-010 klasszifikáció                                      #
    # ------------------------------------------------------------------ #

    def _classify_result(self) -> LearningResultSummary:
        # Teljes kódbázis review 3. kör (2026-08-29), Metrológia/Adatintegritás
        # P1 lelet: korábban `self._profile.nominal_capacity_Ah * 1000.0`-t
        # (FG-sorozatnál C20 alapú) számított újra, holott a BatteryProfile
        # MÁR exponál egy helyes, C10-alapú `design_capacity_mAh` property-t
        # (l. ott a docstringet — "a Design Capacity a C10 kapacitásból, NEM
        # a nominal_capacity_Ah-ból származik"), amit a bq_consistency_gate.py
        # Block 2 provisioning-konzisztencia kapuja MÁR helyesen használ.
        # CLAUDE.md §11.1: "A C10 és C20 nem cserélhető fel."
        design_capacity_mAh = float(self._profile.design_capacity_mAh)
        # Teljes kódbázis review 6. kör (2026-08-29), Session/Workflow P2
        # lelet #2: korábban `self._discharge_integrator.accumulated_
        # discharge_Ah`-t (az ÉLŐ, csak az UTOLSÓ ciklus integrátorát)
        # használta — ha DISCHARGE_2 elindult, ez KIZÁRÓLAG a 2. ciklus
        # kapacitását adta, a discharge_1_capacity_Ah-t figyelmen kívül
        # hagyva. A helyes, MINDKÉT ciklust összegző property
        # (accumulated_discharge_Ah, 389-393. sor) már létezett, de ez a
        # metódus nem ezt olvasta. Felhasználó-látható hatás: report.json
        # measured_capacity_Ah mezője hamisan alacsony (csak DISCHARGE_2)
        # volt egy teljes DISCHARGE_1+DISCHARGE_2 ciklusú session után.
        measured_capacity_Ah = self.accumulated_discharge_Ah
        # R1 B4 (F-10(b)/NS-11): tartományon kívüli Qmax (pl. 0xFFFF -> -1)
        # nem Qmax-frissülési bizonyíték, és belőle százalék sem képezhető.
        qmax_pair_in_ti_range = (
            _qmax_within_ti_range(self._qmax_before) and _qmax_within_ti_range(self._qmax_final)
        )
        qmax_change_percent = None
        if qmax_pair_in_ti_range and self._qmax_before and self._qmax_final:
            qmax_change_percent = (
                (self._qmax_final - self._qmax_before) / self._qmax_before * 100.0
            )

        # R1 B4 (F-10(a)): pontosan 0x06 (korábban `>= 0x06`). None -> False.
        update_status_ok = self._update_status_final == UPDATE_STATUS_FULL_LEARNING
        qmax_update_evidenced = (
            qmax_pair_in_ti_range and self._qmax_final != self._qmax_before
        )
        # Q-4 (2026-11-07): a kezdeti 0x06 nem bizonyít új tanulást.
        update_status_initial: int | None = None
        if self._initial_snapshot is not None and self._initial_snapshot.update_status.valid:
            update_status_initial = self._initial_snapshot.update_status.value
        if update_status_initial == UPDATE_STATUS_FULL_LEARNING:
            flag = "UPDATE_STATUS_ALREADY_0X06_AT_INITIAL_SNAPSHOT"
            if flag not in self._quality_flags:
                self._quality_flags.append(flag)
        ra_update_evidenced = bool(self._ra_before) and bool(self._ra_final) and (
            list(self._ra_before) != list(self._ra_final)
        )
        vok_qen_consistent = bool(self._vok_qen_observed and self._vok_qen_observed.get("qen"))
        no_safety_fault = self._state != LearningState.FAULT

        # Teljes kódbázis review 2. kör (2026-08-28), Session/Workflow P1
        # lelet: korábban `no_safety_fault` csak az 1. és 2. ágban lett
        # ellenőrizve — a 3. ág (`not (qmax_update_evidenced and ...)`)
        # FÜGGETLEN volt tőle. Egy korai (pl. DISCHARGE_1 alatti safety) FAULT
        # után qmax/ra tipikusan még None (nem "evidenced"), tehát a 3. ág
        # futott le, és a MEG SEM KEZDETT/be nem fejezett fizikai ciklus
        # tévesen LEARNING_RESULT_AWAITING ("PHYSICAL_CYCLE_COMPLETED_
        # AWAITING_BQ_VERIFICATION") lett — holott CLAUDE.md §2 szerint ez
        # KIZÁRÓLAG a BEFEJEZETT fizikai ciklusra vonatkozó, elfogadható
        # automatikus eredmény. A safety-fault ellenőrzést most ELSŐKÉNT,
        # az összes evidence-ágtól függetlenül végezzük — ugyanaz a minta,
        # mint a (jelenleg nem bekötött, de referenciaként megtartott)
        # bq_learning_verification.py::evaluate_learning_result()-ben.
        if not no_safety_fault:
            # Review-kör 5 (2026-09-09), Session/Workflow S5-01 (P2, NEW_
            # FAMILY abort_reason_conflated_with_measured_failure): a
            # LearningState.FAULT-ba NEM csak valódi safety/mérési hiba
            # visz -- a runner egy operátori Stop-ot vagy egy runner-szintű,
            # a BQ-tanulással összefüggéstelen kivételt/SCPI-hibát is
            # emergency_stop()-on keresztül zár le. Korábban mindhárom eset
            # egyaránt LEARNING_RESULT_FAILED-et ("BQ_LEARNING_FAILED")
            # adott -- egy egyszerű operátori megszakítást vagy egy
            # lemez-tele hibát tévesen BIZONYÍTOTT BQ-tanulási kudarcként
            # állítva be a riportban (CLAUDE.md §7/§11.6 "ne állíts többet,
            # mint amit az adat alátámaszt").
            #
            # KORREKCIÓ (2026-09-15, C-09/DFAM-025, 2. előfordulás): az
            # eredeti fix egy string-prefix whitelistet (USER_STOP_
            # REQUESTED / UNHANDLED_EXCEPTION:) használt a self._fault_
            # reason-ön, de a runner ÖT különböző szabad szöveges reasont
            # ad át (USER_EMERGENCY_STOP, STEP_FAULT, SCPI_CRITICAL_
            # ERROR[...], UNHANDLED_EXCEPTION, USER_STOP_REQUESTED) -- a
            # whitelist csak 2-t ismert, a másik 3 HAMISAN "BQ_LEARNING_
            # FAILED"-et adott egy egyébként hibátlan, csak megszakított
            # cikluson. Most egy TIPIZÁLT jelzést használunk
            # (self.runner_abort_origin, l. __init__ docstring), amit
            # test_runner.py::_abort_active_controller() állít be a
            # controller emergency_stop()-jának hívása ELŐTT -- ez
            # egyetlen igazságforrás, nem duplikált string-dispatch, és
            # automatikusan lefedi a runner MINDEN jövőbeli reasonját.
            # Csak a self.runner_abort_origin=None esetén (a controller
            # SAJÁT safety-/mérési logikája hívta ki a FAULT-ot, a runner
            # NEM avatkozott közbe) marad FAILED.
            if self.runner_abort_origin is not None:
                result = LEARNING_RESULT_AWAITING
                self._quality_flags.append(
                    f"ABORTED_BY_RUNNER({self.runner_abort_origin})"
                )
            else:
                result = LEARNING_RESULT_FAILED
        elif (
            update_status_ok and qmax_update_evidenced and ra_update_evidenced
            and vok_qen_consistent and not self._quality_flags
        ):
            result = LEARNING_RESULT_VERIFIED
        elif update_status_ok is False:
            # Teljes kódbázis review 9. kör (2026-08-30), Session/Workflow P2
            # lelet: korábban self._update_status_final is None (a végső
            # UpdateStatus OLVASÁSA hiúsult meg, l. _run_verify_update_status()
            # kivétel-ága) esetén FAILED-et adott — összekeverve egy múló BQ
            # soros-port kommunikációs hibát egy VALÓDI, sikeresen leolvasott,
            # 0x06 alatti UpdateStatus-értékkel (ami tényleg AWAITING). Egy
            # olvasási hiba NEM bizonyítja a BQ-tanulás kudarcát (CLAUDE.md
            # §2/§7: nem állíthatunk többet, mint amit az adat alátámaszt —
            # itt sem sikert, sem kudarcot). A UPDATE_STATUS_READ_FAILED_
            # AT_FINAL quality_flag már jelzi az inkonkluzív jelleget, a
            # klasszifikáció mindkét esetben AWAITING.
            result = LEARNING_RESULT_AWAITING
        elif not (qmax_update_evidenced and ra_update_evidenced and vok_qen_consistent):
            result = LEARNING_RESULT_AWAITING
        else:
            # Teljes kódbázis review 11. kör (2026-08-31), Session/Workflow
            # P1 lelet: ez az ág a fenti boolean-szerkezet miatt KIZÁRÓLAG
            # akkor érhető el, ha update_status_ok ÉS qmax_update_evidenced
            # ÉS ra_update_evidenced ÉS vok_qen_consistent MIND igaz — azaz
            # minden hard BQ/fizikai bizonyíték pozitív —, és a klasszifikáció
            # egyetlen oka, hogy nem VERIFIED, az, hogy self._quality_flags
            # NEM üres. A quality_flags viszont jóindulatú (nem-kudarc)
            # bejegyzésekkel is megtelhet minden hard evidence teljesülése
            # mellett (pl. REBOUND_ACCEPTED_AFTER_RETRY_AT_* — a controller
            # saját dokumentációja szerint "NEM safety-fault, csak degraded
            # quality_flags, a fizikai ciklus biztonságosan lezárul"). Korábban
            # ez FAILED-et ("BQ_LEARNING_FAILED") adott — egy ténylegesen
            # sikeres, csak degradált minőségi jelzőt tartalmazó fizikai+BQ
            # ciklust tévesen bizonyított kudarcnak minősítve, ugyanaz a
            # hibaosztály, mint a 9. kör fent (elif update_status_ok is
            # False) már javított olvasási-hiba esete — az akkor bevezetett
            # "bizonytalan ≠ bizonyítottan sikertelen" elv itt nem lett
            # konzisztensen alkalmazva. CLAUDE.md §7/§11.6: ne állíts többet,
            # mint amit az adat alátámaszt — itt a kudarc felé történő
            # túlállítás. Valódi FAILED-nek továbbra is csak az `if not
            # no_safety_fault` (biztonsági FAULT) ág marad.
            result = LEARNING_RESULT_AWAITING

        # Teljes kódbázis review 5. kör (2026-08-29), Session/Workflow P2
        # lelet: discharge_2_capacity_Ah korábban feltétel nélkül az ÉLŐ
        # self._discharge_integrator.accumulated_discharge_Ah-ból töltődött
        # — ha egy FAULT a DISCHARGE_1 közben/után, de a DISCHARGE_2
        # ténylegesen INDÍTÁSA (_begin_discharge(DISCHARGE_2), ami
        # self._discharge_integrator = Integrator()-t hív) ELŐTT történt,
        # self._discharge_1_capacity_Ah MÉG None (csak _begin_discharge()-
        # ben íródik), DE discharge_2_capacity_Ah TÉVESEN az 1. ciklus
        # Ah-ját mutatta volna — felcserélt mezőcímkézés a report.json-ban,
        # pont a hiba utáni elemzésnél a legfontosabb pillanatban
        # (CLAUDE.md §13 "a riport... measurement limitation" elkülönítése,
        # §12 "setpoint/readback/derived mezők egyértelmű elkülönítése"
        # szelleme). Ha a 2. ciklus MÉG nem indult el, az élő integrátor
        # értéke az 1. ciklushoz tartozik.
        if self._discharge_1_capacity_Ah is None:
            discharge_1_capacity_Ah = self._discharge_integrator.accumulated_discharge_Ah
            discharge_2_capacity_Ah: float | None = None
        else:
            discharge_1_capacity_Ah = self._discharge_1_capacity_Ah
            discharge_2_capacity_Ah = self._discharge_integrator.accumulated_discharge_Ah

        return LearningResultSummary(
            result=result,
            quality_flags=tuple(self._quality_flags),
            update_status_initial=update_status_initial,
            update_status_at_0x05_check=self._update_status_at_0x05_check,
            update_status_final=self._update_status_final,
            qmax_before=self._qmax_before,
            qmax_after_first_update=self._qmax_after_first_update,
            qmax_final=self._qmax_final,
            design_capacity_mAh=design_capacity_mAh,
            measured_capacity_Ah=measured_capacity_Ah,
            discharge_1_capacity_Ah=discharge_1_capacity_Ah,
            discharge_2_capacity_Ah=discharge_2_capacity_Ah,
            rebound_retries_1=self._rebound_retries_1,
            rebound_retries_2=self._rebound_retries_2,
            qmax_change_percent=qmax_change_percent,
            ra_before=self._ra_before,
            ra_final=self._ra_final,
            vok_qen_observed_after_it_enable=self._vok_qen_observed,
            cell_bl_original_mV=self._cell_bl_original_mV,
            cell_bl_restore_confirmed=self._cell_bl_restore_confirmed,
        )

    # ------------------------------------------------------------------ #
    # Gauge log — SUP03-LEARN-002                                         #
    # ------------------------------------------------------------------ #

    def _record_gauge_sample(self, dt_s: float) -> None:
        # Review 2026-09-15 (C-01/DFAM-027, P0) -- l. `_bq_poll_extra_s_to_
        # exclude_from_throttle` docstring az __init__-ben: a KÜSZÖB-
        # AKKUMULÁTORBA csak a dt_s azon része kerül, ami NEM a legutóbbi
        # poll saját ideje volt. Ez KIZÁRÓLAG ezt a helyi accumulátort
        # érinti -- a `dt_s` paraméter maga VÁLTOZATLAN marad minden más
        # célra (Ah/Wh-integráció, fázis-időzítők a hívó oldalán).
        # R16-B10F4: megszakadt nyugta utan az OFF elott nincs uj BQ-I/O.
        # A mar konyvelt mintat es energiat megorizzuk; az OFF a runnere.
        if (
            isinstance(self.handoff_stop_requested, ActivationStopHook)
            and self.handoff_stop_requested.activation_interrupted
        ):
            return

        effective_dt_s = max(dt_s - self._bq_poll_extra_s_to_exclude_from_throttle, 0.0)
        self._bq_poll_extra_s_to_exclude_from_throttle = 0.0
        self._log_elapsed_s += effective_dt_s
        if self._log_elapsed_s < self._config.sample_interval_s:
            return
        self._log_elapsed_s = 0.0
        poll_start_t = time.perf_counter()
        try:
            snapshot = self._poll_bq_diagnostics_throttled()
        finally:
            # MAX verifier V-7 lelete (2026-09-15): `poll_bq_diagnostics()`
            # transporthibáit mezőnként minősíti; a vezérlési abort továbbterjed.
            # A `finally` védi a részleges időkönyvelést is -- ha
            # kivételt dobna, a poll RÉSZLEGES ideje akkor is kizáródik a
            # KÖVETKEZŐ hívás akkumulációjából, ugyanazzal a mintával, mint
            # a `_poll_bq_diagnostics_throttled()` saját max_retries-
            # visszaállítása.
            self._bq_poll_extra_s_to_exclude_from_throttle = time.perf_counter() - poll_start_t
        if self.on_gauge_sample is not None:
            self.on_gauge_sample(snapshot)
        self._gauge_log.append({
            "state": self._state.value,
            "u_batt_V": self._u_batt,
            "snapshot": snapshot,
        })
        self._check_bq_transport_escalation(snapshot)

    def _emit_gauge_sample_best_effort(self, snapshot: BqDiagnosticsSnapshot) -> None:
        """Kor 18, Batch C7 R17-16 (DFAM-021+DFAM-001): a DONTO (verdikt-
        kritikus) snapshotok -- INITIAL_SNAPSHOT/FINAL_SNAPSHOT -- korabban
        kizarolag a controller belso mezojebe kerultek: sem az elo BQ-
        diagnosztika nezetbe, sem a bq_diagnostics.csv-be nem jutottak el,
        tehat a learning-verdikt bizonyitéka nem volt rekonstrualhato a
        session-mappabol. A periodikus poll-ut (_record_gauge_sample())
        MAR eddig is emittalt.

        BEST-EFFORT, SZANDEKOSAN guardolt -- a periodikus uttal ELLENTETBEN:
        ez idozites-erzekeny BQ-tranzakcios pontokon (IT_ENABLE-elotti
        baseline / a zaro Cell BL restore ELOTTI, meg UNSEALED allapot)
        fut, es egy GUI-callback kivetele SOHA nem szakithatja meg a
        learning-folyamatot, sem nem maszkolhatja az eredeti hibat
        (DFAM-033). A periodikus ag guard NELKUL marad -- az egy laza
        tick-hatar, nem tranzakcios pont; ez az aszimmetria TUDATOS, nem
        elnezes. NEM bovíti a _gauge_log-ot -- a listanak MA NINCS
        fogyasztoja (l. gauge_log property dokumentalt "production/teszt
        fogyaszto jelenleg nincs" allitasat, es DEFECT_FAMILY_REGISTRY.
        yaml DFAM-026 evidence-jet, Review-kor 4 F5), ezert bovitese holt
        allapotot novelne es szeleseitene a diffet indokolatlanul
        (CLAUDE.md 10. szakasz) -- NEM azert, mert riport/verdikt-logika
        olvasna (az MAS csatornan, a LearningResultSummary property-n
        keresztul jut a report.json-ba)."""
        if self.on_gauge_sample is None:
            return
        try:
            self.on_gauge_sample(snapshot)
        except Exception:  # noqa: BLE001 - l. a docstring DFAM-033 indoklasat
            pass

    def _check_bq_transport_escalation(self, snapshot: BqDiagnosticsSnapshot) -> None:
        """HIL-D5 (2026-09-14, CF-10a/DFAM-027, CLAIM_LEDGER.yaml PICO-
        RECONNECT-001): egy valós Pico-kiesés (kábel-kihúzás/power-cycle)
        SOHA nem áll helyre magától a puszta ismételt próbálkozástól — a
        `LearningConfig.bq_transport_unreachable_after_n_polls` (PROJECT_
        POLICY, k=3) egymást követő TELJESEN érvénytelen poll után explicit
        eseményt ad, hogy az operátor ne maradjon tudatlanul egy tartós
        kiesésről a hátralévő (akár ~30 órás) fizikai ciklus alatt. Ez a
        fizikai ciklust NEM állítja le (a Load/PSU vezérlés a DMM-alapú
        útvonalon fut, ettől független) — csak jelzés, hogy a BQ-learning
        eredmény ettől a ponttól kétséges lehet.

        MELLÉKHATÁS a végső klasszifikációra (MAX verifier 2026-09-14
        F-2 megjegyzése): a `_quality_flags.append(...)` itt NEM csak
        naplózás — `_classify_result()` a `LEARNING_RESULT_VERIFIED`
        eredményhez explicit `not self._quality_flags`-et követel meg
        (l. ott), tehát ez az esemény a session végén automatikusan
        `LEARNING_RESULT_AWAITING`-re fokozza le a klasszifikációt, MÉG
        AKKOR IS, ha minden más BQ-bizonyíték (Qmax/Ra/OCVTAKEN/VOK)
        egyébként pozitív lenne. Ez SZÁNDÉKOS, nem mellékes bug: CLAUDE.md
        §7 ("BQ-sikerhez BQ-oldali bizonyíték kell") + safety-szabály #10
        ("hamis PASS magasabb prioritású, mint kényelmi javítás") szerint
        egy session, amiben a BQ-diagnosztika tartósan megfigyelhetetlen
        volt, nem állíthat BQ-VERIFIED eredményt csendben.

        Reset-on-any-success (MAX verifier F-2b): EGYETLEN nem-teljesen-
        érvénytelen poll nullázza mind a számlálót, mind a "már jelezve"
        flaget — egy szaggatott (pl. minden 3. poll sikeres) valós
        kapcsolati hiba emiatt SOHA nem escalate-elne. Ez tudatosan
        elfogadott: az esemény szemantikája explicit "TARTÓSAN
        elérhetetlen", nem "időnként instabil" — egy degraded-ratio-alapú
        küszöb (pl. "N poll közül M invalid") külön, e körön kívüli
        mérnöki döntést igényelne (CLAUDE.md §10: ne bővítsd a viselkedési
        teret a jelen finding megoldásához szükségesnél jobban)."""
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

    def _poll_bq_diagnostics_throttled(self) -> BqDiagnosticsSnapshot:
        """Governance-v2 review-kör 1 (CF-10a, DFAM-027, ceiling P1): a
        `poll_bq_diagnostics()` 19 mezőt olvas egymástól függetlenül,
        mindegyiket a transport saját retry-logikájával (PicoBQTransport.
        max_retries alapból 3, soros timeout alapból 1.0s) — egy teljes
        Pico-kiesésnél ez akár 19*3*1.0s ~= 57s szinkron blokkolást
        okozhat EGYETLEN advance() tickben, miközben a TestRunner az
        emergency_stop_requested flag-jét csak a tick TETEJÉN nézi (l.
        DEFECT_FAMILY_REGISTRY.yaml DFAM-027) — egy operátori vészleállítás
        akár 57 másodpercig késhetne emiatt.

        Ez a hívás (self._record_gauge_sample()-ből, minden sample_
        interval_s-nél) TISZTÁN diagnosztikai/élő-kijelzési célú — egy
        elveszett mintavétel ártalmatlan, a következő sample_interval_s
        múlva úgyis újra próbálkozunk. Ezért NEM igényli a transport
        kritikus write-utaknál (unseal/it_enable/Data Flash írás)
        indokolt retry-robusztusságát. A poll idejére ideiglenesen 1-re
        csökkentjük a max_retries-t (worst-case ~19*1.0s ~= 19s-re,
        kb. 3x javulás), majd garantáltan (try/finally) visszaállítjuk az
        eredeti értéket — a kritikus write-utak retry-robusztussága ettől
        NEM csökken, mert azok a poll UTÁN, az eredeti max_retries mellett
        futnak.

        CF-10a 2. MAX verifier P3-2 pontosítás (2026-09-08, l. ugyanez
        golden_gg_controller.py::_poll_bq_diagnostics_throttled()-ben):
        ez a hívás NEM csak élő GUI-kijelzést táplál — a snapshot az
        on_gauge_sample-en keresztül a GUI-oldali bq_diagnostics.csv-be
        is íródik, tehát perzisztens mérési artefaktum. (Review-kör 4,
        Architektúra F5, DFAM-026: a self._gauge_log/gauge_log property
        MAGA NEM perzisztens — csak session-élettartamú, in-memory lista,
        production/teszt fogyasztó nélkül, l. a mező docstringje lent.) A
        max_retries=1 csökkentés emiatt megnövelheti a <mező>_valid=False
        arányt egy zajos (de működő) kapcsolat mellett. Ez ELFOGADHATÓ,
        mert a BqFieldReading.valid/error mező EXPLICIT jelöli a
        degradált mintát (CLAUDE.md §7: nincs csendes hamis-érték-
        helyettesítés).

        SZÁNDÉKOSAN NEM érinti a `_run_initial_snapshot()`/`_run_final_
        snapshot()` egyszeri, terminális `poll_bq_diagnostics()` hívásait:
        azok a Qmax/Ra learning-eredmény TÉNYLEGES adatát rögzítik
        (self._qmax_before/_final, self._ra_before/_final -> _classify_
        result()), nem csupán diagnosztikai kijelzést — egy tranziens,
        retry-vel elhárítható hiba itt hamis adatvesztést okozna a
        learning-eredményben, ami elfogadhatatlan (CLAUDE.md #6: kritikus
        readback hiba tesztzöldítésért/kényelemért nem lazítható)."""
        original_max_retries = getattr(self._bq_transport, "max_retries", None)
        if original_max_retries is not None:
            self._bq_transport.max_retries = 1
        try:
            return poll_bq_diagnostics(self._bq_driver)
        finally:
            if original_max_retries is not None:
                self._bq_transport.max_retries = original_max_retries

    # ------------------------------------------------------------------ #
    def _read_dmm(self, dt_s: float) -> bool:
        """Teljes kódbázis review 9. kör (2026-08-30), Szoftver/Architektúra
        P2 lelet #2: korábban a feszültség-DMM kivétele korai return-nel
        MEGAKADÁLYOZTA a hőmérséklet-DMM olvasását is — két teljesen
        FÜGGETLEN fizikai műszerről van szó, egyik hibája nem indokolja a
        másik olvasásának kihagyását. A testvér controllerek
        (discharge_controller.py, chemid_controller.py, ocv_soc_controller.py,
        golden_gg_controller.py) mindig két önálló try/except blokkot
        futtatnak — ez a metódus mostantól ugyanazt a mintát követi."""
        try:
            self._u_batt = self._dmm_v.read_voltage()
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
        except Exception:
            self._safety.record_temperature_dmm_read(valid=False, dt_s=dt_s)
            self._temp_dmm_fault_ticks += 1
            self._temp_dmm_valid = False
