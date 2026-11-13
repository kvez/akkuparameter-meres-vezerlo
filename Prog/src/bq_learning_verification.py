"""
SUP03-LEARN-*: BQ34Z110 tanulási (Qmax/Ra) verifikációs adatmodell.

Ez a modul NEM vezérel BQ-t, NEM ír Data Flash-t — kizárólag a
Bq34z110ReadOnlyDriver-ről (vagy kézi checkpoint-ról) származó adatokból
dönti el, hogy a tanulási ciklus ténylegesen igazolt-e (SUP03-LEARN-007:
"UpdateStatus nem kezzel allitott sikerjelzo" — a 0x05/0x06 értéknek a
gauge-től kell jönnie, ide csak a KIOLVASOTT érték kerül be).

**NEM HASZNÁLT — DEPRECATED (teljes kódbázis review 1. kör, 2026-08-26,
Session/Workflow/Állapotgép P3 lelet)**: élő kód
(`traditional_learning_controller.py`) ebből a modulból KIZÁRÓLAG az
`UPDATE_STATUS_FIRST_QMAX_UPDATE`/`UPDATE_STATUS_FULL_LEARNING` konstansokat
importálja (R1 B4, F-10(a)); az `evaluate_learning_result()` és a
`qmax_plausible` továbbra is NOT_WIRED (ezekre csak a saját tesztje,
`tests/test_bq_learning_verification.py` hivatkozik) — az éles klasszifikációs logika
`traditional_learning_controller.py::_classify_result()`-ban él, EGY,
LÉNYEGESEN ELTÉRŐ VOK-kritériummal (`evaluate_learning_result()` itt
`bool(record.vok)`-ot, azaz VOK=SET-et várna a végső elfogadáshoz, míg a
`traditional_learning_controller.py::_check_relax_bq_acceptance()` a relax-
elfogadáshoz kifejezetten `not bool(cs.vok)`-ot, azaz VOK=CLEARED-et vár,
a végső `_classify_result()` pedig a VOK-ot egyáltalán nem is nézi).
**Ez a modul NEM lett törölve** — a `qmax_plausible`/`ra_valid`/
`ra_update_detected` ellenőrzések a `_classify_result()`-nál
RÉSZLETESEBBEK, lehet, hogy egy szándékosan alaposabb, de sosem bekötött
referencia-implementáció, nem egyszerű elavult kód.

**FRISSÍTÉS (2026-09-07, governance-v2 review-kör 1, CF-06/CF-11d,
BQ-VOK-001)**: a fenti "forrás nélkül nem dönthető el" állítás HAMIS volt.
TI-SLUSB55B p.30 STEP 8 (5./7./9. lépés) és TI-SLUA711 (5./8./10. lépés --
Kör 20 E18/SRC-F-8: az első szám korábban helytelenül 6 volt, önálló
kinyerés a SLUA711 "Calibration Process" számozott listájából megerősítette:
5. lépés = "Allow the pack to rest until the OCVTAKEN flag is set" [az
OCVTAKEN relax], 6. lépés csak "Update Status should change to 5" [egy
státusz-ellenőrzés, NEM relax-lépés])
egyértelműen leírja: a KISÜTÉS UTÁNI relaxban a VOK flag CLEAR-re megy
(ezt a 0x05/0x06 elérése követeli), de a RECHARGE UTÁNI relaxban a VOK
flag SET marad. A `traditional_learning_controller.py::
_check_relax_bq_acceptance()` `not vok` kritériuma tehát a kisütés utáni
relax-fázisokra HELYES volt, a RECHARGE utáni fázisra (RELAX_PRE_
DISCHARGE_2) téves — **JAVÍTVA: commit ab61dba (2026-09-09), MAX
independent verifier VERIFIED** (require_vok_cleared=False paraméter
csak RELAX_PRE_DISCHARGE_2-re, l. CLAIM_LEDGER.yaml BQ-VOK-001). Ennek a modulnak az
`evaluate_learning_result()`-ja pedig a VÉGSŐ elfogadáshoz várja
`bool(record.vok)`-ot (VOK=SET-et) — ez a datasheet szerint is TÉVES: a
záró (kisütés utáni) relaxban a sikeres 0x05/0x06 állapothoz a VOK-nak
CLEARED-nek kell lennie, nem SET-nek. Ha ezt a modult egy jövőbeli
session bekötné, ELŐBB javítsa ki ezt a feltételt (távolítsa el a
`bool(record.vok)` tagot a végső konzisztencia-feltételből, `qen` és
`ocvtaken` marad) — MAX independent verifier kötelező (BQ learning
state-machine változás)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LearningResult(Enum):
    BQ_LEARNING_VERIFIED = "BQ_LEARNING_VERIFIED"
    PHYSICAL_CYCLE_COMPLETED_AWAITING_BQ_VERIFICATION = (
        "PHYSICAL_CYCLE_COMPLETED_AWAITING_BQ_VERIFICATION"
    )
    BQ_LEARNING_FAILED = "BQ_LEARNING_FAILED"


# TI BQ34Z110 adatlap szerinti UpdateStatus értékek (SUP03-SRC-001/005/007).
UPDATE_STATUS_GOLDEN_IMAGE = 0x02          # Qmax+Ra tanult, IT nincs engedélyezve
UPDATE_STATUS_FIRST_QMAX_UPDATE = 0x05     # első sikeres Qmax update után
UPDATE_STATUS_FULL_LEARNING = 0x06         # Qmax ÉS Ra tanult (teljes learning)


@dataclass
class LearningVerificationRecord:
    """SUP03-LEARN-002/003/008/009: gauge-oldali, időbélyeges checkpoint-adatok.

    Minden mező a Bq34z110ReadOnlyDriver-ről ténylegesen KIOLVASOTT (vagy
    kézi checkpoint során a kezelő által rögzített) érték — sosem feltételezett.
    """

    # SUP03-LEARN-002 gauge-oldali kötelező mezők (a legfontosabbak).
    ocvtaken: Optional[bool] = None
    vok: Optional[bool] = None
    qen: Optional[bool] = None
    dod0: Optional[int] = None
    update_status: Optional[int] = None

    # SUP03-LEARN-008 Qmax elfogadás.
    qmax_before: Optional[float] = None
    qmax_after_first_update: Optional[float] = None
    qmax_final: Optional[float] = None
    design_capacity_mAh: Optional[float] = None
    measured_capacity_mAh: Optional[float] = None

    # SUP03-LEARN-009 Ra elfogadás.
    ra_before: Optional[list[float]] = None
    ra_final: Optional[list[float]] = None

    # Biztonsági kontextus — ha volt safety fault a ciklus alatt, a learning
    # NEM tekinthető igazoltnak, még 0x06 esetén sem (SUP03-LEARN-010).
    safety_fault_occurred: bool = False

    notes: list[str] = field(default_factory=list)

    @property
    def qmax_change_percent(self) -> Optional[float]:
        if self.qmax_before is None or self.qmax_final is None or self.qmax_before == 0:
            return None
        return (self.qmax_final - self.qmax_before) / self.qmax_before * 100.0

    @property
    def qmax_plausible(self) -> bool:
        """SUP03-LEARN-008: 'A Qmax nem fogadhato el pusztan azert, mert nem
        nulla' — legalább egy ésszerű tartományellenőrzést végzünk: a
        design_capacity fél és kétszerese közé essen, ha ismert."""
        if self.qmax_final is None or self.qmax_final <= 0:
            return False
        if self.design_capacity_mAh is not None and self.design_capacity_mAh > 0:
            ratio = self.qmax_final / self.design_capacity_mAh
            return 0.5 <= ratio <= 2.0
        return True

    @property
    def ra_valid(self) -> bool:
        """SUP03-LEARN-009: 'no_invalid_or_negative_values', frissülés a
        before/final összevetésével észlelhető."""
        if not self.ra_final:
            return False
        if any(v <= 0 for v in self.ra_final):
            return False
        if len(self.ra_final) != 15:
            return False
        return True

    @property
    def ra_update_detected(self) -> bool:
        if not self.ra_before or not self.ra_final:
            return False
        if len(self.ra_before) != len(self.ra_final):
            return False
        return any(abs(b - f) > 1e-9 for b, f in zip(self.ra_before, self.ra_final))


def evaluate_learning_result(record: LearningVerificationRecord) -> LearningResult:
    """SUP03-LEARN-010: BQ_LEARNING_VERIFIED KIZÁRÓLAG akkor, ha:
      - UpdateStatus == 0x06 (gauge által, nem kézzel írt);
      - Qmax update igazolt és plauzibilis;
      - Ra update igazolt (frissült, minden érték pozitív, 15 elem);
      - OCVTAKEN/VOK/QEN konzisztens (mind True);
      - nincs safety fault.

    Ha az UpdateStatus ismeretlen (None) vagy a fizikai ciklus lezajlott, de a
    gauge-oldali igazolás még nem történt meg: AWAITING_BQ_VERIFICATION.
    Ha a gauge kifejezetten ELLENTMOND a sikeres learningnek (pl. hiányos
    Qmax/Ra vagy safety fault occurred egy 0x06 mellett): BQ_LEARNING_FAILED.
    """
    if record.safety_fault_occurred:
        return LearningResult.BQ_LEARNING_FAILED

    if record.update_status is None:
        return LearningResult.PHYSICAL_CYCLE_COMPLETED_AWAITING_BQ_VERIFICATION

    if record.update_status != UPDATE_STATUS_FULL_LEARNING:
        # 0x05 (részleges) vagy más érték — a fizikai ciklus lezajlott, de a
        # teljes learning gauge-oldali igazolása még nincs meg.
        return LearningResult.PHYSICAL_CYCLE_COMPLETED_AWAITING_BQ_VERIFICATION

    ocv_vok_qen_consistent = bool(record.ocvtaken) and bool(record.vok) and bool(record.qen)

    if not ocv_vok_qen_consistent or not record.qmax_plausible or not record.ra_valid:
        return LearningResult.BQ_LEARNING_FAILED

    return LearningResult.BQ_LEARNING_VERIFIED
