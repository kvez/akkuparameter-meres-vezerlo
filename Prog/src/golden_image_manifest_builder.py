"""
golden_image_manifest_builder.py — WP-AK-13: a `golden_image_manifest.py`
(WP-AK-00 baseline, SUP03-DATA-003) adatmodelljét MINDEN korábbi WP-AK
fázis tényleges eredményéből tölti fel (SUP03-ACCEPT-002 aggregáció).

**Felfedezés, ami ezt a modult indokolja**: a `GoldenImageManifest` mezői
(`chemid_selected_and_programmed`/`calibration_valid`/`learning_verified_0x06`/
`golden_gg_optimized`/`validation_matrix_passed`) a WP-AK-00 baseline óta
LÉTEZNEK az adatmodellben, de EGYETLEN korábbi WP (01-12) SEM töltötte ki
őket ténylegesen egy valós fázis-eredményből — ez a modul zárja ezt a rést,
pure, GUI-független aggregáló függvényként.

SUP03-ACCEPT-002 "ha mindket utvonal futott" kapu: ha a traditional
learning (WP-AK-10) ÉS a Golden GG (WP-AK-11) útvonal is lefutott és
sikeres volt, a manifest csak EGY Qmax/Ra forrást tud tárolni — a hívónak
explicit dokumentálnia KELL, melyiket használta a végső image-hez
(`final_qmax_ra_source`), különben `DualQmaxRaSourceUndocumentedError`.

**NEM BEKÖTÖTT — NOT_WIRED (review-kör 4, Architektúra F6, DFAM-026
3. előfordulás)**: ezt a modult jelenleg SEHOL nem importálja/hívja élő
GUI- vagy TestRunner-kód (kizárólag a saját tesztje, `tests/test_golden_
image_manifest_builder.py`, hivatkozik rá). L. `bq_learning_verification.py`
azonos szerkezetű "NEM HASZNÁLT" jelölése. Ez a modul NEM lett törölve —
önmagában teljes és tesztelt aggregáló logika, jövőbeli GUI-bekötésre vár.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from Prog.src.bq_calibration_statistics import CalibrationStatisticsResult
from Prog.src.golden_gg_export import MATURITY_OPTIMIZED_CANDIDATE, GoldenGgImportResult
from Prog.src.golden_image_manifest import GoldenImageManifest
from Prog.src.traditional_learning_controller import LEARNING_RESULT_VERIFIED, LearningResultSummary
from Prog.src.validation_matrix import GATE_VALIDATED, ValidationMatrixReport

# SUP03-CAL-007/TEST-005 sourced minimum ("20 minta minimum") — nem önkényes,
# a normatív szöveg explicit ezt a darabszámot írja elő.
MINIMUM_CALIBRATION_BOARD_SAMPLES = 20


class DualQmaxRaSourceUndocumentedError(ValueError):
    """SUP03-ACCEPT-002: 'ha mindket utvonal futott, a vegso parameterforras
    es felulirasok dokumentaltak' — ha traditional learning ÉS Golden GG is
    sikeres volt, a hívónak explicit meg kell adnia `final_qmax_ra_source`-t."""


@dataclass
class ChemIdPhaseResult:
    """SUP03-DATA-001/002: a ChemID fázis (WP-AK-05) manifesthez szükséges
    kivonata — a hívó (session-orchesztráció) tölti ki a ChemID
    export/readback tényleges eredményéből."""
    chem_id: int
    verified: bool
    session_reference: str


@dataclass
class FinalImageExportResult:
    """SUP03-PROD-005/006: a végleges, exportált image hash-ei — a hívó
    (production programming controller vagy manuális export) tölti ki."""
    image_file_hash: str
    data_flash_hash: str
    source_reference: str


def build_golden_image_manifest(
    image_id: str,
    device: str = "BQ34Z110",
    firmware: Optional[str] = None,
    update_status: Optional[int] = None,
    qmax: Optional[int] = None,
    chemid_result: Optional[ChemIdPhaseResult] = None,
    calibration_result: Optional[CalibrationStatisticsResult] = None,
    calibration_session_reference: str = "",
    learning_result: Optional[LearningResultSummary] = None,
    learning_session_reference: str = "",
    golden_gg_result: Optional[GoldenGgImportResult] = None,
    golden_gg_session_reference: str = "",
    validation_report: Optional[ValidationMatrixReport] = None,
    validation_session_reference: str = "",
    final_image_result: Optional[FinalImageExportResult] = None,
    final_qmax_ra_source: Optional[str] = None,
    approved_by: str = "",
    approval_date: str = "",
) -> GoldenImageManifest:
    """SUP03-ACCEPT-002 aggregáció: a MEGADOTT fázis-eredményekből tölti fel
    a manifestet, `source_sessions`-be (SUP03-DATA-002 lánchivatkozás) minden
    fázis session-referenciáját rögzítve. Egyetlen None-ként megadott fázis
    SEM tekinthető hallgatólagosan "sikeresnek" — csak a ténylegesen átadott
    eredmények alapján áll a manifest, `missing_phases_for_production_ready()`
    a többit hiányzóként jelzi."""
    manifest = GoldenImageManifest(
        image_id=image_id, device=device, firmware=firmware,
        update_status=update_status, qmax=qmax,
    )
    limitations: list[str] = []
    source_sessions: dict[str, str] = {}

    if chemid_result is not None:
        manifest.chem_id = chemid_result.chem_id
        manifest.chemid_selected_and_programmed = chemid_result.verified
        source_sessions["chemid_session"] = chemid_result.session_reference
        if not chemid_result.verified:
            limitations.append("ChemID session jelen van, de nem verified.")

    if calibration_result is not None:
        calibration_valid = (
            calibration_result.sample_count_accepted >= MINIMUM_CALIBRATION_BOARD_SAMPLES
        )
        manifest.calibration_valid = calibration_valid
        source_sessions["calibration_session"] = calibration_session_reference
        if not calibration_valid:
            limitations.append(
                f"Kalibrációs minta ({calibration_result.sample_count_accepted} elfogadott) "
                f"a SUP03-CAL-007 minimum ({MINIMUM_CALIBRATION_BOARD_SAMPLES}) alatt."
            )

    learning_verified = False
    if learning_result is not None:
        learning_verified = learning_result.result == LEARNING_RESULT_VERIFIED
        manifest.learning_verified_0x06 = learning_verified
        source_sessions["learning_session"] = learning_session_reference
        if not learning_verified:
            limitations.append(
                f"Traditional learning eredmény: {learning_result.result} (nem VERIFIED)."
            )

    golden_gg_optimized = False
    if golden_gg_result is not None:
        golden_gg_optimized = golden_gg_result.maturity_status == MATURITY_OPTIMIZED_CANDIDATE
        manifest.golden_gg_optimized = golden_gg_optimized
        source_sessions["golden_gg_session"] = golden_gg_session_reference
        if not golden_gg_optimized:
            limitations.append(
                f"Golden GG eredmény: {golden_gg_result.maturity_status} (nem "
                f"{MATURITY_OPTIMIZED_CANDIDATE})."
            )

    # SUP03-ACCEPT-002: "ha mindket utvonal futott, a vegso parameterforras
    # es felulirasok dokumentaltak" — explicit blokkoló kapu, nem hallgatólagos
    # elsőbbség (pl. "mindig a learning nyer").
    if learning_verified and golden_gg_optimized:
        if not final_qmax_ra_source or not final_qmax_ra_source.strip():
            raise DualQmaxRaSourceUndocumentedError(
                "SUP03-ACCEPT-002: mind a traditional learning, mind a Golden GG "
                "útvonal sikeres — final_qmax_ra_source kötelező, dokumentálnia "
                "kell, melyik forrás Qmax/Ra értékei kerültek a végső image-be."
            )
        source_sessions["final_qmax_ra_source"] = final_qmax_ra_source
    elif final_qmax_ra_source:
        # Csak egy útvonal futott sikeresen — a hívó akkor is dokumentálhatja
        # explicit, melyikről van szó (nem kötelező, de megengedett).
        source_sessions["final_qmax_ra_source"] = final_qmax_ra_source

    if validation_report is not None:
        validated = validation_report.gate_status == GATE_VALIDATED
        manifest.validation_matrix_passed = validated
        manifest.validation_status = validation_report.gate_status
        source_sessions["validation_session"] = validation_session_reference
        if not validated:
            limitations.append(
                f"Validation matrix státusz: {validation_report.gate_status} "
                f"(nem {GATE_VALIDATED})."
            )

    if final_image_result is not None:
        manifest.image_file_hash = final_image_result.image_file_hash
        manifest.data_flash_hash = final_image_result.data_flash_hash
        manifest.final_image_exported_and_hashed = True
        source_sessions["final_image_export"] = final_image_result.source_reference

    manifest.source_sessions = source_sessions
    manifest.limitations = limitations
    manifest.approved_by = approved_by
    manifest.approval_date = approval_date
    manifest.recompute_maturity()
    return manifest


__all__ = [
    "MINIMUM_CALIBRATION_BOARD_SAMPLES",
    "DualQmaxRaSourceUndocumentedError",
    "ChemIdPhaseResult",
    "FinalImageExportResult",
    "build_golden_image_manifest",
]
