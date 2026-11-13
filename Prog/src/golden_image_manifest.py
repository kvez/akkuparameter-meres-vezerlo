"""
SUP03-DATA-003/004: Golden image maturity state és manifest.

SUP03-DATA-004: a GUI/report SOSEM használhatja a "100_PERCENT_COMPLETE"
állapotot egyszerű ciklusvégen — csak az itt felsorolt, dokumentált
maturity-értékek engedélyezettek.

**NEM BEKÖTÖTT — NOT_WIRED (review-kör 5, Architektúra A-5, DFAM-026
4. előfordulása)**: ezt a modult a `golden_image_manifest_builder.py`
importálja és tölti fel (maga is NOT_WIRED, l. ott) — élő GUI/TestRunner
hívóhely nincs, tehát ez a modul is tranzitívan árva. Ez a modul NEM lett
törölve — önmagában teljes és tesztelt adatmodell, jövőbeli GUI-bekötésre
vár.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class GoldenImageMaturity(Enum):
    CHEMID_PENDING = "CHEMID_PENDING"
    CHEMID_SELECTED = "CHEMID_SELECTED"
    CALIBRATION_PENDING = "CALIBRATION_PENDING"
    CALIBRATED = "CALIBRATED"
    LEARNING_PENDING = "LEARNING_PENDING"
    LEARNING_VERIFIED_0X06 = "LEARNING_VERIFIED_0X06"
    GOLDEN_GG_PENDING = "GOLDEN_GG_PENDING"
    GOLDEN_GG_OPTIMIZED = "GOLDEN_GG_OPTIMIZED"
    VALIDATION_PENDING = "VALIDATION_PENDING"
    VALIDATED_FOR_DEFINED_MATRIX = "VALIDATED_FOR_DEFINED_MATRIX"
    PRODUCTION_IMAGE_READY = "PRODUCTION_IMAGE_READY"


@dataclass
class GoldenImageManifest:
    """SUP03-DATA-002/003: a végső golden-image manifest — láncolt
    hivatkozásokkal minden forrás-fázisra, hash-ekkel, és explicit
    maturity állapottal (sosem "100%")."""

    image_id: str
    device: str = "BQ34Z110"
    firmware: Optional[str] = None
    chem_id: Optional[int] = None
    update_status: Optional[int] = None
    qmax: Optional[float] = None
    ra_hash: Optional[str] = None
    calibration_average_hash: Optional[str] = None
    voltage_calibration_policy: Optional[str] = None
    data_flash_hash: Optional[str] = None
    image_file_hash: Optional[str] = None
    source_sessions: dict[str, str] = field(default_factory=dict)
    validation_status: Optional[str] = None
    limitations: list[str] = field(default_factory=list)
    approved_by: Optional[str] = None
    approval_date: Optional[str] = None

    maturity: GoldenImageMaturity = GoldenImageMaturity.CHEMID_PENDING

    # Fázis-teljesítettségi jelzők — a maturity/production-ready döntés ebből
    # számolódik, nem szabadon állítható maturity mezőből (SUP03-DATA-004).
    chemid_selected_and_programmed: bool = False
    calibration_valid: bool = False
    learning_verified_0x06: bool = False
    golden_gg_optimized: bool = False
    validation_matrix_passed: bool = False
    final_image_exported_and_hashed: bool = False

    @property
    def qmax_ra_optimized(self) -> bool:
        """Legalább egy elfogadott Qmax/Ra útvonal (traditional learning VAGY
        Golden GG) teljesült (SUP03-ACCEPT-002)."""
        return self.learning_verified_0x06 or self.golden_gg_optimized

    def missing_phases_for_production_ready(self) -> list[str]:
        """SUP03-TEST-007: 'nincs ready hianyzo fazissal' — pontosan
        felsorolja, mi hiányzik még."""
        missing: list[str] = []
        if not self.chemid_selected_and_programmed:
            missing.append("chemid_selected_and_programmed")
        if not self.calibration_valid:
            missing.append("calibration_valid")
        if not self.qmax_ra_optimized:
            missing.append("qmax_ra_optimized (learning_verified_0x06 vagy golden_gg_optimized)")
        if not self.validation_matrix_passed:
            missing.append("validation_matrix_passed")
        if not self.final_image_exported_and_hashed:
            missing.append("final_image_exported_and_hashed")
        if self.update_status != 0x02:
            missing.append(f"update_status musz 0x02 legyen (jelenleg: {self.update_status!r})")
        # Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: az
        # approved_by/approval_date mezők explicit CLAUDE.md §11.6 "külön
        # kézi igazolás" rögzítésére szolgálnak, de korábban a
        # is_production_ready() sosem ellenőrizte őket — egy automatikusan
        # teljesült 6 fázis-jelző (Qmax/Ra/UpdateStatus stb.) önmagában
        # PRODUCTION_IMAGE_READY-nek minősült volna emberi jóváhagyás
        # ténye nélkül.
        if not self.approved_by or not self.approved_by.strip():
            missing.append("approved_by")
        if not self.approval_date or not self.approval_date.strip():
            missing.append("approval_date")
        return missing

    def is_production_ready(self) -> bool:
        return len(self.missing_phases_for_production_ready()) == 0

    def recompute_maturity(self) -> GoldenImageMaturity:
        """A maturity-t a fázis-jelzőkből számolja, nem külső beállításból —
        így nem lehet "hazudni" egy magasabb maturity-t (SUP03-DATA-004)."""
        if self.is_production_ready():
            self.maturity = GoldenImageMaturity.PRODUCTION_IMAGE_READY
        elif self.validation_matrix_passed:
            self.maturity = GoldenImageMaturity.VALIDATED_FOR_DEFINED_MATRIX
        elif self.golden_gg_optimized:
            self.maturity = GoldenImageMaturity.GOLDEN_GG_OPTIMIZED
        elif self.learning_verified_0x06:
            self.maturity = GoldenImageMaturity.LEARNING_VERIFIED_0X06
        elif self.calibration_valid:
            self.maturity = GoldenImageMaturity.CALIBRATED
        elif self.chemid_selected_and_programmed:
            self.maturity = GoldenImageMaturity.CHEMID_SELECTED
        else:
            self.maturity = GoldenImageMaturity.CHEMID_PENDING
        return self.maturity

    def to_dict(self) -> dict:
        return {
            "image_id": self.image_id,
            "device": self.device,
            "firmware": self.firmware,
            "ChemID": self.chem_id,
            "UpdateStatus": self.update_status,
            "Qmax": self.qmax,
            "Ra_hash": self.ra_hash,
            "calibration_average_hash": self.calibration_average_hash,
            "voltage_calibration_policy": self.voltage_calibration_policy,
            "data_flash_hash": self.data_flash_hash,
            "image_file_hash": self.image_file_hash,
            "source_sessions": self.source_sessions,
            "validation_status": self.validation_status,
            "limitations": self.limitations,
            "approved_by": self.approved_by,
            "approval_date": self.approval_date,
            "maturity": self.maturity.value,
        }
