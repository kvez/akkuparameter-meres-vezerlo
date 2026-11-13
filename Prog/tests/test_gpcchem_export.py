"""
SUP03-CHEMID-017/018/019/TEST-002: GPCCHEM export unit tesztek.
"""
from __future__ import annotations

import csv
import io
import itertools
import json
import zipfile

import pytest

from Prog.src.gpcchem_export import (
    ChemIdSample,
    GpcChemConfig,
    MeasurementQuality,
    NumCellSeriesMismatchError,
    build_gpcchem_submission_zip,
    build_roomtemp_csv_text,
    evaluate_ti_compatible_quality,
    resample_for_export,
    validate_num_cell_series,
)


def _make_samples(n: int = 20, dt: float = 1.0, current_A: float = -0.35) -> list[ChemIdSample]:
    return [
        ChemIdSample(
            elapsed_s=i * dt, voltage_V=12.5 - i * 0.001,
            current_A=current_A, temperature_C=22.0,
        )
        for i in range(n)
    ]


class TestNegativeDischargeCurrent:
    def test_csv_preserves_negative_current_sign(self):
        samples = [ChemIdSample(0.0, 12.5, -0.35, 22.0)]
        csv_text = build_roomtemp_csv_text(samples)
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        assert float(rows[0]["current_mA"]) < 0

    def test_csv_current_in_mA_not_A(self):
        samples = [ChemIdSample(0.0, 12.5, -0.35, 22.0)]
        csv_text = build_roomtemp_csv_text(samples)
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        assert float(rows[0]["current_mA"]) == pytest.approx(-350.0)


class TestUnits:
    def test_voltage_in_mv(self):
        samples = [ChemIdSample(0.0, 12.5, -0.35, 22.0)]
        rows = list(csv.DictReader(io.StringIO(build_roomtemp_csv_text(samples))))
        assert float(rows[0]["voltage_mV"]) == pytest.approx(12500.0)

    def test_temperature_in_celsius(self):
        samples = [ChemIdSample(0.0, 12.5, -0.35, 23.4)]
        rows = list(csv.DictReader(io.StringIO(build_roomtemp_csv_text(samples))))
        assert float(rows[0]["cell_surface_temperature_C"]) == pytest.approx(23.4)

    def test_elapsed_time_in_seconds(self):
        samples = [ChemIdSample(123.4, 12.5, -0.35, 22.0)]
        rows = list(csv.DictReader(io.StringIO(build_roomtemp_csv_text(samples))))
        assert float(rows[0]["elapsed_time_s"]) == pytest.approx(123.4)

    def test_required_columns_present(self):
        samples = [ChemIdSample(0.0, 12.5, -0.35, 22.0)]
        header = build_roomtemp_csv_text(samples).splitlines()[0].split(",")
        for expected in (
            "elapsed_time_s", "voltage_mV", "current_mA", "cell_surface_temperature_C",
        ):
            assert expected in header


class TestResampling5to100s:
    def test_rejects_interval_below_5s(self):
        with pytest.raises(ValueError):
            resample_for_export(_make_samples(), sample_interval_s=4.9)

    def test_rejects_interval_above_100s(self):
        with pytest.raises(ValueError):
            resample_for_export(_make_samples(), sample_interval_s=100.1)

    def test_accepts_boundary_5s(self):
        result = resample_for_export(_make_samples(n=20, dt=1.0), sample_interval_s=5.0)
        assert result  # nem üres, nem dob kivételt

    def test_accepts_boundary_100s(self):
        samples = _make_samples(n=200, dt=1.0)
        result = resample_for_export(samples, sample_interval_s=100.0)
        assert result

    def test_resampling_reduces_sample_count(self):
        samples = _make_samples(n=100, dt=1.0)  # 100 s teljes hossz, 1s natív
        result = resample_for_export(samples, sample_interval_s=10.0)
        assert len(result) < len(samples)

    def test_first_and_last_sample_always_kept(self):
        samples = _make_samples(n=37, dt=1.0)
        result = resample_for_export(samples, sample_interval_s=10.0)
        assert result[0].elapsed_s == pytest.approx(0.0)
        assert result[-1].elapsed_s == pytest.approx(samples[-1].elapsed_s)

    def test_empty_samples_returns_empty(self):
        assert resample_for_export([], sample_interval_s=30.0) == []


class TestNumCellSeriesValidation:
    def test_matching_cell_count_ok(self):
        validate_num_cell_series(6, 6)  # nem dob kivételt

    def test_mismatched_cell_count_raises(self):
        with pytest.raises(NumCellSeriesMismatchError):
            validate_num_cell_series(12, 6)


class TestTiCompatibleQualityHonesty:
    """SUP03-CHEMID-018: nem állítható TI-kompatibilis minőség alátámasztás nélkül."""

    def test_missing_accuracy_data_does_not_claim_compatibility(self):
        quality = evaluate_ti_compatible_quality(None, None, None)
        assert quality.ti_compatible_quality_claimed is False
        assert len(quality.limitations) == 3

    def test_insufficient_current_accuracy_blocks_claim(self):
        quality = evaluate_ti_compatible_quality(0.5, 0.5, 16)
        assert quality.ti_compatible_quality_claimed is False

    def test_all_specs_met_allows_claim(self):
        quality = evaluate_ti_compatible_quality(0.05, 0.5, 16)
        assert quality.ti_compatible_quality_claimed is True
        assert quality.limitations == []

    def test_adc_below_16bit_blocks_claim(self):
        quality = evaluate_ti_compatible_quality(0.05, 0.5, 12)
        assert quality.ti_compatible_quality_claimed is False


class TestSubmissionZip:
    def test_zip_blocked_on_wrong_num_cell_series(self, tmp_path):
        config = GpcChemConfig(
            num_cell_series=12, battery_model="FG20721", battery_sample_id="S1",
        )
        with pytest.raises(NumCellSeriesMismatchError):
            build_gpcchem_submission_zip(
                tmp_path, config, _make_samples(), MeasurementQuality(),
                "session_ref", profile_cell_count=6,
            )

    def test_zip_created_with_all_files(self, tmp_path):
        config = GpcChemConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            sample_interval_s=30.0,
        )
        zip_path = build_gpcchem_submission_zip(
            tmp_path, config, _make_samples(n=200, dt=1.0), MeasurementQuality(),
            "session_ref_abc", profile_cell_count=6,
        )
        assert zip_path.name == "gpcchem_submission.zip"
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            assert names == {
                "config.txt", "roomtemp_rel_dis_rel.csv",
                "measurement_quality.json", "source_session_reference.txt",
                "project_metadata.json",
            }
            config_text = zf.read("config.txt").decode("utf-8")
            # CF-35 (governance-v2 review-kör 1, TI-SLVA725A §2.2.1
            # VERIFIED_PRIMARY formátum): "Key = Value", nem "Key=Value".
            # R14-B1: a ZIP konfiguraciojaban is teljes numerikus sorok.
            config_lines = config_text.splitlines()
            assert "NumCellSeries = 6" in config_lines
            assert "ElapsedTimeColumn = 0" in config_lines
            assert "VoltageColumn = 1" in config_lines
            assert "CurrentColumn = 2" in config_lines
            assert "TemperatureColumn = 3" in config_lines

    def test_individual_files_also_written_to_output_dir(self, tmp_path):
        config = GpcChemConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
        )
        build_gpcchem_submission_zip(
            tmp_path, config, _make_samples(), MeasurementQuality(),
            "session_ref", profile_cell_count=6,
        )
        assert (tmp_path / "config.txt").exists()
        assert (tmp_path / "roomtemp_rel_dis_rel.csv").exists()
        assert (tmp_path / "measurement_quality.json").exists()
        assert (tmp_path / "source_session_reference.txt").exists()
        assert (tmp_path / "project_metadata.json").exists()

    def test_quality_json_is_valid_json(self, tmp_path):
        config = GpcChemConfig(num_cell_series=6, battery_model="M", battery_sample_id="S1")
        build_gpcchem_submission_zip(
            tmp_path, config, _make_samples(), MeasurementQuality(),
            "ref", profile_cell_count=6,
        )
        data = json.loads((tmp_path / "measurement_quality.json").read_text())
        assert "ti_compatible_quality_claimed" in data


class TestCf35ConfigTxtConformsToTiSlva725a:
    """[regresszió, governance-v2 review-kör 1, CF-35] a config.txt korábban
    NEM konform a TI-SLVA725A §2.1/§2.2.1 forrással: hiányzott a kötelező
    `ProcessingType = 2` és mind a 4 nulla-alapú oszlopindex kulcs
    (ElapsedTimeColumn/VoltageColumn/CurrentColumn/TemperatureColumn), és
    egy nem-specifikált `[GPCCHEM_CONFIG]` szekciófejlécet + `#` komment
    sort tartalmazott sima kulcs=érték szótár helyett -- emiatt a valós TI
    GPC tool nem tudta volna értelmezni sem a fájl formátumát, sem a
    roomtemp_rel_dis_rel.csv oszlopainak jelentését."""

    def _config(self) -> GpcChemConfig:
        return GpcChemConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
        )

    def test_no_section_header(self):
        assert "[GPCCHEM_CONFIG]" not in self._config().to_text()

    def test_no_comment_lines(self):
        text = self._config().to_text()
        assert not any(line.strip().startswith("#") for line in text.splitlines())

    def test_processing_type_is_2(self):
        assert "ProcessingType = 2" in self._config().to_text()

    def test_all_four_column_index_keys_present(self):
        text = self._config().to_text()
        # R14-B1: a szamjegy-prefix helyett a teljes sor egyezzen.
        lines = text.splitlines()
        assert "ElapsedTimeColumn = 0" in lines
        assert "VoltageColumn = 1" in lines
        assert "CurrentColumn = 2" in lines
        assert "TemperatureColumn = 3" in lines

    def test_column_indices_match_actual_csv_header_order(self):
        """A config.txt oszlopindexei NEM lehetnek egy külön, kézzel
        karbantartott másolat -- ugyanannak a forrásnak (a tényleges CSV
        fejlécnek) kell megfelelniük, amit build_roomtemp_csv_text() ír."""
        csv_text = build_roomtemp_csv_text(_make_samples(n=3))
        header = next(csv.reader(io.StringIO(csv_text)))
        config_text = self._config().to_text()
        column_keys = {
            "elapsed_time_s": "ElapsedTimeColumn",
            "voltage_mV": "VoltageColumn",
            "current_mA": "CurrentColumn",
            "cell_surface_temperature_C": "TemperatureColumn",
        }
        for csv_col_name, config_key in column_keys.items():
            expected_index = header.index(csv_col_name)
            assert f"{config_key} = {expected_index}" in config_text.splitlines()

    def test_processing_type_appears_before_column_keys(self):
        """TI-SLVA725A §2.2.1 Example Config.txt File sorrendje: ProcessingType,
        NumCellSeries, majd a 4 oszlopindex kulcs."""
        lines = [ln for ln in self._config().to_text().splitlines() if ln.strip()]
        assert lines[0] == "ProcessingType = 2"
        assert lines[1] == "NumCellSeries = 6"


class TestDExtraConfigTxtStaysAsciiProjectFieldsMoveToSidecar:
    """[regresszió, D-Extra, 2026-09-13, DECISION_REGISTER.md GOV-023] a
    `config.txt` korábban a 6 TI-kötelező kulcs UTÁN a projekt saját
    szabad szöveges mezőit (Chemistry/BatteryModel/Operator/stb.) is
    tartalmazta -- ezek elvben nem-ASCII karaktert hordozhatnak (pl.
    magyar operátornév), ami sérti a TI-SLVA725A §2.1 "ASCII text
    dictionary" előírását (CF-35 MAX verifier, 2026-09-09, empirikusan
    igazolva `UnicodeEncodeError`-ral). A fix a projekt-specifikus
    mezőket egy külön, UTF-8 `project_metadata.json` sidecarba viszi."""

    def _config(self, **overrides) -> GpcChemConfig:
        defaults = {
            "num_cell_series": 6, "battery_model": "FG20721", "battery_sample_id": "S1",
            "operator": "Kovács Ádám", "chemistry": "PbA (AGM)",
        }
        defaults.update(overrides)
        return GpcChemConfig(**defaults)

    def test_to_text_contains_only_the_six_ti_required_keys(self):
        """Diszkrimináló: a régi kódon ez a lista a projekt-mezőkkel
        (Chemistry/BatteryModel/stb.) bővülne."""
        lines = [ln for ln in self._config().to_text().splitlines() if ln.strip()]
        keys = [ln.split(" = ", 1)[0] for ln in lines]
        assert keys == [
            "ProcessingType", "NumCellSeries", "ElapsedTimeColumn",
            "VoltageColumn", "CurrentColumn", "TemperatureColumn",
        ]

    def test_to_text_is_always_ascii_encodable_even_with_non_ascii_operator(self):
        """Diszkrimináló: a régi kódon ez `UnicodeEncodeError`-ral bukna,
        mert az `Operator`/`Chemistry` mező bekerült a config.txt-be."""
        text = self._config(operator="Kovács Ádám", chemistry="ólomsavas").to_text()
        text.encode("ascii")  # nem dobhat

    def test_project_metadata_dict_carries_the_free_text_fields(self):
        meta = self._config(operator="Kovács Ádám").project_metadata_dict()
        assert meta["operator"] == "Kovács Ádám"
        assert meta["battery_model"] == "FG20721"
        assert meta["chemistry"] == "PbA (AGM)"

    def test_zip_includes_project_metadata_json_sidecar_with_non_ascii_operator(
        self, tmp_path
    ):
        config = self._config(operator="Kovács Ádám")
        zip_path = build_gpcchem_submission_zip(
            tmp_path, config, _make_samples(), MeasurementQuality(),
            "session_ref", profile_cell_count=6,
        )
        with zipfile.ZipFile(zip_path) as zf:
            assert "project_metadata.json" in zf.namelist()
            meta_text = zf.read("project_metadata.json").decode("utf-8")
            meta = json.loads(meta_text)
            assert meta["operator"] == "Kovács Ádám"
            config_text = zf.read("config.txt").decode("ascii")  # nem dobhat
            assert "Kovács" not in config_text
            assert "Operator" not in config_text

    def test_project_metadata_json_also_written_to_output_dir(self, tmp_path):
        config = self._config(operator="Kovács Ádám")
        build_gpcchem_submission_zip(
            tmp_path, config, _make_samples(), MeasurementQuality(),
            "session_ref", profile_cell_count=6,
        )
        meta = json.loads(
            (tmp_path / "project_metadata.json").read_text(encoding="utf-8")
        )
        assert meta["operator"] == "Kovács Ádám"


def _f28_samples(elapsed: list[float]) -> list[ChemIdSample]:
    return [ChemIdSample(t, 12.5, -0.35, 22.0) for t in elapsed]


def _f28_gaps(samples) -> list[float]:
    return [b.elapsed_s - a.elapsed_s for a, b in itertools.pairwise(samples)]


class TestR1B8cF28ExportOutputInterval5to100s:
    """[regresszió, R1 B8c, F-28 (P3), DFAM-003, D-B8-4] TI SLVA725A §2.2 /
    SLUUBD0A §3 (CLAIM_LEDGER GPC-CHEMID-001): az adatnapló mintavételi
    intervalluma 5-100 s. A resample_for_export() korábban az utolsó nyers
    mintát feltétel nélkül hozzáfűzte (akár <5 s-ra az előzőtől), és a
    KIMENETI hézagokat (>100 s adatlyuk) semmi nem ellenőrizte. Interpoláció
    nincs: a hézag-ellenőrzés a RESAMPLED kimeneten fut."""

    def test_last_raw_sample_closer_than_5s_is_dropped(self):
        result = resample_for_export(
            _f28_samples([0, 2, 4, 30, 32, 60, 62, 64]), sample_interval_s=30.0,
        )
        assert [s.elapsed_s for s in result] == [0, 30, 60]
        assert all(gap >= 5.0 for gap in _f28_gaps(result))

    def test_last_raw_sample_exactly_5s_is_kept(self):
        result = resample_for_export(_f28_samples([0, 30, 35]), sample_interval_s=30.0)
        assert [s.elapsed_s for s in result] == [0, 30, 35]

    def test_last_raw_sample_just_below_5s_is_dropped(self):
        result = resample_for_export(_f28_samples([0, 30, 34.9]), sample_interval_s=30.0)
        assert [s.elapsed_s for s in result] == [0, 30]

    def test_interval_violation_detected_on_resampled_output(self):
        from Prog.src.gpcchem_export import export_interval_violations
        resampled = resample_for_export(
            _f28_samples([0, 30, 60, 400, 430]), sample_interval_s=30.0,
        )
        # nincs interpoláció: a 340 s-os adatlyuk a kimenetben is megmarad
        assert [s.elapsed_s for s in resampled] == [0, 30, 60, 400, 430]
        violations = export_interval_violations(resampled)
        assert len(violations) == 1
        assert "60" in violations[0] and "400" in violations[0]

    def test_gap_exactly_100s_is_not_a_violation(self):
        from Prog.src.gpcchem_export import export_interval_violations
        assert export_interval_violations(_f28_samples([0, 30, 130])) == []

    def test_gap_just_above_100s_is_a_violation(self):
        from Prog.src.gpcchem_export import export_interval_violations
        assert len(export_interval_violations(_f28_samples([0, 30, 130.1]))) == 1

    def test_helper_is_pure_and_handles_short_inputs(self):
        from Prog.src.gpcchem_export import export_interval_violations
        samples = _f28_samples([0, 200])
        snapshot = list(samples)
        assert export_interval_violations([]) == []
        assert export_interval_violations(_f28_samples([5])) == []
        assert len(export_interval_violations(samples)) == 1
        assert samples == snapshot

    def _zip_quality(self, zip_path) -> dict:
        with zipfile.ZipFile(zip_path) as zf:
            return json.loads(zf.read("measurement_quality.json").decode("utf-8"))

    def test_gap_above_100s_downgrades_originally_true_claim_in_zip(self, tmp_path):
        quality = evaluate_ti_compatible_quality(0.05, 0.5, 16)
        assert quality.ti_compatible_quality_claimed is True
        config = GpcChemConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            sample_interval_s=30.0,
        )
        zip_path = build_gpcchem_submission_zip(
            tmp_path, config, _f28_samples([0, 30, 60, 400, 430]), quality,
            "ref", profile_cell_count=6,
        )
        zipped = self._zip_quality(zip_path)
        assert zipped["ti_compatible_quality_claimed"] is False
        assert any("100" in lim for lim in zipped["limitations"])
        on_disk = json.loads((tmp_path / "measurement_quality.json").read_text("utf-8"))
        assert on_disk == zipped

    def test_regular_intervals_keep_originally_true_claim(self, tmp_path):
        quality = evaluate_ti_compatible_quality(0.05, 0.5, 16)
        config = GpcChemConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            sample_interval_s=30.0,
        )
        zip_path = build_gpcchem_submission_zip(
            tmp_path, config, _f28_samples([0, 30, 60, 90, 130]), quality,
            "ref", profile_cell_count=6,
        )
        zipped = self._zip_quality(zip_path)
        assert zipped["ti_compatible_quality_claimed"] is True
        assert zipped["limitations"] == []


def _f28_csv_elapsed_text(zip_path, member: str) -> list[str]:
    """A ZIP-ből VISSZAOLVASOTT CSV elapsed_time_s oszlopa, változatlan
    szövegként (a gyártó-tool ezt látja)."""
    with zipfile.ZipFile(zip_path) as zf:
        text = zf.read(member).decode("utf-8")
    return [row["elapsed_time_s"] for row in csv.DictReader(io.StringIO(text))]


def _f28_csv_gaps(elapsed_text: list[str]):
    from decimal import Decimal
    values = [Decimal(t) for t in elapsed_text]
    return [b - a for a, b in itertools.pairwise(values)]


class TestR1B8cFixupF28CsvCanonicalTimestampInterval:
    """[regresszió, R1 B8c fixup, F-28 N1 (Codex max verifier), DFAM-003]
    TI SLVA725A §2.2 / SLUUBD0A §3.2: 5-100 s mintaköz a TÉNYLEGES
    adatnaplóban. A B8c-ben a mintakiválasztás és az
    export_interval_violations() a kerekítés ELŐTTI float időbélyegen
    dolgozott, a közös CSV-író viszont egy tizedesre kerekít:
    [0.05, 5.05]/5 s -> CSV 0.1, 5.0 (4,9 s köz); [0.15, 100.15]/100 s ->
    CSV 0.1, 100.2 (100,1 s köz), miközben a GPC claimed=True maradt. Az
    elvárások a ZIP-ből visszaolvasott CSV szövegén (Decimal, nem float)
    ellenőrzöttek, nem a kód saját kerekítő helperén."""

    _MEMBER = "roomtemp_rel_dis_rel.csv"

    def _build(self, tmp_path, elapsed, interval_s):
        quality = evaluate_ti_compatible_quality(0.05, 0.5, 16)
        assert quality.ti_compatible_quality_claimed is True
        config = GpcChemConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            sample_interval_s=interval_s,
        )
        zip_path = build_gpcchem_submission_zip(
            tmp_path, config, _f28_samples(elapsed), quality,
            "ref", profile_cell_count=6,
        )
        with zipfile.ZipFile(zip_path) as zf:
            zipped_quality = json.loads(zf.read("measurement_quality.json").decode("utf-8"))
        return _f28_csv_elapsed_text(zip_path, self._MEMBER), zipped_quality

    @pytest.mark.parametrize("interval_s", [5.0, 30.0])
    def test_csv_gap_rounding_below_5s_is_not_exported(self, tmp_path, interval_s):
        # 5.0: a köztes (küszöb-) kiválasztás; 30.0: a záró-minta szabály.
        raw = [0.05, 5.05]
        assert raw[1] - raw[0] >= 5.0  # a float köz NEM sértene
        elapsed_text, quality = self._build(tmp_path, raw, interval_s)
        from decimal import Decimal
        assert all(gap >= Decimal("5.0") for gap in _f28_csv_gaps(elapsed_text))
        assert elapsed_text == ["0.1"]
        assert quality["ti_compatible_quality_claimed"] is True

    def test_csv_gap_rounding_above_100s_downgrades_claim(self, tmp_path):
        raw = [0.15, 100.15]
        assert raw[1] - raw[0] <= 100.0  # a float köz NEM sértene
        elapsed_text, quality = self._build(tmp_path, raw, 100.0)
        from decimal import Decimal
        gaps = _f28_csv_gaps(elapsed_text)
        assert elapsed_text == ["0.1", "100.2"]
        assert gaps == [Decimal("100.1")]
        assert quality["ti_compatible_quality_claimed"] is False
        assert any("100.1" in lim for lim in quality["limitations"])

    @pytest.mark.parametrize(
        ("raw", "interval_s"),
        [
            ([0.05, 5.05], 5.0),
            ([0.05, 5.05], 30.0),
            ([0.15, 100.15], 100.0),
            ([0.05, 5.05, 10.05, 15.05, 20.05], 5.0),
            ([0.0, 30.05, 60.0, 90.05, 95.05], 30.0),
        ],
    )
    def test_claim_true_implies_every_csv_gap_within_5_to_100s(
        self, tmp_path, raw, interval_s,
    ):
        from decimal import Decimal
        elapsed_text, quality = self._build(tmp_path, raw, interval_s)
        gaps = _f28_csv_gaps(elapsed_text)
        assert all(gap >= Decimal("5.0") for gap in gaps)
        if quality["ti_compatible_quality_claimed"]:
            assert all(gap <= Decimal("100.0") for gap in gaps)
        else:
            assert any(gap > Decimal("100.0") for gap in gaps)


class TestR1B8cFixupF28GateRunsOnResampledOutputNotRawSamples:
    """[regresszió, R1 B8c fixup, F-28 P3-1 (Claude MAX verifier), DFAM-003]
    A nyers mintaközök mind <= 100 s (60 s), de a 100 s-os intervallumú
    mohó resample kimenete [0, 120, 240, 300] -> két 120 s-os kimeneti köz.
    Ha a builder a NYERS mintákon ellenőrizne, nem találna sértést."""

    _RAW = (0.0, 60.0, 120.0, 180.0, 240.0, 300.0)

    def test_gpc_builder_downgrades_on_resampled_gap(self, tmp_path):
        assert max(b - a for a, b in itertools.pairwise(self._RAW)) <= 100.0
        quality = evaluate_ti_compatible_quality(0.05, 0.5, 16)
        config = GpcChemConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            sample_interval_s=100.0,
        )
        zip_path = build_gpcchem_submission_zip(
            tmp_path, config, _f28_samples(list(self._RAW)), quality,
            "ref", profile_cell_count=6,
        )
        assert _f28_csv_elapsed_text(zip_path, "roomtemp_rel_dis_rel.csv") == [
            "0.0", "120.0", "240.0", "300.0",
        ]
        with zipfile.ZipFile(zip_path) as zf:
            zipped = json.loads(zf.read("measurement_quality.json").decode("utf-8"))
        assert zipped["ti_compatible_quality_claimed"] is False
        limitation = next(lim for lim in zipped["limitations"] if "100" in lim)
        assert "0.0 s -> 120.0 s" in limitation
        assert "120.0 s -> 240.0 s" in limitation


class TestR7B2LooseCsvMatchesZip:
    """R7-B2: a laza CSV sorvégei is a csv.writer kimenetét őrizzék."""

    def test_loose_csv_bytes_and_rows(self, tmp_path):
        config = GpcChemConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            sample_interval_s=30.0,
        )
        zip_path = build_gpcchem_submission_zip(
            tmp_path, config, _make_samples(n=61), MeasurementQuality(),
            "ref", profile_cell_count=6,
        )
        member = "roomtemp_rel_dis_rel.csv"
        csv_path = tmp_path / member
        with zipfile.ZipFile(zip_path) as zf:
            assert csv_path.read_bytes() == zf.read(member)
        with csv_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        assert len(rows) > 1
        assert all(rows)


class TestR14B1GpcChemNumericConfig:
    """R14-B1: a kiirt cellaszam a konfiguralt bemenetet kovesse."""

    @pytest.mark.parametrize("num_cell_series", [6, 12])
    def test_num_cell_series_matches_input(self, num_cell_series):
        config = GpcChemConfig(
            num_cell_series=num_cell_series, battery_model="FG20721", battery_sample_id="S1",
        )
        assert f"NumCellSeries = {num_cell_series}" in config.to_text().splitlines()
