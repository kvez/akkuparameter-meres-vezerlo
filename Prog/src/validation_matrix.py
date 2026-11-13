"""
validation_matrix.py — WP-AK-12: Golden image validation matrix (SUP03-VALID-001..007).

FELTÉTELEZÉS-mentes, forrásból levezetett tervezési döntés: a mátrix minden cellája (SUP03-VALID-002
A-G lépései) egy-egy MÁR meglévő, GUI-ból elérhető teszttípussal (`CHARGE_ONLY`/`DISCHARGE_ONLY`,
eltérő `discharge_current_A`-val "alacsony"/"nagy tipikus terhelés"-hez) fut le, KÜLÖN
session-könyvtárban — nincs szükség egy új, monolitikus "fusd le az egész mátrixot" fizikai
állapotgépre (ellentétben a WP-AK-10/11 fizikai ciklusaival). Ez a modul egy POST-HOC, GUI-független
kiértékelő/összehasonlító réteg: minden mátrix-cellához tartozó session-könyvtárból
(`report.json`+`samples.csv`+`bq_diagnostics.csv`, mind a Logger — WP-AK-01/07 — által már
előállított, meglévő artifact) kinyeri az SUP03-VALID-003 összehasonlítandó mezőit, majd egy
explicit, forrásolt (SUP03-VALID-006: "ne legyen forrás nélkül hardcoded") tolerancia alapján
állítja elő a `VALIDATED_FOR_DEFINED_MATRIX` Kaput.

**gauge_SOC vs reference_SOC összehasonlítás módszertana**: mivel minden mátrix-cella KÜLÖN
session (a Logger Ah-integrátora minden session elején nullázódik), egy abszolút, session-határokon
átívelő SOC-bázisvonal fenntartása extra, dokumentálatlan feltevést igényelne. Ehelyett minden
cellánál a session SAJÁT eleje/vége közötti DELTA-kat hasonlítjuk össze: mennyit változott a gauge
SOC%-a a session alatt, versus mennyit kellett volna változnia a ténylegesen mért (reference)
töltött/kisütött Ah alapján, a `COMPLETE_DISCHARGE` lépés mért használható kapacitásához (Ah)
viszonyítva. Ez pontosan megfelel a SUP03-VALID-006 "SOC pontonkent... ertekelendo" előírásának, és
nem igényel session-határokon átívelő állapot-feltételezést.

**gauge adat hiánya sosem csendes 0** (ugyanaz az elv, mint `bq_diagnostics.py`/`golden_gg_export.py`
kapu-mintája): ha a BQ Diagnosztika panel nem volt csatlakoztatva egy adott mátrix-cella
session-je alatt, a `bq_diagnostics.csv` üres/hiányzik — ez explicit `gauge_data_available=False` +
dokumentált limitáció, a cella NEM értékelhető ki (nem "automatikusan sikeres").

**NEM BEKÖTÖTT — NOT_WIRED (review-kör 5, Architektúra A-5, DFAM-026
4. előfordulása)**: ezt a modult jelenleg SEHOL nem importálja/hívja élő
GUI- vagy TestRunner-kód (kizárólag a saját tesztje hivatkozik rá) — a
`golden_image_manifest_builder.py` (maga is NOT_WIRED, l. ott) importálja,
de tranzitívan ez sem ér el élő hívóhelyet. L. `golden_image_manifest_
builder.py` azonos szerkezetű jelölése. Ez a modul NEM lett törölve —
önmagában teljes és tesztelt kiértékelő logika, jövőbeli GUI-bekötésre vár.

**FONTOS, BEKÖTÉS ELŐTT JAVÍTANDÓ RÉS (2026-09-16, Kör 17 MAX-effort review,
metrológia/adatintegritás reviewer P3-2, DFAM-001 -- ma LATENS, nem elérhető
kód, ezért csak dokumentálva, nincs kódváltozás)**: a modul a BQ-mezőket
(`_bq_field()`) a saját `_valid` testvér-oszlopukra gátolja -- egy érvénytelen
BQ-mező sosem válik csendben hihető értékké. A `samples.csv`-ből olvasott
`voltage_V_end`/`temperature_C_end` (a `_bq_field()`-del PÁRHUZAMOS, de
KÜLÖN implementált olvasó út, `:259-267` körül) ugyanakkor NEM néz a
`dmm_voltage_valid`/`dmm_temperature_valid` oszlopokra, holott ugyanaz a
sample-sor hordozza őket. Mielőtt ez a modul bármilyen élő GUI/TestRunner
útra bekötésre kerül, a `samples.csv`-olvasó ágat is a `_bq_field()`
mintájára kell gátolni (pl. egy `_sample_field(row, name, valid_col)`
helperrel) -- l. `DEFECT_FAMILY_REGISTRY.yaml` DFAM-001 evidence.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from Prog.src.bq_diagnostics import decode_flags


class MatrixStepKind(Enum):
    FULL_CHARGE = "FULL_CHARGE"
    LOW_TYPICAL_LOAD = "LOW_TYPICAL_LOAD"
    HIGH_TYPICAL_LOAD = "HIGH_TYPICAL_LOAD"
    COMPLETE_DISCHARGE = "COMPLETE_DISCHARGE"
    RELAX_SOC_CORRECTION = "RELAX_SOC_CORRECTION"
    RECHARGE = "RECHARGE"
    STANDBY = "STANDBY"  # SUP03-VALID-002 G — opcionális, "ha relevans"


# SUP03-VALID-002: A-F kötelező minimum, G ("standby/self-discharge") csak akkor,
# ha a termékhasználat indokolja — ezért NEM eleme az alapértelmezett kötelező halmaznak.
REQUIRED_STEP_KINDS: frozenset[MatrixStepKind] = frozenset({
    MatrixStepKind.FULL_CHARGE,
    MatrixStepKind.LOW_TYPICAL_LOAD,
    MatrixStepKind.HIGH_TYPICAL_LOAD,
    MatrixStepKind.COMPLETE_DISCHARGE,
    MatrixStepKind.RELAX_SOC_CORRECTION,
    MatrixStepKind.RECHARGE,
})

_CHARGE_STEP_KINDS = (MatrixStepKind.FULL_CHARGE, MatrixStepKind.RECHARGE)
_DISCHARGE_STEP_KINDS = (
    MatrixStepKind.LOW_TYPICAL_LOAD, MatrixStepKind.HIGH_TYPICAL_LOAD, MatrixStepKind.COMPLETE_DISCHARGE,
)


class ValidationMatrixDataError(ValueError):
    """A session-könyvtár nem tartalmazza a kiértékeléshez kötelező artifactot
    (`report.json` — a session még nem fejeződött be, vagy nem érvényes session)."""


@dataclass
class ValidationTolerance:
    """SUP03-VALID-006: "ne legyen forrás nélkül hardcoded" — a `source_reference`
    kötelező, nem üres (pl. termékkövetelmény-dokumentum azonosítója/verziója).
    A TI "as little as 1%" marketing-állítás NEM használható forrásként
    (SUP03-VALID-006 explicit kizárja, hogy ez automatikus elfogadási garancia)."""
    soc_error_percent_max: float
    source_reference: str

    def __post_init__(self) -> None:
        if self.soc_error_percent_max <= 0:
            raise ValueError("soc_error_percent_max > 0 kötelező")
        if not self.source_reference.strip():
            raise ValueError(
                "SUP03-VALID-006: source_reference kötelező, nem lehet üres — a "
                "tolerancia forrását (termékkövetelmény-dokumentum) explicit meg kell adni."
            )

    def to_dict(self) -> dict:
        return {
            "soc_error_percent_max": self.soc_error_percent_max,
            "source_reference": self.source_reference,
        }


@dataclass
class MatrixStepMeasurement:
    """SUP03-VALID-003 nyers, session-szintű összehasonlítandó adatai —
    egyetlen mátrix-cella (egyetlen, már lezárult teszt-session) alapján."""
    step_kind: MatrixStepKind
    session_dir: str
    status: str
    reason: str
    reference_added_Ah: float
    reference_removed_Ah: float
    gauge_soc_percent_start: Optional[float]
    gauge_soc_percent_end: Optional[float]
    gauge_fcc_mAh_end: Optional[float]
    voltage_V_end: Optional[float]
    current_A_end: Optional[float]
    temperature_C_end: Optional[float]
    end_of_charge: bool
    end_of_discharge: bool
    ocv_correction_events: int
    gauge_data_available: bool
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "step_kind": self.step_kind.value,
            "session_dir": self.session_dir,
            "status": self.status,
            "reason": self.reason,
            "reference_added_Ah": self.reference_added_Ah,
            "reference_removed_Ah": self.reference_removed_Ah,
            "gauge_soc_percent_start": self.gauge_soc_percent_start,
            "gauge_soc_percent_end": self.gauge_soc_percent_end,
            "gauge_fcc_mAh_end": self.gauge_fcc_mAh_end,
            "voltage_V_end": self.voltage_V_end,
            "current_A_end": self.current_A_end,
            "temperature_C_end": self.temperature_C_end,
            "end_of_charge": self.end_of_charge,
            "end_of_discharge": self.end_of_discharge,
            "ocv_correction_events": self.ocv_correction_events,
            "gauge_data_available": self.gauge_data_available,
            "limitations": self.limitations,
        }


def _read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _safe_float(raw: object) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _bq_field(row: dict, name: str) -> tuple[Optional[float], bool]:
    """A `bq_diagnostics.py`/`Logger.log_bq_diagnostics()` kapu-elvét követve:
    egy érvénytelen (`<name>_valid` != "True") mező SOHA nem válik csendben
    0/hihető értékké."""
    if row.get(f"{name}_valid") != "True":
        return None, False
    value = _safe_float(row.get(name))
    return value, value is not None


def _count_ocv_correction_events(bq_rows: list[dict]) -> int:
    """Flags() OCVTAKEN bit 0->1 átmenetek száma a session alatt (SUP03-VALID-003
    "OCV_correction_events") — a `bq_diagnostics.py::decode_flags()` meglévő,
    forrásolt bit-dekódolását újrahasznosítva."""
    count = 0
    prev_ocvtaken: Optional[bool] = None
    for row in bq_rows:
        flags_value, valid = _bq_field(row, "flags")
        if not valid or flags_value is None:
            continue
        ocvtaken = decode_flags(int(flags_value)).ocvtaken
        if prev_ocvtaken is False and ocvtaken is True:
            count += 1
        prev_ocvtaken = ocvtaken
    return count


def extract_step_measurement(session_dir: Path, step_kind: MatrixStepKind) -> MatrixStepMeasurement:
    """Egy MÁR lezárult teszt-session könyvtárából (Logger — WP-AK-01/07 — által
    előállított `report.json`/`samples.csv`/`bq_diagnostics.csv`) kinyeri az
    SUP03-VALID-003 összehasonlítandó mezőit egyetlen mátrix-cellához."""
    report_path = session_dir / "report.json"
    if not report_path.exists():
        raise ValidationMatrixDataError(
            f"report.json hiányzik: {report_path} — a session még nem fejeződött "
            f"be, vagy nem érvényes session-könyvtár."
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    status = report.get("status", "UNKNOWN")
    reason = report.get("reason", "")
    reference_added_Ah = float(report.get("total_charge_ah", 0.0) or 0.0)
    reference_removed_Ah = float(report.get("total_discharge_ah", 0.0) or 0.0)

    limitations: list[str] = []

    bq_rows = _read_csv_rows(session_dir / "bq_diagnostics.csv")
    if not bq_rows:
        limitations.append(
            "bq_diagnostics.csv hiányzik vagy üres — a BQ Diagnosztika panel nem "
            "volt csatlakoztatva e session alatt, gauge-alapú összehasonlítás nem "
            "végezhető ehhez a cellához."
        )
        gauge_soc_percent_start = gauge_soc_percent_end = gauge_fcc_mAh_end = None
        gauge_data_available = False
        ocv_correction_events = 0
    elif len(bq_rows) < 2:
        # [review-lelet] EGYETLEN sor esetén start==end lenne, ami hamisan
        # "0 gauge-drift"-et sugallna — pl. ha a BQ Diagnosztika panel csak a
        # session VÉGÉN, röviden csatlakozott. A delta-számításhoz legalább
        # 2, egymástól időben eltérő minta kell.
        limitations.append(
            f"bq_diagnostics.csv csak {len(bq_rows)} sort tartalmaz — a "
            f"SOC%-delta számításához legalább 2 minta kell (session eleje ÉS "
            f"vége), egyetlen sor hamis '0 drift' eredményt adna."
        )
        gauge_soc_percent_start = gauge_soc_percent_end = gauge_fcc_mAh_end = None
        gauge_data_available = False
        ocv_correction_events = 0
    else:
        gauge_soc_percent_start, soc_start_valid = _bq_field(bq_rows[0], "soc_percent")
        gauge_soc_percent_end, soc_end_valid = _bq_field(bq_rows[-1], "soc_percent")
        gauge_fcc_mAh_end, _ = _bq_field(bq_rows[-1], "full_charge_capacity_mAh")
        gauge_data_available = soc_start_valid and soc_end_valid
        if not gauge_data_available:
            limitations.append(
                "gauge SOC% nem érvényes (BqFieldReading.valid=False) a session "
                "eleje és/vagy vége pillanatában."
            )
        else:
            # [review-lelet] a bq_diagnostics.csv egy TELJESEN KÜLÖN,
            # független QThread-en (WP-AK-07) naplózódik, mint a samples.csv —
            # a modul nem ellenőrzi automatikusan (nincs sourced, nem
            # önkényes küszöbérték rá), hogy a BQ-napló időben lefedi-e a
            # TELJES session-t, vagy csak egy részét. Ez explicit, dokumentált
            # korlátozás, nem hallgatólagos feltételezés.
            limitations.append(
                "gauge log időbeli lefedettsége (bq_diagnostics.csv első/utolsó "
                "sora vs. a session teljes időtartománya) NINCS automatikusan "
                "ellenőrizve — az operátornak kell biztosítania, hogy a BQ "
                "Diagnosztika panel a TELJES session alatt csatlakoztatva maradt."
            )
        ocv_correction_events = _count_ocv_correction_events(bq_rows)

    sample_rows = _read_csv_rows(session_dir / "samples.csv")
    if sample_rows:
        last = sample_rows[-1]
        voltage_V_end = _safe_float(last.get("battery_voltage_V"))
        current_A_end = _safe_float(last.get("signed_current_A"))
        temperature_C_end = _safe_float(last.get("battery_temperature_C"))
    else:
        limitations.append("samples.csv hiányzik vagy üres.")
        voltage_V_end = current_A_end = temperature_C_end = None

    end_of_charge = step_kind in _CHARGE_STEP_KINDS and status == "DONE"
    end_of_discharge = step_kind in _DISCHARGE_STEP_KINDS and status == "DONE"

    return MatrixStepMeasurement(
        step_kind=step_kind,
        session_dir=str(session_dir),
        status=status,
        reason=reason,
        reference_added_Ah=reference_added_Ah,
        reference_removed_Ah=reference_removed_Ah,
        gauge_soc_percent_start=gauge_soc_percent_start,
        gauge_soc_percent_end=gauge_soc_percent_end,
        gauge_fcc_mAh_end=gauge_fcc_mAh_end,
        voltage_V_end=voltage_V_end,
        current_A_end=current_A_end,
        temperature_C_end=temperature_C_end,
        end_of_charge=end_of_charge,
        end_of_discharge=end_of_discharge,
        ocv_correction_events=ocv_correction_events,
        gauge_data_available=gauge_data_available,
        limitations=limitations,
    )


@dataclass
class MatrixStepEvaluation:
    measurement: MatrixStepMeasurement
    reference_delta_soc_percent: Optional[float]
    gauge_delta_soc_percent: Optional[float]
    soc_error_percent: Optional[float]
    passed: Optional[bool]   # None = nem értékelhető ki (hiányzó gauge adat)
    fail_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "measurement": self.measurement.to_dict(),
            "reference_delta_soc_percent": self.reference_delta_soc_percent,
            "gauge_delta_soc_percent": self.gauge_delta_soc_percent,
            "soc_error_percent": self.soc_error_percent,
            "passed": self.passed,
            "fail_reasons": self.fail_reasons,
        }


def evaluate_step(
    measurement: MatrixStepMeasurement,
    reference_usable_capacity_Ah: float,
    tolerance: ValidationTolerance,
) -> MatrixStepEvaluation:
    """Egyetlen mátrix-cella kiértékelése: a session ALATT mért gauge SOC%-
    delta összevetése a ténylegesen mért (reference) Ah-delta alapján várt
    SOC%-deltával, a `COMPLETE_DISCHARGE` lépésből származó használható
    kapacitáshoz (Ah) viszonyítva."""
    if reference_usable_capacity_Ah <= 0:
        raise ValueError(
            "reference_usable_capacity_Ah > 0 kötelező — a COMPLETE_DISCHARGE "
            "lépés méréséből származik."
        )

    fail_reasons: list[str] = []
    if measurement.status != "DONE":
        fail_reasons.append(
            f"session status={measurement.status!r} (nem DONE): {measurement.reason}"
        )

    if not measurement.gauge_data_available:
        return MatrixStepEvaluation(
            measurement=measurement,
            reference_delta_soc_percent=None,
            gauge_delta_soc_percent=None,
            soc_error_percent=None,
            passed=None,
            fail_reasons=fail_reasons + ["gauge SOC adat nem érvényes/elérhető — cella nem értékelhető ki"],
        )

    assert measurement.gauge_soc_percent_start is not None
    assert measurement.gauge_soc_percent_end is not None

    net_reference_Ah = measurement.reference_added_Ah - measurement.reference_removed_Ah
    reference_delta_soc_percent = net_reference_Ah / reference_usable_capacity_Ah * 100.0
    gauge_delta_soc_percent = measurement.gauge_soc_percent_end - measurement.gauge_soc_percent_start
    soc_error_percent = gauge_delta_soc_percent - reference_delta_soc_percent

    tolerance_ok = abs(soc_error_percent) <= tolerance.soc_error_percent_max
    if not tolerance_ok:
        fail_reasons.append(
            f"|SOC_error_percent|={abs(soc_error_percent):.2f} > tolerancia "
            f"{tolerance.soc_error_percent_max} ({tolerance.source_reference})"
        )

    passed = tolerance_ok and measurement.status == "DONE"

    return MatrixStepEvaluation(
        measurement=measurement,
        reference_delta_soc_percent=reference_delta_soc_percent,
        gauge_delta_soc_percent=gauge_delta_soc_percent,
        soc_error_percent=soc_error_percent,
        passed=passed,
        fail_reasons=fail_reasons,
    )


GATE_VALIDATED = "VALIDATED_FOR_DEFINED_MATRIX"
GATE_INCOMPLETE = "INCOMPLETE_MATRIX"
GATE_FAILED_TOLERANCE = "FAILED_TOLERANCE"


@dataclass
class ValidationMatrixReport:
    step_evaluations: dict[MatrixStepKind, MatrixStepEvaluation]
    reference_usable_capacity_Ah: float
    tolerance: ValidationTolerance
    gate_status: str
    missing_steps: list[str] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "gate_status": self.gate_status,
            "reference_usable_capacity_Ah": self.reference_usable_capacity_Ah,
            "tolerance": self.tolerance.to_dict(),
            "missing_steps": self.missing_steps,
            "failed_steps": self.failed_steps,
            "steps": {k.value: v.to_dict() for k, v in self.step_evaluations.items()},
        }


def evaluate_validation_matrix(
    measurements: dict[MatrixStepKind, MatrixStepMeasurement],
    tolerance: ValidationTolerance,
    required_steps: frozenset[MatrixStepKind] = REQUIRED_STEP_KINDS,
) -> ValidationMatrixReport:
    """SUP03-VALID-002/006 Kapu: `VALIDATED_FOR_DEFINED_MATRIX` KIZÁRÓLAG akkor,
    ha minden kötelező (`required_steps`) cella jelen van, gauge adattal
    kiértékelhető, ÉS a dokumentált toleranciát teljesíti."""
    missing = sorted(k.value for k in required_steps if k not in measurements)
    if missing:
        return ValidationMatrixReport(
            step_evaluations={}, reference_usable_capacity_Ah=0.0, tolerance=tolerance,
            gate_status=GATE_INCOMPLETE, missing_steps=missing, failed_steps=[],
        )

    complete_discharge = measurements[MatrixStepKind.COMPLETE_DISCHARGE]
    reference_usable_capacity_Ah = complete_discharge.reference_removed_Ah
    if reference_usable_capacity_Ah <= 0:
        return ValidationMatrixReport(
            step_evaluations={}, reference_usable_capacity_Ah=0.0, tolerance=tolerance,
            gate_status=GATE_INCOMPLETE,
            missing_steps=["COMPLETE_DISCHARGE: reference_removed_Ah<=0, nincs használható kapacitás-bázis"],
            failed_steps=[],
        )

    evaluations: dict[MatrixStepKind, MatrixStepEvaluation] = {}
    incomplete: list[str] = []
    failed: list[str] = []
    for kind, measurement in measurements.items():
        evaluation = evaluate_step(measurement, reference_usable_capacity_Ah, tolerance)
        evaluations[kind] = evaluation
        if kind not in required_steps:
            continue
        if evaluation.passed is None:
            incomplete.append(kind.value)
        elif evaluation.passed is False:
            failed.append(kind.value)

    if incomplete:
        gate_status = GATE_INCOMPLETE
    elif failed:
        gate_status = GATE_FAILED_TOLERANCE
    else:
        gate_status = GATE_VALIDATED

    return ValidationMatrixReport(
        step_evaluations=evaluations,
        reference_usable_capacity_Ah=reference_usable_capacity_Ah,
        tolerance=tolerance,
        gate_status=gate_status,
        missing_steps=incomplete,
        failed_steps=failed,
    )


def write_validation_matrix_report(output_dir: Path, report: ValidationMatrixReport) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "validation_matrix_report.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


__all__ = [
    "MatrixStepKind",
    "REQUIRED_STEP_KINDS",
    "ValidationMatrixDataError",
    "ValidationTolerance",
    "MatrixStepMeasurement",
    "extract_step_measurement",
    "MatrixStepEvaluation",
    "evaluate_step",
    "GATE_VALIDATED",
    "GATE_INCOMPLETE",
    "GATE_FAILED_TOLERANCE",
    "ValidationMatrixReport",
    "evaluate_validation_matrix",
    "write_validation_matrix_report",
]
