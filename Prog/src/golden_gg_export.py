"""
golden_gg_export.py — WP-AK-11: TI Golden GG Maker (TI-SLUUBD0A) system-rate
csomag-előkészítés export/import rétege (SUP03-GG-001..012).

Governance-v2 review-kör 1 (CF-11a, 2026-09-07): a `Leírások/TI_BQ/`
tévesen MISSING_LOCAL-nak jelölte a TI-SLUUBD0A ("Golden GG Maker and
Resistance Temperature Compensation Optimizer") dokumentumot, holott az
mindvégig fizikailag jelen volt (a modul docstringje korábban tévesen
"SLUUBC9"-re hivatkozott) — ez hibás premisszát okozott: a `config.txt`
formátumát "FELTÉTELEZÉS"-ként implementáltuk, a tényleges TI-specifikáció
nélkül, ugyanaz a hibaosztály, mint a `gpcchem_export.py` (CF-35) esetén.
A dokumentum §3.1 (Configuration File) + §3.4.1 (Config.txt File example)
alapján (VERIFIED_PRIMARY, l. CLAIM_LEDGER.yaml GPC-GG-001) javítva —
`GoldenGgConfig.to_text()`, l. ott (CF-36).

**Architekturálisan NYITOTT, TUDATOSAN NEM javított rész** (l.
DECISION_REGISTER.md GOV-018, MAX verifier 2026-09-09 P1 korrekciója
UTÁN pontosítva): a forrás §3 szerint a submission zip kötelezően
tartalmazza a `roomtemp.csv` MELLETT egy KÜLÖN `lowtemp.csv`-t is (egy
MÁSODIK, alacsony hőmérsékletű kamratesztből, §2/§3/§3.2.3 8. lépés:
"repeat all steps with temperature set to 0°C") — a projekt jelenlegi
GOLDEN_GG session-je NEM végez alacsony hőmérsékletű fizikai ciklust,
ezért `lowtemp.csv` nem állítható elő. Ez VALÓDI, forrás szerinti
NON-CONFORMANCE, NEM a forrás által engedélyezett üzemmód — a §3.2.1
"egy felállás is elég" mondata a RbL VS. termikus-modell paraméterek
KÖZÖTTI választásról szól (§3.2.3 6.a/6.b/6.c lépés, mindkettő
SZOBAHŐMÉRSÉKLETI teszt), NEM a room/low temperature választásról,
aminek mindkét fájlja forrás szerint kötelező kivétel nélkül. A hiány
TUDATOSAN NEM javítva, mert a pótlásához egy TELJESEN ÚJ, alacsony
hőmérsékletű fizikai teszt-eljárás (hőkamra, külön session-fázis)
bevezetése kellene, ami messze túlmutat egy code-only export-formátum-
fixen — a csomag emiatt a `GOLDEN_GG_EXPERIMENTAL_PACKAGE_ONLY` kapun
és a `metadata_limitations.json`-ön keresztül jelzi a hiányos állapotot,
nem állít hamis PASS-t, de a hiány maga NEM forrás-szentesített. Hasonló,
tudatosan nem javított rés a `gg.csv`: a forrás §3.3 szerint egy VALÓDI
EVSW (*.gg) vagy bqStudio (*.gg.csv) export kellene legyen — a projekt
jelenlegi `build_gg_csv_text()`-je a BQ34Z110 register-readbackből
ÁLLÍTJA ELŐ ezt a fájlt, ami tartalmilag releváns, de NEM formailag
azonos egy tényleges EVSW/bqStudio exporttal (nincs EVSW/bqStudio a
jelenlegi hardver-felállásban). Mindkettő PROJECT_POLICY/HIL-mérnöki
döntést igénylő, valódi non-conformance, nem code-only formátumhiba —
l. GOV-018 a teljes indoklásért.

A `roomtemp.csv` oszlopai és mértékegységei (mV, mA, s, °C)
a SUP03-GG-008 által explicit előírtak. Az áram előjele: POZITÍV töltés
alatt, NEGATÍV a rendszerre jellemző kisütés alatt (SUP03-GG-005/006: a
teljes töltési szakasz IS a folytonos napló része, ellentétben a ChemID
`roomtemp_rel_dis_rel.csv`-jével, ami csak a relax/kisütés/relax szakaszt
tartalmazza).

SUP03-GG-011 kompatibilitás-kapu: a `gg_out.csv` importja SOHA nem
feltételezheti hallgatólagosan, hogy a BQ34Z110 end-to-end kompatibilis a
TI szolgáltatással — a `bq34z110_compatibility_confirmed=True` csak
explicit, dokumentált evidence-szöveggel (próbaküldés eredménye vagy TI
support-hivatkozás) állítható, különben `GOLDEN_GG_EXPERIMENTAL_PACKAGE_ONLY`
marad (l. `import_ti_gg_output()`).
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

from Prog.src.bq_data_flash_service import FIELD_REGISTRY
from Prog.src.bq_diagnostics import BqDiagnosticsSnapshot
from Prog.src.gpcchem_export import (
    CSV_COLUMN_ORDER,
    NumCellSeriesMismatchError,
    build_roomtemp_csv_text,
    export_interval_violations,
    resample_for_export,
    sha256_of_file,
    validate_num_cell_series,
)


class GoldenGgSample(NamedTuple):
    """Egy nyers mintapont — elapsed_s a Golden GG session TELJES fizikai
    ciklusának (töltés kezdetétől az 5h relax végéig) folytonos idődomén-je.
    current_A: POZITÍV töltés, NEGATÍV kisütés alatt (SUP03-GG-005/006)."""
    elapsed_s: float
    voltage_V: float
    current_A: float
    temperature_C: float


class GoldenGgSnapshotIncompleteError(ValueError):
    """SUP03-GG-009/011: a gg.csv forrás-pillanatkép hiányos (qmax/Ra
    table nem olvasható) — a csomag NEM állítható elő hiányos gauge-
    parameterekkel, mert az a TI eszköznél csendben félrevezető eredményt
    okozna."""


class GoldenGgExportIntervalError(ValueError):
    """R1 B8c, F-28 (P3, DFAM-003, D-B8-4): a resample-elt kimenetben
    100 s-nál nagyobb mintaköz (adatlyuk) van -- TI SLUUBD0A §3: 5-100 s.
    GG-ben nincs külön minőségi objektum (a sikeres builder-visszatérés
    közvetlenül "beküldhető" csomagot jelent), ezért a csomag írás ELŐTT
    blokkolva (SUP03-GG-011 precedens, GoldenGgSnapshotIncompleteError
    mintája)."""


class GoldenGgCompatibilityNotConfirmedError(ValueError):
    """SUP03-GG-011: `compatibility_confirmed=True` csak dokumentált,
    nem üres evidence-szöveggel állítható — nem hallgatólagos feltételezés."""


# Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra P3
# lelet: `resample_for_export()` korábban byte-azonosan duplikálva volt itt
# és `gpcchem_export.py`-ban — a canonikus implementáció most ott él (a
# `ChemIdSample`/`GoldenGgSample` struktúrálisan azonos, mindkettő csak
# `elapsed_s`-t olvas a logikában), ide csak importálva, l. gpcchem_export.
# py::resample_for_export() docstringje.


@dataclass
class GoldenGgConfig:
    """config.txt tartalmának forrás-adatai (SUP03-GG-002/003/004/009)."""
    num_cell_series: int
    battery_model: str
    battery_sample_id: str
    system_rate_current_A: float
    thermal_environment_description: str = ""
    thermistor_placement_description: str = ""
    chemistry: str = "PbA"
    sample_interval_s: float = 30.0
    room_temperature_C: float = 20.0
    manufacturer_cutoff_voltage_V: float = 0.0
    app_version: str = ""
    git_commit: str = ""
    operator: str = ""

    def to_text(self, chem_id: str) -> str:
        """CF-36 (governance-v2 review-kör 1, VERIFIED_PRIMARY kulcsnevek/
        sorrend/ProcessingType=4-re, TI-SLUUBD0A §3.1 Configuration File
        (1 Each) + §3.4.1 Config.txt File example -- a ChemID érték
        számrendszere KIVÉTEL, l. lent):
        a `config.txt` "ASCII text dictionary", `[section]` fejléc és `#`
        komment sor NÉLKÜL (ugyanaz a hibaosztály, mint CF-35-nél). A
        dokumentum §3.4.1 példája "Key=Value" alakot használ (space
        nélkül, eltérően a TI-SLVA725A §2.2.1 "Key = Value" példájától —
        két KÜLÖN TI-dokumentum, két KÜLÖN, önmagában konzisztens
        formázási minta; itt A SAJÁT forrás saját példáját követjük).
        Kötelező kulcsok (§3.1): `ProcessingType` (érték MINDIG 4 a
        Resistance Temperature Compensation Optimizer/Golden GG Maker
        toolhoz -- keresztellenőrizve a TI-SLVA725A testvér-dokumentummal,
        ami MÁS toolhoz 2-t ír elő), `ChemID` (a ténylegesen programozott
        BQ34Z110 chem_id regiszter readback-je -- l. build_golden_gg_
        submission_package(), NEM a projekt saját `chemistry` string
        mezője, ami csak "PbA"-t tartalmaz). MAX verifier (2026-09-09) P2
        lelete: a `ChemID` érték SZÁMRENDSZERE (decimális vs. hexa)
        forrásból NEM igazolható -- a §3.4.1 példa ("ChemID=3514") nem
        egyértelműsíti, miközben a projekt MINDENÜTT MÁSHOL hexában
        jeleníti meg a chem_id-t (pl. bq_diagnostics_panel.py, golden_gg_
        controller.py, traditional_learning_controller.py mind "0x{...:
        04X}" formátumot használ). Az itt írt `str(int_érték)` (decimális)
        emiatt SOURCE_REQUIRED/HIL_PENDING, NEM VERIFIED_PRIMARY -- l.
        CLAIM_LEDGER.yaml GPC-GG-001, valós bqStudio/EVSW chem-ID
        megjelenítéssel egyeztetendő submission előtt.

        `NumCellSeries`, majd a 4,
        nulla-alapú oszlopindex kulcs (`ElapsedTimeColumn`/`VoltageColumn`/
        `CurrentColumn`/`TemperatureColumn`, a §3.4.1 példa sorrendjét
        követve) -- ezek NÉLKÜL a valós TI tool nem tudja értelmezni a
        `roomtemp.csv` adatfájlt.

        D-Extra (2026-09-13, felhasználói döntés, DECISION_REGISTER.md
        GOV-023, eredetileg `gpcchem_export.py::GpcChemConfig`-ra hozott
        döntés, SIBLING-SWEEP-pel ide is átvezetve -- azonos defekt-
        család, l. `gpcchem_export.py::GpcChemConfig.to_text()`
        docstringje): a projekt saját nyomonkövethetőségi mezői
        (Chemistry/BatteryModel/BatterySampleID/SystemRateCurrentA/
        SampleIntervalSeconds/RoomTemperatureC/
        ManufacturerCutoffVoltageV/ThermalEnvironmentDescription/
        ThermistorPlacementDescription/AppVersion/GitCommit/Operator)
        KORÁBBAN itt, a 7 kötelező kulcs UTÁN szerepeltek -- ez
        megsértette a forrás "ASCII text dictionary" előírását, mert a
        szabad szöveges mezők (főleg `Operator`/
        `ThermalEnvironmentDescription`/`ThermistorPlacementDescription`)
        elvben nem-ASCII karaktert tartalmazhatnak. Mostantól ezek a
        `project_metadata_dict()`-en keresztül egy KÜLÖN, UTF-8 sidecar
        fájlba (`project_metadata.json`, l.
        `build_golden_gg_submission_package()`) kerülnek -- a `config.txt`
        innentől KIZÁRÓLAG a 7 TI-kötelező, inherensen ASCII-biztos (int/
        fix string) kulcsot tartalmazza. A hiányzó metaadat-limitációkat
        (korábban egy `# LIMITATION` komment sor formájában, MAGÁBAN a
        config.txt-ben) a metadata_limitations.json MÁR hordozza
        (ugyanabban a zip-ben) -- a duplikált, komment-alapú csatorna
        eltávolítva, elkerülve egy MÁSODIK strukturális non-conformance
        forrást ugyanabban a fájlban."""
        idx = CSV_COLUMN_ORDER
        lines = [
            "ProcessingType=4",
            f"ChemID={chem_id}",
            f"NumCellSeries={self.num_cell_series}",
            f"ElapsedTimeColumn={idx.index('elapsed_time_s')}",
            f"VoltageColumn={idx.index('voltage_mV')}",
            f"CurrentColumn={idx.index('current_mA')}",
            f"TemperatureColumn={idx.index('cell_surface_temperature_C')}",
        ]
        return "\n".join(lines) + "\n"

    def project_metadata_dict(self) -> dict:
        """D-Extra (2026-09-13, sibling-sweep a GpcChemConfig-fixből): a
        projekt-specifikus nyomonkövethetőségi mezők, KÜLÖN a
        TI-kötelező `config.txt`-től -- l. `to_text()` docstringje.
        Szabad szöveges (potenciálisan nem-ASCII) mezőket is
        tartalmazhat, ezért csak UTF-8 sidecar fájlba (`project_metadata.
        json`) íródik, sosem a `config.txt`-be."""
        return {
            "chemistry": self.chemistry,
            "battery_model": self.battery_model,
            "battery_sample_id": self.battery_sample_id,
            "system_rate_current_A": self.system_rate_current_A,
            "sample_interval_s": self.sample_interval_s,
            "room_temperature_C": self.room_temperature_C,
            "manufacturer_cutoff_voltage_V": self.manufacturer_cutoff_voltage_V,
            "thermal_environment_description": self.thermal_environment_description,
            "thermistor_placement_description": self.thermistor_placement_description,
            "app_version": self.app_version,
            "git_commit": self.git_commit,
            "operator": self.operator,
        }


# Teljes kódbázis review 4. kör (2026-08-29), Szoftver/Architektúra P3
# lelet: build_sysrate_csv_text() byte-azonosan duplikálva volt itt és
# gpcchem_export.py::build_roomtemp_csv_text()-ben (a GoldenGgSample/
# ChemIdSample struktúrálisan azonos, mindkettő ugyanezt a 4 mezőt olvassa)
# — a canonikus implementáció most ott él, ide csak delegálva. SUP03-GG-008:
# kötelező oszlopok, mV/mA/°C egységekben. Előjel: pozitív töltés, negatív
# kisütés alatt.
def build_sysrate_csv_text(samples: list[GoldenGgSample]) -> str:
    return build_roomtemp_csv_text(samples)


# SUP03-GG-009/010: a gg.csv-be kerülő gauge-paraméterek — a Golden GG Maker
# tool bemeneteként a ChemID programozás UTÁN érvényes gauge-állapotot
# reprezentálják, ugyanabból a Bq34z110ReadOnlyDriver-ből, amit a
# BQ Diagnostics/kalibráció rétegek is használnak (WP-AK-06..09).
_GG_CSV_FIELDS: tuple[tuple[str, str], ...] = (
    ("device_type", "device_type"),
    ("fw_version", "fw_version"),
    ("hw_version", "hw_version"),
    ("chem_id", "chem_id"),
    ("update_status", "update_status"),
    ("qmax", "qmax"),
    ("ra_table_mohm", "ra_table_mohm"),
    ("full_charge_capacity_mAh", "full_charge_capacity_mAh"),
    ("cycle_count", "cycle_count"),
    ("dod0", "dod0"),
)


def build_gg_csv_text(snapshot: BqDiagnosticsSnapshot) -> str:
    """SUP03-GG-009: "gg.csv — a helyes ChemID programozása után exportált
    gauge parameter fájl". Minden mező a snapshot saját valid/error
    állapotát is hordozza (ugyanaz a kapu-elv, mint `bq_diagnostics.py`
    modul docstring-je: malformed reply sosem válhat csendesen 0-vá)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["parameter", "value", "valid", "error"])
    for label, attr in _GG_CSV_FIELDS:
        reading = getattr(snapshot, attr)
        writer.writerow([
            label,
            reading.value if reading.valid else "",
            reading.valid,
            reading.error or "",
        ])
    return buf.getvalue()


def _qmax_within_ti_range(value: int | None) -> bool:
    """R1 F-43 (NS-13, DFAM-003): a gg.csv-be kerülő Qmax a TI-tartományon
    belül van-e. A tartományforrás ugyanaz a FIELD_REGISTRY-bejegyzés
    ("qmax_cell0_initial_mAh": SLUSB55B Table 20, Qmax Cell 0, SC82/off0,
    I2, Min 0, Max 32767), mint a traditional_learning_controller.py
    azonos nevű B4-predikátumáé. Csak tartomány-, nem plauzibilitás-
    ellenőrzés (Q-3). None és hiányzó határ -> False."""
    spec = FIELD_REGISTRY["qmax_cell0_initial_mAh"]
    lo, hi = spec.min_value, spec.max_value
    return value is not None and lo is not None and hi is not None and lo <= value <= hi


@dataclass
class MetadataLimitations:
    """SUP03-GG-003/004/009: a fizikailag nem automatikusan ellenőrizhető
    metaadatok (termikus környezet, thermisztor-elhelyezés, azonosítók)
    hiányát explicit rögzíti — sosem hallgatólagos elhagyás."""
    missing_fields: list[str] = field(default_factory=list)
    gg_snapshot_field_errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "missing_fields": self.missing_fields,
            "gg_snapshot_field_errors": self.gg_snapshot_field_errors,
        }


def build_golden_gg_submission_package(
    output_dir: Path,
    config: GoldenGgConfig,
    samples: list[GoldenGgSample],
    gg_snapshot: BqDiagnosticsSnapshot,
    source_session_reference: str,
    profile_cell_count: int,
    missing_metadata_fields: Optional[list[str]] = None,
) -> Path:
    """SUP03-GG-009/010: config.txt + roomtemp.csv + gg.csv +
    metadata_limitations.json + source_session_reference.txt +
    project_metadata.json (D-Extra, 2026-09-13, sibling-sweep -- l.
    GoldenGgConfig.to_text() docstringje) → golden_gg_submission.zip.
    Blokkol, ha NumCellSeries eltér a profiltól,
    vagy a gg.csv forrás-pillanatkép hiányos (qmax/Ra table/chem_id nem
    olvasható — SUP03-GG-011: hiányos gauge-parameterekkel nem szabad
    TI-nek csomagot küldeni; CF-36: a chem_id KÖTELEZŐ config.txt kulcs
    is egyben, TI-SLUUBD0A §3.1 -- ha nem olvasható, a config.txt sem
    állítható elő értelmesen, ugyanaz a gate-elv, mint qmax/ra_table_mohm-nál),
    vagy a kiolvasott Qmax a SLUSB55B Table 20 szerinti 0…32767 mAh
    tartományon kívüli (R1 F-43),
    vagy (R1 B8c, F-28) a resample-elt kimenetben 100 s-nál nagyobb mintaköz
    van (GoldenGgExportIntervalError)."""
    validate_num_cell_series(config.num_cell_series, profile_cell_count)

    field_errors: dict[str, str] = {}
    if not gg_snapshot.qmax.valid:
        field_errors["qmax"] = gg_snapshot.qmax.error or "invalid"
    elif not _qmax_within_ti_range(gg_snapshot.qmax.value):
        field_errors["qmax"] = f"OUT_OF_TI_RANGE(value={gg_snapshot.qmax.value})"
    if not gg_snapshot.ra_table_mohm.valid:
        field_errors["ra_table_mohm"] = gg_snapshot.ra_table_mohm.error or "invalid"
    if not gg_snapshot.chem_id.valid:
        field_errors["chem_id"] = gg_snapshot.chem_id.error or "invalid"
    if field_errors:
        raise GoldenGgSnapshotIncompleteError(
            f"gg.csv forrás-pillanatkép hiányos: {field_errors} — export blokkolva."
        )

    resampled = resample_for_export(samples, config.sample_interval_s)
    interval_violations = export_interval_violations(resampled)
    if interval_violations:
        raise GoldenGgExportIntervalError(
            "roomtemp.csv kimeneti mintaköz a TI-kompatibilis 5-100 s "
            "tartományon kívül: " + "; ".join(interval_violations)
            + " — export blokkolva."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = output_dir / "config.txt"
    # CF-36 (governance-v2 review-kör 1, TI-SLUUBD0A §3): a forrás a
    # room-hőmérsékletű naplófájlt "roomtemp.csv" néven várja el ("The
    # room data log should be renamed as: roomtemp.csv") -- a projekt
    # belső "sysrate_rel_dis_rel.csv" neve NEM ez, egy valós TI submission
    # már a fájlnév-egyezésnél elakadt volna. `lowtemp.csv` (a MÁSODIK,
    # alacsony hőmérsékletű kamrateszt naplója) TUDATOSAN NEM készül --
    # a projekt jelenlegi GOLDEN_GG session nem végez ilyen fizikai
    # ciklust. MAX verifier (2026-09-09) korrekciója: ez VALÓDI non-
    # conformance, NEM a forrás által engedélyezett üzemmód -- a §3.2.1
    # "egy felállás is elég" mondata a RbL/termikus-modell paraméterek
    # KÖZÖTTI választásról szól, nem a room/low temperature választásról
    # (mindkét fájl kötelező kivétel nélkül). L. modul docstring + GOV-018.
    csv_path = output_dir / "roomtemp.csv"
    gg_csv_path = output_dir / "gg.csv"
    limitations_path = output_dir / "metadata_limitations.json"
    source_ref_path = output_dir / "source_session_reference.txt"
    project_metadata_path = output_dir / "project_metadata.json"
    zip_path = output_dir / "golden_gg_submission.zip"

    limitations = MetadataLimitations(
        missing_fields=list(missing_metadata_fields or []),
        gg_snapshot_field_errors={
            attr: getattr(gg_snapshot, attr).error or ""
            for _, attr in _GG_CSV_FIELDS
            if not getattr(gg_snapshot, attr).valid
        },
    )

    config_text = config.to_text(chem_id=str(gg_snapshot.chem_id.value))
    csv_text = build_sysrate_csv_text(resampled)
    gg_csv_text = build_gg_csv_text(gg_snapshot)
    limitations_text = json.dumps(limitations.to_dict(), indent=2, ensure_ascii=False)
    # D-Extra (2026-09-13, sibling-sweep): l. GoldenGgConfig.to_text()
    # docstringje -- a projekt-specifikus mezők UTF-8 sidecarja.
    project_metadata_text = json.dumps(
        config.project_metadata_dict(), indent=2, ensure_ascii=False
    )

    config_path.write_text(config_text, encoding="utf-8")
    # R7-B2: a kész CSV-k sorvégeit ne fordítsuk le újra Windows alatt.
    csv_path.write_text(csv_text, encoding="utf-8", newline="")
    gg_csv_path.write_text(gg_csv_text, encoding="utf-8", newline="")
    limitations_path.write_text(limitations_text, encoding="utf-8")
    source_ref_path.write_text(source_session_reference, encoding="utf-8")
    project_metadata_path.write_text(project_metadata_text, encoding="utf-8")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.txt", config_text)
        zf.writestr("roomtemp.csv", csv_text)
        zf.writestr("gg.csv", gg_csv_text)
        zf.writestr("metadata_limitations.json", limitations_text)
        zf.writestr("source_session_reference.txt", source_session_reference)
        zf.writestr("project_metadata.json", project_metadata_text)

    return zip_path


@dataclass
class GoldenGgImportResult:
    """SUP03-GG-010 kötelező kimenetek + SUP03-GG-011 kompatibilitás-
    állapot — a TI Golden GG Maker által VISSZAADOTT eredmény nyoma. Ez a
    lépés SZÁNDÉKOSAN különálló a fizikai `GoldenGgController`-től: a TI
    feldolgozás külső, aszinkron folyamat (nem valós idejű, nem a
    fizikai mérési session része)."""

    gg_out_csv_path: str
    gg_out_csv_hash: str
    qmax: Optional[int]
    ra_table_mohm: tuple[float, ...]
    ra0_charge_mohm: Optional[float]
    thermal_parameters: dict
    gpc_report_path: Optional[str]
    gpc_report_hash: Optional[str]
    warnings: tuple[str, ...]
    bq34z110_compatibility_confirmed: bool
    compatibility_confirmation_evidence: str
    # SUP03-GG-011: amíg a kompatibilitás nincs igazolva, a csomag NEM
    # tekinthető "optimalizáltnak" — a state EXPERIMENTAL/PACKAGE_ONLY marad.
    maturity_status: str

    def to_dict(self) -> dict:
        return {
            "gg_out_csv_path": self.gg_out_csv_path,
            "gg_out_csv_hash": self.gg_out_csv_hash,
            "qmax": self.qmax,
            "ra_table_mohm": list(self.ra_table_mohm),
            "ra0_charge_mohm": self.ra0_charge_mohm,
            "thermal_parameters": self.thermal_parameters,
            "gpc_report_path": self.gpc_report_path,
            "gpc_report_hash": self.gpc_report_hash,
            "warnings": list(self.warnings),
            "bq34z110_compatibility_confirmed": self.bq34z110_compatibility_confirmed,
            "compatibility_confirmation_evidence": self.compatibility_confirmation_evidence,
            "maturity_status": self.maturity_status,
        }


MATURITY_EXPERIMENTAL_PACKAGE_ONLY = "GOLDEN_GG_EXPERIMENTAL_PACKAGE_ONLY"
MATURITY_OPTIMIZED_CANDIDATE = "GOLDEN_GG_OPTIMIZED_CANDIDATE"


def import_ti_gg_output(
    output_dir: Path,
    gg_out_csv_path: Path,
    qmax: Optional[int],
    ra_table_mohm: tuple[float, ...],
    ra0_charge_mohm: Optional[float],
    thermal_parameters: dict,
    gpc_report_path: Optional[Path],
    warnings: tuple[str, ...],
    compatibility_confirmed: bool,
    compatibility_confirmation_evidence: str = "",
) -> GoldenGgImportResult:
    """SUP03-GG-010/011: a TI Golden GG Maker által visszaadott
    gg_out.csv/Qmax/Ra table/Ra0_charge/thermal_parameters/GPC report/
    warning-ok nyomon-követhető rögzítése, file hash-ekkel (SUP03-GG-010).

    `compatibility_confirmed=True` KIZÁRÓLAG akkor fogadható el, ha
    `compatibility_confirmation_evidence` nem üres — SUP03-GG-011 explicit
    tiltja a hallgatólagos kompatibilitás-feltételezést (próbaküldés vagy
    TI support visszaigazolása szükséges). Enélkül a `maturity_status`
    mindig `GOLDEN_GG_EXPERIMENTAL_PACKAGE_ONLY` marad."""
    if not gg_out_csv_path.exists():
        raise FileNotFoundError(f"gg_out.csv nem található: {gg_out_csv_path}")
    if compatibility_confirmed and not compatibility_confirmation_evidence.strip():
        raise GoldenGgCompatibilityNotConfirmedError(
            "SUP03-GG-011: bq34z110_compatibility_confirmed=True csak dokumentált "
            "evidence-szöveggel (próbaküldés eredménye vagy TI support-hivatkozás) "
            "állítható — nem hallgatólagos feltételezés."
        )

    gg_out_hash = sha256_of_file(gg_out_csv_path)
    gpc_report_hash = (
        sha256_of_file(gpc_report_path)
        if gpc_report_path is not None and gpc_report_path.exists()
        else None
    )

    maturity_status = (
        MATURITY_OPTIMIZED_CANDIDATE if compatibility_confirmed
        else MATURITY_EXPERIMENTAL_PACKAGE_ONLY
    )

    result = GoldenGgImportResult(
        gg_out_csv_path=str(gg_out_csv_path),
        gg_out_csv_hash=gg_out_hash,
        qmax=qmax,
        ra_table_mohm=tuple(ra_table_mohm),
        ra0_charge_mohm=ra0_charge_mohm,
        thermal_parameters=dict(thermal_parameters),
        gpc_report_path=str(gpc_report_path) if gpc_report_path is not None else None,
        gpc_report_hash=gpc_report_hash,
        warnings=tuple(warnings),
        bq34z110_compatibility_confirmed=compatibility_confirmed,
        compatibility_confirmation_evidence=compatibility_confirmation_evidence,
        maturity_status=maturity_status,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    record_path = output_dir / "golden_gg_import_record.json"
    record_path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


__all__ = [
    "GoldenGgSample",
    "GoldenGgSnapshotIncompleteError",
    "GoldenGgExportIntervalError",
    "GoldenGgCompatibilityNotConfirmedError",
    "NumCellSeriesMismatchError",
    "resample_for_export",
    "GoldenGgConfig",
    "build_sysrate_csv_text",
    "build_gg_csv_text",
    "MetadataLimitations",
    "build_golden_gg_submission_package",
    "GoldenGgImportResult",
    "MATURITY_EXPERIMENTAL_PACKAGE_ONLY",
    "MATURITY_OPTIMIZED_CANDIDATE",
    "import_ti_gg_output",
    "sha256_of_file",
]
