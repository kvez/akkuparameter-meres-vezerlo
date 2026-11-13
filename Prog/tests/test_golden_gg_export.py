"""
SUP03-GG-005..011: Golden GG export/import unit tesztek.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import zipfile

import pytest

from Prog.src.bq_data_flash_service import FIELD_REGISTRY
from Prog.src.bq_diagnostics import BqDiagnosticsSnapshot, BqFieldReading
from Prog.src.golden_gg_export import (
    GoldenGgCompatibilityNotConfirmedError,
    GoldenGgConfig,
    GoldenGgSample,
    GoldenGgSnapshotIncompleteError,
    MATURITY_EXPERIMENTAL_PACKAGE_ONLY,
    MATURITY_OPTIMIZED_CANDIDATE,
    build_gg_csv_text,
    build_golden_gg_submission_package,
    build_sysrate_csv_text,
    import_ti_gg_output,
    resample_for_export,
)
from Prog.src.gpcchem_export import NumCellSeriesMismatchError


def _reading(value, valid=True, error=None):
    return BqFieldReading(value=value, valid=valid, error=error, timestamp_monotonic=0.0)


def _make_snapshot(qmax_valid: bool = True, ra_valid: bool = True) -> BqDiagnosticsSnapshot:
    return BqDiagnosticsSnapshot(
        device_type=_reading(0x0541),
        fw_version=_reading(0x0311),
        hw_version=_reading(0x0003),
        chem_id=_reading(0x0800),
        control_status=_reading(0x0000),
        voltage_mV=_reading(12500),
        avg_current_mA=_reading(0),
        temperature_C=_reading(22.0),
        soc_percent=_reading(50),
        remaining_capacity_mAh=_reading(3500),
        full_charge_capacity_mAh=_reading(7000),
        flags=_reading(0x0000),
        qmax=_reading(150 if qmax_valid else None, valid=qmax_valid, error=None if qmax_valid else "timeout"),
        update_status=_reading(0x06),
        ra_table_mohm=_reading(
            [10.0, 11.0] if ra_valid else None, valid=ra_valid, error=None if ra_valid else "malformed",
        ),
        cycle_count=_reading(3),
        dod0=_reading(0),
        avg_i_last_run_mA=_reading(0),
        avg_p_last_run=_reading(0),
    )


def _make_samples(n: int = 20, dt: float = 1.0, current_A: float = -1.5) -> list[GoldenGgSample]:
    return [
        GoldenGgSample(
            elapsed_s=i * dt, voltage_V=12.5 - i * 0.001,
            current_A=current_A, temperature_C=22.0,
        )
        for i in range(n)
    ]


class TestSignConvention:
    def test_charge_current_positive(self):
        samples = [GoldenGgSample(0.0, 12.5, 2.0, 22.0)]
        rows = list(csv.DictReader(io.StringIO(build_sysrate_csv_text(samples))))
        assert float(rows[0]["current_mA"]) > 0

    def test_discharge_current_negative(self):
        samples = [GoldenGgSample(0.0, 12.5, -1.5, 22.0)]
        rows = list(csv.DictReader(io.StringIO(build_sysrate_csv_text(samples))))
        assert float(rows[0]["current_mA"]) < 0

    def test_units_mV_mA(self):
        samples = [GoldenGgSample(0.0, 12.5, -1.5, 22.0)]
        rows = list(csv.DictReader(io.StringIO(build_sysrate_csv_text(samples))))
        assert float(rows[0]["voltage_mV"]) == pytest.approx(12500.0)
        assert float(rows[0]["current_mA"]) == pytest.approx(-1500.0)


class TestResampling5to100s:
    def test_rejects_interval_below_5s(self):
        with pytest.raises(ValueError):
            resample_for_export(_make_samples(), sample_interval_s=4.9)

    def test_rejects_interval_above_100s(self):
        with pytest.raises(ValueError):
            resample_for_export(_make_samples(), sample_interval_s=100.1)

    def test_first_and_last_sample_always_kept(self):
        samples = _make_samples(n=37, dt=1.0)
        result = resample_for_export(samples, sample_interval_s=10.0)
        assert result[0].elapsed_s == pytest.approx(0.0)
        assert result[-1].elapsed_s == pytest.approx(samples[-1].elapsed_s)

    def test_empty_samples_returns_empty(self):
        assert resample_for_export([], sample_interval_s=30.0) == []


class TestGgCsv:
    def test_valid_fields_present_with_values(self):
        snapshot = _make_snapshot()
        rows = list(csv.DictReader(io.StringIO(build_gg_csv_text(snapshot))))
        by_param = {r["parameter"]: r for r in rows}
        assert by_param["qmax"]["value"] == "150"
        assert by_param["qmax"]["valid"] == "True"

    def test_invalid_field_never_silently_zero(self):
        """Kapu-elv (bq_diagnostics.py mintája): egy érvénytelen mező SOHA
        nem jelenhet meg csendben 0/üres 'sikeres' értékként jelzés nélkül."""
        snapshot = _make_snapshot(qmax_valid=False)
        rows = list(csv.DictReader(io.StringIO(build_gg_csv_text(snapshot))))
        by_param = {r["parameter"]: r for r in rows}
        assert by_param["qmax"]["value"] == ""
        assert by_param["qmax"]["valid"] == "False"
        assert by_param["qmax"]["error"] == "timeout"


class TestSubmissionPackage:
    def test_blocked_on_wrong_num_cell_series(self, tmp_path):
        config = GoldenGgConfig(
            num_cell_series=12, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0,
        )
        with pytest.raises(NumCellSeriesMismatchError):
            build_golden_gg_submission_package(
                tmp_path, config, _make_samples(), _make_snapshot(),
                "session_ref", profile_cell_count=6,
            )

    def test_blocked_on_incomplete_gauge_snapshot(self, tmp_path):
        """SUP03-GG-011: hiányos gauge-parameterekkel (Qmax/Ra table nem
        olvasható) nem szabad TI-nek csomagot előállítani."""
        config = GoldenGgConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0,
        )
        with pytest.raises(GoldenGgSnapshotIncompleteError):
            build_golden_gg_submission_package(
                tmp_path, config, _make_samples(), _make_snapshot(ra_valid=False),
                "session_ref", profile_cell_count=6,
            )

    def test_package_created_with_all_files(self, tmp_path):
        config = GoldenGgConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0, sample_interval_s=30.0,
        )
        zip_path = build_golden_gg_submission_package(
            tmp_path, config, _make_samples(n=200, dt=1.0), _make_snapshot(),
            "session_ref_abc", profile_cell_count=6,
        )
        assert zip_path.name == "golden_gg_submission.zip"
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            # CF-36 (governance-v2 review-kör 1, TI-SLUUBD0A §3): a fájlnév
            # "roomtemp.csv", nem a projekt korábbi belső "sysrate_rel_
            # dis_rel.csv" neve -- a forrás explicit ezt a nevet várja.
            assert names == {
                "config.txt", "roomtemp.csv", "gg.csv",
                "metadata_limitations.json", "source_session_reference.txt",
                "project_metadata.json",
            }
            config_text = zf.read("config.txt").decode("utf-8")
            # R14-B1: a ZIP konfiguraciojaban is teljes numerikus sorok.
            config_lines = config_text.splitlines()
            assert "NumCellSeries=6" in config_lines
            assert "ElapsedTimeColumn=0" in config_lines
            assert "VoltageColumn=1" in config_lines
            assert "CurrentColumn=2" in config_lines
            assert "TemperatureColumn=3" in config_lines
            # CF-36: a §3.1 szerint kötelező ProcessingType/ChemID kulcsok.
            assert "ProcessingType=4" in config_text
            assert "ChemID=2048" in config_lines  # 0x0800 == 2048; R14-B1: teljes sor
            # D-Extra (2026-09-13, sibling-sweep): a projekt-specifikus
            # mezők (nem TI-kötelező kulcsok) a config.txt-ből a
            # project_metadata.json sidecarba kerültek.
            assert "SystemRateCurrentA" not in config_text
            project_metadata = json.loads(
                zf.read("project_metadata.json").decode("utf-8")
            )
            assert project_metadata["system_rate_current_A"] == pytest.approx(5.0)

    def test_missing_metadata_recorded_as_limitation_not_silently_dropped(self, tmp_path):
        config = GoldenGgConfig(
            num_cell_series=6, battery_model="M", battery_sample_id="S1",
            system_rate_current_A=5.0,
        )
        build_golden_gg_submission_package(
            tmp_path, config, _make_samples(), _make_snapshot(), "ref",
            profile_cell_count=6, missing_metadata_fields=["ThermalEnvironmentDescription"],
        )
        data = json.loads((tmp_path / "metadata_limitations.json").read_text())
        assert "ThermalEnvironmentDescription" in data["missing_fields"]

    def test_blocks_export_when_chem_id_unreadable(self, tmp_path):
        """CF-36 (governance-v2 review-kör 1): a chem_id KÖTELEZŐ config.txt
        kulcs is (TI-SLUUBD0A §3.1) -- ha a BQ34Z110-ről nem olvasható,
        ugyanaz a gate-elv érvényes, mint qmax/ra_table_mohm-nál."""
        config = GoldenGgConfig(
            num_cell_series=6, battery_model="M", battery_sample_id="S1",
            system_rate_current_A=5.0,
        )
        snapshot = dataclasses.replace(
            _make_snapshot(),
            chem_id=BqFieldReading(value=None, valid=False, error="timeout", timestamp_monotonic=0.0),
        )
        with pytest.raises(GoldenGgSnapshotIncompleteError):
            build_golden_gg_submission_package(
                tmp_path, config, _make_samples(), snapshot, "ref", profile_cell_count=6,
            )


class TestCf36ConfigTxtConformsToTiSluubd0a:
    """[regresszió, governance-v2 review-kör 1, CF-36] a config.txt korábban
    NEM konform a TI-SLUUBD0A §3.1/§3.4.1 forrással: hiányzott a kötelező
    `ProcessingType=4` és `ChemID` kulcs, mind a 4 nulla-alapú oszlopindex
    kulcs, és egy nem-specifikált `[GOLDEN_GG_CONFIG]` szekciófejlécet +
    `#` komment sort tartalmazott sima kulcs=érték szótár helyett."""

    def _config(self) -> GoldenGgConfig:
        return GoldenGgConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0,
        )

    def test_no_section_header(self):
        assert "[GOLDEN_GG_CONFIG]" not in self._config().to_text(chem_id="2048")

    def test_no_comment_lines(self):
        text = self._config().to_text(chem_id="2048")
        assert not any(line.strip().startswith("#") for line in text.splitlines())

    def test_processing_type_is_4(self):
        assert "ProcessingType=4" in self._config().to_text(chem_id="2048")

    def test_chem_id_key_present(self):
        assert "ChemID=2048" in self._config().to_text(chem_id="2048")

    def test_all_four_column_index_keys_present(self):
        text = self._config().to_text(chem_id="2048")
        # R14-B1: a szamjegy-prefix helyett a teljes sor egyezzen.
        lines = text.splitlines()
        assert "ElapsedTimeColumn=0" in lines
        assert "VoltageColumn=1" in lines
        assert "CurrentColumn=2" in lines
        assert "TemperatureColumn=3" in lines

    def test_column_indices_match_actual_csv_header_order(self):
        csv_text = build_sysrate_csv_text(_make_samples(n=3))
        header = next(csv.reader(io.StringIO(csv_text)))
        config_text = self._config().to_text(chem_id="2048")
        column_keys = {
            "elapsed_time_s": "ElapsedTimeColumn",
            "voltage_mV": "VoltageColumn",
            "current_mA": "CurrentColumn",
            "cell_surface_temperature_C": "TemperatureColumn",
        }
        for csv_col_name, config_key in column_keys.items():
            expected_index = header.index(csv_col_name)
            assert f"{config_key}={expected_index}" in config_text.splitlines()

    def test_processing_type_and_chem_id_appear_before_column_keys(self):
        """TI-SLUUBD0A §3.4.1 Config.txt File sorrendje: ProcessingType,
        ChemID, NumCellSeries, majd a 4 oszlopindex kulcs."""
        lines = [ln for ln in self._config().to_text(chem_id="2048").splitlines() if ln.strip()]
        assert lines[0] == "ProcessingType=4"
        assert lines[1] == "ChemID=2048"
        assert lines[2] == "NumCellSeries=6"


class TestTiOutputImport:
    def test_requires_evidence_when_compatibility_confirmed(self, tmp_path):
        gg_out = tmp_path / "gg_out.csv"
        gg_out.write_text("dummy", encoding="utf-8")
        with pytest.raises(GoldenGgCompatibilityNotConfirmedError):
            import_ti_gg_output(
                tmp_path, gg_out, qmax=150, ra_table_mohm=(10.0, 11.0),
                ra0_charge_mohm=9.5, thermal_parameters={}, gpc_report_path=None,
                warnings=(), compatibility_confirmed=True,
                compatibility_confirmation_evidence="",
            )

    def test_missing_gg_out_csv_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            import_ti_gg_output(
                tmp_path, tmp_path / "missing.csv", qmax=150,
                ra_table_mohm=(10.0,), ra0_charge_mohm=None, thermal_parameters={},
                gpc_report_path=None, warnings=(), compatibility_confirmed=False,
            )

    def test_unconfirmed_stays_experimental_package_only(self, tmp_path):
        gg_out = tmp_path / "gg_out.csv"
        gg_out.write_text("dummy", encoding="utf-8")
        result = import_ti_gg_output(
            tmp_path, gg_out, qmax=150, ra_table_mohm=(10.0, 11.0),
            ra0_charge_mohm=9.5, thermal_parameters={"a": 1}, gpc_report_path=None,
            warnings=("W1",), compatibility_confirmed=False,
        )
        assert result.maturity_status == MATURITY_EXPERIMENTAL_PACKAGE_ONLY
        assert result.bq34z110_compatibility_confirmed is False

    def test_confirmed_with_evidence_yields_optimized_candidate(self, tmp_path):
        gg_out = tmp_path / "gg_out.csv"
        gg_out.write_text("dummy", encoding="utf-8")
        result = import_ti_gg_output(
            tmp_path, gg_out, qmax=150, ra_table_mohm=(10.0, 11.0),
            ra0_charge_mohm=9.5, thermal_parameters={}, gpc_report_path=None,
            warnings=(), compatibility_confirmed=True,
            compatibility_confirmation_evidence="TI support ticket #12345 confirmed compatibility",
        )
        assert result.maturity_status == MATURITY_OPTIMIZED_CANDIDATE

    def test_file_hash_computed_and_traceability_record_written(self, tmp_path):
        gg_out = tmp_path / "gg_out.csv"
        gg_out.write_text("some content", encoding="utf-8")
        result = import_ti_gg_output(
            tmp_path, gg_out, qmax=150, ra_table_mohm=(10.0,), ra0_charge_mohm=None,
            thermal_parameters={}, gpc_report_path=None, warnings=(),
            compatibility_confirmed=False,
        )
        assert len(result.gg_out_csv_hash) == 64  # sha256 hex
        record = json.loads((tmp_path / "golden_gg_import_record.json").read_text())
        assert record["gg_out_csv_hash"] == result.gg_out_csv_hash


class TestR1B8cF28GgExportBlocksOnOutputGapAbove100s:
    """[regresszió, R1 B8c, F-28 (P3), DFAM-003, D-B8-4] GG-ben nincs
    explicit minőségi objektum -- a sikeres builder-visszatérés közvetlenül
    "beküldhető" csomagot jelent (golden_gg_controller.py package_ready_for_
    ti_submission). Ezért >100 s kimeneti hézagnál a builder ÍRÁS ELŐTT
    ValueError-alapú kivétellel blokkol (SUP03-GG-011 precedens)."""

    def _config(self):
        return GoldenGgConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0, sample_interval_s=30.0,
        )

    def _samples(self, elapsed):
        return [GoldenGgSample(t, 12.5, -1.5, 22.0) for t in elapsed]

    def test_gap_above_100s_blocks_before_any_file_is_written(self, tmp_path):
        from Prog.src.golden_gg_export import GoldenGgExportIntervalError
        out = tmp_path / "gg_out"
        with pytest.raises(GoldenGgExportIntervalError) as excinfo:
            build_golden_gg_submission_package(
                out, self._config(), self._samples([0, 30, 60, 400, 430]),
                _make_snapshot(), "ref", profile_cell_count=6,
            )
        assert isinstance(excinfo.value, ValueError)
        assert not out.exists()

    def test_gap_exactly_100s_builds_package(self, tmp_path):
        zip_path = build_golden_gg_submission_package(
            tmp_path, self._config(), self._samples([0, 30, 130]),
            _make_snapshot(), "ref", profile_cell_count=6,
        )
        assert zip_path.exists()

    def test_last_sample_closer_than_5s_not_exported(self, tmp_path):
        zip_path = build_golden_gg_submission_package(
            tmp_path, self._config(), self._samples([0, 2, 4, 30, 32, 60, 62, 64]),
            _make_snapshot(), "ref", profile_cell_count=6,
        )
        with zipfile.ZipFile(zip_path) as zf:
            rows = list(csv.DictReader(io.StringIO(zf.read("roomtemp.csv").decode("utf-8"))))
        assert [float(r["elapsed_time_s"]) for r in rows] == [0.0, 30.0, 60.0]


class TestR1B8cFixupF28GgCsvCanonicalTimestampInterval:
    """[regresszió, R1 B8c fixup, F-28 N1 (Codex max verifier) + P3-1
    (Claude MAX verifier), DFAM-003] TI SLUUBD0A §3.2: 5-100 s mintaköz a
    TÉNYLEGES roomtemp.csv-ben. A B8c a kerekítés előtti float időbélyegen
    döntött, a közös CSV-író egy tizedesre kerekít: [0.05, 5.05]/5 s -> CSV
    4,9 s köz; [0.15, 100.15]/100 s -> CSV 100,1 s köz, és a builder
    sikeresen ("beküldhető" csomag) visszatért. Elvárások a ZIP-ből
    visszaolvasott CSV szövegén (Decimal)."""

    def _config(self, interval_s):
        return GoldenGgConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0, sample_interval_s=interval_s,
        )

    def _samples(self, elapsed):
        return [GoldenGgSample(t, 12.5, -1.5, 22.0) for t in elapsed]

    @staticmethod
    def _csv_elapsed_text(zip_path) -> list[str]:
        with zipfile.ZipFile(zip_path) as zf:
            text = zf.read("roomtemp.csv").decode("utf-8")
        return [row["elapsed_time_s"] for row in csv.DictReader(io.StringIO(text))]

    @pytest.mark.parametrize("interval_s", [5.0, 30.0])
    def test_csv_gap_rounding_below_5s_is_not_exported(self, tmp_path, interval_s):
        from decimal import Decimal
        from itertools import pairwise
        raw = [0.05, 5.05]
        assert raw[1] - raw[0] >= 5.0  # a float köz NEM sértene
        zip_path = build_golden_gg_submission_package(
            tmp_path, self._config(interval_s), self._samples(raw),
            _make_snapshot(), "ref", profile_cell_count=6,
        )
        elapsed_text = self._csv_elapsed_text(zip_path)
        gaps = [Decimal(b) - Decimal(a) for a, b in pairwise(elapsed_text)]
        assert all(Decimal("5.0") <= gap <= Decimal("100.0") for gap in gaps)
        assert elapsed_text == ["0.1"]

    def test_csv_gap_rounding_above_100s_blocks_before_any_file(self, tmp_path):
        from Prog.src.golden_gg_export import GoldenGgExportIntervalError
        raw = [0.15, 100.15]
        assert raw[1] - raw[0] <= 100.0  # a float köz NEM sértene
        out = tmp_path / "gg_out"
        with pytest.raises(GoldenGgExportIntervalError) as excinfo:
            build_golden_gg_submission_package(
                out, self._config(100.0), self._samples(raw),
                _make_snapshot(), "ref", profile_cell_count=6,
            )
        assert "100.1" in str(excinfo.value)
        assert not out.exists()

    def test_gate_runs_on_resampled_output_not_raw_samples(self, tmp_path):
        # P3-1: nyers közök 60 s (<= 100), a 100 s-os resample kimenete
        # [0, 120, 240, 300] -> két 120 s-os kimeneti köz.
        from itertools import pairwise

        from Prog.src.golden_gg_export import GoldenGgExportIntervalError
        raw = [0.0, 60.0, 120.0, 180.0, 240.0, 300.0]
        assert max(b - a for a, b in pairwise(raw)) <= 100.0
        out = tmp_path / "gg_out"
        with pytest.raises(GoldenGgExportIntervalError) as excinfo:
            build_golden_gg_submission_package(
                out, self._config(100.0), self._samples(raw),
                _make_snapshot(), "ref", profile_cell_count=6,
            )
        assert "0.0 s -> 120.0 s" in str(excinfo.value)
        assert "120.0 s -> 240.0 s" in str(excinfo.value)
        assert not out.exists()


class TestR1F43QmaxTiRangeGate:
    """[regresszió, R1 F-43 (P2, NS-13), DFAM-003] A gg.csv-be kerülő Qmax
    a .valid mellett a SLUSB55B Table 20 szerinti TI-tartományon (Qmax Cell 0,
    SC82/off0, I2, 0..32767 mAh; FIELD_REGISTRY "qmax_cell0_initial_mAh") belül
    kell legyen. HEAD c05b511-en a -1/-32768/32768 Qmax "beküldhető" csomagot
    adott (a driver előjeles I2-ként dekódol: 0xFFFF -> -1, 0x8000 -> -32768).
    Elvárás: GoldenGgSnapshotIncompleteError írás előtt, OUT_OF_TI_RANGE."""

    def _config(self):
        return GoldenGgConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0,
        )

    @staticmethod
    def _snapshot_with_qmax(value):
        return dataclasses.replace(_make_snapshot(), qmax=_reading(value))

    @pytest.mark.parametrize("qmax", [-1, -32768, 32768])
    def test_out_of_ti_range_qmax_blocks_before_any_file_is_written(self, tmp_path, qmax):
        out = tmp_path / "pkg"
        with pytest.raises(GoldenGgSnapshotIncompleteError) as excinfo:
            build_golden_gg_submission_package(
                out, self._config(), _make_samples(n=200), self._snapshot_with_qmax(qmax),
                "ref", profile_cell_count=6,
            )
        assert f"OUT_OF_TI_RANGE(value={qmax})" in str(excinfo.value)
        assert not out.exists()

    @pytest.mark.parametrize("qmax", [0, 32767])
    def test_ti_range_bounds_build_package(self, tmp_path, qmax):
        zip_path = build_golden_gg_submission_package(
            tmp_path, self._config(), _make_samples(n=200), self._snapshot_with_qmax(qmax),
            "ref", profile_cell_count=6,
        )
        assert zip_path.exists()

    def test_range_comes_from_field_registry(self, tmp_path, monkeypatch):
        spec = FIELD_REGISTRY["qmax_cell0_initial_mAh"]
        monkeypatch.setitem(
            FIELD_REGISTRY, "qmax_cell0_initial_mAh", dataclasses.replace(spec, max_value=100),
        )
        with pytest.raises(GoldenGgSnapshotIncompleteError) as excinfo:
            build_golden_gg_submission_package(
                tmp_path / "pkg", self._config(), _make_samples(n=200),
                self._snapshot_with_qmax(150), "ref", profile_cell_count=6,
            )
        assert "OUT_OF_TI_RANGE(value=150)" in str(excinfo.value)

    def test_unreadable_qmax_keeps_read_error_not_range_text(self, tmp_path):
        snapshot = dataclasses.replace(
            _make_snapshot(), qmax=_reading(None, valid=False, error="timeout"),
        )
        with pytest.raises(GoldenGgSnapshotIncompleteError) as excinfo:
            build_golden_gg_submission_package(
                tmp_path / "pkg", self._config(), _make_samples(n=200), snapshot,
                "ref", profile_cell_count=6,
            )
        assert "'qmax': 'timeout'" in str(excinfo.value)
        assert "OUT_OF_TI_RANGE" not in str(excinfo.value)

    def test_out_of_range_qmax_and_invalid_ra_both_reported(self, tmp_path):
        snapshot = dataclasses.replace(_make_snapshot(ra_valid=False), qmax=_reading(-1))
        with pytest.raises(GoldenGgSnapshotIncompleteError) as excinfo:
            build_golden_gg_submission_package(
                tmp_path / "pkg", self._config(), _make_samples(n=200), snapshot,
                "ref", profile_cell_count=6,
            )
        assert "OUT_OF_TI_RANGE(value=-1)" in str(excinfo.value)
        assert "ra_table_mohm" in str(excinfo.value)


class TestR7B2LooseCsvMatchesZip:
    """R7-B2: mindkét laza CSV egyezzen a ZIP-beli példányával."""

    @pytest.mark.parametrize("member", ["roomtemp.csv", "gg.csv"])
    def test_loose_csv_bytes_and_rows(self, tmp_path, member):
        config = GoldenGgConfig(
            num_cell_series=6, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0, sample_interval_s=30.0,
        )
        zip_path = build_golden_gg_submission_package(
            tmp_path, config, _make_samples(n=61), _make_snapshot(),
            "ref", profile_cell_count=6,
        )
        csv_path = tmp_path / member
        with zipfile.ZipFile(zip_path) as zf:
            assert csv_path.read_bytes() == zf.read(member)
        with csv_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        assert len(rows) > 1
        assert all(rows)


class TestR14B1GoldenGgNumericConfig:
    """R14-B1: a kiirt cellaszam a konfiguralt bemenetet kovesse."""

    @pytest.mark.parametrize("num_cell_series", [6, 12])
    def test_num_cell_series_matches_input(self, num_cell_series):
        config = GoldenGgConfig(
            num_cell_series=num_cell_series, battery_model="FG20721", battery_sample_id="S1",
            system_rate_current_A=5.0,
        )
        assert f"NumCellSeries={num_cell_series}" in config.to_text(chem_id="2048").splitlines()
