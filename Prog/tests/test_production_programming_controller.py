"""
production_programming_controller.py tesztek — WP-AK-13 (SUP03-PROD-001..006).

Ugyanaz a minta, mint a WP-AK-08/09/11 teszt-szuitjeiben: `PicoBQTransport` +
`FakePicoSerial`, in-memory állapottartó fake BQ-eszközzel.
"""
from __future__ import annotations

import pytest

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.production_programming_controller import (
    ImageBlockSpec,
    ProductionProgrammingController,
    ProductionState,
    write_production_record,
)
from Prog.src.ti_reference_validation import (
    GATE_PASS,
    TiReferenceField,
    evaluate_ti_reference_validation,
)
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial


class _FakeBqDevice:
    def __init__(self, device_type: int = 0x0541, fw_version: int = 0x0311) -> None:
        self.device_type = device_type
        self.fw_version = fw_version
        self.control_status_word = 0x0000
        self.data_flash: dict[tuple[int, int], bytes] = {}
        self.voltage_divider = 20000
        self.voltage_mV = 12500
        self.calls: list[str] = []
        self.data_flash_error = False
        self.full_access_error = False
        self.it_enable_error = False
        self.qen_after_it_enable = True
        self.corrupt_next_write = False
        self.data_flash_read_call_count = 0
        self.fail_data_flash_read_after: int | None = None

    def _trigger(self, name: str):
        def _handler(params):
            self.calls.append(name)
            if name == "it_enable":
                if self.it_enable_error:
                    raise RuntimeError("simulated it_enable failure")
                if self.qen_after_it_enable:
                    self.control_status_word |= 0x0001  # QEN bit0
            if name == "full_access" and self.full_access_error:
                raise RuntimeError("simulated full_access failure")
            return {"status": "ok"}
        return _handler

    def data_flash_block(self, params):
        if self.data_flash_error:
            raise RuntimeError("simulated data flash read failure")
        self.data_flash_read_call_count += 1
        if (
            self.fail_data_flash_read_after is not None
            and self.data_flash_read_call_count > self.fail_data_flash_read_after
        ):
            raise RuntimeError("simulated transient data flash read failure")
        key = (params["subclass"], params["block"])
        return self.data_flash.get(key, bytes(32)).hex()

    def write_data_flash_block(self, params):
        if self.data_flash_error:
            raise RuntimeError("simulated data flash write failure")
        key = (params["subclass"], params["block"])
        written = bytes(32) if self.corrupt_next_write else bytes.fromhex(params["data_hex"])
        self.data_flash[key] = written
        return {"status": "ok"}

    def read_voltage_divider(self, params):
        return self.voltage_divider

    def write_voltage_divider(self, params):
        self.voltage_divider = params["new_value_mV"]
        return {"status": "ok"}


def _make_transport(device: _FakeBqDevice) -> PicoBQTransport:
    results = {
        "device_type": lambda params: device.device_type,
        "firmware_version": lambda params: device.fw_version,
        "control_status": lambda params: device.control_status_word,
        "data_flash_block": device.data_flash_block,
        "write_data_flash_block": device.write_data_flash_block,
        "unseal": device._trigger("unseal"),
        "full_access": device._trigger("full_access"),
        "seal": device._trigger("seal"),
        "it_enable": device._trigger("it_enable"),
        "voltage_mV": lambda params: device.voltage_mV,
        "read_voltage_divider": device.read_voltage_divider,
        "write_voltage_divider": device.write_voltage_divider,
    }
    return PicoBQTransport(serial=FakePicoSerial(results=results))


def _one_block() -> list[ImageBlockSpec]:
    return [ImageBlockSpec(subclass=82, block=0, data32=bytes(range(32)))]


def _drive_to_image_programmed(ctrl: ProductionProgrammingController) -> None:
    ctrl.run_identify()
    ctrl.program_image(_one_block())


def _drive_to_pack_test(ctrl: ProductionProgrammingController) -> None:
    _drive_to_image_programmed(ctrl)
    vc = ctrl.begin_voltage_calibration()
    vc.submit_dmm_voltage_mV(12600)
    ctrl.complete_voltage_calibration(vc)
    ctrl.record_unique_data("SN-001", "LOT-42", "2026-08-22")
    ctrl.record_board_test_result(True)
    ctrl.confirm_battery_connected()
    ctrl.record_pack_test_result(True)


class TestSequenceEnforcement:
    def test_starts_in_init(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        assert ctrl.state == ProductionState.INIT

    def test_program_image_before_identify_raises(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        with pytest.raises(RuntimeError):
            ctrl.program_image(_one_block())

    def test_run_it_enable_before_pack_test_raises(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        with pytest.raises(RuntimeError):
            ctrl.run_it_enable()


class TestIdentify:
    def test_successful_identify_records_device_info(self):
        device = _FakeBqDevice(device_type=0x0541, fw_version=0x0311)
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        ctrl.run_identify()
        assert ctrl.state == ProductionState.IDENTIFY
        assert ctrl.record.device_id == 0x0541
        assert ctrl.record.firmware_version == 0x0311

    def test_device_type_mismatch_faults(self):
        device = _FakeBqDevice(device_type=0x0541)
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        ctrl.run_identify(expected_device_type=0x9999)
        assert ctrl.state == ProductionState.FAULT
        assert "IDENTIFY_MISMATCH" in ctrl.fault_reason


class TestImageProgram:
    def test_single_block_programmed_successfully(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        assert ctrl.state == ProductionState.IMAGE_PROGRAM
        assert len(ctrl.record.block_results) == 1
        assert ctrl.record.block_results[0].passed is True

    def test_data_flash_failure_faults_and_reseals(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        ctrl.run_identify()
        device.data_flash_error = True
        ctrl.program_image(_one_block())
        assert ctrl.state == ProductionState.FAULT
        assert "IMAGE_PROGRAM_FAILED" in ctrl.fault_reason
        assert "seal" in device.calls

    def test_readback_mismatch_faults(self):
        """[traceability] ha a ténylegesen írt adat eltér a szándékolttól
        (pl. silent corruption), a pipeline COMPARE lépésnél buktat — a
        program_image() ezt FAULT-ba viszi."""
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        ctrl.run_identify()
        device.corrupt_next_write = True
        ctrl.program_image(_one_block())
        assert ctrl.state == ProductionState.FAULT
        assert "IMAGE_PROGRAM_BLOCK_FAILED" in ctrl.fault_reason
        assert "COMPARE" in ctrl.fault_reason

    def test_full_access_failure_after_successful_unseal_still_reseals(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Szoftver/
        Architektúra P2 lelet] Ha unseal() sikeresen lezajlik, de az utána
        hívott full_access() hibázik, a chip fizikailag UNSEALED állapotban
        maradt volna — a fájl MINDEN MÁS hibaága best-effort reselel a
        _fail() előtt (l. test_data_flash_failure_faults_and_reseals), ez
        az egyetlen kivétel volt."""
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        ctrl.run_identify()
        device.full_access_error = True
        ctrl.program_image(_one_block())
        assert ctrl.state == ProductionState.FAULT
        assert "IMAGE_PROGRAM_UNSEAL_FAILED" in ctrl.fault_reason
        assert "unseal" in device.calls
        assert "seal" in device.calls

    def test_transient_read_failure_on_second_block_identifies_failing_block(self):
        """[review-lelet] ha a `write_single_df_block_controlled` egy
        KÉSŐBBI blokknál IDENTIFY/BACKUP-szinten (transport-hiba) kivétellel
        áll meg, a hibaüzenetnek tartalmaznia kell, MELYIK blokknál történt
        — ez WP-AK-08 egyblokkos hatókörében irreleváns volt, de több
        blokknál nyomon-követhetetlen lenne nélküle."""
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        ctrl.run_identify()
        blocks = [
            ImageBlockSpec(subclass=82, block=0, data32=bytes(range(32))),
            ImageBlockSpec(subclass=88, block=0, data32=bytes(reversed(range(32)))),
        ]
        # Az 1. blokk BACKUP+READBACK-ja (2 hívás) sikeres, a 2. blokk
        # BACKUP-ja (3. hívás) már hibázik.
        device.fail_data_flash_read_after = 2
        ctrl.program_image(blocks)
        assert ctrl.state == ProductionState.FAULT
        assert "IMAGE_PROGRAM_FAILED" in ctrl.fault_reason
        assert "subclass=88" in ctrl.fault_reason
        assert "block=0" in ctrl.fault_reason
        # Az 1. (sikeres) blokk eredménye megőrződött, a 2. sosem került be.
        assert len(ctrl.record.block_results) == 1
        assert ctrl.record.block_results[0].subclass == 82


class TestVoltageCalibration:
    def test_successful_voltage_calibration_records_snapshot(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        vc = ctrl.begin_voltage_calibration()
        vc.submit_dmm_voltage_mV(12600)
        ctrl.complete_voltage_calibration(vc)
        assert ctrl.state == ProductionState.VOLTAGE_CALIBRATION
        assert ctrl.record.voltage_calibration is not None
        assert ctrl.record.voltage_calibration.new_voltage_divider is not None

    def test_incomplete_voltage_calibration_faults(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        vc = ctrl.begin_voltage_calibration()
        # sosem hívjuk submit_dmm_voltage_mV-t - a vc AWAITING_DMM_VOLTAGE marad
        ctrl.complete_voltage_calibration(vc)
        assert ctrl.state == ProductionState.FAULT
        assert "VOLTAGE_CALIBRATION_NOT_DONE" in ctrl.fault_reason


class TestUniqueDataAndTests:
    def test_unique_data_requires_all_fields_non_empty(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        vc = ctrl.begin_voltage_calibration()
        vc.submit_dmm_voltage_mV(12600)
        ctrl.complete_voltage_calibration(vc)
        with pytest.raises(ValueError):
            ctrl.record_unique_data("", "LOT-42", "2026-08-22")

    def test_unique_data_never_written_to_bq_documented_as_limitation(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        vc = ctrl.begin_voltage_calibration()
        vc.submit_dmm_voltage_mV(12600)
        ctrl.complete_voltage_calibration(vc)
        ctrl.record_unique_data("SN-001", "LOT-42", "2026-08-22")
        assert ctrl.record.unique_data["serial_number"] == "SN-001"
        assert any("SUP03-PROD-003" in lim for lim in ctrl.record.limitations)

    def test_unique_data_limitation_does_not_claim_source_absence(self):
        # F4-2 (review-kor-4, gyari forras reviewer): a korabbi szoveg tevesen
        # "nincs sourced BQ34Z110 Data Flash cim/eljaras"-t allitott, holott
        # TI-SLUSB55B Table 20 (SC48/SC56) es TI-SLUA665 forrasolja - a valodi
        # ok PROJECT_POLICY, nem forraslany (l. CLAIM_LEDGER.yaml PROD-UNIQ-001).
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        vc = ctrl.begin_voltage_calibration()
        vc.submit_dmm_voltage_mV(12600)
        ctrl.complete_voltage_calibration(vc)
        ctrl.record_unique_data("SN-001", "LOT-42", "2026-08-22")
        limitation = next(
            lim for lim in ctrl.record.limitations if "SUP03-PROD-003" in lim
        )
        assert "nincs sourced" not in limitation
        assert "PROJECT_POLICY" in limitation
        assert "PROD-UNIQ-001" in limitation

    def test_board_test_failure_faults(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        vc = ctrl.begin_voltage_calibration()
        vc.submit_dmm_voltage_mV(12600)
        ctrl.complete_voltage_calibration(vc)
        ctrl.record_unique_data("SN-001", "LOT-42", "2026-08-22")
        ctrl.record_board_test_result(False, notes="visual defect")
        assert ctrl.state == ProductionState.FAULT
        assert "BOARD_TEST_FAILED" in ctrl.fault_reason

    def test_pack_test_failure_faults(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        vc = ctrl.begin_voltage_calibration()
        vc.submit_dmm_voltage_mV(12600)
        ctrl.complete_voltage_calibration(vc)
        ctrl.record_unique_data("SN-001", "LOT-42", "2026-08-22")
        ctrl.record_board_test_result(True)
        ctrl.confirm_battery_connected()
        ctrl.record_pack_test_result(False, notes="capacity out of spec")
        assert ctrl.state == ProductionState.FAULT
        assert "PACK_TEST_FAILED" in ctrl.fault_reason

    def test_battery_connect_before_board_test_raises(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_image_programmed(ctrl)
        vc = ctrl.begin_voltage_calibration()
        vc.submit_dmm_voltage_mV(12600)
        ctrl.complete_voltage_calibration(vc)
        ctrl.record_unique_data("SN-001", "LOT-42", "2026-08-22")
        with pytest.raises(RuntimeError):
            ctrl.confirm_battery_connected()


class TestItEnableVerifySealAndFinalize:
    def test_full_sequence_reaches_done(self, tmp_path):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_pack_test(ctrl)
        ctrl.run_it_enable()
        assert ctrl.state == ProductionState.IT_ENABLE
        ctrl.run_verify()
        assert ctrl.state == ProductionState.VERIFY
        assert ctrl.record.it_enable_result == "QEN_CONFIRMED"
        ctrl.run_seal()
        assert ctrl.state == ProductionState.SEAL
        assert ctrl.record.sealed_state is True
        record = ctrl.finalize()
        assert ctrl.state == ProductionState.DONE
        assert record is ctrl.record

        path = write_production_record(tmp_path, record)
        assert path.name == "production_record.json"
        assert path.exists()

    def test_it_enable_failure_faults_and_reseals(self):
        device = _FakeBqDevice()
        device.it_enable_error = True
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_pack_test(ctrl)
        ctrl.run_it_enable()
        assert ctrl.state == ProductionState.FAULT
        assert "IT_ENABLE_FAILED" in ctrl.fault_reason
        assert "seal" in device.calls

    def test_qen_not_confirmed_does_not_hard_fault_but_documents(self):
        """[WP-AK-08/10 mintáját követve] QEN hiánya limitáció, nem FAULT."""
        device = _FakeBqDevice()
        device.qen_after_it_enable = False
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_pack_test(ctrl)
        ctrl.run_it_enable()
        ctrl.run_verify()
        assert ctrl.state == ProductionState.VERIFY
        assert ctrl.record.it_enable_result == "QEN_NOT_CONFIRMED"
        assert any("QEN" in lim for lim in ctrl.record.limitations)

    def test_finalize_before_seal_raises(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        _drive_to_pack_test(ctrl)
        ctrl.run_it_enable()
        ctrl.run_verify()
        with pytest.raises(RuntimeError):
            ctrl.finalize()


class TestEmergencyAbort:
    def test_emergency_abort_seals_and_faults(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        ctrl.run_identify()
        ctrl.emergency_abort("OPERATOR_ABORT")
        assert ctrl.state == ProductionState.FAULT
        assert ctrl.fault_reason == "OPERATOR_ABORT"
        assert "seal" in device.calls


class TestTiReferenceValidation:
    """WP-AK-14: a TI referencia-validáció rögzítése nem sorrendhez kötött
    (l. modul docstring `record_ti_reference_validation()` felett) és
    `ProductionRecord.to_dict()`-be szerializálódik."""

    def test_record_ti_reference_validation_before_identify(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        own = {TiReferenceField.QMAX: 7000}
        reference = {TiReferenceField.QMAX: 7000}
        validation = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400", evidence_reference="LOG-1",
            operator="tech", required_fields=frozenset({TiReferenceField.QMAX}),
        )
        ctrl.record_ti_reference_validation(validation)
        assert ctrl.state == ProductionState.INIT
        assert ctrl.record.ti_reference_validation is validation
        assert validation.gate_status == GATE_PASS

    def test_ti_reference_validation_serializes_in_to_dict(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        ctrl.run_identify()
        own = {TiReferenceField.QMAX: 7000}
        reference = {TiReferenceField.QMAX: 6990}
        validation = evaluate_ti_reference_validation(
            own, reference, reference_instrument="EV2400", evidence_reference="LOG-2",
            operator="tech", required_fields=frozenset({TiReferenceField.QMAX}),
        )
        ctrl.record_ti_reference_validation(validation)
        record_dict = ctrl.record.to_dict()
        assert record_dict["ti_reference_validation"]["gate_status"] == "TI_REFERENCE_VALIDATED_MISMATCH"

    def test_to_dict_ti_reference_validation_none_by_default(self):
        device = _FakeBqDevice()
        ctrl = ProductionProgrammingController(_make_transport(device), image_version="v1.0")
        assert ctrl.record.to_dict()["ti_reference_validation"] is None
