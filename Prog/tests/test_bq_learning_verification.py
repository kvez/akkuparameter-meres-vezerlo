"""
SUP03-LEARN-007/010/TEST-003: learning verifikáció (0x05/0x06 logika) tesztek.
"""
from __future__ import annotations

import pytest

from Prog.src.bq_learning_verification import (
    LearningResult,
    LearningVerificationRecord,
    UPDATE_STATUS_FIRST_QMAX_UPDATE,
    UPDATE_STATUS_FULL_LEARNING,
    UPDATE_STATUS_GOLDEN_IMAGE,
    evaluate_learning_result,
)


def _full_valid_record(**overrides) -> LearningVerificationRecord:
    defaults = dict(
        ocvtaken=True,
        vok=True,
        qen=True,
        update_status=UPDATE_STATUS_FULL_LEARNING,
        qmax_before=6800.0,
        qmax_final=7150.0,
        design_capacity_mAh=7200.0,
        ra_before=[15.0] * 15,
        ra_final=[13.5, 13.2, 13.0, 12.8, 12.5, 12.2, 12.0, 11.8,
                  11.5, 11.2, 11.0, 10.8, 10.5, 10.2, 10.0],
        safety_fault_occurred=False,
    )
    defaults.update(overrides)
    return LearningVerificationRecord(**defaults)  # type: ignore[arg-type]


class TestUpdateStatus0x05Only:
    """SUP03-SRC-001/005: az első kisütés utáni 0x05 önmagában NEM elégséges
    a teljes learninghez — csak a részleges (Qmax) update jele."""

    def test_0x05_gives_awaiting_verification_not_verified(self):
        record = _full_valid_record(update_status=UPDATE_STATUS_FIRST_QMAX_UPDATE)
        assert evaluate_learning_result(record) == (
            LearningResult.PHYSICAL_CYCLE_COMPLETED_AWAITING_BQ_VERIFICATION
        )


class TestUpdateStatus0x06Full:
    def test_0x06_with_all_conditions_is_verified(self):
        record = _full_valid_record()
        assert evaluate_learning_result(record) == LearningResult.BQ_LEARNING_VERIFIED

    def test_0x06_without_ocvtaken_is_failed(self):
        record = _full_valid_record(ocvtaken=False)
        assert evaluate_learning_result(record) == LearningResult.BQ_LEARNING_FAILED

    def test_0x06_without_vok_is_failed(self):
        record = _full_valid_record(vok=False)
        assert evaluate_learning_result(record) == LearningResult.BQ_LEARNING_FAILED

    def test_0x06_without_qen_is_failed(self):
        record = _full_valid_record(qen=False)
        assert evaluate_learning_result(record) == LearningResult.BQ_LEARNING_FAILED

    def test_0x06_with_safety_fault_is_failed_even_if_otherwise_valid(self):
        record = _full_valid_record(safety_fault_occurred=True)
        assert evaluate_learning_result(record) == LearningResult.BQ_LEARNING_FAILED


class TestManualOverrideNotAccepted:
    """SUP03-LEARN-007: 'Tilos kezzel 0x06-ra irni, majd sikeres learningnek
    jelenteni' — ezt a modul architekturálisan azzal védi, hogy update_status
    csak a gauge-ről (vagy kézi checkpointon rögzített, ténylegesen leolvasott
    értékből) kerülhet be; None esetén sosem VERIFIED."""

    def test_missing_update_status_is_awaiting_not_verified(self):
        record = _full_valid_record(update_status=None)
        assert evaluate_learning_result(record) == (
            LearningResult.PHYSICAL_CYCLE_COMPLETED_AWAITING_BQ_VERIFICATION
        )

    def test_golden_image_status_0x02_is_not_verified_learning(self):
        """0x02 = golden image állapot (Qmax+Ra tanult, IT nincs engedélyezve)
        — ez NEM azonos a frissen lezajlott 0x06 learning-igazolással."""
        record = _full_valid_record(update_status=UPDATE_STATUS_GOLDEN_IMAGE)
        assert evaluate_learning_result(record) == (
            LearningResult.PHYSICAL_CYCLE_COMPLETED_AWAITING_BQ_VERIFICATION
        )


class TestQmaxPlausibility:
    def test_zero_qmax_not_plausible(self):
        record = _full_valid_record(qmax_final=0.0)
        assert record.qmax_plausible is False
        assert evaluate_learning_result(record) == LearningResult.BQ_LEARNING_FAILED

    def test_wildly_out_of_range_qmax_not_plausible(self):
        record = _full_valid_record(qmax_final=50000.0, design_capacity_mAh=7200.0)
        assert record.qmax_plausible is False

    def test_qmax_change_percent_computed(self):
        record = _full_valid_record(qmax_before=7000.0, qmax_final=7350.0)
        assert record.qmax_change_percent == pytest.approx(5.0)


class TestRaValidity:
    def test_negative_ra_value_invalid(self):
        ra = [10.0] * 14 + [-1.0]
        record = _full_valid_record(ra_final=ra)
        assert record.ra_valid is False
        assert evaluate_learning_result(record) == LearningResult.BQ_LEARNING_FAILED

    def test_wrong_length_ra_invalid(self):
        record = _full_valid_record(ra_final=[10.0] * 10)
        assert record.ra_valid is False

    def test_ra_update_detected_when_values_change(self):
        record = _full_valid_record()
        assert record.ra_update_detected is True

    def test_ra_update_not_detected_when_unchanged(self):
        same = [12.0] * 15
        record = _full_valid_record(ra_before=same, ra_final=list(same))
        assert record.ra_update_detected is False
