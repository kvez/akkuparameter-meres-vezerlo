"""
golden_image_manifest_builder.py tesztek — WP-AK-13 (SUP03-ACCEPT-002 aggregáció).
"""
from __future__ import annotations

import pytest

from Prog.src.bq_calibration_statistics import CalibrationStatisticsResult, FieldStatistics
from Prog.src.golden_gg_export import (
    MATURITY_EXPERIMENTAL_PACKAGE_ONLY,
    MATURITY_OPTIMIZED_CANDIDATE,
    GoldenGgImportResult,
)
from Prog.src.golden_image_manifest import GoldenImageMaturity
from Prog.src.golden_image_manifest_builder import (
    MINIMUM_CALIBRATION_BOARD_SAMPLES,
    ChemIdPhaseResult,
    DualQmaxRaSourceUndocumentedError,
    FinalImageExportResult,
    build_golden_image_manifest,
)
from Prog.src.traditional_learning_controller import (
    LEARNING_RESULT_AWAITING,
    LEARNING_RESULT_VERIFIED,
    LearningResultSummary,
)
from Prog.src.validation_matrix import GATE_INCOMPLETE, GATE_VALIDATED, ValidationMatrixReport, ValidationTolerance


def _cal_result(accepted: int) -> CalibrationStatisticsResult:
    return CalibrationStatisticsResult(
        sample_count_total=accepted + 2, sample_count_accepted=accepted, sample_count_rejected=2,
        outlier_policy="TEST_POLICY", rejected_reasons={},
        cc_gain=FieldStatistics(n=accepted), cc_delta=FieldStatistics(n=accepted),
        cc_offset=FieldStatistics(n=accepted), board_offset=FieldStatistics(n=accepted),
        temp_offset=FieldStatistics(n=0),
    )


def _learning_result(result: str) -> LearningResultSummary:
    return LearningResultSummary(result=result)


def _gg_result(maturity: str) -> GoldenGgImportResult:
    return GoldenGgImportResult(
        gg_out_csv_path="gg_out.csv", gg_out_csv_hash="abc123",
        qmax=150, ra_table_mohm=(10.0, 11.0), ra0_charge_mohm=9.5,
        thermal_parameters={}, gpc_report_path=None, gpc_report_hash=None,
        warnings=(), bq34z110_compatibility_confirmed=(maturity == MATURITY_OPTIMIZED_CANDIDATE),
        compatibility_confirmation_evidence="TI ticket #1" if maturity == MATURITY_OPTIMIZED_CANDIDATE else "",
        maturity_status=maturity,
    )


def _validation_report(gate_status: str) -> ValidationMatrixReport:
    return ValidationMatrixReport(
        step_evaluations={}, reference_usable_capacity_Ah=7.0,
        tolerance=ValidationTolerance(soc_error_percent_max=5.0, source_reference="TEST-SPEC"),
        gate_status=gate_status,
    )


class TestNoInputsYieldChemidPending:
    def test_empty_manifest_is_chemid_pending(self):
        manifest = build_golden_image_manifest(image_id="IMG-001")
        assert manifest.maturity == GoldenImageMaturity.CHEMID_PENDING
        assert manifest.is_production_ready() is False


class TestChemIdPhase:
    def test_verified_chemid_sets_flag_and_source_session(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-002",
            chemid_result=ChemIdPhaseResult(chem_id=0x0800, verified=True, session_reference="session_chemid_001"),
        )
        assert manifest.chemid_selected_and_programmed is True
        assert manifest.chem_id == 0x0800
        assert manifest.source_sessions["chemid_session"] == "session_chemid_001"
        assert manifest.maturity == GoldenImageMaturity.CHEMID_SELECTED

    def test_unverified_chemid_does_not_set_flag_but_documents_limitation(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-003",
            chemid_result=ChemIdPhaseResult(chem_id=0x0800, verified=False, session_reference="s1"),
        )
        assert manifest.chemid_selected_and_programmed is False
        assert any("ChemID" in lim for lim in manifest.limitations)


class TestCalibrationPhase:
    def test_at_least_20_accepted_samples_valid(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-004",
            calibration_result=_cal_result(accepted=MINIMUM_CALIBRATION_BOARD_SAMPLES),
            calibration_session_reference="cal_session_1",
        )
        assert manifest.calibration_valid is True
        assert manifest.source_sessions["calibration_session"] == "cal_session_1"

    def test_below_20_accepted_samples_invalid_with_limitation(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-005",
            calibration_result=_cal_result(accepted=MINIMUM_CALIBRATION_BOARD_SAMPLES - 1),
        )
        assert manifest.calibration_valid is False
        assert any("Kalibrációs minta" in lim for lim in manifest.limitations)


class TestLearningPhase:
    def test_verified_sets_flag(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-006",
            learning_result=_learning_result(LEARNING_RESULT_VERIFIED),
            learning_session_reference="learn_session_1",
        )
        assert manifest.learning_verified_0x06 is True
        assert manifest.qmax_ra_optimized is True

    def test_awaiting_does_not_set_flag(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-007",
            learning_result=_learning_result(LEARNING_RESULT_AWAITING),
        )
        assert manifest.learning_verified_0x06 is False
        assert manifest.qmax_ra_optimized is False


class TestGoldenGgPhase:
    def test_optimized_candidate_sets_flag(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-008",
            golden_gg_result=_gg_result(MATURITY_OPTIMIZED_CANDIDATE),
        )
        assert manifest.golden_gg_optimized is True
        assert manifest.qmax_ra_optimized is True

    def test_experimental_package_only_does_not_set_flag(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-009",
            golden_gg_result=_gg_result(MATURITY_EXPERIMENTAL_PACKAGE_ONLY),
        )
        assert manifest.golden_gg_optimized is False


class TestDualRouteDocumentation:
    """SUP03-ACCEPT-002: 'ha mindket utvonal futott, a vegso parameterforras
    es felulirasok dokumentaltak' — kötelező explicit döntés."""

    def test_both_routes_successful_without_source_raises(self):
        with pytest.raises(DualQmaxRaSourceUndocumentedError):
            build_golden_image_manifest(
                image_id="IMG-010",
                learning_result=_learning_result(LEARNING_RESULT_VERIFIED),
                golden_gg_result=_gg_result(MATURITY_OPTIMIZED_CANDIDATE),
            )

    def test_both_routes_successful_with_source_documented(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-011",
            learning_result=_learning_result(LEARNING_RESULT_VERIFIED),
            golden_gg_result=_gg_result(MATURITY_OPTIMIZED_CANDIDATE),
            final_qmax_ra_source="TRADITIONAL_LEARNING",
        )
        assert manifest.source_sessions["final_qmax_ra_source"] == "TRADITIONAL_LEARNING"
        assert manifest.learning_verified_0x06 is True
        assert manifest.golden_gg_optimized is True

    def test_only_one_route_successful_does_not_require_source(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-012",
            learning_result=_learning_result(LEARNING_RESULT_VERIFIED),
            golden_gg_result=_gg_result(MATURITY_EXPERIMENTAL_PACKAGE_ONLY),
        )
        assert manifest.learning_verified_0x06 is True
        assert "final_qmax_ra_source" not in manifest.source_sessions


class TestValidationPhase:
    def test_validated_gate_sets_flag(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-013",
            validation_report=_validation_report(GATE_VALIDATED),
        )
        assert manifest.validation_matrix_passed is True
        assert manifest.validation_status == GATE_VALIDATED

    def test_incomplete_gate_does_not_set_flag(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-014",
            validation_report=_validation_report(GATE_INCOMPLETE),
        )
        assert manifest.validation_matrix_passed is False


class TestFinalImageExport:
    def test_final_image_result_sets_hashes_and_flag(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-015",
            final_image_result=FinalImageExportResult(
                image_file_hash="hash_img", data_flash_hash="hash_df",
                source_reference="production_session_1",
            ),
        )
        assert manifest.final_image_exported_and_hashed is True
        assert manifest.image_file_hash == "hash_img"
        assert manifest.data_flash_hash == "hash_df"
        assert manifest.source_sessions["final_image_export"] == "production_session_1"


class TestFullyProductionReady:
    def test_all_phases_complete_yields_production_ready(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-016", update_status=0x02, qmax=150,
            chemid_result=ChemIdPhaseResult(chem_id=0x0800, verified=True, session_reference="s_chemid"),
            calibration_result=_cal_result(accepted=25),
            calibration_session_reference="s_cal",
            learning_result=_learning_result(LEARNING_RESULT_VERIFIED),
            learning_session_reference="s_learn",
            validation_report=_validation_report(GATE_VALIDATED),
            validation_session_reference="s_valid",
            final_image_result=FinalImageExportResult(
                image_file_hash="h1", data_flash_hash="h2", source_reference="s_final",
            ),
            approved_by="Technikus X",
            approval_date="2026-08-26",
        )
        assert manifest.is_production_ready() is True
        assert manifest.maturity == GoldenImageMaturity.PRODUCTION_IMAGE_READY
        assert manifest.missing_phases_for_production_ready() == []

    def test_all_phases_complete_but_not_approved_is_not_production_ready(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Az
        approved_by/approval_date mezők explicit CLAUDE.md §11.6 "külön
        kézi igazolás" rögzítésére szolgálnak — mind a 6 automatikus
        fázis-jelző teljesülése ÖNMAGÁBAN NEM elég PRODUCTION_IMAGE_READY-
        hez, ha nincs emberi jóváhagyás rögzítve."""
        manifest = build_golden_image_manifest(
            image_id="IMG-016B", update_status=0x02, qmax=150,
            chemid_result=ChemIdPhaseResult(chem_id=0x0800, verified=True, session_reference="s_chemid"),
            calibration_result=_cal_result(accepted=25),
            calibration_session_reference="s_cal",
            learning_result=_learning_result(LEARNING_RESULT_VERIFIED),
            learning_session_reference="s_learn",
            validation_report=_validation_report(GATE_VALIDATED),
            validation_session_reference="s_valid",
            final_image_result=FinalImageExportResult(
                image_file_hash="h1", data_flash_hash="h2", source_reference="s_final",
            ),
            # approved_by/approval_date szándékosan kihagyva
        )
        assert manifest.is_production_ready() is False
        assert manifest.maturity != GoldenImageMaturity.PRODUCTION_IMAGE_READY
        assert "approved_by" in manifest.missing_phases_for_production_ready()
        assert "approval_date" in manifest.missing_phases_for_production_ready()

    def test_missing_one_phase_is_not_production_ready(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-017", update_status=0x02,
            chemid_result=ChemIdPhaseResult(chem_id=0x0800, verified=True, session_reference="s_chemid"),
            calibration_result=_cal_result(accepted=25),
            learning_result=_learning_result(LEARNING_RESULT_VERIFIED),
            validation_report=_validation_report(GATE_VALIDATED),
            # final_image_result hiányzik
        )
        assert manifest.is_production_ready() is False
        assert "final_image_exported_and_hashed" in manifest.missing_phases_for_production_ready()

    def test_wrong_update_status_blocks_production_ready(self):
        manifest = build_golden_image_manifest(
            image_id="IMG-018", update_status=0x00,
            chemid_result=ChemIdPhaseResult(chem_id=0x0800, verified=True, session_reference="s"),
            calibration_result=_cal_result(accepted=25),
            learning_result=_learning_result(LEARNING_RESULT_VERIFIED),
            validation_report=_validation_report(GATE_VALIDATED),
            final_image_result=FinalImageExportResult(
                image_file_hash="h1", data_flash_hash="h2", source_reference="s_final",
            ),
        )
        assert manifest.is_production_ready() is False
        assert any("update_status" in m for m in manifest.missing_phases_for_production_ready())
