"""
SUP03-CHEMID-017/018/019: TI GPCCHEM-kompatibilis export.

Governance-v2 review-kör 1 (CF-11a, 2026-09-07): a `Leírások/TI_BQ/`
tévesen MISSING_LOCAL-nak jelölte a TI-SLVA725A ("Simple Guide to
Chemical ID Selection Tool (GPC)") dokumentumot, holott az mindvégig
fizikailag jelen volt — ez hibás premisszát okozott: a `config.txt`
formátumát korábban "FELTÉTELEZÉS"-ként, a tényleges TI-specifikáció
nélkül implementáltuk. A dokumentum §2.1 (Configuration File) és §2.2.1
(Example Config.txt File) alapján (VERIFIED_PRIMARY, l. CLAIM_LEDGER.yaml
GPC-CHEMID-001) a `config.txt` egy PLAIN ASCII "key = value" szöveges
szótár — NINCS benne `[section]` fejléc és `#` komment sor —, és
KÖTELEZŐEN tartalmazza a `ProcessingType = 2` és a 4, nulla-alapú
oszlopindex kulcsot (`ElapsedTimeColumn`/`VoltageColumn`/`CurrentColumn`/
`TemperatureColumn`) — ezek nélkül a valós TI GPC tool nem tudja
értelmezni a `roomtemp_rel_dis_rel.csv` adatfájlt. L. CF-35 (governance-v2
review-kör 1) — `GpcChemConfig.to_text()` javítva, l. ott.

A CSV-oszlopok és mértékegységek (mV, mA, s, °C) a forrásdokumentum által
EXPLICIT előírtak (SUP03-CHEMID-019), ezek nem feltételezés.
"""
from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import math
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple, Protocol, TypeVar

# CF-35 (governance-v2 review-kör 1): egyetlen forrás a roomtemp CSV
# oszlopsorrendjéhez — a `config.txt` `*Column` kulcsainak (TI-SLVA725A
# §2.1: "Zero-based column number... in your data logs") és a tényleges
# CSV fejlécnek (build_roomtemp_csv_text()) EGYMÁSSAL kell egyeznie, hogy
# a TI GPC tool a helyes oszlopot olvassa — két külön hardcode-olt
# másolat DFAM-001 (sibling_safety_gate_omission-rokon) sodródási
# kockázatot jelentene. CF-36 (2026-09-09): a golden_gg_export.py::
# GoldenGgConfig.to_text() ugyanezt az oszlopsorrendet igényli a saját
# `*Column` kulcsaihoz (TI-SLUUBD0A §3.1, azonos "zero-based column
# number" szemantika) — a build_sysrate_csv_text() amúgy is erre a
# build_roomtemp_csv_text()-re delegál, ezért NEM public prefix nélküli
# név (`CSV_COLUMN_ORDER`), hogy golden_gg_export.py importálhassa,
# ahelyett hogy egy 3. hardcode-olt másolatot vezetne be.
CSV_COLUMN_ORDER: tuple[str, str, str, str] = (
    "elapsed_time_s", "voltage_mV", "current_mA", "cell_surface_temperature_C",
)


class ChemIdSample(NamedTuple):
    """Egy nyers mintapont — elapsed_s a ChemID-session RELAX_5H_FULL
    kezdetétől számítva (folyamatos görbe RELAX→DISCHARGE→RELAX felett)."""
    elapsed_s: float
    voltage_V: float
    current_A: float  # kisütés ELŐJELE NEGATÍV (SUP03-CHEMID-019)
    temperature_C: float


class NumCellSeriesMismatchError(ValueError):
    """SUP03-TEST-002: 'wrong NumCellSeries blokkolo validacio'."""


def validate_num_cell_series(config_num_cell_series: int, profile_cell_count: int) -> None:
    if config_num_cell_series != profile_cell_count:
        raise NumCellSeriesMismatchError(
            f"config.txt NumCellSeries={config_num_cell_series} nem egyezik "
            f"a profil cellaszámával ({profile_cell_count}) — export blokkolva."
        )


class _HasElapsedS(Protocol):
    # Kör 19 D8a (SW19-02, DFAM-026 declared_policy_without_consumer): a
    # tagot korábban mutable attribútumként deklaráltuk, miközben az
    # egyetlen két konkrét felhasználó (ChemIdSample/GoldenGgSample) MINDIG
    # read-only NamedTuple mező -- a strukturális egyezés emiatt SOHA nem
    # teljesült (mypy: "cannot be ChemIdSample"/"cannot be GoldenGgSample"),
    # tehát ez a Protocol a gyakorlatban ZÉRÓ statikus védelmet adott. A
    # @property forma NamedTuple mezővel is strukturálisan egyezik.
    @property
    def elapsed_s(self) -> float: ...


_SampleT = TypeVar("_SampleT", bound=_HasElapsedS)

# R1 B8c, F-28: a TI-kompatibilis kimeneti mintaköz határai (TI SLVA725A
# §2.2 / SLUUBD0A §3: "sampling interval must be 5-100 s", CLAIM_LEDGER
# GPC-CHEMID-001) -- ugyanaz a tartomány, mint a resample_for_export()
# sample_interval_s paraméterkapuja.
_EXPORT_MIN_INTERVAL_S = 5.0
_EXPORT_MAX_INTERVAL_S = 100.0
# R1 B8c fixup, F-28 N1 (DFAM-003): ugyanezek a határok a kanonikus CSV-
# időbélyeg egységében (egész tized-másodperc), l. _export_elapsed_ds().
_EXPORT_MIN_INTERVAL_DS = round(_EXPORT_MIN_INTERVAL_S * 10)
_EXPORT_MAX_INTERVAL_DS = round(_EXPORT_MAX_INTERVAL_S * 10)


def _export_elapsed_s_text(elapsed_s: float) -> str:
    """R1 B8c fixup, F-28 N1 (P3, DFAM-003): a CSV `elapsed_time_s`
    oszlopába írt időbélyeg EGYETLEN kanonikus formázója (1 tizedes). A
    CSV-író (build_roomtemp_csv_text()), a resample_for_export()
    mintakiválasztása és az export_interval_violations() kapu is ezen
    keresztül látja az időt. Korábban a kiválasztás és a kapu a kerekítés
    ELŐTTI float-on dolgozott: [0.05, 5.05]/5 s -> CSV 0.1, 5.0 (4,9 s
    köz); [0.15, 100.15]/100 s -> CSV 0.1, 100.2 (100,1 s köz), mindkettő
    átment (Codex max verifier, B8c N1)."""
    return f"{elapsed_s:.1f}"


def _export_elapsed_ds(elapsed_s: float) -> int:
    """A kanonikus CSV-időbélyeg (_export_elapsed_s_text()) egész
    tized-másodpercben, PONTOSAN a kiírt szövegből (Decimal) -- nem
    újraszámolt kerekítéssel (a round() banker's rounding, a format a
    bináris érték szerint kerekít; a kettő eltérhet). Nem véges
    időbélyegnek nincs kanonikus CSV-reprezentációja -> ValueError."""
    if not math.isfinite(elapsed_s):
        raise ValueError(f"elapsed_s={elapsed_s!r} nem véges -- nem exportálható.")
    return int(Decimal(_export_elapsed_s_text(elapsed_s)) * 10)


def resample_for_export(
    samples: list[_SampleT], sample_interval_s: float
) -> list[_SampleT]:
    """SUP03-CHEMID-017/TEST-002: 5 s <= sample_interval_s <= 100 s.
    Az első minta mindig megmarad; az utolsó nyers minta akkor kerül a
    kimenetbe, ha legalább `_EXPORT_MIN_INTERVAL_S` (5,0 s) távolságra van
    az utolsó megtartott mintától -- R1 B8c, F-28 (P3, DFAM-003): korábban
    feltétel nélkül hozzáfűződött, akár 5 s-nál rövidebb kimeneti
    intervallumot okozva (TI SLVA725A §2.2 / SLUUBD0A §3: 5-100 s,
    CLAIM_LEDGER GPC-CHEMID-001). Interpoláció nincs; a >100 s kimeneti
    hézagot NEM ez a függvény kezeli, l. export_interval_violations().
    R1 B8c fixup, F-28 N1: a köztes (küszöb-) kiválasztás ÉS a záró-minta
    szabály is a CSV-be írt kanonikus időbélyegek (_export_elapsed_ds())
    különbségén dönt, így a CSV-ben minden KÖZTES kimeneti köz >= sample_interval_s,
    a ZÁRÓ köz pedig >= 5,0 s (a float-köz kerekítés után 5 s alá
    csúszhatott; R1 B8c verifier N2/P3-RV-1 pontosítás).

    Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra P3
    lelet: ez a logika korábban BYTE-AZONOSAN duplikálva volt
    `golden_gg_export.py`-ban (`ChemIdSample`/`GoldenGgSample` struktúrálisan
    azonos, mindkettő csak `elapsed_s`-t olvas itt) — a `TypeVar`/`Protocol`
    általánosítás mindkét mintatípusra (és bármely jövőbeli, `elapsed_s`
    mezőt hordozó NamedTuple-re) helyesen típusellenőrizhető marad, a
    canonikus implementáció itt él, `golden_gg_export.py` innen importálja."""
    if not (5.0 <= sample_interval_s <= 100.0):
        raise ValueError(
            f"sample_interval_s={sample_interval_s} a TI-kompatibilis "
            f"5-100 s tartományon kívül esik."
        )
    if not samples:
        return []

    ordered = sorted(samples, key=lambda s: s.elapsed_s)
    resampled = [ordered[0]]
    last_kept_ds = _export_elapsed_ds(ordered[0].elapsed_s)
    for sample in ordered[1:]:
        sample_ds = _export_elapsed_ds(sample.elapsed_s)
        if (sample_ds - last_kept_ds) / 10.0 >= sample_interval_s:
            resampled.append(sample)
            last_kept_ds = sample_ds
    if _export_elapsed_ds(ordered[-1].elapsed_s) - last_kept_ds >= _EXPORT_MIN_INTERVAL_DS:
        resampled.append(ordered[-1])
    return resampled


def export_interval_violations(resampled: Sequence[_HasElapsedS]) -> list[str]:
    """R1 B8c, F-28 (P3, DFAM-003): a MÁR resample-elt kimenet minden
    `_EXPORT_MAX_INTERVAL_S`-nál (100,0 s) NAGYOBB egymást követő
    hézagának leírása (TI SLVA725A §2.2 / SLUUBD0A §3: 5-100 s). Tiszta
    függvény: nem módosít, nem interpolál. A hívó builder dönt a
    következményről (GPC: minőség-lefokozás + limitation; GG: blokkolás).
    R1 B8c fixup, F-28 N1: a köz a CSV-be ténylegesen írt kanonikus
    időbélyegekből számolódik (_export_elapsed_ds()), nem a kerekítés
    előtti float-ból."""
    violations: list[str] = []
    for prev, curr in itertools.pairwise(resampled):
        gap_ds = _export_elapsed_ds(curr.elapsed_s) - _export_elapsed_ds(prev.elapsed_s)
        if gap_ds > _EXPORT_MAX_INTERVAL_DS:
            violations.append(
                f"{_export_elapsed_s_text(prev.elapsed_s)} s -> "
                f"{_export_elapsed_s_text(curr.elapsed_s)} s: "
                f"{gap_ds / 10:.1f} s kimeneti (CSV) mintaköz > "
                f"{_EXPORT_MAX_INTERVAL_S:.0f} s (adatlyuk, interpoláció nincs)"
            )
    return violations


@dataclass
class GpcChemConfig:
    """config.txt tartalmának forrás-adatai (SUP03-DATA-001 azonosítók egy
    részhalmaza + a GPCCHEM-specifikus mezők)."""
    num_cell_series: int
    battery_model: str
    battery_sample_id: str
    chemistry: str = "PbA"
    sample_interval_s: float = 30.0
    room_temperature_C: float = 20.0
    manufacturer_cutoff_voltage_V: float = 0.0
    app_version: str = ""
    git_commit: str = ""
    operator: str = ""

    def to_text(self) -> str:
        """CF-35 (governance-v2 review-kör 1, VERIFIED_PRIMARY: TI-SLVA725A
        §2.1 Configuration File (1 Each) + §2.2.1 Example Config.txt File,
        MAX verifier önállóan megerősítette a saját pdftotext kinyerésével):
        a `config.txt` "ASCII text dictionary" (a forrás szó szerinti
        megfogalmazása), `[section]` fejléc és `#` komment sor NÉLKÜL. A
        dokumentum 6 kulcsot ír elő kötelezőként -- a sorrend maga NEM
        forrás-előírás (a §2.1 bullet-lista és a §2.2.1 példa két EGYMÁSTÓL
        ELTÉRŐ sorrendet mutat, egy szótárnak pedig nincs sorrend-
        szemantikája) --, itt a §2.2.1 példa sorrendjét követjük:
        `ProcessingType` (érték MINDIG 2 a chemistry ID selection toolhoz,
        keresztellenőrizve a TI-SLUUBD0A testvér-dokumentummal, ami MÁS
        toolhoz 4-et ír elő), `NumCellSeries`, majd a 4, nulla-alapú
        oszlopindex kulcs (`ElapsedTimeColumn`/`VoltageColumn`/
        `CurrentColumn`/`TemperatureColumn`) -- ezek NÉLKÜL a valós TI GPC
        tool nem tudja értelmezni a `roomtemp_rel_dis_rel.csv` adatfájlt
        (melyik oszlop micsoda).

        D-Extra (2026-09-13, felhasználói döntés, DECISION_REGISTER.md
        GOV-023): a projekt saját nyomonkövethetőségi mezői (Chemistry/
        BatteryModel/BatterySampleID/Operator/stb.) KORÁBBAN itt, a 6
        kötelező kulcs UTÁN szerepeltek -- ez megsértette a forrás "ASCII
        text dictionary" előírását, mert ezek a szabad szöveges mezők
        (főleg `Operator`) elvben nem-ASCII karaktert (pl. magyar
        operátornév) tartalmazhatnak (empirikusan igazolva, CF-35 MAX
        verifier 2026-09-09: `UnicodeEncodeError` `operator="Kovács
        Ádám"`-ra). Mostantól ezek a `project_metadata_dict()`-en
        keresztül egy KÜLÖN, UTF-8 sidecar fájlba (`project_metadata.json`,
        l. `build_gpcchem_submission_zip()`) kerülnek -- a `config.txt`
        innentől KIZÁRÓLAG a 6 TI-kötelező, inherensen ASCII-biztos (int/
        fix string) kulcsot tartalmazza, garantáltan megfelelve a forrás
        előírásnak, függetlenül attól, milyen szabad szöveget ír be az
        operátor."""
        lines = [
            "ProcessingType = 2",
            f"NumCellSeries = {self.num_cell_series}",
            f"ElapsedTimeColumn = {CSV_COLUMN_ORDER.index('elapsed_time_s')}",
            f"VoltageColumn = {CSV_COLUMN_ORDER.index('voltage_mV')}",
            f"CurrentColumn = {CSV_COLUMN_ORDER.index('current_mA')}",
            f"TemperatureColumn = {CSV_COLUMN_ORDER.index('cell_surface_temperature_C')}",
        ]
        return "\n".join(lines) + "\n"

    def project_metadata_dict(self) -> dict:
        """D-Extra (2026-09-13): a projekt-specifikus nyomonkövethetőségi
        mezők, KÜLÖN a TI-kötelező `config.txt`-től -- l. `to_text()`
        docstringje. Szabad szöveges (potenciálisan nem-ASCII) mezőket is
        tartalmazhat, ezért csak UTF-8 sidecar fájlba (`project_metadata.
        json`) íródik, sosem a `config.txt`-be."""
        return {
            "chemistry": self.chemistry,
            "battery_model": self.battery_model,
            "battery_sample_id": self.battery_sample_id,
            "sample_interval_s": self.sample_interval_s,
            "room_temperature_C": self.room_temperature_C,
            "manufacturer_cutoff_voltage_V": self.manufacturer_cutoff_voltage_V,
            "app_version": self.app_version,
            "git_commit": self.git_commit,
            "operator": self.operator,
        }


class _HasCsvSampleFields(Protocol):
    # Kör 19 D8a (SW19-02): l. _HasElapsedS fenti kommentje -- ugyanaz az
    # indoklás, ugyanaz a 4-mezős alakzat.
    @property
    def elapsed_s(self) -> float: ...
    @property
    def voltage_V(self) -> float: ...
    @property
    def current_A(self) -> float: ...
    @property
    def temperature_C(self) -> float: ...


def build_roomtemp_csv_text(samples: Sequence[_HasCsvSampleFields]) -> str:
    """SUP03-CHEMID-019: kötelező oszlopok, mV/mA/°C egységekben,
    negatív kisütési árammal.

    Kör 19 D8a (SW19-02): a paraméter `list[...]` helyett `Sequence[...]` --
    a `list` invariáns, tehát `list[ChemIdSample]`/`list[GoldenGgSample]`
    átadása egy `list[_HasCsvSampleFields]`-paraméternek KONTÉNER-szinten
    bukna, még akkor is, ha az elem-típus (a fenti @property-fix után)
    strukturálisan megfelel a Protocolnak — a `Sequence` kovariáns, ez a
    fennmaradó statikus rést zárja. A függvény csak iterál (`for s in
    samples`), listaspecifikus műveletet nem használ.

    Teljes kódbázis review 4. kör (2026-08-29), Szoftver/Architektúra P3
    lelet: ez a CSV-író logika korábban byte-azonosan duplikálva volt
    `golden_gg_export.py::build_sysrate_csv_text()`-ben (a `ChemIdSample`/
    `GoldenGgSample` struktúrálisan azonos, mindkettő ugyanezt a 4 mezőt
    olvassa) — a `Protocol` általánosítás mindkét mintatípusra helyesen
    típusellenőrizhető marad, a canonikus implementáció itt él,
    `golden_gg_export.py::build_sysrate_csv_text()` erre delegál."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(CSV_COLUMN_ORDER))
    for s in samples:
        writer.writerow([
            _export_elapsed_s_text(s.elapsed_s),
            f"{s.voltage_V * 1000.0:.1f}",
            f"{s.current_A * 1000.0:.1f}",
            f"{s.temperature_C:.2f}",
        ])
    return buf.getvalue()


@dataclass
class MeasurementQuality:
    current_accuracy_better_than_percent_of_range: float | None = None
    voltage_accuracy_at_room_temperature_mV: float | None = None
    adc_resolution_bits: int | None = None
    ti_compatible_quality_claimed: bool = False
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "current_accuracy_better_than_percent_of_range":
                self.current_accuracy_better_than_percent_of_range,
            "voltage_accuracy_at_room_temperature_mV":
                self.voltage_accuracy_at_room_temperature_mV,
            "adc_resolution_bits": self.adc_resolution_bits,
            # SUP03-CHEMID-018: "Az alkalmazas nem allithat TI-kompatibilis
            # minoseget, ha a teljes meresi lanc specifikacioja ezt nem
            # tamasztja ala." — ezért ez explicit, sosem hallgatólagos True.
            "ti_compatible_quality_claimed": self.ti_compatible_quality_claimed,
            "limitations": self.limitations,
        }


def evaluate_ti_compatible_quality(
    current_accuracy_percent: float | None,
    voltage_accuracy_mV: float | None,
    adc_bits: int | None,
) -> MeasurementQuality:
    """SUP03-CHEMID-018: csak akkor állítható TI-kompatibilis minőség, ha a
    teljes mért lánc alátámasztja — nincs csendes, alapértelmezett True."""
    limitations: list[str] = []
    claimed = True

    if current_accuracy_percent is None or current_accuracy_percent > 0.1:
        claimed = False
        limitations.append(
            "Áram-pontosság nem igazolt <= 0.1% range accuracy szintre "
            "(PSU/Load readback specifikáció alapján)."
        )
    if voltage_accuracy_mV is None or voltage_accuracy_mV > 1.0:
        claimed = False
        limitations.append(
            "Feszültség-pontosság nem igazolt <= 1 mV szintre szobahőmérsékleten."
        )
    if adc_bits is None or adc_bits < 16:
        claimed = False
        limitations.append("ADC felbontás nem igazolt >= 16 bit szintre.")

    return MeasurementQuality(
        current_accuracy_better_than_percent_of_range=current_accuracy_percent,
        voltage_accuracy_at_room_temperature_mV=voltage_accuracy_mV,
        adc_resolution_bits=adc_bits,
        ti_compatible_quality_claimed=claimed,
        limitations=limitations,
    )


def build_gpcchem_submission_zip(
    output_dir: Path,
    config: GpcChemConfig,
    samples: list[ChemIdSample],
    quality: MeasurementQuality,
    source_session_reference: str,
    profile_cell_count: int,
) -> Path:
    """SUP03-CHEMID-019: config.txt + roomtemp_rel_dis_rel.csv +
    measurement_quality.json + source_session_reference.txt +
    project_metadata.json → gpcchem_submission.zip. Blokkol, ha
    NumCellSeries eltér a profiltól.

    D-Extra (2026-09-13, DECISION_REGISTER.md GOV-023): a
    `project_metadata.json` sidecar hordozza a korábban `config.txt`-ben
    élt, projekt-specifikus szabad szöveges mezőket (l. `GpcChemConfig.
    to_text()`/`project_metadata_dict()` docstringje) -- UTF-8, nem
    TI-kötelező, a GPC tool nem olvassa, csak a projekt saját
    nyomonkövethetőségét szolgálja."""
    validate_num_cell_series(config.num_cell_series, profile_cell_count)

    resampled = resample_for_export(samples, config.sample_interval_s)
    # R1 B8c, F-28 (D-B8-4): >100 s kimeneti adatlyuk mellett a csomag
    # elkészül (a nyers mérés megmarad), de TI-kompatibilis minőség NEM
    # állítható -- írás ELŐTT lefokozva, explicit limitációval
    # (SUP03-CHEMID-018 minta: nincs hallgatólagos True).
    interval_violations = export_interval_violations(resampled)
    if interval_violations:
        quality.ti_compatible_quality_claimed = False
        quality.limitations.append(
            "A kimeneti mintaköz a TI-kompatibilis 5-100 s tartományon kívül "
            "esik (>100 s adatlyuk, interpoláció nincs): "
            + "; ".join(interval_violations) + "."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = output_dir / "config.txt"
    csv_path = output_dir / "roomtemp_rel_dis_rel.csv"
    quality_path = output_dir / "measurement_quality.json"
    source_ref_path = output_dir / "source_session_reference.txt"
    project_metadata_path = output_dir / "project_metadata.json"
    zip_path = output_dir / "gpcchem_submission.zip"

    config_text = config.to_text()
    csv_text = build_roomtemp_csv_text(resampled)
    quality_text = json.dumps(quality.to_dict(), indent=2, ensure_ascii=False)
    project_metadata_text = json.dumps(
        config.project_metadata_dict(), indent=2, ensure_ascii=False
    )

    config_path.write_text(config_text, encoding="utf-8")
    # R7-B2: a kész CSV sorvégeit ne fordítsuk le újra Windows alatt.
    csv_path.write_text(csv_text, encoding="utf-8", newline="")
    quality_path.write_text(quality_text, encoding="utf-8")
    source_ref_path.write_text(source_session_reference, encoding="utf-8")
    project_metadata_path.write_text(project_metadata_text, encoding="utf-8")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.txt", config_text)
        zf.writestr("roomtemp_rel_dis_rel.csv", csv_text)
        zf.writestr("measurement_quality.json", quality_text)
        zf.writestr("source_session_reference.txt", source_session_reference)
        zf.writestr("project_metadata.json", project_metadata_text)

    return zip_path


def sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
