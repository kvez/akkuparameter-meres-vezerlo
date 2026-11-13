"""
ti_reference_validation.py tesztek — WP-AK-14 (EV2400 / TI reference validation).
"""
from __future__ import annotations

import json

import pytest

from Prog.src.bq_diagnostics import BqDiagnosticsSnapshot, BqFieldReading
from Prog.src.ti_reference_validation import (
    GATE_FAIL,
    GATE_INCOMPLETE,
    GATE_PASS,
    REQUIRED_TI_REFERENCE_FIELDS,
    FieldComparisonResult,
    TiReferenceField,
    TiReferenceValidationDataError,
    evaluate_ti_reference_validation,
    is_automatic_programming_permitted,
    own_readings_from_bq_diagnostics,
    write_ti_reference_validation_record,
)


def _full_matching_readings() -> dict:
    return {f: f"value-{f.value}" for f in TiReferenceField}


class TestEvidenceGate:
    def test_empty_evidence_reference_raises(self):
        readings = _full_matching_readings()
        with pytest.raises(TiReferenceValidationDataError):
            evaluate_ti_reference_validation(
                readings, readings, reference_instrument="EV2400",
                evidence_reference="   ", operator="tech",
            )

    def test_empty_reference_instrument_raises(self):
        readings = _full_matching_readings()
        with pytest.raises(TiReferenceValidationDataError):
            evaluate_ti_reference_validation(
                readings, readings, reference_instrument="",
                evidence_reference="LOG-1", operator="tech",
            )


class TestGateStatus:
    def test_all_fields_present_and_matching_is_pass(self):
        readings = _full_matching_readings()
        record = evaluate_ti_reference_validation(
            readings, readings, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
        )
        assert record.gate_status == GATE_PASS
        assert record.missing_fields == []
        assert record.mismatched_fields == []
        assert len(record.field_results) == len(REQUIRED_TI_REFERENCE_FIELDS)
        for result in record.field_results.values():
            assert result.available is True
            assert result.match is True

    def test_missing_field_on_own_side_is_incomplete_not_pass(self):
        own = _full_matching_readings()
        del own[TiReferenceField.QMAX]
        reference = _full_matching_readings()
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
        )
        assert record.gate_status == GATE_INCOMPLETE
        assert record.missing_fields == [TiReferenceField.QMAX.value]
        qmax_result = record.field_results[TiReferenceField.QMAX]
        assert qmax_result.available is False
        assert qmax_result.match is None

    def test_missing_field_on_reference_side_is_incomplete(self):
        own = _full_matching_readings()
        reference = _full_matching_readings()
        del reference[TiReferenceField.RA]
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
        )
        assert record.gate_status == GATE_INCOMPLETE
        assert record.missing_fields == [TiReferenceField.RA.value]

    def test_mismatched_field_is_fail_not_pass(self):
        own = _full_matching_readings()
        reference = _full_matching_readings()
        reference[TiReferenceField.CHECKSUM] = "different-value"
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
        )
        assert record.gate_status == GATE_FAIL
        assert record.mismatched_fields == [TiReferenceField.CHECKSUM.value]
        assert record.field_results[TiReferenceField.CHECKSUM].match is False

    def test_missing_takes_priority_over_mismatch(self):
        # [gate-precedencia] ha VAN hiányzó mező, a gate INCOMPLETE marad,
        # még akkor is, ha egy MÁSIK, jelen levő mező eltér — egy hiányos
        # validáció sosem "csendben" FAIL-lé silányulhat, mert az elfedné,
        # hogy a validáció maga sem volt teljes.
        own = _full_matching_readings()
        del own[TiReferenceField.EXPORT_READBACK]
        reference = _full_matching_readings()
        reference[TiReferenceField.CHECKSUM] = "different-value"
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
        )
        assert record.gate_status == GATE_INCOMPLETE

    def test_required_fields_can_be_narrowed(self):
        own = {TiReferenceField.QMAX: 7000}
        reference = {TiReferenceField.QMAX: 7000}
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
            required_fields=frozenset({TiReferenceField.QMAX}),
        )
        assert record.gate_status == GATE_PASS
        assert len(record.field_results) == 1

    def test_default_exact_equality_fails_on_time_varying_measurement_drift(self):
        """[regresszió, Teljes kódbázis review 3. kör (2026-08-29),
        Metrológia/Adatintegritás P3 lelet] Alapból (field_comparators
        nélkül) a viselkedés VÁLTOZATLAN marad: egy STANDARD_COMMANDS mezőn
        akár egyetlen mV-os eltérés is GATE_FAIL-t ad — ez dokumentálja a
        DORMANT hibát, amit a `field_comparators` paraméter jövőbeli hívói
        oldhatnak fel tűréssel."""
        own = _full_matching_readings()
        reference = _full_matching_readings()
        own[TiReferenceField.STANDARD_COMMANDS] = {"voltage_mV": 12500}
        reference[TiReferenceField.STANDARD_COMMANDS] = {"voltage_mV": 12501}
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
        )
        assert record.gate_status == GATE_FAIL
        assert record.field_results[TiReferenceField.STANDARD_COMMANDS].match is False

    def test_field_comparators_allows_tolerance_for_time_varying_fields(self):
        """Ugyanaz a mérési eltérés, mint fent, de a hívó egy tűréssel
        összehasonlító függvényt ad meg a STANDARD_COMMANDS mezőhöz — ekkor
        a fizikailag lényegtelen 1 mV-os eltérés NEM okoz hamis FAIL-t,
        miközben a többi (identitás/checksum jellegű) mező továbbra is
        szigorú `==`-t kap."""
        own = _full_matching_readings()
        reference = _full_matching_readings()
        own[TiReferenceField.STANDARD_COMMANDS] = {"voltage_mV": 12500}
        reference[TiReferenceField.STANDARD_COMMANDS] = {"voltage_mV": 12501}

        def _voltage_within_tolerance(o, r):
            return abs(o["voltage_mV"] - r["voltage_mV"]) <= 5

        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
            field_comparators={TiReferenceField.STANDARD_COMMANDS: _voltage_within_tolerance},
        )
        assert record.gate_status == GATE_PASS
        assert record.field_results[TiReferenceField.STANDARD_COMMANDS].match is True
        # a nem érintett mezők (pl. CHECKSUM) továbbra is szigorú ==-t kapnak
        assert record.field_results[TiReferenceField.CHECKSUM].match is True

    def test_field_comparators_still_flags_real_mismatch(self):
        """A tűréssel összehasonlító comparator NEM nyelheti el a tényleges,
        a tűrésen kívül eső eltérést."""
        own = _full_matching_readings()
        reference = _full_matching_readings()
        own[TiReferenceField.STANDARD_COMMANDS] = {"voltage_mV": 12500}
        reference[TiReferenceField.STANDARD_COMMANDS] = {"voltage_mV": 13000}

        def _voltage_within_tolerance(o, r):
            return abs(o["voltage_mV"] - r["voltage_mV"]) <= 5

        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
            field_comparators={TiReferenceField.STANDARD_COMMANDS: _voltage_within_tolerance},
        )
        assert record.gate_status == GATE_FAIL
        assert record.field_results[TiReferenceField.STANDARD_COMMANDS].match is False


class TestAutomaticProgrammingGate:
    def test_none_record_is_not_permitted(self):
        assert is_automatic_programming_permitted(None) is False

    def test_incomplete_record_is_not_permitted(self):
        own = {TiReferenceField.QMAX: 7000}
        reference: dict = {}
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
            required_fields=frozenset({TiReferenceField.QMAX}),
        )
        assert is_automatic_programming_permitted(record) is False

    def test_narrowed_field_set_pass_is_not_permitted(self):
        # [safety-lelet] egy szűkített required_fields-szel (pl. csak QMAX)
        # GATE_PASS is elérhető, de ez NEM a terv teljes 9 kategóriás
        # listáját fedi le — a WP-AK-14 "Elv" gátja csak a TELJES lefedettségű
        # validációt fogadhatja el, különben egy részleges/gyors ellenőrzés
        # tévesen "kész" jelzést adna egy jövőbeli automatikus módnak.
        own = {TiReferenceField.QMAX: 7000}
        reference = {TiReferenceField.QMAX: 7000}
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
            required_fields=frozenset({TiReferenceField.QMAX}),
        )
        assert record.gate_status == GATE_PASS
        assert is_automatic_programming_permitted(record) is False

    def test_full_field_set_pass_is_permitted(self):
        readings = _full_matching_readings()
        record = evaluate_ti_reference_validation(
            readings, readings, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
        )
        assert is_automatic_programming_permitted(record) is True

    def test_fail_record_is_not_permitted(self):
        own = {TiReferenceField.QMAX: 7000}
        reference = {TiReferenceField.QMAX: 6990}
        record = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
            required_fields=frozenset({TiReferenceField.QMAX}),
        )
        assert is_automatic_programming_permitted(record) is False


class TestSerialization:
    def test_write_ti_reference_validation_record_round_trips_as_json(self, tmp_path):
        readings = _full_matching_readings()
        record = evaluate_ti_reference_validation(
            readings, readings, reference_instrument="EV2400",
            evidence_reference="LOG-1", operator="tech",
        )
        path = write_ti_reference_validation_record(tmp_path, record)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["gate_status"] == GATE_PASS
        assert data["reference_instrument"] == "EV2400"
        assert data["evidence_reference"] == "LOG-1"
        assert len(data["field_results"]) == len(REQUIRED_TI_REFERENCE_FIELDS)

    def test_field_comparison_result_to_dict(self):
        result = FieldComparisonResult(
            field=TiReferenceField.QMAX, own_value=7000, reference_value=7000,
            available=True, match=True,
        )
        assert result.to_dict() == {
            "field": "QMAX", "own_value": 7000, "reference_value": 7000,
            "available": True, "match": True, "note": "",
        }


def _valid_reading(value):
    return BqFieldReading(value=value, valid=True, error=None, timestamp_monotonic=0.0)


def _invalid_reading():
    return BqFieldReading(value=None, valid=False, error="simulated", timestamp_monotonic=0.0)


def _full_snapshot(**overrides) -> BqDiagnosticsSnapshot:
    defaults = dict(
        device_type=_valid_reading(0x0541),
        fw_version=_valid_reading(0x0311),
        hw_version=_valid_reading(0x0003),
        chem_id=_valid_reading(0x1202),
        control_status=_valid_reading(0x2001),
        voltage_mV=_valid_reading(12500),
        avg_current_mA=_valid_reading(-150),
        temperature_C=_valid_reading(23.5),
        soc_percent=_valid_reading(87),
        remaining_capacity_mAh=_valid_reading(6100),
        full_charge_capacity_mAh=_valid_reading(7000),
        flags=_valid_reading(0x0080),
        qmax=_valid_reading(7050),
        update_status=_valid_reading(0x06),
        ra_table_mohm=_valid_reading([20, 21, 22]),
        cycle_count=_valid_reading(12),
        dod0=_valid_reading(500),
        avg_i_last_run_mA=_valid_reading(-140),
        avg_p_last_run=_valid_reading(-1750),
    )
    defaults.update(overrides)
    return BqDiagnosticsSnapshot(**defaults)


class TestOwnReadingsFromBqDiagnostics:
    def test_full_valid_snapshot_covers_five_categories(self):
        snapshot = _full_snapshot()
        readings = own_readings_from_bq_diagnostics(snapshot)
        assert set(readings.keys()) == {
            TiReferenceField.DEVICE_IDENTITY,
            TiReferenceField.STANDARD_COMMANDS,
            TiReferenceField.QMAX,
            TiReferenceField.RA,
            TiReferenceField.SECURITY_STATE,
        }
        assert readings[TiReferenceField.QMAX] == 7050
        assert readings[TiReferenceField.RA] == [20, 21, 22]
        assert readings[TiReferenceField.DEVICE_IDENTITY] == {
            "device_type": 0x0541, "fw_version": 0x0311,
            "hw_version": 0x0003, "chem_id": 0x1202,
        }
        assert readings[TiReferenceField.SECURITY_STATE] == {"ss": True, "fas": False}

    def test_invalid_qmax_is_omitted_not_zero(self):
        snapshot = _full_snapshot(qmax=_invalid_reading())
        readings = own_readings_from_bq_diagnostics(snapshot)
        assert TiReferenceField.QMAX not in readings

    def test_one_invalid_device_identity_field_omits_whole_category(self):
        # [BqFieldReading kapu-elve] a device identity négy mezőből áll —
        # ha bármelyik invalid, a KATEGÓRIA egésze nem kerül be, nem csak a
        # hibás rész (nincs részleges, csendben hihető device identity).
        snapshot = _full_snapshot(hw_version=_invalid_reading())
        readings = own_readings_from_bq_diagnostics(snapshot)
        assert TiReferenceField.DEVICE_IDENTITY not in readings

    def test_data_flash_and_export_categories_never_fabricated(self):
        snapshot = _full_snapshot()
        readings = own_readings_from_bq_diagnostics(snapshot)
        assert TiReferenceField.DATA_FLASH_READ not in readings
        assert TiReferenceField.DATA_FLASH_WRITE not in readings
        assert TiReferenceField.CHECKSUM not in readings
        assert TiReferenceField.EXPORT_READBACK not in readings
