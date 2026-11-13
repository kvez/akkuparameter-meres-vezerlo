"""
ti_reference_validation.py — WP-AK-14: EV2400 / TI reference validation.

Cél (Konszolidált terv WP-AK-14 szakasza, szó szerint): "A saját Pico backend
hiteles összehasonlítása a TI referenciaeszközzel", kilenc kategóriában:
device identity, standard commands, Data Flash read, Data Flash write,
checksum, Qmax, Ra, security state, export/readback.

Ez a modul egy POST-HOC, GUI-független ÖSSZEHASONLÍTÓ réteg — a
`validation_matrix.py` (WP-AK-12) mintáját követve NEM hoz létre új fizikai
állapotgépet. A "saját" oldal adatai a MEGLÉVŐ WP-AK-06/07/08/09 rétegekből
(`BqDiagnosticsSnapshot` / `ControlledWriteResult` / DF snapshot hash-ek)
származnak; a "referencia" oldal (egy valódi TI EV2400 + bqStudio session
tényleges kiolvasása, ugyanarról a fizikai BQ34Z110-ről) fizikai HIL-eszközt
igényel (`BLK-025` alá tartozik — a szabályrendszer SUP03-ACCEPT-003 pontja
"valos EVSW/EV2400 vagy sajat BQ kommunikacio"-t sorol fel kötelező fizikai
bizonyítékként). **Emiatt ez a modul szándékosan NEM feltételez semmilyen
konkrét EV2400/bqStudio export-fájlformátumot** (nincs erre sourced adat sem
a `Leírások/TUDÁSBÁZIS.txt`-ben, sem a normatív dokumentumokban — kitalált
formátum-parsert írni megsértené a "hardver/BQ/TI tényt ne találj ki" elvet).
A hívó (egy jövőbeli GUI-wizard vagy manuális jegyzőkönyv) adja át a
referencia-oldali értékeket egy egyszerű, kategóriánkénti dict-ként.

**"Elv" (WP-AK-14, szó szerint)**: "A saját rendszer production programming
funkciója addig ne legyen teljesen automatikus, amíg ez a referencia-validáció
nem történt meg." A `ProductionProgrammingController` (WP-AK-13) MÁR eleve
kizárólag operátor-vezérelt (minden lépés explicit metódushívás, nincs
"futtasd le mindet automatikusan" mód) — ez az Elv-et SZERKEZETILEG teljesíti,
nincs jelenleg olyan automatikus mód, amit korlátozni kellene.
`is_automatic_programming_permitted()` egy explicit, tesztelt policy-kapu egy
JÖVŐBELI automatikus mód számára — ez nem feltételezett jövőbeli kód, hanem a
normatív Elv végrehajtható formába öntése, hívó hely nélkül addig, amíg ilyen
mód nem létezik.

**NEM BEKÖTÖTT — NOT_WIRED (review-kör 5, Architektúra A-5, DFAM-026
4. előfordulása)**: ezt a modult a `production_programming_controller.py`
importálja és fogadja el (`ProductionRecord.ti_reference_validation`,
`record_ti_reference_validation()`) — DE a `production_programming_
controller.py` MAGA is NOT_WIRED (review-kör 4, Architektúra F6, l. ott):
nincs élő GUI/TestRunner hívóhely, ami ténylegesen létrehozná/meghívná a
controllert, tehát ez a modul is tranzitívan árva. Ez a modul NEM lett
törölve — önmagában teljes és tesztelt összehasonlító logika, jövőbeli
GUI-wizard/HIL-validáció bekötésére vár (l. `BLK-025`).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from Prog.src.bq_diagnostics import BqDiagnosticsSnapshot, decode_control_status


class TiReferenceField(Enum):
    """A Konszolidált terv WP-AK-14 "Compare" listája, szó szerint."""
    DEVICE_IDENTITY = "DEVICE_IDENTITY"
    STANDARD_COMMANDS = "STANDARD_COMMANDS"
    DATA_FLASH_READ = "DATA_FLASH_READ"
    DATA_FLASH_WRITE = "DATA_FLASH_WRITE"
    CHECKSUM = "CHECKSUM"
    QMAX = "QMAX"
    RA = "RA"
    SECURITY_STATE = "SECURITY_STATE"
    EXPORT_READBACK = "EXPORT_READBACK"


REQUIRED_TI_REFERENCE_FIELDS: frozenset[TiReferenceField] = frozenset(TiReferenceField)


class TiReferenceValidationDataError(ValueError):
    """`evidence_reference`/`reference_instrument` hiányzik vagy üres."""


@dataclass
class FieldComparisonResult:
    """Egyetlen kategória összehasonlítása — SOHA nem csendes egyezés: ha
    valamelyik oldal hiányzik, `available=False`/`match=None`, nem `True`."""
    field: TiReferenceField
    own_value: Any
    reference_value: Any
    available: bool
    match: Optional[bool]
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "field": self.field.value,
            "own_value": self.own_value,
            "reference_value": self.reference_value,
            "available": self.available,
            "match": self.match,
            "note": self.note,
        }


GATE_PASS = "TI_REFERENCE_VALIDATED_MATCH"
GATE_FAIL = "TI_REFERENCE_VALIDATED_MISMATCH"
GATE_INCOMPLETE = "TI_REFERENCE_VALIDATION_INCOMPLETE"


@dataclass
class TiReferenceValidationRecord:
    reference_instrument: str
    evidence_reference: str
    operator: str
    field_results: dict[TiReferenceField, FieldComparisonResult] = field(default_factory=dict)
    gate_status: str = GATE_INCOMPLETE
    missing_fields: list[str] = field(default_factory=list)
    mismatched_fields: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "reference_instrument": self.reference_instrument,
            "evidence_reference": self.evidence_reference,
            "operator": self.operator,
            "gate_status": self.gate_status,
            "missing_fields": self.missing_fields,
            "mismatched_fields": self.mismatched_fields,
            "limitations": self.limitations,
            "field_results": {k.value: v.to_dict() for k, v in self.field_results.items()},
        }


def evaluate_ti_reference_validation(
    own_readings: dict[TiReferenceField, Any],
    reference_readings: dict[TiReferenceField, Any],
    reference_instrument: str,
    evidence_reference: str,
    operator: str,
    required_fields: frozenset[TiReferenceField] = REQUIRED_TI_REFERENCE_FIELDS,
    field_comparators: dict[TiReferenceField, Callable[[Any, Any], bool]] | None = None,
) -> TiReferenceValidationRecord:
    """A `validation_matrix.py::evaluate_validation_matrix()` mintáját követő
    Kapu: `GATE_PASS` KIZÁRÓLAG akkor, ha minden kötelező kategória mindkét
    oldalon jelen van ÉS egyezik. Egyetlen hiányzó kategória `GATE_INCOMPLETE`-
    et ad (nem hallgatólagos PASS-t), egyetlen eltérés `GATE_FAIL`-t.

    `evidence_reference` kötelező, nem lehet üres — ugyanaz az elv, mint
    `ValidationTolerance.source_reference` (SUP03-VALID-006) vagy a Golden GG
    `bq34z110_compatibility_confirmed` evidence-szöveg (SUP03-GG-011): egy
    fizikai referencia-mérés sosem fogadható el forrás/jegyzőkönyv nélkül.

    `field_comparators` (Teljes kódbázis review 3. kör, 2026-08-29,
    Metrológia/Adatintegritás P3 lelet — a modul DORMANT, jelenleg nincs
    bekötve élő útvonalba, csak jövőbeli bekötéskor releváns): alapból (ha
    nincs megadva) minden mezőt szigorú `==` hasonlít össze, ami a
    STANDARD_COMMANDS kategória (voltage_mV/avg_current_mA/temperature_C/
    soc_percent) esetén helytelen — ezek a saját és a referencia műszer
    ELTÉRŐ IDŐPONTBAN vett mérései, a batteria állapota a két mérés között
    ténylegesen változik, így egy fizikailag EGYEZŐ állapotot is hamis
    `GATE_FAIL`-nek minősítene (CLAUDE.md §12: "ne ígérj nagyobb pontosságot,
    mint amit... az időzítés alátámaszt"). Ez a paraméter STRUKTURÁLISAN
    teszi lehetővé egy jövőbeli hívó számára, hogy tűréssel összehasonlító
    függvényt adjon meg mezőnként (pl. `{TiReferenceField.STANDARD_COMMANDS:
    lambda o, r: abs(o["voltage_mV"] - r["voltage_mV"]) <= TOLERANCE}`) —
    szándékosan NEM ad meg itt konkrét tűrésértéket, mert forrás (gyári
    adatlap vagy dokumentált fizikai mérés) nélkül számszerű küszöböt
    kitalálni megsértené a CLAUDE.md §6/§10.7 elvét. Alapértelmezett `None`
    esetén a viselkedés változatlan (szigorú `==`), így ez a paraméter a
    meglévő hívókra nézve NEM jelent viselkedésváltozást.
    """
    if not evidence_reference.strip():
        raise TiReferenceValidationDataError(
            "evidence_reference kötelező, nem lehet üres — a referencia-mérés "
            "(EV2400/bqStudio session vagy manuális jegyzőkönyv) azonosítója "
            "nélkül a validáció nem fogadható el (SUP03-VALID-006/SUP03-GG-011 "
            "mintájú 'ne legyen forrás nélkül elfogadva' elv)."
        )
    if not reference_instrument.strip():
        raise TiReferenceValidationDataError("reference_instrument kötelező, nem lehet üres.")

    field_results: dict[TiReferenceField, FieldComparisonResult] = {}
    missing: list[str] = []
    mismatched: list[str] = []
    comparators = field_comparators or {}

    for f in required_fields:
        own_present = f in own_readings
        ref_present = f in reference_readings
        if not (own_present and ref_present):
            missing.append(f.value)
            field_results[f] = FieldComparisonResult(
                field=f,
                own_value=own_readings.get(f),
                reference_value=reference_readings.get(f),
                available=False,
                match=None,
                note="hiányzó mérés (saját vagy referencia oldal)",
            )
            continue
        own_value = own_readings[f]
        ref_value = reference_readings[f]
        comparator = comparators.get(f)
        match = comparator(own_value, ref_value) if comparator is not None else own_value == ref_value
        if not match:
            mismatched.append(f.value)
        field_results[f] = FieldComparisonResult(
            field=f, own_value=own_value, reference_value=ref_value,
            available=True, match=match,
        )

    if missing:
        gate_status = GATE_INCOMPLETE
    elif mismatched:
        gate_status = GATE_FAIL
    else:
        gate_status = GATE_PASS

    return TiReferenceValidationRecord(
        reference_instrument=reference_instrument,
        evidence_reference=evidence_reference,
        operator=operator,
        field_results=field_results,
        gate_status=gate_status,
        missing_fields=missing,
        mismatched_fields=mismatched,
    )


def is_automatic_programming_permitted(record: Optional[TiReferenceValidationRecord]) -> bool:
    """WP-AK-14 "Elv": a production programming funkció csak akkor válhatna
    teljesen automatikussá, ha van egy `GATE_PASS` állapotú TI referencia-
    validáció. Jelenleg a `ProductionProgrammingController` (WP-AK-13) MÁR
    kizárólag operátor-vezérelt (nincs "automatikus" mód) — ez a függvény egy
    jövőbeli automatikus mód kötelező előfeltétele lenne, nincs jelenleg
    hívó helye.

    A `gate_status==GATE_PASS` önmagában NEM elég: `evaluate_ti_reference_
    validation()` egy szűkített `required_fields`-szel (pl. csak QMAX-ra) is
    hívható részleges/gyors ellenőrzéshez — egy ilyen rekord GATE_PASS-t
    kapna, holott a terv teljes 9 kategóriás listájából csak egyet
    ellenőrzött. Ezért itt explicit ellenőrizzük, hogy a rekord a TELJES,
    kanonikus `REQUIRED_TI_REFERENCE_FIELDS` halmazt fedte-e le."""
    return (
        record is not None
        and record.gate_status == GATE_PASS
        and set(record.field_results.keys()) >= REQUIRED_TI_REFERENCE_FIELDS
    )


def write_ti_reference_validation_record(output_dir: Path, record: TiReferenceValidationRecord) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "ti_reference_validation.json"
    path.write_text(json.dumps(record.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def own_readings_from_bq_diagnostics(snapshot: BqDiagnosticsSnapshot) -> dict[TiReferenceField, Any]:
    """Kényelmi helper: a WP-AK-07 `BqDiagnosticsSnapshot`-ból tölti ki azokat
    a kategóriákat, amik passzív OLVASÁSSAL lefedhetők (DEVICE_IDENTITY,
    STANDARD_COMMANDS, QMAX, RA, SECURITY_STATE) — a `bq_diagnostics.py`
    kapu-elvét követve egy mező csak akkor kerül be, ha `valid=True` (egy
    érvénytelen mező sosem válik csendben "jelen levő" összehasonlítási
    alappá). A DATA_FLASH_READ/DATA_FLASH_WRITE/CHECKSUM/EXPORT_READBACK
    kategóriák NEM passzív olvasások, hanem write/backup/restore MŰVELETEK
    bizonyítékai (l. `bq_controlled_write.ControlledWriteResult` /
    `pico_bq_service` DF snapshot hash-ek) — ezeket a hívónak KÜLÖN kell
    hozzáadnia a visszaadott dict-hez, ez a helper nem fabrikálja."""
    readings: dict[TiReferenceField, Any] = {}

    if (
        snapshot.device_type.valid and snapshot.fw_version.valid
        and snapshot.hw_version.valid and snapshot.chem_id.valid
    ):
        readings[TiReferenceField.DEVICE_IDENTITY] = {
            "device_type": snapshot.device_type.value,
            "fw_version": snapshot.fw_version.value,
            "hw_version": snapshot.hw_version.value,
            "chem_id": snapshot.chem_id.value,
        }

    if (
        snapshot.voltage_mV.valid and snapshot.avg_current_mA.valid
        and snapshot.temperature_C.valid and snapshot.soc_percent.valid
        and snapshot.flags.valid
    ):
        readings[TiReferenceField.STANDARD_COMMANDS] = {
            "voltage_mV": snapshot.voltage_mV.value,
            "avg_current_mA": snapshot.avg_current_mA.value,
            "temperature_C": snapshot.temperature_C.value,
            "soc_percent": snapshot.soc_percent.value,
            "flags": snapshot.flags.value,
        }

    if snapshot.qmax.valid:
        readings[TiReferenceField.QMAX] = snapshot.qmax.value

    if snapshot.ra_table_mohm.valid:
        readings[TiReferenceField.RA] = snapshot.ra_table_mohm.value

    if snapshot.control_status.valid and snapshot.control_status.value is not None:
        bits = decode_control_status(snapshot.control_status.value)
        readings[TiReferenceField.SECURITY_STATE] = {"ss": bits.ss, "fas": bits.fas}

    return readings


__all__ = [
    "TiReferenceField",
    "REQUIRED_TI_REFERENCE_FIELDS",
    "TiReferenceValidationDataError",
    "FieldComparisonResult",
    "GATE_PASS",
    "GATE_FAIL",
    "GATE_INCOMPLETE",
    "TiReferenceValidationRecord",
    "evaluate_ti_reference_validation",
    "is_automatic_programming_permitted",
    "write_ti_reference_validation_record",
    "own_readings_from_bq_diagnostics",
]
