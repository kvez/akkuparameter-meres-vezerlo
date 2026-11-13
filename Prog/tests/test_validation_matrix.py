"""
validation_matrix.py tesztek — WP-AK-12 (SUP03-VALID-001..007).

A szintetikus session-könyvtárakat a valós `Logger` (Prog/src/logger.py)
`CSV_COLUMNS`/`BQ_DIAGNOSTICS_COLUMNS` sémájával PONTOSAN egyező oszlopokkal
építjük fel, hogy a fixture ne hordozhasson a kódtól eltérő, tautologikus
feltevést (l. feedback_tautological_fixture_shared_assumption, WP-AK-06).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from Prog.src.logger import BQ_DIAGNOSTICS_COLUMNS, CSV_COLUMNS
from Prog.src.validation_matrix import (
    GATE_FAILED_TOLERANCE,
    GATE_INCOMPLETE,
    GATE_VALIDATED,
    REQUIRED_STEP_KINDS,
    MatrixStepKind,
    ValidationMatrixDataError,
    ValidationTolerance,
    evaluate_step,
    evaluate_validation_matrix,
    extract_step_measurement,
    write_validation_matrix_report,
)


def _write_report(session_dir: Path, status: str, reason: str = "", charge_ah: float = 0.0, discharge_ah: float = 0.0) -> None:
    (session_dir / "report.json").write_text(
        json.dumps({
            "status": status, "reason": reason,
            "total_charge_ah": charge_ah, "total_discharge_ah": discharge_ah,
        }),
        encoding="utf-8",
    )


def _write_samples(session_dir: Path, voltage_V: float = 12.5, current_A: float = 0.0, temp_C: float = 22.0) -> None:
    path = session_dir / "samples.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        row: dict[str, object] = {col: "" for col in CSV_COLUMNS}
        row["battery_voltage_V"] = voltage_V
        row["signed_current_A"] = current_A
        row["battery_temperature_C"] = temp_C
        writer.writerow(row)


def _bq_row(soc_percent: float | None, fcc_mAh: float | None = 7000.0, flags: int | None = 0x0000) -> dict:
    row: dict[str, object] = {col: "" for col in BQ_DIAGNOSTICS_COLUMNS}
    if soc_percent is not None:
        row["soc_percent"] = soc_percent
        row["soc_percent_valid"] = "True"
    else:
        row["soc_percent_valid"] = "False"
    if fcc_mAh is not None:
        row["full_charge_capacity_mAh"] = fcc_mAh
        row["full_charge_capacity_mAh_valid"] = "True"
    else:
        row["full_charge_capacity_mAh_valid"] = "False"
    if flags is not None:
        row["flags"] = flags
        row["flags_valid"] = "True"
    else:
        row["flags_valid"] = "False"
    return row


def _write_bq_diagnostics(session_dir: Path, rows: list[dict]) -> None:
    path = session_dir / "bq_diagnostics.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BQ_DIAGNOSTICS_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _make_session(
    tmp_path: Path, name: str, status: str = "DONE", reason: str = "",
    charge_ah: float = 0.0, discharge_ah: float = 0.0,
    bq_rows: list[dict] | None = None,
    with_samples: bool = True,
) -> Path:
    session_dir = tmp_path / name
    session_dir.mkdir()
    _write_report(session_dir, status, reason, charge_ah, discharge_ah)
    if with_samples:
        _write_samples(session_dir)
    if bq_rows is not None:
        _write_bq_diagnostics(session_dir, bq_rows)
    return session_dir


class TestExtractStepMeasurement:
    def test_missing_report_json_raises(self, tmp_path):
        session_dir = tmp_path / "empty_session"
        session_dir.mkdir()
        with pytest.raises(ValidationMatrixDataError):
            extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)

    def test_missing_bq_diagnostics_marks_unavailable_not_silently_zero(self, tmp_path):
        session_dir = _make_session(tmp_path, "s1", status="DONE", charge_ah=5.0)
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        assert m.gauge_data_available is False
        assert m.gauge_soc_percent_start is None
        assert any("bq_diagnostics.csv" in lim for lim in m.limitations)

    def test_invalid_soc_field_never_becomes_zero(self, tmp_path):
        session_dir = _make_session(
            tmp_path, "s2", status="DONE",
            bq_rows=[_bq_row(soc_percent=None), _bq_row(soc_percent=None)],
        )
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        assert m.gauge_data_available is False
        assert m.gauge_soc_percent_end is None

    def test_valid_gauge_data_extracted(self, tmp_path):
        session_dir = _make_session(
            tmp_path, "s3", status="DONE", charge_ah=3.5,
            bq_rows=[_bq_row(soc_percent=40.0), _bq_row(soc_percent=100.0)],
        )
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        assert m.gauge_data_available is True
        assert m.gauge_soc_percent_start == 40.0
        assert m.gauge_soc_percent_end == 100.0
        assert m.gauge_fcc_mAh_end == 7000.0
        assert m.reference_added_Ah == 3.5

    def test_end_of_charge_true_for_full_charge_when_done(self, tmp_path):
        session_dir = _make_session(tmp_path, "s4", status="DONE")
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        assert m.end_of_charge is True
        assert m.end_of_discharge is False

    def test_end_of_discharge_true_for_complete_discharge_when_done(self, tmp_path):
        session_dir = _make_session(tmp_path, "s5", status="DONE")
        m = extract_step_measurement(session_dir, MatrixStepKind.COMPLETE_DISCHARGE)
        assert m.end_of_discharge is True
        assert m.end_of_charge is False

    def test_not_done_status_means_no_end_of_charge(self, tmp_path):
        session_dir = _make_session(tmp_path, "s6", status="FAULT", reason="DMM_FEEDBACK_LOST")
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        assert m.end_of_charge is False
        assert m.status == "FAULT"

    def test_ocv_correction_events_counts_0_to_1_transitions(self, tmp_path):
        # OCVTAKEN = bit7 = 0x0080
        session_dir = _make_session(
            tmp_path, "s7", status="DONE",
            bq_rows=[
                _bq_row(soc_percent=50.0, flags=0x0000),
                _bq_row(soc_percent=50.0, flags=0x0080),  # 0->1: 1. esemény
                _bq_row(soc_percent=50.0, flags=0x0080),  # marad 1: nem új esemény
                _bq_row(soc_percent=50.0, flags=0x0000),  # 1->0
                _bq_row(soc_percent=50.0, flags=0x0080),  # 0->1: 2. esemény
            ],
        )
        m = extract_step_measurement(session_dir, MatrixStepKind.RELAX_SOC_CORRECTION)
        assert m.ocv_correction_events == 2

    def test_single_bq_row_does_not_falsely_report_zero_drift(self, tmp_path):
        """[review-lelet] egyetlen bq_diagnostics.csv sor start==end lenne,
        hamis '0 gauge-drift' eredményt sugallva (pl. ha a panel csak a
        session VÉGÉN csatlakozott röviden) — ez NEM lehet kiértékelhető."""
        session_dir = _make_session(
            tmp_path, "s9", status="DONE",
            bq_rows=[_bq_row(soc_percent=77.0)],
        )
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        assert m.gauge_data_available is False
        assert m.gauge_soc_percent_start is None
        assert any("legalább 2 minta" in lim for lim in m.limitations)

    def test_missing_samples_csv_documented_as_limitation(self, tmp_path):
        session_dir = _make_session(tmp_path, "s8", status="DONE", with_samples=False)
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        assert m.voltage_V_end is None
        assert any("samples.csv" in lim for lim in m.limitations)


class TestValidationTolerance:
    def test_empty_source_reference_rejected(self):
        """SUP03-VALID-006: nincs forrás nélküli tolerancia."""
        with pytest.raises(ValueError):
            ValidationTolerance(soc_error_percent_max=3.0, source_reference="")

    def test_whitespace_only_source_reference_rejected(self):
        with pytest.raises(ValueError):
            ValidationTolerance(soc_error_percent_max=3.0, source_reference="   ")

    def test_non_positive_tolerance_rejected(self):
        with pytest.raises(ValueError):
            ValidationTolerance(soc_error_percent_max=0.0, source_reference="spec v1.2")

    def test_valid_tolerance_accepted(self):
        tol = ValidationTolerance(soc_error_percent_max=3.0, source_reference="spec v1.2 §4.1")
        assert tol.soc_error_percent_max == 3.0


class TestEvaluateStep:
    def _tolerance(self, max_pct: float = 5.0) -> ValidationTolerance:
        return ValidationTolerance(soc_error_percent_max=max_pct, source_reference="TEST-SPEC-001")

    def test_gauge_unavailable_yields_none_passed(self, tmp_path):
        session_dir = _make_session(tmp_path, "s1", status="DONE")
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        ev = evaluate_step(m, reference_usable_capacity_Ah=7.0, tolerance=self._tolerance())
        assert ev.passed is None
        assert ev.soc_error_percent is None

    def test_within_tolerance_passes(self, tmp_path):
        # Reference: +3.5 Ah / 7.0 Ah kapacitás = +50% delta. Gauge: 40->92 = +52%.
        # SOC_error = 52-50 = 2% <= 5% tolerancia -> PASS.
        session_dir = _make_session(
            tmp_path, "s2", status="DONE", charge_ah=3.5,
            bq_rows=[_bq_row(soc_percent=40.0), _bq_row(soc_percent=92.0)],
        )
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        ev = evaluate_step(m, reference_usable_capacity_Ah=7.0, tolerance=self._tolerance(5.0))
        assert ev.reference_delta_soc_percent == pytest.approx(50.0)
        assert ev.gauge_delta_soc_percent == pytest.approx(52.0)
        assert ev.soc_error_percent == pytest.approx(2.0)
        assert ev.passed is True

    def test_outside_tolerance_fails(self, tmp_path):
        session_dir = _make_session(
            tmp_path, "s3", status="DONE", charge_ah=3.5,
            bq_rows=[_bq_row(soc_percent=40.0), _bq_row(soc_percent=99.0)],
        )
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        ev = evaluate_step(m, reference_usable_capacity_Ah=7.0, tolerance=self._tolerance(5.0))
        assert ev.soc_error_percent == pytest.approx(9.0)
        assert ev.passed is False
        assert any("tolerancia" in r for r in ev.fail_reasons)

    def test_session_not_done_fails_even_within_soc_tolerance(self, tmp_path):
        session_dir = _make_session(
            tmp_path, "s4", status="FAULT", reason="LOAD_COMM_LOST", charge_ah=3.5,
            bq_rows=[_bq_row(soc_percent=40.0), _bq_row(soc_percent=92.0)],
        )
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        ev = evaluate_step(m, reference_usable_capacity_Ah=7.0, tolerance=self._tolerance(5.0))
        assert ev.passed is False
        assert any("FAULT" in r for r in ev.fail_reasons)

    def test_zero_reference_capacity_rejected(self, tmp_path):
        session_dir = _make_session(
            tmp_path, "s5", status="DONE",
            bq_rows=[_bq_row(soc_percent=40.0), _bq_row(soc_percent=92.0)],
        )
        m = extract_step_measurement(session_dir, MatrixStepKind.FULL_CHARGE)
        with pytest.raises(ValueError):
            evaluate_step(m, reference_usable_capacity_Ah=0.0, tolerance=self._tolerance())


def _make_full_valid_matrix(tmp_path: Path) -> dict:
    """A teljes, SUP03-VALID-002 A-F kötelező mátrixot állítja elő, mind
    érvényes gauge adattal, egymással konzisztens SOC-deltákkal.

    `COMPLETE_DISCHARGE` (D) egy ÖNÁLLÓ, teljes 100% -> cutoff kisütés — ez adja
    a `reference_usable_capacity_Ah` (7.0 Ah) bázisértékét, amihez MINDEN cella
    (A/B/C/E/F is) SOC%-deltája viszonyítva van kiszámítva (a modul dokumentált
    módszertana szerint minden cella a SAJÁT session-je alatti Ah-delta / teljes
    kapacitás arányát hasonlítja a gauge SOC%-deltájához, l. validation_matrix.py
    modul docstring). B/C ezért NEM D folytatásaként (60%-ról indulva), hanem
    saját, 100%-hoz közeli tipikus terheléses excursion-ökként szerepelnek."""
    measurements = {}

    full_charge = _make_session(
        tmp_path, "A_full_charge", status="DONE", charge_ah=3.5,
        bq_rows=[_bq_row(soc_percent=50.0), _bq_row(soc_percent=100.0)],
    )
    measurements[MatrixStepKind.FULL_CHARGE] = extract_step_measurement(full_charge, MatrixStepKind.FULL_CHARGE)

    low_load = _make_session(
        tmp_path, "B_low_load", status="DONE", discharge_ah=0.7,
        bq_rows=[_bq_row(soc_percent=100.0), _bq_row(soc_percent=90.0)],
    )
    measurements[MatrixStepKind.LOW_TYPICAL_LOAD] = extract_step_measurement(low_load, MatrixStepKind.LOW_TYPICAL_LOAD)

    high_load = _make_session(
        tmp_path, "C_high_load", status="DONE", discharge_ah=2.1,
        bq_rows=[_bq_row(soc_percent=100.0), _bq_row(soc_percent=70.0)],
    )
    measurements[MatrixStepKind.HIGH_TYPICAL_LOAD] = extract_step_measurement(high_load, MatrixStepKind.HIGH_TYPICAL_LOAD)

    complete_discharge = _make_session(
        tmp_path, "D_complete_discharge", status="DONE", discharge_ah=7.0,
        bq_rows=[_bq_row(soc_percent=100.0), _bq_row(soc_percent=0.0)],
    )
    measurements[MatrixStepKind.COMPLETE_DISCHARGE] = extract_step_measurement(
        complete_discharge, MatrixStepKind.COMPLETE_DISCHARGE
    )

    relax = _make_session(
        tmp_path, "E_relax", status="DONE",
        bq_rows=[_bq_row(soc_percent=0.0), _bq_row(soc_percent=1.0)],
    )
    measurements[MatrixStepKind.RELAX_SOC_CORRECTION] = extract_step_measurement(relax, MatrixStepKind.RELAX_SOC_CORRECTION)

    recharge = _make_session(
        tmp_path, "F_recharge", status="DONE", charge_ah=6.93,
        bq_rows=[_bq_row(soc_percent=1.0), _bq_row(soc_percent=100.0)],
    )
    measurements[MatrixStepKind.RECHARGE] = extract_step_measurement(recharge, MatrixStepKind.RECHARGE)

    return measurements


class TestEvaluateValidationMatrix:
    def _tolerance(self, max_pct: float = 5.0) -> ValidationTolerance:
        return ValidationTolerance(soc_error_percent_max=max_pct, source_reference="TEST-SPEC-001")

    def test_missing_required_step_yields_incomplete(self, tmp_path):
        measurements = _make_full_valid_matrix(tmp_path)
        del measurements[MatrixStepKind.RECHARGE]
        report = evaluate_validation_matrix(measurements, self._tolerance())
        assert report.gate_status == GATE_INCOMPLETE
        assert "RECHARGE" in report.missing_steps

    def test_all_required_steps_within_tolerance_validates(self, tmp_path):
        measurements = _make_full_valid_matrix(tmp_path)
        report = evaluate_validation_matrix(measurements, self._tolerance(max_pct=10.0))
        assert report.gate_status == GATE_VALIDATED
        assert report.reference_usable_capacity_Ah == pytest.approx(7.0)
        assert report.failed_steps == []

    def test_one_step_outside_tolerance_fails_gate(self, tmp_path):
        measurements = _make_full_valid_matrix(tmp_path)
        report = evaluate_validation_matrix(measurements, self._tolerance(max_pct=0.5))
        assert report.gate_status == GATE_FAILED_TOLERANCE
        assert len(report.failed_steps) > 0

    def test_zero_capacity_from_complete_discharge_yields_incomplete(self, tmp_path):
        measurements = _make_full_valid_matrix(tmp_path)
        cd = measurements[MatrixStepKind.COMPLETE_DISCHARGE]
        cd.reference_removed_Ah = 0.0
        report = evaluate_validation_matrix(measurements, self._tolerance())
        assert report.gate_status == GATE_INCOMPLETE

    def test_missing_gauge_data_on_required_step_yields_incomplete(self, tmp_path):
        measurements = _make_full_valid_matrix(tmp_path)
        no_gauge_session = _make_session(tmp_path, "F_recharge_no_gauge", status="DONE", charge_ah=7.0)
        measurements[MatrixStepKind.RECHARGE] = extract_step_measurement(
            no_gauge_session, MatrixStepKind.RECHARGE
        )
        report = evaluate_validation_matrix(measurements, self._tolerance())
        assert report.gate_status == GATE_INCOMPLETE
        assert "RECHARGE" in report.missing_steps

    def test_standby_step_not_required_by_default(self, tmp_path):
        measurements = _make_full_valid_matrix(tmp_path)
        assert MatrixStepKind.STANDBY not in REQUIRED_STEP_KINDS
        report = evaluate_validation_matrix(measurements, self._tolerance(max_pct=10.0))
        assert report.gate_status == GATE_VALIDATED

    def test_report_written_to_disk_as_valid_json(self, tmp_path):
        measurements = _make_full_valid_matrix(tmp_path)
        report = evaluate_validation_matrix(measurements, self._tolerance(max_pct=10.0))
        out_dir = tmp_path / "report_out"
        path = write_validation_matrix_report(out_dir, report)
        assert path.name == "validation_matrix_report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["gate_status"] == GATE_VALIDATED
        assert "FULL_CHARGE" in data["steps"]
        assert data["tolerance"]["source_reference"] == "TEST-SPEC-001"
