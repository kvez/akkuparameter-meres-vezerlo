"""
test_bq_calibration_statistics.py — WP-AK-09: Prog/src/bq_calibration_
statistics.py tesztek (SUP03-CAL-007/009).
"""
from __future__ import annotations

import csv
import json

import pytest

from Prog.src.bq_calibration_statistics import (
    OUTLIER_POLICY,
    CalibrationSample,
    CalibrationStatisticsCollector,
    write_all_calibration_artifacts,
    write_calibration_20_board_statistics_csv,
    write_calibration_master_report,
    write_reference_instruments_json,
    write_traceability_notes_txt,
    write_voltage_calibration_procedure_txt,
)


def _make_sample(board_id: str, **kwargs) -> CalibrationSample:
    sample = CalibrationSample(
        board_id=board_id, cc_gain=0.47, cc_delta=560000.0, cc_offset=-10, board_offset=2,
    )
    for key, value in kwargs.items():
        setattr(sample, key, value)
    return sample


class TestCollectorAcceptance:
    def test_in_range_sample_is_accepted(self):
        collector = CalibrationStatisticsCollector()
        sample = collector.add_sample(_make_sample("B001"))
        assert sample.accepted is True
        assert sample.reject_reason == ""

    def test_gain_out_of_range_is_rejected_with_reason(self):
        collector = CalibrationStatisticsCollector()
        sample = collector.add_sample(_make_sample("B002", cc_gain=50.0))
        assert sample.accepted is False
        assert "cc_gain_out_of_datasheet_range" in sample.reject_reason

    def test_delta_out_of_range_is_rejected_with_reason(self):
        collector = CalibrationStatisticsCollector()
        sample = collector.add_sample(_make_sample("B003", cc_delta=1.0))
        assert sample.accepted is False
        assert "cc_delta_out_of_datasheet_range" in sample.reject_reason

    def test_temp_offset_without_source_is_rejected(self):
        collector = CalibrationStatisticsCollector()
        sample = collector.add_sample(_make_sample("B004", temp_offset=5, temp_offset_source=""))
        assert sample.accepted is False
        assert "temp_offset_missing_source" in sample.reject_reason

    def test_temp_offset_with_source_is_accepted(self):
        collector = CalibrationStatisticsCollector()
        sample = collector.add_sample(
            _make_sample("B005", temp_offset=5, temp_offset_source="EXTERNAL_TI_TOOL")
        )
        assert sample.accepted is True

    def test_board_offset_out_of_range_is_rejected_with_reason(self):
        """Review-kör 5, Gyári forrás F5-4 (P2): Table 20 (SLUSB55B)
        Board Offset (I1) explicit Min=-128/Max=127 -- korábban ez a mező
        SEMMILYEN tartomány-ellenőrzést nem kapott itt."""
        collector = CalibrationStatisticsCollector()
        sample = collector.add_sample(_make_sample("B006", board_offset=128))
        assert sample.accepted is False
        assert "board_offset_out_of_datasheet_range" in sample.reject_reason

    def test_board_offset_at_exact_boundary_is_accepted(self):
        collector = CalibrationStatisticsCollector()
        sample = collector.add_sample(_make_sample("B007", board_offset=-128))
        assert sample.accepted is True
        sample2 = collector.add_sample(_make_sample("B008", board_offset=127))
        assert sample2.accepted is True

    def test_temp_offset_out_of_range_is_rejected_with_reason(self):
        """L. test_board_offset_out_of_range_is_rejected_with_reason
        docstringje ugyanerről a Table 20 forrásról (Int Temp Offset,
        ugyanaz az I1 Min=-128/Max=127)."""
        collector = CalibrationStatisticsCollector()
        sample = collector.add_sample(
            _make_sample("B009", temp_offset=200, temp_offset_source="EXTERNAL_TI_TOOL")
        )
        assert sample.accepted is False
        assert "temp_offset_out_of_datasheet_range" in sample.reject_reason


class TestStatisticsComputation:
    def test_mean_stddev_min_max_over_accepted_samples(self):
        collector = CalibrationStatisticsCollector()
        for i, gain in enumerate([0.40, 0.45, 0.50]):
            collector.add_sample(_make_sample(f"B{i}", cc_gain=gain))
        result = collector.compute()
        assert result.sample_count_total == 3
        assert result.sample_count_accepted == 3
        assert result.sample_count_rejected == 0
        assert result.cc_gain.n == 3
        assert result.cc_gain.mean == pytest.approx(0.45)
        assert result.cc_gain.minimum == pytest.approx(0.40)
        assert result.cc_gain.maximum == pytest.approx(0.50)
        assert result.cc_gain.stddev is not None
        assert result.outlier_policy == OUTLIER_POLICY

    def test_rejected_samples_excluded_from_statistics_but_counted(self):
        collector = CalibrationStatisticsCollector()
        collector.add_sample(_make_sample("B1", cc_gain=0.40))
        collector.add_sample(_make_sample("B2", cc_gain=50.0))  # rejected
        result = collector.compute()
        assert result.sample_count_accepted == 1
        assert result.sample_count_rejected == 1
        assert result.cc_gain.n == 1
        assert result.rejected_reasons == {"cc_gain_out_of_datasheet_range": 1}

    def test_single_sample_has_no_stddev(self):
        collector = CalibrationStatisticsCollector()
        collector.add_sample(_make_sample("B1"))
        result = collector.compute()
        assert result.cc_gain.n == 1
        assert result.cc_gain.stddev is None

    def test_no_samples_yields_zero_n_and_none_fields(self):
        collector = CalibrationStatisticsCollector()
        result = collector.compute()
        assert result.sample_count_total == 0
        assert result.cc_gain.n == 0
        assert result.cc_gain.mean is None

    def test_none_valued_fields_are_ignored_not_treated_as_zero(self):
        collector = CalibrationStatisticsCollector()
        collector.add_sample(_make_sample("B1", board_offset=None))
        collector.add_sample(_make_sample("B2", board_offset=4))
        result = collector.compute()
        assert result.board_offset.n == 1, (
            "a None board_offset ne szamitson bele 0-kent a statisztikaba"
        )
        assert result.board_offset.mean == pytest.approx(4.0)


class TestArtifactWriters:
    def test_write_all_calibration_artifacts_creates_all_five_files(self, tmp_path):
        collector = CalibrationStatisticsCollector()
        for i in range(3):
            collector.add_sample(_make_sample(f"B{i}"))
        result = collector.compute()

        paths = write_all_calibration_artifacts(
            tmp_path,
            result=result,
            samples=list(collector.samples),
            meta={"session_id": "S1", "app_version": "test"},
            voltage_calibration_description="per-unit eljárás leírása",
            reference_instruments={"dmm": {"model": "34465A", "serial": "SN1"}},
            traceability_notes="teszt jegyzet",
        )
        names = {p.name for p in paths}
        assert names == {
            "calibration_master_report.json",
            "calibration_20_board_statistics.csv",
            "voltage_calibration_procedure.txt",
            "reference_instruments.json",
            "traceability_notes.txt",
        }
        for p in paths:
            assert p.exists()

    def test_master_report_contains_meta_and_statistics(self, tmp_path):
        collector = CalibrationStatisticsCollector()
        collector.add_sample(_make_sample("B1"))
        result = collector.compute()
        path = write_calibration_master_report(tmp_path, result, {"session_id": "S1"})
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["meta"]["session_id"] == "S1"
        assert payload["statistics"]["sample_count_accepted"] == 1
        assert payload["statistics"]["outlier_policy"] == OUTLIER_POLICY

    def test_csv_includes_rejected_samples_with_reason(self, tmp_path):
        collector = CalibrationStatisticsCollector()
        collector.add_sample(_make_sample("B1"))
        collector.add_sample(_make_sample("B2", cc_gain=50.0))
        path = write_calibration_20_board_statistics_csv(tmp_path, list(collector.samples))
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        rejected_row = next(r for r in rows if r["board_id"] == "B2")
        assert rejected_row["accepted"] == "False"
        assert "cc_gain_out_of_datasheet_range" in rejected_row["reject_reason"]

    def test_reference_instruments_json_rejects_empty_dict(self, tmp_path):
        with pytest.raises(ValueError, match="nem lehet üres"):
            write_reference_instruments_json(tmp_path, {})

    def test_voltage_calibration_procedure_txt_documents_per_unit_policy(self, tmp_path):
        path = write_voltage_calibration_procedure_txt(tmp_path, "egyedi leírás")
        text = path.read_text(encoding="utf-8")
        assert "EGYEDI" in text
        assert "egyedi leírás" in text

    def test_traceability_notes_txt_writes_raw_text(self, tmp_path):
        path = write_traceability_notes_txt(tmp_path, "szabad szöveg")
        assert path.read_text(encoding="utf-8") == "szabad szöveg"
