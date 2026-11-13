"""
SUP03-DATA-003/004/TEST-007: Golden image manifest + maturity tesztek.
"""
from __future__ import annotations

from Prog.src.golden_image_manifest import GoldenImageManifest, GoldenImageMaturity


def _make_manifest(**overrides) -> GoldenImageManifest:
    defaults: dict = {"image_id": "TEST-IMG-001"}
    defaults.update(overrides)
    return GoldenImageManifest(**defaults)


class TestMaturityProgression:
    def test_starts_at_chemid_pending(self):
        manifest = _make_manifest()
        assert manifest.recompute_maturity() == GoldenImageMaturity.CHEMID_PENDING

    def test_chemid_selected(self):
        manifest = _make_manifest(chemid_selected_and_programmed=True)
        assert manifest.recompute_maturity() == GoldenImageMaturity.CHEMID_SELECTED

    def test_calibrated(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True, calibration_valid=True
        )
        assert manifest.recompute_maturity() == GoldenImageMaturity.CALIBRATED

    def test_learning_verified(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            learning_verified_0x06=True,
        )
        assert manifest.recompute_maturity() == GoldenImageMaturity.LEARNING_VERIFIED_0X06

    def test_golden_gg_optimized_alternative_route(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            golden_gg_optimized=True,
        )
        assert manifest.recompute_maturity() == GoldenImageMaturity.GOLDEN_GG_OPTIMIZED

    def test_validated(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            learning_verified_0x06=True,
            validation_matrix_passed=True,
        )
        assert manifest.recompute_maturity() == GoldenImageMaturity.VALIDATED_FOR_DEFINED_MATRIX


class TestNoOneHundredPercentState:
    def test_maturity_enum_has_no_100_percent_value(self):
        values = {m.value for m in GoldenImageMaturity}
        assert "100_PERCENT_COMPLETE" not in values
        assert not any("100" in v for v in values)


class TestProductionReadyRequiresAllPhases:
    """SUP03-TEST-007: 'nincs ready hianyzo fazissal'."""

    def test_missing_all_phases_lists_all(self):
        manifest = _make_manifest()
        missing = manifest.missing_phases_for_production_ready()
        assert len(missing) >= 5
        assert manifest.is_production_ready() is False

    def test_missing_only_validation(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            learning_verified_0x06=True,
            final_image_exported_and_hashed=True,
            update_status=0x02,
        )
        missing = manifest.missing_phases_for_production_ready()
        assert any("validation_matrix_passed" in m for m in missing)
        assert manifest.is_production_ready() is False

    def test_wrong_update_status_blocks_ready_even_if_phases_done(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            learning_verified_0x06=True,
            validation_matrix_passed=True,
            final_image_exported_and_hashed=True,
            update_status=0x06,  # nem 0x02 — gyártási golden image állapot még nincs
        )
        assert manifest.is_production_ready() is False

    def test_all_phases_and_correct_update_status_is_ready(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            learning_verified_0x06=True,
            validation_matrix_passed=True,
            final_image_exported_and_hashed=True,
            update_status=0x02,
            approved_by="Technikus X",
            approval_date="2026-08-26",
        )
        assert manifest.is_production_ready() is True
        assert manifest.missing_phases_for_production_ready() == []
        assert manifest.recompute_maturity() == GoldenImageMaturity.PRODUCTION_IMAGE_READY

    def test_all_phases_done_but_not_approved_is_not_ready(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Az
        approved_by/approval_date CLAUDE.md §11.6 "külön kézi igazolás"
        rögzítésére szolgál — enélkül minden fázis kész ellenére sem
        PRODUCTION_IMAGE_READY."""
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            learning_verified_0x06=True,
            validation_matrix_passed=True,
            final_image_exported_and_hashed=True,
            update_status=0x02,
        )
        assert manifest.is_production_ready() is False
        missing = manifest.missing_phases_for_production_ready()
        assert "approved_by" in missing
        assert "approval_date" in missing

    def test_whitespace_only_approval_fields_not_accepted(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            learning_verified_0x06=True,
            validation_matrix_passed=True,
            final_image_exported_and_hashed=True,
            update_status=0x02,
            approved_by="   ",
            approval_date="   ",
        )
        assert manifest.is_production_ready() is False

    def test_neither_learning_nor_golden_gg_blocks_ready(self):
        manifest = _make_manifest(
            chemid_selected_and_programmed=True,
            calibration_valid=True,
            validation_matrix_passed=True,
            final_image_exported_and_hashed=True,
            update_status=0x02,
            # sem learning_verified_0x06, sem golden_gg_optimized
        )
        missing = manifest.missing_phases_for_production_ready()
        assert any("qmax_ra_optimized" in m for m in missing)


class TestToDict:
    def test_to_dict_contains_maturity_value(self):
        manifest = _make_manifest(chemid_selected_and_programmed=True)
        manifest.recompute_maturity()
        d = manifest.to_dict()
        assert d["maturity"] == "CHEMID_SELECTED"
