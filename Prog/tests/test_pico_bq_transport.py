"""
PicoBQTransport unit tesztek — WP-AK-06. FakePicoSerial-lel, valós Pico/BQ
HIL nélkül (l. Riportok/wp_ak_06_implementacios_zarojelentes.md).
"""
from __future__ import annotations

import json

import pytest

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.drivers.pico_bq_transport import (
    CALIBRATION_WRITE_METHODS,
    LEARNING_SESSION_WRITE_METHODS,
    PROTOCOL_VERSION,
    PicoBQTransport,
    PicoBQTransportError,
)
from Prog.src.exceptions import InstrumentTimeoutError
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial


class TestConnectDisconnectInterface:
    """Teljes kódbázis review 2. kör (2026-08-28), Hardver/Műszervezérlés
    P2 lelet: CLAUDE.md §9 minden driverhez saját connect()/disconnect()/
    is_connected() metódust ír elő — korábban a soros port életciklus-
    kezelése kizárólag a GUI-rétegben (duplikáltan) történt."""

    def test_connect_opens_serial_via_module(self, monkeypatch):
        import serial  # type: ignore[import-untyped]
        fake = FakePicoSerial()
        calls: list = []
        monkeypatch.setattr(
            serial, "Serial",
            lambda port, baudrate, timeout: calls.append((port, baudrate, timeout)) or fake,
        )
        transport = PicoBQTransport()
        assert transport.is_connected() is False

        transport.connect("COM8", timeout=1.5)

        assert calls == [("COM8", 115200, 1.5)]
        assert transport.serial is fake
        assert transport.is_connected() is True

    def test_disconnect_closes_serial_and_clears_state(self):
        fake = FakePicoSerial()
        close_calls: list = []
        fake.close = lambda: close_calls.append(True)
        transport = PicoBQTransport(serial=fake)

        transport.disconnect()

        assert close_calls == [True]
        assert transport.serial is None
        assert transport.is_connected() is False

    def test_disconnect_noop_when_not_connected(self):
        transport = PicoBQTransport()
        transport.disconnect()  # nem dob kivételt

    def test_call_without_connect_raises_transport_error(self):
        """Ha a hívó connect() nélkül próbál RPC-t hívni, explicit,
        egyértelmű hibát kell kapnia — nem egy AttributeError-t a None
        soros-port objektumon."""
        transport = PicoBQTransport()
        with pytest.raises(PicoBQTransportError, match="nincs aktív soros kapcsolat"):
            transport.negotiate()


class TestNegotiate:
    def test_negotiate_accepts_matching_protocol_version(self):
        serial = FakePicoSerial()
        transport = PicoBQTransport(serial=serial)
        capabilities = transport.negotiate()
        assert capabilities["protocol_version"] == PROTOCOL_VERSION

    def test_negotiate_rejects_mismatched_protocol_version(self):
        serial = FakePicoSerial(protocol_version="9.9")
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="protocol_version"):
            transport.negotiate()

    def test_negotiate_sends_capabilities_method(self):
        serial = FakePicoSerial()
        transport = PicoBQTransport(serial=serial)
        transport.negotiate()
        assert serial.written_requests[0]["method"] == "capabilities"

    def test_negotiate_non_dict_capabilities_raises_transport_error(self):
        """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), Hardver/
        Műszervezérlés P1 lelet] a "capabilities" RPC-válasz típusa korábban
        validálatlan volt — egy nem-dict eredmény (csonka Pico-válasz)
        esetén a `.get("protocol_version")` bare AttributeError-t dobott
        volna, ami NEM PicoBQTransportError, tehát a hívóhelyek szűk
        `except PicoBQTransportError` ága nem kapta volna el."""
        serial = FakePicoSerial(results={"capabilities": None})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="nem dict"):
            transport.negotiate()

    def test_negotiate_default_required_write_methods_is_backward_compatible(self):
        """Kör 21 F1/ARC21-3(b), counter-teszt: a `required_write_methods`
        argumentum nélküli (üres halmaz alapértékű) hívás -- a MEGLÉVŐ,
        csak-olvasó hívók (pl. _BqConnectWorker) mintája -- MÉG egy
        read_only=True/write_methods=[] firmware ellen is sikeres marad."""
        serial = FakePicoSerial(
            results={
                "capabilities": {
                    "protocol_version": PROTOCOL_VERSION,
                    "methods": ["capabilities", "voltage_mV"],
                    "read_only": True,
                    "write_methods": [],
                }
            }
        )
        transport = PicoBQTransport(serial=serial)
        capabilities = transport.negotiate()
        assert capabilities["write_methods"] == []

    def test_negotiate_accepts_when_required_write_methods_present(self):
        serial = FakePicoSerial(
            results={
                "capabilities": {
                    "protocol_version": PROTOCOL_VERSION,
                    "methods": ["capabilities", "unseal", "seal"],
                    "read_only": False,
                    "write_methods": ["full_access", "seal", "unseal"],
                }
            }
        )
        transport = PicoBQTransport(serial=serial)
        capabilities = transport.negotiate(
            required_write_methods=frozenset({"unseal", "seal"})
        )
        assert capabilities["write_methods"] == ["full_access", "seal", "unseal"]

    def test_negotiate_rejects_when_required_write_methods_missing(self):
        """Kör 21 F1/ARC21-3(b), a fő diszkrimináló teszt: egy read-only
        (pl. WP-AK-06-kori) firmware UGYANAZT a protocol_version-t adja
        vissza, de a write_methods listája nem tartalmazza a hívó által
        igényelt primitíveket -- a kapcsolódás pillanatában, MIELŐTT
        bármilyen write megtörténne, kell buknia."""
        serial = FakePicoSerial(
            results={
                "capabilities": {
                    "protocol_version": PROTOCOL_VERSION,
                    "methods": ["capabilities", "voltage_mV"],
                    "read_only": True,
                    "write_methods": [],
                }
            }
        )
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="unseal"):
            transport.negotiate(
                required_write_methods=frozenset({"unseal", "seal"})
            )
        # A hívás ELŐTT egyetlen write-primitív sem futott -- csak a
        # "capabilities" kérés ment ki.
        assert [r["method"] for r in serial.written_requests] == ["capabilities"]

    def test_negotiate_rejects_when_write_methods_key_missing_entirely(self):
        """Egy régi (WP-AK-06-kori) firmware talán MÉG a `write_methods`
        kulcsot sem hordozza (csak a régi, fix `read_only: True`-t) --
        ez ne AttributeError-t/KeyError-t dobjon, hanem a MEGLÉVŐ
        PicoBQTransportError-t, ugyanazzal a hiányzó-lista logikával."""
        serial = FakePicoSerial(
            results={
                "capabilities": {
                    "protocol_version": PROTOCOL_VERSION,
                    "methods": ["capabilities", "voltage_mV"],
                    "read_only": True,
                }
            }
        )
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="unseal"):
            transport.negotiate(required_write_methods=frozenset({"unseal"}))


class TestNegotiateWriteMethodParity:
    """Kör 21 F1/ARC21-3(b) MAX-verifier NB-1/B2 lelet nyomán: a kliens
    LEARNING_SESSION_WRITE_METHODS/CALIBRATION_WRITE_METHODS konstansai a
    firmware `_WRITE_METHOD_NAMES`-jétől (Prog/pico_firmware/pico_bq_rpc.py)
    FÜGGETLENÜL karbantartott, kézzel írt listák -- e teszt nélkül egy
    jövőbeli firmware-oldali WP-AK-09 bővítés (új write-metódus) csendben
    szinkronizálatlanná válhatna a klienssel, és a `negotiate()` gate téves
    biztonságérzetet adna (a hiányzó metódus sosem kerülne bele a required
    halmazba, tehát sosem bukna el a kapcsolódáskor). A firmware modul
    importja PC-n technikailag lehetséges (csak `sys`/`json`-t importál
    modulszinten), FUTTATÁSRA viszont NEM PC-n szánt (l. modul docstring) --
    ez a teszt ezért KIZÁRÓLAG a `_WRITE_METHOD_NAMES` konstans halmazt
    olvassa ki, semmilyen RPC-dispatch/soros-I/O kódot nem hív."""

    def test_client_write_method_union_matches_firmware_write_method_names(self):
        import importlib.util
        from pathlib import Path

        firmware_path = (
            Path(__file__).resolve().parents[2]
            / "Prog" / "pico_firmware" / "pico_bq_rpc.py"
        )
        spec = importlib.util.spec_from_file_location(
            "pico_bq_rpc_firmware_parity_check", firmware_path
        )
        assert spec is not None and spec.loader is not None
        firmware_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(firmware_module)

        client_union = LEARNING_SESSION_WRITE_METHODS | CALIBRATION_WRITE_METHODS
        assert client_union == firmware_module._WRITE_METHOD_NAMES

    def test_calibration_write_methods_covers_unseal_full_access_seal(self):
        """Kör 21 F1/ARC21-3(b) MAX-verifier B3 lelet, diszkrimináló teszt: a
        régi `CALIBRATION_WRITE_METHODS` NEM tartalmazta unseal()/full_access()/
        seal()-t, holott ezek a valódi kalibrációs szekvencia első két és
        utolsó write-hívása (l. bq_gauge_calibration.py:439-445, :674 stb.) --
        e nélkül a `negotiate(required_write_methods=CALIBRATION_WRITE_METHODS)`
        gate egy unseal/full_access/seal-t nem exponáló firmware-t átengedett
        volna a kapcsolódáskor, és csak az első valódi write-nál bukott volna
        el, energizált chip mellett."""
        assert {"unseal", "full_access", "seal"} <= CALIBRATION_WRITE_METHODS


class TestReadWord:
    def test_maps_voltage_command_to_rpc_method(self):
        serial = FakePicoSerial(results={"voltage_mV": 12500})
        transport = PicoBQTransport(serial=serial)
        result = transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert result == 12500
        assert serial.written_requests[0]["method"] == "voltage_mV"

    def test_maps_average_current_command(self):
        serial = FakePicoSerial(results={"average_current_mA": -350})
        transport = PicoBQTransport(serial=serial)
        assert transport.read_standard_command_word(
            Bq34z110ReadOnlyDriver.STD_CMD_AVERAGE_CURRENT
        ) == -350

    def test_maps_control_status_subcommand(self):
        serial = FakePicoSerial(results={"control_status": 0x2003})
        transport = PicoBQTransport(serial=serial)
        assert transport.read_control_subcommand_word(
            Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS
        ) == 0x2003

    def test_unmapped_standard_command_raises_transport_error(self):
        serial = FakePicoSerial()
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="nincs RPC-metódus"):
            transport.read_standard_command_word(0x99)

    def test_unmapped_control_subcommand_raises_transport_error(self):
        serial = FakePicoSerial()
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="nincs RPC-metódus"):
            transport.read_control_subcommand_word(0x9999)

    def test_nan_result_raises_transport_error_not_silent_passthrough(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P1] egy
        Pico-oldali hiba (pl. 0-val osztás) JSON NaN-t adhat vissza — Python
        json.loads() ezt kivétel nélkül elfogadja (float('nan')). Korábban ez
        csendben átfolyt volna a hívóig (Bq34z110ReadOnlyDriver `int`-ként
        deklarálta, de nem kényszerítette), és a decode_flags()/
        decode_control_status() bitwise `&`-je egy el nem kapott TypeError-t
        dobott volna a NaN (float) szón — elszállítva a TELJES
        poll_bq_diagnostics()-t egyetlen rossz mező miatt."""
        serial = FakePicoSerial(results={"voltage_mV": float("nan")})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="érvénytelen"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

    def test_null_result_raises_transport_error_not_silent_passthrough(self):
        """Ugyanaz a védelem, mint a NaN esetén — egy `null` eredmény (nem
        implementált/hibázó Pico-metódus) sem folyhat át csendben."""
        serial = FakePicoSerial(results={"control_status": None})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="érvénytelen"):
            transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS)

    def test_bool_result_raises_transport_error(self):
        """Python bool az int alosztálya (isinstance(True, int) is True) —
        a validáció explicit kizárja, mert egy bool sosem lenne érvényes BQ
        regiszterérték."""
        serial = FakePicoSerial(results={"voltage_mV": True})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="érvénytelen"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

    def test_voltage_divider_none_result_raises_transport_error(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Hardver/
        Műszervezérlés P1 lelet] read_voltage_divider() korábban kimaradt a
        _validate_word_result() védelemből — egy malformed Pico-válasz
        (None) a downstream bq_gauge_calibration.py-ban egy el nem kapott
        TypeError-t dobott volna (round(None * ...)), MIELŐTT a _fail()
        meghívta volna az exit_cal()/seal() takarítást (BQ UNSEALED
        maradna dokumentálatlanul)."""
        serial = FakePicoSerial(results={"read_voltage_divider": None})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="érvénytelen"):
            transport.read_voltage_divider()

    def test_voltage_divider_nan_result_raises_transport_error(self):
        serial = FakePicoSerial(results={"read_voltage_divider": float("nan")})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="érvénytelen"):
            transport.read_voltage_divider()

    def test_voltage_divider_valid_result_passes_through(self):
        serial = FakePicoSerial(results={"read_voltage_divider": 32768})
        transport = PicoBQTransport(serial=serial)
        assert transport.read_voltage_divider() == 32768

    def test_calibration_data_non_dict_result_raises_transport_error(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Hardver/
        Műszervezérlés P1 lelet] read_calibration_data() korábban semmilyen
        validációt nem végzett — a downstream bq_gauge_calibration.py
        data["cc_offset"]/["cc_gain"] indexeléssel olvassa, ami egy nem-dict
        eredmény esetén (pl. None) el nem kapott TypeError-t dobott volna."""
        serial = FakePicoSerial(results={"read_calibration_data": None})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="nem dict"):
            transport.read_calibration_data()

    def test_calibration_data_nan_field_raises_transport_error(self):
        serial = FakePicoSerial(results={
            "read_calibration_data": {
                "cc_offset": 10, "board_offset": 5,
                "cc_gain": float("nan"), "cc_delta": 0.5,
            },
        })
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="cc_gain"):
            transport.read_calibration_data()

    def test_calibration_data_bool_field_raises_transport_error(self):
        serial = FakePicoSerial(results={
            "read_calibration_data": {"cc_offset": True, "board_offset": 5},
        })
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="cc_offset"):
            transport.read_calibration_data()

    def test_calibration_data_missing_field_raises_transport_error(self):
        """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), Hardver/
        Műszervezérlés P1 lelet] korábban egy TELJESEN HIÁNYZÓ mező (nem
        NaN/típushiba, hanem a kulcs hiánya — pl. egy csonka Pico-válasz)
        csendben átment ezen a validáción (`if key not in value: continue`),
        a dict változatlanul visszaadva. A downstream bq_gauge_calibration.py
        `data["cc_delta"]`-ja ilyenkor bare KeyError-t dob — ami NEM
        InstrumentError, tehát a _guarded() (kizárólag InstrumentError-ra
        szűkített except-ág) NEM kapja el, a _fail()->exit_cal()/seal()
        takarítás sosem fut le, a chip UNSEALED+FULL_ACCESS állapotban
        maradhat."""
        serial = FakePicoSerial(results={
            "read_calibration_data": {
                "cc_offset": 10, "board_offset": 5, "cc_gain": 1.002,
                # cc_delta hiányzik
            },
        })
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="cc_delta"):
            transport.read_calibration_data()

    def test_calibration_data_valid_result_passes_through(self):
        serial = FakePicoSerial(results={
            "read_calibration_data": {
                "cc_offset": 10, "board_offset": 5,
                "cc_gain": 1.002, "cc_delta": 0.5,
            },
        })
        transport = PicoBQTransport(serial=serial)
        data = transport.read_calibration_data()
        assert data["cc_offset"] == 10
        assert data["cc_gain"] == pytest.approx(1.002)

    def test_standard_command_and_control_subcommand_do_not_collide(self):
        """WP-AK-06 review-lelet regressziós tesztje: STD_CMD_VOLTAGE(0x08)
        és MAC_CHEM_ID(0x0008) ugyanaz a Python int (8) — a két, külön
        metóduson keresztüli feloldásnak EGYMÁSTÓL FÜGGETLENÜL kell
        működnie."""
        serial = FakePicoSerial(results={"voltage_mV": 12500, "chem_id": 0x0800})
        transport = PicoBQTransport(serial=serial)
        assert transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE) == 12500
        assert transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_CHEM_ID) == 0x0800

    def test_maps_cycle_count_and_dod0_commands(self):
        # WP-AK-10 (SUP03-LEARN-002 gauge log)
        serial = FakePicoSerial(results={"cycle_count": 5, "dod0": 100})
        transport = PicoBQTransport(serial=serial)
        assert transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_CYCLE_COUNT) == 5
        assert transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_DOD0) == 100

    def test_maps_device_type_fw_hw_version_subcommands(self):
        """WP-AK-07: BQ Diagnostics GUI "BQ device" mezőihez — a TF1 repo
        pico_bq_rpc.py-ban granularis RPC-metódusokként (nem a kombinált
        "identify" válaszon keresztül) exponált."""
        serial = FakePicoSerial(results={
            "device_type": 0x0541, "firmware_version": 0x0311, "hardware_version": 0x0003,
        })
        transport = PicoBQTransport(serial=serial)
        assert transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_DEVICE_TYPE) == 0x0541
        assert transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_FW_VERSION) == 0x0311
        assert transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_HW_VERSION) == 0x0003


class TestReadDataFlashBlock:
    def test_decodes_hex_response_to_bytes(self):
        serial = FakePicoSerial(results={"data_flash_block": "5200" + "00" * 30})
        transport = PicoBQTransport(serial=serial)
        result = transport.read_data_flash_block(82, 0)
        assert result == bytes([0x52, 0x00]) + bytes(30)

    def test_sends_subclass_and_block_params(self):
        serial = FakePicoSerial(results={"data_flash_block": "00" * 32})
        transport = PicoBQTransport(serial=serial)
        transport.read_data_flash_block(88, 1)
        params = serial.written_requests[0]["params"]
        assert params == {"subclass": 88, "block": 1}

    def test_invalid_hex_response_raises_transport_error(self):
        serial = FakePicoSerial(results={"data_flash_block": "not-hex"})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="hex"):
            transport.read_data_flash_block(82, 0)

    def test_wrong_length_response_raises_transport_error(self):
        """Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-06 lelet:
        a korábbi kód a modul 116. sora körüli komment ÁLLÍTÁSA ellenére NEM
        ellenőrizte a válasz BÁJT-hosszát -- `bytes.fromhex()` egy 31 vagy 33
        bájtos, egyébként érvényes hex-stringet is csendben átengedett volna,
        holott a BQ34Z110 Data Flash OLVASÁSA (SLUSB55B rev B, ACCESSING
        DATA FLASH, p.13-14: "Reading and writing subclass data are block
        operations 32 bytes in length") mindig a teljes 32 bájtot adja
        vissza -- a rövidebb blokk lehetősége ugyanott KIZÁRÓLAG az ÍRÁS
        irányra vonatkozik, ahogy `write_data_flash_block()` az írás
        oldalán ezt már kikényszerítette."""
        serial = FakePicoSerial(results={"data_flash_block": "00" * 31})  # 31 bájt, nem 32
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match=r"(?<![\w.+-])32(?![\w.])"):
            transport.read_data_flash_block(82, 0)


class TestReadRawBlock:
    """2026-09-12, CALMODE-anomália kivizsgálása (l. CLAIM_LEDGER.yaml
    BQ-CTRLSTATUS-CALMODE-001) - csak-olvasó, tetszőleges címtartomány
    olvasása, ugyanaz a hex-dekódolási/hosszellenőrzési fegyelem, mint
    read_data_flash_block()-nál, de a hívó által megadott (nem fix 32)
    hosszra."""

    def test_decodes_hex_response_to_bytes(self):
        serial = FakePicoSerial(results={"read_raw_block": "0102030405"})
        transport = PicoBQTransport(serial=serial)
        result = transport.read_raw_block(0x79, 5)
        assert result == bytes([1, 2, 3, 4, 5])

    def test_sends_addr_and_length_params(self):
        serial = FakePicoSerial(results={"read_raw_block": "00" * 5})
        transport = PicoBQTransport(serial=serial)
        transport.read_raw_block(0x79, 5)
        params = serial.written_requests[0]["params"]
        assert params == {"addr": 0x79, "length": 5}

    def test_invalid_hex_response_raises_transport_error(self):
        serial = FakePicoSerial(results={"read_raw_block": "not-hex"})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="hex"):
            transport.read_raw_block(0x79, 5)

    def test_wrong_length_response_raises_transport_error(self):
        serial = FakePicoSerial(results={"read_raw_block": "00" * 4})  # 4 bájt, nem 5
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match=r"(?<![\w.+-])5(?![\w.])"):
            transport.read_raw_block(0x79, 5)


class TestErrorPropagation:
    def test_pico_error_response_raises_transport_error_with_code(self):
        serial = FakePicoSerial(errors={"voltage_mV": ("METHOD_ERROR", "simulated I2C failure")})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="METHOD_ERROR"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

    def test_unknown_method_error_from_pico_propagates(self):
        serial = FakePicoSerial()  # nincs "voltage_mV" a results/errors-ban -> UNKNOWN_METHOD
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="UNKNOWN_METHOD"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

    def test_non_dict_error_field_raises_transport_error_not_attributeerror(self):
        """[regresszió, Teljes kódbázis review 12. kör (2026-08-31),
        Hardver/Műszervezérlés P1 lelet] ha a Pico egy ok:false válaszban
        NEM dict "error" mezőt küld (pl. sima string), a _call() ne bare
        AttributeError-t dobjon (error.get('code') egy stringen), hanem
        strukturált PicoBQTransportError-t."""
        serial = FakePicoSerial(errors_raw={"voltage_mV": "i2c timeout"})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="Pico RPC hiba"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)


class TestKor20BatchE15MissingOkKeyIsRetriedNotRaisedImmediately:
    """Kör 20 Batch E15 (HW20-08): egy hiányzó "ok" kulcs NEM egyenértékű egy
    explicit ok=false-szal -- ez az az állapot, amikor a Pico raw REPL-ben
    van és NINCS RPC szerver futva -- a LEGINKÁBB retry-olható eset, korábban
    viszont AZONNAL raise-elt (nincs continue/retry), mint egy valós,
    feldolgozott RPC hiba."""

    def test_missing_ok_key_recovers_on_retry(self):
        """Az első válaszból hiányzik az "ok" kulcs, a MÁSODIK (retry)
        válasz normál ok:true -- a hívó SIKERES eredményt kap. A régi kódon
        ez a teszt BUKNA: az első válasz "ok" hiánya azonnal PicoBQTransport-
        Error-t dobott volna, retry/continue nélkül, tehát a serial sosem
        kapott volna második írást."""
        serial = FakePicoSerial(results={"voltage_mV": 12500})
        call_count = {"n": 0}
        original_build_response = serial._build_response

        def flaky_build_response(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                serial.simulate_missing_ok_key = True
            else:
                serial.simulate_missing_ok_key = False
            return original_build_response(request)

        serial._build_response = flaky_build_response  # type: ignore[method-assign]
        transport = PicoBQTransport(serial=serial, max_retries=2)
        result = transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert result == 12500
        assert len(serial.written_requests) == 2

    def test_missing_ok_key_retries_then_raises_distinct_message(self):
        """Ha MINDEN próbálkozás "ok" kulcs nélküli választ ad, a végső
        kivétel a hiányzó-kulcs szöveget hordozza (NEM a "Pico RPC hiba"
        szöveget, ami az explicit ok:false ágra jellemző), ÉS a serial
        tényleg max_retries-szer kapott írást (bizonyítva, hogy retry-olt,
        nem csak egyszer bukott)."""
        serial = FakePicoSerial(results={"voltage_mV": 12500}, simulate_missing_ok_key=True)
        transport = PicoBQTransport(serial=serial, max_retries=3)
        with pytest.raises(PicoBQTransportError, match="hiányzó 'ok' mező"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert len(serial.written_requests) == 3

    def test_explicit_ok_false_still_raises_immediately_without_retry(self):
        """Counter-teszt: egy VALÓDI, feldolgozott RPC hiba (explicit
        ok:false + error dict) TOVÁBBRA IS azonnal raise-el, retry nélkül --
        a HW20-08 fix nem tágította a retry-olható esetek körét ide."""
        serial = FakePicoSerial(errors={"voltage_mV": ("METHOD_ERROR", "simulated I2C failure")})
        transport = PicoBQTransport(serial=serial, max_retries=3)
        with pytest.raises(PicoBQTransportError, match="METHOD_ERROR"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert len(serial.written_requests) == 1


class TestFrameSyncSafety:
    """WP-AK-06 review-szempont: keret-szinkron-vesztés (id-eltérés) SOSEM
    fogadhat el csendben egy másik kéréshez tartozó választ."""

    def test_response_id_mismatch_retries_then_raises(self):
        serial = FakePicoSerial(results={"voltage_mV": 12500}, force_response_id=9999)
        transport = PicoBQTransport(serial=serial, max_retries=2)
        with pytest.raises(PicoBQTransportError, match="id-eltérés"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert len(serial.written_requests) == 2  # max_retries szerint próbálkozott

    def test_no_response_raises_timeout_after_retries(self):
        serial = FakePicoSerial(simulate_no_response=True)
        transport = PicoBQTransport(serial=serial, max_retries=2)
        with pytest.raises(InstrumentTimeoutError):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert len(serial.written_requests) == 2

    def test_garbage_response_raises_transport_error_after_retries(self):
        serial = FakePicoSerial(simulate_garbage_response=True)
        transport = PicoBQTransport(serial=serial, max_retries=2)
        with pytest.raises(PicoBQTransportError, match="JSON"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

    def test_write_failure_raises_timeout_after_retries(self):
        serial = FakePicoSerial(simulate_write_failure=True)
        transport = PicoBQTransport(serial=serial, max_retries=2)
        with pytest.raises(InstrumentTimeoutError):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

    def test_transient_failure_recovers_on_retry(self):
        """Az elso hivas 'elveszik' (nincs valasz), a masodik sikeres -
        max_retries=2 mellett a hivo NEM lat hibat."""
        serial = FakePicoSerial(results={"voltage_mV": 12500})
        call_count = {"n": 0}
        original_readline = serial.readline

        def flaky_readline():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return b""
            return original_readline()

        serial.readline = flaky_readline  # type: ignore[method-assign]
        transport = PicoBQTransport(serial=serial, max_retries=2)
        result = transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert result == 12500

    def test_reset_input_buffer_called_before_every_write_attempt(self):
        """[Important finding, 2. review-kör] minden ÚJ kérés írása előtt
        eldobjuk a pufferben esetlegesen ottragadt, korábbi válaszokat —
        enélkül egy id-eltérés utáni retry "egy lépéssel lemaradva"
        sodródhatna, soha nem érve utol a friss választ."""
        serial = FakePicoSerial(results={"voltage_mV": 12500})
        transport = PicoBQTransport(serial=serial)
        transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert serial.reset_input_buffer_calls >= 1

    def test_stale_buffered_response_does_not_leak_into_next_call(self):
        """[Important finding, 2. review-kör] regressziós teszt a "egy
        lépéssel lemaradt" keret-szinkron-vesztésre: egy KORÁBBI (elfelejtett)
        hívásból a pufferben maradt válasz NE szennyezze be a KÖVETKEZŐ,
        egyébként hibátlan hívást. `FakePicoSerial` nem tudja ezt modellezni
        (egyetlen, write()-onként felülírt "pending" foglalata van — l.
        review-lelet), ezért egy dedikált, valódi FIFO-t tartó fake soros
        port szükséges hozzá."""

        class _FifoSerial:
            def __init__(self):
                self.queue: list[bytes] = []
                self.reset_calls = 0

            def inject_stale(self, response_id: int) -> None:
                self.queue.append(
                    (json.dumps({"id": response_id, "ok": True, "result": -999}) + "\n").encode()
                )

            def write(self, data: bytes) -> int:
                request = json.loads(data.decode("utf-8").strip())
                self.queue.append(
                    (json.dumps({"id": request["id"], "ok": True, "result": 12500}) + "\n").encode()
                )
                return len(data)

            def readline(self) -> bytes:
                return self.queue.pop(0) if self.queue else b""

            def reset_input_buffer(self) -> None:
                self.reset_calls += 1
                self.queue.clear()

        serial = _FifoSerial()
        # Egy korábbi (más) hívásból "ottragadt" válasz, MIELŐTT ez a hívás
        # egyáltalán elindulna.
        serial.inject_stale(response_id=9999)

        transport = PicoBQTransport(serial=serial, max_retries=3)
        result = transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

        assert result == 12500  # NEM a stale -999 érték
        assert serial.reset_calls >= 1


class TestReconnectOnIoFailure:
    """HIL-D5 (2026-09-13, Folyamatok/tervek/HIL_TESZTTERV_2026-09-11_24V_
    7200mAh.md 7. szakasz, CLAIM_LEDGER.yaml PICO-RECONNECT-001): valós
    COM10 egységen igazolva, hogy egy fizikai USB-kábel-kiesés után a MÁR
    MEGNYITOTT pyserial.Serial handle sosem állt helyre önmagától, még a
    fizikai eszköz visszacsatlakozása UTÁN sem — minden további hívás
    azonnali OS-szintű kivétellel hibázott. `_reopen_serial()` ezt javítja:
    soros I/O kivétel után megpróbálja UGYANAZZAL a port/baudrate/timeout
    paraméterezéssel újranyitni a portot."""

    def _patch_serial_factory(self, monkeypatch, fakes: list):
        import serial
        calls: list = []
        it = iter(fakes)

        def _factory(port, baudrate, timeout):
            calls.append((port, baudrate, timeout))
            return next(it)

        monkeypatch.setattr(serial, "Serial", _factory)
        return calls

    def test_io_exception_triggers_reopen_with_same_port_params(self, monkeypatch):
        broken = FakePicoSerial(simulate_write_failure=True)
        self._patch_serial_factory(monkeypatch, [broken])
        transport = PicoBQTransport(max_retries=1)
        transport.connect("COM10", timeout=1.0)
        assert transport.serial is broken

        reopened = FakePicoSerial(results={"voltage_mV": 12500})
        calls2 = self._patch_serial_factory(monkeypatch, [reopened])

        with pytest.raises(InstrumentTimeoutError):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

        assert calls2 == [("COM10", 115200, 1.0)]  # ugyanaz a port/baudrate/timeout
        assert transport.serial is reopened  # a törött handle-t lecseréltük

    def test_reconnect_allows_retry_to_succeed_within_same_call(self, monkeypatch):
        """max_retries=2: az 1. próbálkozás a törött handle-lel hibázik,
        a reopen UTÁN a 2. próbálkozás a FRISSEN megnyitott, működő
        handle-lel már sikeres — a hívó NEM lát hibát."""
        broken = FakePicoSerial(simulate_write_failure=True)
        working = FakePicoSerial(results={"voltage_mV": 12500})
        self._patch_serial_factory(monkeypatch, [broken])
        transport = PicoBQTransport(max_retries=2)
        transport.connect("COM10", timeout=1.0)

        self._patch_serial_factory(monkeypatch, [working])
        result = transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

        assert result == 12500
        assert transport.serial is working

    def test_reconnect_allows_subsequent_independent_call_to_succeed(self, monkeypatch):
        """A production throttled-poll mintája: max_retries=1, tehát az
        ELSŐ poll, ami a kiesést észleli, HIBÁT ad (nem rejtjük el) — DE a
        reopen a háttérben megtörténik, és egy KÉSŐBBI, FÜGGETLEN _call()
        (a következő periodikus poll) már a frissen megnyitott handle-lel
        sikeres, anélkül hogy a session-t újraindítanánk."""
        broken = FakePicoSerial(simulate_write_failure=True)
        self._patch_serial_factory(monkeypatch, [broken])
        transport = PicoBQTransport(max_retries=1)
        transport.connect("COM10", timeout=1.0)

        working = FakePicoSerial(results={"voltage_mV": 12500})
        self._patch_serial_factory(monkeypatch, [working])

        with pytest.raises(InstrumentTimeoutError):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

        # A "következő poll" -- FÜGGETLEN _call(), a transport már a
        # reopen során kapott friss handle-t használja.
        result = transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert result == 12500

    def test_device_absent_then_returns_before_next_independent_call_succeeds(self, monkeypatch):
        """MAX verifier 1. kör (V1 lelet, 2026-09-14): a HIL-D5-ön
        megfigyelt VALÓS forgatókönyv -- a HIBA pillanatában (write()
        kivétel) a reopen-KÍSÉRLET IS hibázik (az eszköz még nincs vissza),
        `self.serial` None marad EGY TELJES _call()-on át. Egy KÉSŐBBI,
        FÜGGETLEN _call() -- amikorra az eszköz már visszatért -- sikeres
        kell legyen, anélkül hogy a transportot el kellene dobni/újra
        connect()-elni. (Az eredeti, hibás kód ezen a ponton véglegesen
        "nincs aktív kapcsolat"-ot adott volna MINDEN további hívásra,
        mert a ciklus ELŐTTI kapu sosem próbált újra reopen-elni.)"""
        broken = FakePicoSerial(simulate_write_failure=True)
        self._patch_serial_factory(monkeypatch, [broken])
        transport = PicoBQTransport(max_retries=1)
        transport.connect("COM10", timeout=1.0)

        import serial

        def _factory_device_absent(port, baudrate, timeout):
            raise OSError("could not open port 'COM10': eszköz nincs jelen")

        monkeypatch.setattr(serial, "Serial", _factory_device_absent)

        with pytest.raises(InstrumentTimeoutError):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert transport.serial is None  # a reopen IS hibázott, nincs handle

        # Az eszköz "visszatér" -- a KÖVETKEZŐ Serial()-hívás már sikeres.
        working = FakePicoSerial(results={"voltage_mV": 12500})
        self._patch_serial_factory(monkeypatch, [working])

        result = transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert result == 12500

    def test_reopen_failure_when_device_still_absent_raises_clean_error(self, monkeypatch):
        """Ha az eszköz a reopen pillanatában MÉG nincs vissza, a
        pyserial.Serial(...) maga is hibázhat -- ez nem szabad, hogy
        AttributeError-t okozzon a self.serial None-on, hanem tiszta,
        egyértelmű hibát kell adnia."""
        broken = FakePicoSerial(simulate_write_failure=True)

        def _factory_always_fails(port, baudrate, timeout):
            raise OSError("could not open port 'COM10': eszköz nincs jelen")

        import serial
        monkeypatch.setattr(serial, "Serial", lambda port, baudrate, timeout: broken)
        transport = PicoBQTransport(max_retries=2)
        transport.connect("COM10", timeout=1.0)

        monkeypatch.setattr(serial, "Serial", _factory_always_fails)

        # max_retries=2: az 1. próbálkozás I/O-hibával hibázik -> reopen is
        # hibázik (self.serial=None) -> a 2. próbálkozás ELEJÉN is megpróbál
        # reopen-elni (MAX verifier 1. kör V1 javítás után MINDEN iteráció
        # újra próbál), az is hibázik -> a "reconnect sikertelen" explicit
        # PicoBQTransportError-t adja -- tiszta, egyértelmű hiba, NEM
        # AttributeError. (MAX verifier 1. kör V6 lelet: korábban a
        # "nincs aktív soros kapcsolat" match mindkét üzenetre illeszkedett
        # volna, ezért a régi, hibás ág is "zölden" átment volna -- most
        # explicit a "reconnect sikertelen" szövegrészre szűkítve.)
        with pytest.raises(PicoBQTransportError, match="reconnect sikertelen"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

        assert transport.serial is None
        # Egy KÖVETKEZŐ, FÜGGETLEN hívás is UJRA megpróbál reopen-elni (nem
        # adja fel véglegesen) -- a device_factory továbbra is hibázik, tehát
        # ugyanazt a "reconnect sikertelen" hibát adja, NEM AttributeError-t.
        with pytest.raises(PicoBQTransportError, match="reconnect sikertelen"):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

    def test_plain_timeout_without_exception_does_not_trigger_reopen(self, monkeypatch):
        """Sima timeout (üres readline(), NINCS kivétel) NEM soros-handle-
        törés -- nem szabad feleslegesen újranyitni a portot minden lassú
        Pico-válasznál."""
        serial_obj = FakePicoSerial(simulate_no_response=True)
        calls = self._patch_serial_factory(monkeypatch, [serial_obj])
        transport = PicoBQTransport(max_retries=2)
        transport.connect("COM10", timeout=1.0)
        calls.clear()  # a connect() saját hívását ne számoljuk

        with pytest.raises(InstrumentTimeoutError):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)

        assert calls == []  # nem hívtuk újra a Serial()-t
        assert transport.serial is serial_obj  # a handle változatlan

    def test_reopen_noop_when_connect_never_called(self):
        """Ha a transport serial=... konstruktor-paraméterrel lett
        felépítve (connect() sosem hívva, mint a meglévő unit tesztek
        többségében), a reopen-kísérlet NEM szabad, hogy megpróbálja
        .close()-olni/helyettesíteni a beinjektált fake-et (ami nem is
        biztos, hogy definiál close()-t) -- a régi, connect() nélküli
        viselkedés változatlan kell maradjon."""
        serial_obj = FakePicoSerial(simulate_write_failure=True)
        transport = PicoBQTransport(serial=serial_obj, max_retries=2)
        with pytest.raises(InstrumentTimeoutError):
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert transport.serial is serial_obj  # NEM cserélődött/törölődött

    def test_original_io_error_text_survives_in_final_exception_message(self, monkeypatch):
        """MAX verifier 2. kör (V3 lelet, 2026-09-14): production alapértelmezés
        (max_retries=3) mellett, ha a reopen IS hibázik, a VÉGÜL dobott
        kivétel üzenete ne csak az általános "reconnect sikertelen"
        szöveget tartalmazza, hanem az EREDETI OS-szintű I/O-hiba szövegét
        is -- ez a bq_diagnostics.csv <mező>_error oszlopába str(exc)-ként
        kerül (l. bq_diagnostics.py::poll_bq_diagnostics()._read()), ami
        SOSEM látná a __cause__-t, csak az üzenet szövegét."""
        broken = FakePicoSerial(simulate_write_failure=True)
        self._patch_serial_factory(monkeypatch, [broken])
        transport = PicoBQTransport(max_retries=3)
        transport.connect("COM10", timeout=1.0)

        import serial

        def _factory_device_absent(port, baudrate, timeout):
            raise OSError("could not open port 'COM10': eszköz nincs jelen")

        monkeypatch.setattr(serial, "Serial", _factory_device_absent)

        with pytest.raises(PicoBQTransportError) as excinfo:
            transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        assert "simulated write failure" in str(excinfo.value)

    def test_disconnect_clears_port_so_later_call_does_not_silently_reopen(self):
        """MAX verifier 2. kör (NEW-1 lelet, 2026-09-14): egy SZÁNDÉKOS
        disconnect() után egy KÉSŐBBI _call() NEM szabad, hogy csendben
        újranyissa a portot -- a disconnect() a hívó explicit szándéka,
        nem egy véletlen I/O-kiesés, amit a reconnect-logikának orvosolnia
        kellene."""
        serial_obj = FakePicoSerial(results={"voltage_mV": 12500})
        open_calls: list = []

        class _TrackedFactory:
            def __call__(self, port, baudrate, timeout):
                open_calls.append((port, baudrate, timeout))
                return serial_obj

        import serial

        factory = _TrackedFactory()
        from unittest import mock
        with mock.patch.object(serial, "Serial", factory):
            transport = PicoBQTransport(max_retries=1)
            transport.connect("COM10", timeout=1.0)
            assert len(open_calls) == 1

            transport.disconnect()
            assert transport.serial is None

            with pytest.raises(PicoBQTransportError, match="connect\\(\\) nem hívva"):
                transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
            assert len(open_calls) == 1  # NEM nyitotta újra a portot


class TestRequestIdsAreMonotonicAndUnique:
    def test_consecutive_calls_use_increasing_ids(self):
        serial = FakePicoSerial(results={"voltage_mV": 1, "soc_percent": 2})
        transport = PicoBQTransport(serial=serial)
        transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_STATE_OF_CHARGE)
        ids = [r["id"] for r in serial.written_requests]
        assert ids[1] > ids[0]


class TestWriteMethods:
    """WP-AK-08: allapotmodosito primitivek - mindegyik explicit `confirm`
    kulcsszavas parametert var, es a TF1-oldali pico_bq_rpc.py-val megegyezo
    parameter-nevekkel kuldi a keres.params-ot (data_hex/df_bytes_hex hex-
    kodolva, mert a JSON nem tud nyers bajtot hordozni)."""

    def test_unseal_sends_confirm_param(self):
        serial = FakePicoSerial(results={"unseal": {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        transport.unseal(confirm=True)
        assert serial.written_requests[0]["method"] == "unseal"
        assert serial.written_requests[0]["params"] == {"confirm": True}

    def test_full_access_sends_confirm_param(self):
        serial = FakePicoSerial(results={"full_access": {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        transport.full_access(confirm=True)
        assert serial.written_requests[0]["params"] == {"confirm": True}

    def test_seal_sends_confirm_param(self):
        serial = FakePicoSerial(results={"seal": {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        transport.seal(confirm=True)
        assert serial.written_requests[0]["params"] == {"confirm": True}

    def test_it_enable_sends_confirm_param(self):
        serial = FakePicoSerial(results={"it_enable": {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        transport.it_enable(confirm=True)
        assert serial.written_requests[0]["params"] == {"confirm": True}

    def test_confirm_false_is_still_sent_explicitly_not_silently_upgraded(self):
        """A transport SOSEM dontheti el maga, hogy egy confirm=False hivas
        "biztonsagos iranyu" es ezert ok - tovabbitja valtozatlanul, a
        tenyleges kapu a TF1-oldali pico_bq_service.py-ban van."""
        serial = FakePicoSerial(errors={"seal": ("METHOD_ERROR", "bq_seal() requires confirm=True")})
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="confirm=True"):
            transport.seal(confirm=False)
        assert serial.written_requests[0]["params"] == {"confirm": False}

    def test_write_data_flash_block_hex_encodes_payload(self):
        serial = FakePicoSerial(results={"write_data_flash_block": {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        data32 = bytes(range(32))
        transport.write_data_flash_block(104, 0, data32, confirm=True)
        params = serial.written_requests[0]["params"]
        assert params == {
            "subclass": 104, "block": 0, "data_hex": data32.hex(), "confirm": True,
        }

    def test_write_data_flash_block_rejects_wrong_length_before_sending(self):
        serial = FakePicoSerial()
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(ValueError, match=r"(?<![\w.+-])32 bytes\b"):
            transport.write_data_flash_block(104, 0, bytes(31), confirm=True)
        assert serial.written_requests == []  # nem kuldott semmit egy ervenytelen hosszal

    def test_restore_df_snapshot_hex_encodes_payload(self):
        serial = FakePicoSerial(results={"restore_df_snapshot": {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        manifest = {"artifact_type": "SELECTED_DF_SNAPSHOT", "df_blocks": [[104, 0]]}
        df_bytes = bytes(range(32))
        transport.restore_df_snapshot(manifest, df_bytes, confirm=True)
        params = serial.written_requests[0]["params"]
        assert params == {
            "manifest": manifest, "df_bytes_hex": df_bytes.hex(), "confirm": True,
        }

    def test_write_method_pico_error_propagates_as_transport_error(self):
        serial = FakePicoSerial(errors={
            "write_data_flash_block": ("METHOD_ERROR", "bq_df_write readback-verify mismatch"),
        })
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="readback-verify mismatch"):
            transport.write_data_flash_block(104, 0, bytes(32), confirm=True)


class TestCalibrationMethods:
    """WP-AK-09: kalibrációs trigger-parancsok + kalibrációs DF olvasás/írás
    - ugyanaz az explicit `confirm` mintája, mint a WP-AK-08 write-metódusoknál."""

    @pytest.mark.parametrize("method_name", [
        "enter_cal", "exit_cal", "board_offset_cal", "cc_offset_cal", "cc_offset_save",
    ])
    def test_trigger_methods_send_confirm_param(self, method_name):
        serial = FakePicoSerial(results={method_name: {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        getattr(transport, method_name)(confirm=True)
        assert serial.written_requests[0]["method"] == method_name
        assert serial.written_requests[0]["params"] == {"confirm": True}

    def test_read_calibration_data_returns_dict(self):
        expected = {"cc_gain": 0.47095, "cc_delta": 559500.0, "cc_offset": -1200, "board_offset": -10}
        serial = FakePicoSerial(results={"read_calibration_data": expected})
        transport = PicoBQTransport(serial=serial)
        assert transport.read_calibration_data() == expected

    def test_write_calibration_gain_delta_sends_values_and_confirm(self):
        serial = FakePicoSerial(results={"write_calibration_gain_delta": {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        transport.write_calibration_gain_delta(0.5, 500000.0, confirm=True)
        assert serial.written_requests[0]["params"] == {
            "new_gain": 0.5, "new_delta": 500000.0, "confirm": True,
        }

    def test_read_voltage_divider_returns_int(self):
        serial = FakePicoSerial(results={"read_voltage_divider": 36124})
        transport = PicoBQTransport(serial=serial)
        assert transport.read_voltage_divider() == 36124

    def test_write_voltage_divider_sends_value_and_confirm(self):
        serial = FakePicoSerial(results={"write_voltage_divider": {"status": "ok"}})
        transport = PicoBQTransport(serial=serial)
        transport.write_voltage_divider(20000, confirm=True)
        assert serial.written_requests[0]["params"] == {
            "new_value_mV": 20000, "confirm": True,
        }

    def test_calibration_write_pico_error_propagates_as_transport_error(self):
        serial = FakePicoSerial(errors={
            "write_calibration_gain_delta": ("METHOD_ERROR", "new_gain out of valid range"),
        })
        transport = PicoBQTransport(serial=serial)
        with pytest.raises(PicoBQTransportError, match="out of valid range"):
            transport.write_calibration_gain_delta(50.0, 500000.0, confirm=True)


class TestFullDriverIntegration:
    """Bq34z110ReadOnlyDriver + PicoBQTransport + FakePicoSerial végponttól
    végpontig - a "workflow -> BQDevice -> PicoBQTransport" absztrakció
    tényleges összeillesztésének igazolása (WP-AK-06 kötelező absztrakció)."""

    def test_driver_reads_full_telemetry_set_through_transport(self):
        state_block = bytearray(32)
        state_block[0:2] = (7200).to_bytes(2, "big")  # Qmax
        state_block[4] = 0x06  # UpdateStatus

        # A blokk első 2 bájtja a "Cell0 R_a flag" (SLUA664, bq34z100
        # háttérforrás — CLAIM_LEDGER.yaml BQ-RA-001), NEM egy R_a érték —
        # l. device_bq34z110.py DF_RA_TABLE_FLAG_BYTES kommentje (forrás-
        # hivatkozás frissítve F5-1, review-kör 5).
        ra_block = bytes([0xFF, 0x55]) + b"".join(
            v.to_bytes(2, "big", signed=True) for v in range(1, 16)
        )

        def _data_flash_block(params):
            if (params["subclass"], params["block"]) == (82, 0):
                return bytes(state_block).hex()
            if (params["subclass"], params["block"]) == (88, 0):
                return ra_block.hex()
            raise AssertionError(f"unexpected DF block request: {params}")

        serial = FakePicoSerial(results={
            "voltage_mV": 12500,
            "average_current_mA": -350,
            "temperature_01K": 2981,
            "soc_percent": 77,
            "remaining_capacity_mAh": 5000,
            "full_charge_capacity_mAh": 10000,
            "flags": 0xC080,
            "control_status": 0x2003,
            "chem_id": 0x0800,
            "device_type": 0x0541,
            "firmware_version": 0x0311,
            "hardware_version": 0x0003,
            "data_flash_block": _data_flash_block,
        })

        transport = PicoBQTransport(serial=serial)
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        assert driver.read_voltage_mV() == 12500
        assert driver.read_avg_current_mA() == -350
        assert driver.read_temperature_C() == pytest.approx(2981 / 10.0 - 273.15, abs=0.01)
        assert driver.read_state_of_charge_percent() == 77
        assert driver.read_remaining_capacity_mAh() == 5000
        assert driver.read_full_charge_capacity_mAh() == 10000
        assert driver.read_flags() == 0xC080
        assert driver.read_control_status() == 0x2003
        assert driver.read_chem_id() == 0x0800
        assert driver.read_device_type() == 0x0541
        assert driver.read_fw_version() == 0x0311
        assert driver.read_hw_version() == 0x0003
        assert driver.read_qmax() == 7200
        assert driver.read_update_status() == 0x06
        assert driver.read_ra_table() == [float(v) for v in range(1, 16)]
