"""
bq_calibration_statistics.py — WP-AK-09 (SUP03-CAL-007/009): legalább 20
boardos gyártási kalibrációs mintavételezés statisztikája + a kötelező
kalibrációs artefaktumok írása.

SUP03-CAL-007 forrás (kiegeszito_szabalyok_03_golden_image_teljes_meresi_
program_v1_0_20260723.txt 524-547. sor): "sample_count >= 20", mérendő és
átlagolandó CC Gain/CC Delta/CC Offset/Board Offset/Temp Offset, tárolandó
átlag/szórás/minimum/maximum/outlier policy/elfogadott mintaszám/elutasított
minták indoka.

**Temp Offset forrás-korlát** (WP-AK-09 gap-lelet, l. Prog/src/
bq_gauge_calibration.py modul docstring): sem a TI datasheet, sem a
Prog/TF1_basic.c nem tartalmaz forrásolt, automatizálható eljárást az Int/
Ext Temp Offset méréséhez — ez a modul ezért `temp_offset`-et KÜLSŐ
bemenetként fogadja el (pl. TI bq Multi Station Tester-ből átvett érték),
NEM méri/számítja saját maga. Egy `temp_offset_source` mező kötelezően
dokumentálja az érték eredetét, hogy a riport sose sugalljon automatizált
mérést egy valójában kézzel átvett értékre.

**Outlier policy** (SUP03-CAL-007 kötelező mező): ez a modul KIZÁRÓLAG a
Data Flash dokumentált érvényességi tartományát (Leírások/TI_BQ/
BQ34Z110PWR.pdf, SLUSB55B, "Table 20. Data Flash Summary" — forráshivatkozás
frissítve F5-1, review-kör 5, l. device_bq34z110.py azonos hivatkozású
kommentje) alkalmazza elutasítási szabályként — egy statisztikai (pl. ±3σ) kizárás
NEM automatikus, mert az egy gyártási minőségbiztosítási/folyamat-döntés,
nem tiszta metrológiai tény (l. Akkuteszter CLAUDE.md §10.7 "safety limitet
ne emelj/ne lazíts tesztzöldítésért" elve analóg alkalmazása: egy kizárási
szabályt sem szabad hallgatólagosan, dokumentálatlanul bevezetni).

**NEM BEKÖTÖTT — NOT_WIRED (review-kör 5, Architektúra A-5, DFAM-026
4. előfordulása)**: ezt a modult jelenleg SEHOL nem importálja/hívja élő
GUI- vagy TestRunner-kód (kizárólag a saját tesztje hivatkozik rá) — a
`golden_image_manifest_builder.py` (maga is NOT_WIRED, l. ott) importálja,
de tranzitívan ez sem ér el élő hívóhelyet. Ez a modul NEM lett törölve —
önmagában teljes és tesztelt statisztikai/artefaktum-előállító logika,
jövőbeli GUI-bekötésre vár.
"""
from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from Prog.src.bq_gauge_calibration import (
    CAL_DELTA_MAX,
    CAL_DELTA_MIN,
    CAL_GAIN_MAX,
    CAL_GAIN_MIN,
)

OUTLIER_POLICY = "DATASHEET_RANGE_VALIDATED_NO_STATISTICAL_EXCLUSION"

# Review-kör 5, Gyári forrás F5-4 (P2): Leírások/TI_BQ/BQ34Z110PWR.pdf
# (SLUSB55B) "Table 20. Data Flash Summary" (Calibration/104, offset 10 =
# Board Offset, offset 11 = Int Temp Offset) mindkettőt explicit I1
# Min=-128/Max=127-tel adja meg — pdftotext -table kinyeréssel önállóan
# visszaellenőrizve (a -layout mód ennél a többoszlopos táblánál
# oszlop-eltolódást okoz, l. F5-1 MAX verifier megjegyzése). Numerikusan
# egyezik az I1 típus natív tartományával, DE ez a modul a `board_offset`/
# `temp_offset` értékeket egy sima `Optional[int]` mezőben, a tényleges
# Data Flash-írás/-olvasás ELŐTT/attól függetlenül fogadja (l. modul
# docstring "temp_offset-et KÜLSŐ bemenetként fogadja el") — a natív I1
# típuskorlát itt SEMMI nem kényszeríti ki programozottan, ezért az
# `add_sample()`-beli explicit ellenőrzés NEM redundáns.
CAL_BOARD_OFFSET_MIN, CAL_BOARD_OFFSET_MAX = -128, 127
CAL_TEMP_OFFSET_MIN, CAL_TEMP_OFFSET_MAX = -128, 127


@dataclass
class CalibrationSample:
    board_id: str
    cc_gain: Optional[float] = None
    cc_delta: Optional[float] = None
    cc_offset: Optional[int] = None
    board_offset: Optional[int] = None
    temp_offset: Optional[int] = None
    temp_offset_source: str = ""
    accepted: bool = True
    reject_reason: str = ""


@dataclass
class FieldStatistics:
    n: int
    mean: Optional[float] = None
    stddev: Optional[float] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None


@dataclass
class CalibrationStatisticsResult:
    sample_count_total: int
    sample_count_accepted: int
    sample_count_rejected: int
    outlier_policy: str
    rejected_reasons: dict[str, int]
    cc_gain: FieldStatistics
    cc_delta: FieldStatistics
    cc_offset: FieldStatistics
    board_offset: FieldStatistics
    temp_offset: FieldStatistics


def _field_stats(values: list[Optional[float]]) -> FieldStatistics:
    clean = [v for v in values if v is not None]
    if not clean:
        return FieldStatistics(n=0)
    return FieldStatistics(
        n=len(clean),
        mean=statistics.mean(clean),
        stddev=statistics.stdev(clean) if len(clean) >= 2 else None,
        minimum=min(clean),
        maximum=max(clean),
    )


class CalibrationStatisticsCollector:
    """SUP03-CAL-007 gyűjtő - `add_sample()` a Data Flash dokumentált
    tartománya (Table 20) ellen validál CC Gain/CC Delta/Board Offset/Temp
    Offset mezőkre (ugyanaz a tartomány, mint amit `bq_gauge_calibration.
    CcGainCalibrationController` is használ íráskor a CC Gain/CC Delta
    esetén - egyetlen forrás a határértékekre). Board Offset/Temp Offset
    explicit ellenőrzése review-kör 5, Gyári forrás F5-4 (P2) pótlása - l.
    CAL_BOARD_OFFSET_MIN/MAX kommentje arról, miért NEM redundáns a natív
    I1 típustartomány mellett sem. CC Offset (I2, szintén van explicit
    Table 20 Min/Max: -32768..32767) NINCS itt ellenőrizve - sibling-
    észrevétel, nem ennek a findingnak a hatóköre (F5-4 csak a két I1
    mezőt nevezte meg), külön batchba halasztva, ha indokolt."""

    def __init__(self) -> None:
        self._samples: list[CalibrationSample] = []

    def add_sample(self, sample: CalibrationSample) -> CalibrationSample:
        reasons = []
        if sample.cc_gain is not None and not (CAL_GAIN_MIN <= sample.cc_gain <= CAL_GAIN_MAX):
            reasons.append("cc_gain_out_of_datasheet_range")
        if sample.cc_delta is not None and not (CAL_DELTA_MIN <= sample.cc_delta <= CAL_DELTA_MAX):
            reasons.append("cc_delta_out_of_datasheet_range")
        if sample.board_offset is not None and not (
            CAL_BOARD_OFFSET_MIN <= sample.board_offset <= CAL_BOARD_OFFSET_MAX
        ):
            reasons.append("board_offset_out_of_datasheet_range")
        if sample.temp_offset is not None and not (
            CAL_TEMP_OFFSET_MIN <= sample.temp_offset <= CAL_TEMP_OFFSET_MAX
        ):
            reasons.append("temp_offset_out_of_datasheet_range")
        if sample.temp_offset is not None and not sample.temp_offset_source:
            reasons.append("temp_offset_missing_source")
        if reasons:
            sample.accepted = False
            sample.reject_reason = ";".join(reasons)
        self._samples.append(sample)
        return sample

    @property
    def samples(self) -> tuple[CalibrationSample, ...]:
        return tuple(self._samples)

    def compute(self) -> CalibrationStatisticsResult:
        accepted = [s for s in self._samples if s.accepted]
        rejected = [s for s in self._samples if not s.accepted]
        rejected_reasons: dict[str, int] = {}
        for s in rejected:
            for reason in s.reject_reason.split(";"):
                if reason:
                    rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
        return CalibrationStatisticsResult(
            sample_count_total=len(self._samples),
            sample_count_accepted=len(accepted),
            sample_count_rejected=len(rejected),
            outlier_policy=OUTLIER_POLICY,
            rejected_reasons=rejected_reasons,
            cc_gain=_field_stats([s.cc_gain for s in accepted]),
            cc_delta=_field_stats([s.cc_delta for s in accepted]),
            cc_offset=_field_stats([s.cc_offset for s in accepted]),
            board_offset=_field_stats([s.board_offset for s in accepted]),
            temp_offset=_field_stats([s.temp_offset for s in accepted]),
        )


# ── SUP03-CAL-009: kötelező artefaktumok ────────────────────────────────

def write_calibration_master_report(
    output_dir: Path, result: CalibrationStatisticsResult, meta: dict,
) -> Path:
    """calibration_master_report.json - a `meta` dict a hívó felelőssége
    (pl. session_id, dátum, app verzió, hardver profil) - ez a modul nem
    ismer semmilyen session-fogalmat, csak a statisztikát tudja."""
    path = output_dir / "calibration_master_report.json"
    payload = {"meta": meta, "statistics": _statistics_to_jsonable(result)}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _statistics_to_jsonable(result: CalibrationStatisticsResult) -> dict:
    d = asdict(result)
    return d


def write_calibration_20_board_statistics_csv(
    output_dir: Path, samples: list[CalibrationSample],
) -> Path:
    """calibration_20_board_statistics.csv - minden mintát külön sorként ír
    (elfogadott ÉS elutasított is, `accepted`/`reject_reason` oszlopokkal -
    az elutasított minták indoka SUP03-CAL-007 kötelező mezője, nem szabad
    csendben kihagyni a CSV-ből)."""
    path = output_dir / "calibration_20_board_statistics.csv"
    fieldnames = [
        "board_id", "cc_gain", "cc_delta", "cc_offset", "board_offset",
        "temp_offset", "temp_offset_source", "accepted", "reject_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sample in samples:
            writer.writerow(asdict(sample))
    return path


def write_voltage_calibration_procedure_txt(output_dir: Path, description: str) -> Path:
    """voltage_calibration_procedure.txt - SUP03-CAL-008: a feszültség-
    kalibráció EGYEDI, per-unit eljárás, nem átlagolható a 20-boardos
    statisztikával - a `description` a hívó (workflow) felelőssége, ez a
    modul csak a fájlba írást végzi, dokumentálva a policy-t is."""
    path = output_dir / "voltage_calibration_procedure.txt"
    header = (
        "Voltage Divider kalibráció - EGYEDI eljárás per board/pack "
        "(SUP03-CAL-008: nem helyettesíthető a 20-boardos current/temp "
        "átlaggal, a voltage-divider és board-tolerancia egyedi).\n\n"
    )
    path.write_text(header + description, encoding="utf-8")
    return path


def write_reference_instruments_json(output_dir: Path, instruments: dict) -> Path:
    """reference_instruments.json - referencia műszerek azonosítói
    (DMM/áramforrás modell, sorozatszám, utolsó kalibráció dátuma stb.) -
    a hívó adja meg, ez a modul csak validálja, hogy a dict nem üres."""
    if not instruments:
        raise ValueError("write_reference_instruments_json: instruments dict nem lehet üres")
    path = output_dir / "reference_instruments.json"
    path.write_text(json.dumps(instruments, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_traceability_notes_txt(output_dir: Path, notes: str) -> Path:
    """traceability_notes.txt - szabad szöveges nyomon-követhetőségi
    jegyzetek (pl. eltérések, elutasított minták indoklása részletesen,
    operátor megjegyzések)."""
    path = output_dir / "traceability_notes.txt"
    path.write_text(notes, encoding="utf-8")
    return path


def write_all_calibration_artifacts(
    output_dir: Path,
    *,
    result: CalibrationStatisticsResult,
    samples: list[CalibrationSample],
    meta: dict,
    voltage_calibration_description: str,
    reference_instruments: dict,
    traceability_notes: str,
) -> list[Path]:
    """SUP03-CAL-009 mind az öt kötelező artefaktumot megírja egy hívással -
    kényelmi wrapper, a hívónak nem kell az öt egyedi függvényt egyenként
    hívnia (de megteheti, ha csak egy artefaktumot kell frissítenie)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    return [
        write_calibration_master_report(output_dir, result, meta),
        write_calibration_20_board_statistics_csv(output_dir, samples),
        write_voltage_calibration_procedure_txt(output_dir, voltage_calibration_description),
        write_reference_instruments_json(output_dir, reference_instruments),
        write_traceability_notes_txt(output_dir, traceability_notes),
    ]
