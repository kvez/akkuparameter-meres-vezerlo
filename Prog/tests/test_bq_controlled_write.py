"""
test_bq_controlled_write.py — WP-AK-08: Prog/src/bq_controlled_write.py
pipeline tesztek, FakePicoSerial-lel (valós Pico/BQ HIL nélkül, l.
Riportok/wp_ak_08_implementacios_zarojelentes.md).

A CONFIRM->IDENTIFY->VERIFY DEVICE/FW->BACKUP->HASH->WRITE->READBACK->
COMPARE->CHECKSUM->STATUS VERIFY->PASS/FAIL pipeline minden lépését külön
teszteli, beleértve minden FAIL-ágat — a modul saját elve szerint (l.
docstring "kettős ellenőrzés"), hogy egyetlen lépés se ugorható át csendben.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.bq_controlled_write import (
    ControlledBqWriteError,
    write_single_df_block_controlled,
)
from Prog.src.exceptions import InstrumentTimeoutError
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _StatefulDevice:
    """A FakePicoSerial "results" dict-jébe illeszthető callable-ok, amik
    egy in-memory DF-blokk-tartalmat tartanak fenn - így a write_data_flash_
    block hívás UTÁN a data_flash_block olvasás ténylegesen a frissen írt
    tartalmat adja vissza (realisztikusabb, mint egy statikus fixture)."""

    def __init__(self, initial: bytes):
        self.block = bytearray(initial)
        self.write_calls = 0
        self.next_readback_override: bytes | None = None

    def read(self, params):
        if self.next_readback_override is not None:
            data = self.next_readback_override
            self.next_readback_override = None
            return data.hex()
        return bytes(self.block).hex()

    def write(self, params):
        self.write_calls += 1
        self.block = bytearray(bytes.fromhex(params["data_hex"]))
        return {"status": "ok"}


def _make_transport(device: _StatefulDevice, **extra_results) -> tuple[PicoBQTransport, FakePicoSerial]:
    results = {
        "device_type": 0x0541,
        "firmware_version": 0x0311,
        "control_status": 0x2003,
        "data_flash_block": device.read,
        "write_data_flash_block": device.write,
    }
    results.update(extra_results)
    serial = FakePicoSerial(results=results)
    return PicoBQTransport(serial=serial), serial


class TestPassPath:
    def test_full_pipeline_passes_and_captures_all_evidence(self):
        initial = bytes(range(32))
        new_data = bytes(reversed(range(32)))
        device = _StatefulDevice(initial)
        transport, serial = _make_transport(device)

        result = write_single_df_block_controlled(
            transport, subclass=104, block=0, new_data32=new_data, confirm=True,
        )

        assert result.passed is True
        assert result.stage_reached == "PASS"
        assert result.fail_reason is None
        assert result.identify == {"device_type": 0x0541, "firmware_version": 0x0311}
        assert result.backup_bytes_hex == initial.hex()
        assert result.backup_sha256 == _sha256(initial)
        assert result.intended_data_hex == new_data.hex()
        assert result.intended_sha256 == _sha256(new_data)
        assert result.readback_bytes_hex == new_data.hex()
        assert result.readback_sha256 == _sha256(new_data)
        assert result.status_after_write == {"control_status": 0x2003}
        assert device.write_calls == 1

    def test_pipeline_call_order_backup_before_write_before_readback(self):
        device = _StatefulDevice(bytes(32))
        transport, serial = _make_transport(device)

        write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
        )

        methods = [r["method"] for r in serial.written_requests]
        backup_idx = methods.index("data_flash_block")
        write_idx = methods.index("write_data_flash_block")
        readback_idx = methods.index("data_flash_block", backup_idx + 1)
        status_idx = methods.index("control_status")
        assert backup_idx < write_idx < readback_idx < status_idx

    def test_expected_device_fw_matching_does_not_block(self):
        device = _StatefulDevice(bytes(32))
        transport, _ = _make_transport(device)
        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
            expected_device_type=0x0541, expected_fw_version=0x0311,
        )
        assert result.passed is True


class TestConfirmGate:
    def test_missing_confirm_raises_before_any_call(self):
        device = _StatefulDevice(bytes(32))
        transport, serial = _make_transport(device)
        with pytest.raises(ValueError, match="confirm=True"):
            write_single_df_block_controlled(
                transport, subclass=2, block=0, new_data32=bytes(32), confirm=False,
            )
        assert serial.written_requests == []

    def test_wrong_length_payload_raises_before_any_call(self):
        device = _StatefulDevice(bytes(32))
        transport, serial = _make_transport(device)
        with pytest.raises(ValueError, match=r"(?<![\w.+-])32 bytes\b"):
            write_single_df_block_controlled(
                transport, subclass=2, block=0, new_data32=bytes(31), confirm=True,
            )
        assert serial.written_requests == []


class TestVerifyDeviceFwGate:
    def test_device_type_mismatch_fails_before_write(self):
        device = _StatefulDevice(bytes(32))
        transport, serial = _make_transport(device)
        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
            expected_device_type=0x9999,
        )
        assert result.passed is False
        assert result.stage_reached == "VERIFY_DEVICE_FW"
        assert result.device_fw_mismatch == {
            "device_type": {"expected": 0x9999, "actual": 0x0541},
        }
        assert device.write_calls == 0
        assert "write_data_flash_block" not in [r["method"] for r in serial.written_requests]

    def test_fw_version_mismatch_fails_before_write(self):
        device = _StatefulDevice(bytes(32))
        transport, _ = _make_transport(device)
        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
            expected_fw_version=0x0099,
        )
        assert result.passed is False
        assert result.stage_reached == "VERIFY_DEVICE_FW"
        assert device.write_calls == 0


class TestEarlyTransportFailures:
    def test_identify_transport_error_raises_controlled_bq_write_error(self):
        serial = FakePicoSerial(errors={"device_type": ("METHOD_ERROR", "simulated I2C failure")})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(ControlledBqWriteError, match="IDENTIFY"):
            write_single_df_block_controlled(
                transport, subclass=2, block=0, new_data32=bytes(32), confirm=True,
            )

    def test_backup_read_transport_error_raises_controlled_bq_write_error(self):
        serial = FakePicoSerial(
            results={"device_type": 0x0541, "firmware_version": 0x0311},
            errors={"data_flash_block": ("METHOD_ERROR", "simulated I2C failure")},
        )
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(ControlledBqWriteError, match="BACKUP"):
            write_single_df_block_controlled(
                transport, subclass=2, block=0, new_data32=bytes(32), confirm=True,
            )


class TestWriteFailure:
    def test_pico_side_write_error_is_reported_as_write_stage_fail_not_exception(self):
        """A Pico-oldali bq_df_write() saját readback-verify-ja is elbukhat
        (l. TF1 repo pico_bq_service.py) - ez NEM ControlledBqWriteError-t
        dob, hanem egy FAIL eredményt "WRITE" stage-dzsel, mert a fizikai
        írás MEGTÖRTÉNT (a hívónak ezt külön kell látnia a "meg sem
        próbáltuk" IDENTIFY/BACKUP hibáktól)."""
        serial = FakePicoSerial(
            results={"device_type": 0x0541, "firmware_version": 0x0311, "data_flash_block": "00" * 32},
            errors={"write_data_flash_block": ("METHOD_ERROR", "bq_df_write readback-verify mismatch")},
        )
        transport = PicoBQTransport(serial=serial)
        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
        )
        assert result.passed is False
        assert result.stage_reached == "WRITE"
        assert "readback-verify mismatch" in result.fail_reason
        assert result.backup_bytes_hex == "00" * 32


class TestReadbackAndCompareFailures:
    def test_readback_transport_error_fails_at_readback_stage(self):
        # A device.write() sikeres marad, de a KOVETKEZO data_flash_block
        # (readback) hivas hibazzon - FakePicoSerial "errors" bejegyzese a
        # write hatasakent, egy dinamikusan atallitott callable-lel.
        device = _StatefulDevice(bytes(32))
        transport, serial = _make_transport(device)

        original_write = device.write

        def write_then_break_readback(params):
            response = original_write(params)
            serial.errors["data_flash_block"] = (
                "METHOD_ERROR", "simulated I2C failure after write",
            )
            return response

        serial.results["write_data_flash_block"] = write_then_break_readback

        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
        )
        assert result.passed is False
        assert result.stage_reached == "READBACK"

    def test_readback_mismatch_fails_at_compare_stage(self):
        """Szimulálja, hogy a Pico-oldali write sikeresnek jelentkezik (saját
        readback-verify-ja átment), de a PC-oldali független readback MÁS
        tartalmat lát (pl. valami megváltoztatta a blokkot a két olvasás
        között) - a modul "kettős ellenőrzés" elve pontosan ezt hivatott
        elkapni."""
        device = _StatefulDevice(bytes(32))
        transport, serial = _make_transport(device)

        original_write = device.write

        def write_then_corrupt(params):
            response = original_write(params)
            device.next_readback_override = bytes([0xEE] * 32)
            return response

        serial.results["write_data_flash_block"] = write_then_corrupt

        new_data = bytes(range(32))
        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=new_data, confirm=True,
        )
        assert result.passed is False
        assert result.stage_reached == "COMPARE"
        assert result.readback_bytes_hex == ("ee" * 32)
        assert result.intended_data_hex == new_data.hex()


class TestStatusVerifyFailure:
    def test_status_verify_transport_error_fails_at_status_verify_stage(self):
        device = _StatefulDevice(bytes(32))
        transport, serial = _make_transport(device)
        serial.errors["control_status"] = ("METHOD_ERROR", "simulated I2C failure")

        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
        )
        assert result.passed is False
        assert result.stage_reached == "STATUS_VERIFY"
        assert result.readback_bytes_hex is not None  # a COMPARE/CHECKSUM mar lezajlott


class _TimeoutOnMethodSerial:
    """[review finding, WP-AK-08] `FakePicoSerial.simulate_no_response` a
    TELJES példányra globális, nem tud egyetlen metódust szelektíven
    timeoutoltatni - ez a fake szándékosan CSAK a megadott metódusnál ad
    üres readline()-t (valós timeout szimulációja), minden mást normálisan
    kiszolgál egy `_StatefulDevice`-on keresztül."""

    def __init__(self, device, timeout_method: str):
        self.device = device
        self.timeout_method = timeout_method
        self.written_requests: list[dict] = []
        self.reset_input_buffer_calls = 0
        self._pending: bytes = b""

    def write(self, data: bytes) -> int:
        request = json.loads(data.decode("utf-8").strip())
        self.written_requests.append(request)
        method = request["method"]
        req_id = request["id"]
        if method == self.timeout_method:
            self._pending = b""  # nincs valasz - valodi timeout szimulacioja
            return len(data)
        if method == "device_type":
            result = 0x0541
        elif method == "firmware_version":
            result = 0x0311
        elif method == "data_flash_block":
            result = self.device.read(request.get("params", {}))
        elif method == "write_data_flash_block":
            result = self.device.write(request.get("params", {}))
        elif method == "control_status":
            result = 0x2003
        else:
            raise AssertionError(f"unexpected method in timeout test: {method}")
        self._pending = (json.dumps({"id": req_id, "ok": True, "result": result}) + "\n").encode()
        return len(data)

    def readline(self) -> bytes:
        return self._pending

    def reset_input_buffer(self) -> None:
        self.reset_input_buffer_calls += 1
        self._pending = b""


class TestTimeoutDuringPipeline:
    """[review finding, WP-AK-08] `InstrumentTimeoutError` (soros I/O
    timeout/nincs válasz) az `InstrumentError` ALATT testvérként áll a
    `PicoBQTransportError` mellett, NEM annak leszármazottja (l.
    Prog/src/exceptions.py) - egy `except PicoBQTransportError` egyedül NEM
    kapná el. Ez a legsúlyosabb eset épp a WRITE lépésnél (nem tudni,
    megtörtént-e a fizikai írás) - ezért minden pipeline-lépésnek explicit
    tesztje van timeout-ra, nem csak METHOD_ERROR-ra."""

    def test_timeout_during_identify_raises_controlled_bq_write_error_not_uncaught(self):
        device = _StatefulDevice(bytes(32))
        serial = _TimeoutOnMethodSerial(device, timeout_method="device_type")
        transport = PicoBQTransport(serial=serial, max_retries=1)
        with pytest.raises(ControlledBqWriteError, match="IDENTIFY") as exc_info:
            write_single_df_block_controlled(
                transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
            )
        assert isinstance(exc_info.value.__cause__, InstrumentTimeoutError)

    def test_timeout_during_backup_raises_controlled_bq_write_error_not_uncaught(self):
        device = _StatefulDevice(bytes(32))
        serial = _TimeoutOnMethodSerial(device, timeout_method="data_flash_block")
        transport = PicoBQTransport(serial=serial, max_retries=1)
        with pytest.raises(ControlledBqWriteError, match="BACKUP") as exc_info:
            write_single_df_block_controlled(
                transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
            )
        assert isinstance(exc_info.value.__cause__, InstrumentTimeoutError)

    def test_timeout_during_write_yields_structured_write_stage_fail_not_uncaught_exception(self):
        """A LEGKRITIKUSABB eset: ha a WRITE hívás időtúllépéssel jár, a
        hívó NEM kaphat nyers, elkapatlan kivételt - egy strukturált FAIL
        eredményt kell kapnia "WRITE" stage-dzsel, mert a fizikai írás
        állapota bizonytalan (talán megtörtént, talán nem)."""
        device = _StatefulDevice(bytes(32))
        serial = _TimeoutOnMethodSerial(device, timeout_method="write_data_flash_block")
        transport = PicoBQTransport(serial=serial, max_retries=1)

        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
        )
        assert result.passed is False
        assert result.stage_reached == "WRITE"
        assert isinstance(result.fail_reason, str)

    def test_timeout_during_readback_yields_structured_readback_stage_fail(self):
        device = _StatefulDevice(bytes(32))
        # az elso data_flash_block (BACKUP) hivas sikeres, csak a MASODIK
        # (READBACK) timeoutoljon - ehhez a device.write()-ot kovetoen
        # attesszuk a metodust timeout-ra egy write-utani hookkal.
        serial = _TimeoutOnMethodSerial(device, timeout_method="__never__")
        original_write = device.write

        def write_then_time_out_next_read(params):
            response = original_write(params)
            serial.timeout_method = "data_flash_block"
            return response

        device.write = write_then_time_out_next_read  # type: ignore[method-assign]
        transport = PicoBQTransport(serial=serial, max_retries=1)

        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
        )
        assert result.passed is False
        assert result.stage_reached == "READBACK"

    def test_timeout_during_status_verify_yields_structured_status_verify_stage_fail(self):
        device = _StatefulDevice(bytes(32))
        serial = _TimeoutOnMethodSerial(device, timeout_method="control_status")
        transport = PicoBQTransport(serial=serial, max_retries=1)

        result = write_single_df_block_controlled(
            transport, subclass=2, block=0, new_data32=bytes(range(32)), confirm=True,
        )
        assert result.passed is False
        assert result.stage_reached == "STATUS_VERIFY"
        # a COMPARE/CHECKSUM addig mar lezajlott es sikeres volt
        assert result.readback_bytes_hex == bytes(range(32)).hex()
