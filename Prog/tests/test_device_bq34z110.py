"""
Bq34z110ReadOnlyDriver unit tesztek — MockBq34z110Transport-tal.
SUP03-TEST-004: Qmax, Ra, flags, update status, timeout, checksum hiba,
wrong ChemID, sealed/unsealed állapot (ControlStatus-on keresztül).
"""
from __future__ import annotations

import pytest

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.exceptions import InstrumentInvalidReading, InstrumentTimeoutError
from Prog.tests.mock_drivers.mock_bq34z110_transport import (
    Bq34z110ChecksumError,
    MockBq34z110Transport,
)


def make_driver() -> tuple[Bq34z110ReadOnlyDriver, MockBq34z110Transport]:
    transport = MockBq34z110Transport()
    driver = Bq34z110ReadOnlyDriver(transport=transport)
    return driver, transport


class TestBasicReadback:
    def test_read_voltage_mV(self):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 13200)
        assert driver.read_voltage_mV() == 13200

    def test_read_avg_current_positive_is_charge(self):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_AVERAGE_CURRENT, 1400)
        assert driver.read_avg_current_mA() == 1400

    def test_read_avg_current_negative_is_discharge(self):
        """Az AverageCurrent() 16 bites előjeles — negatív kisütési áram."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_AVERAGE_CURRENT, 0x10000 - 700)  # -700 mA
        assert driver.read_avg_current_mA() == -700

    def test_read_temperature_converts_from_01k(self):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_TEMPERATURE, 2982)  # 298.2 * 0.1K = 25.05°C
        assert driver.read_temperature_C() == pytest.approx(25.05, abs=0.01)

    def test_read_flags(self):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_FLAGS, 0x00C0)
        assert driver.read_flags() == 0x00C0

    def test_read_state_of_charge_percent(self):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_STATE_OF_CHARGE, 87)
        assert driver.read_state_of_charge_percent() == 87

    def test_read_cycle_count(self):
        # WP-AK-10 (SUP03-LEARN-002 gauge log)
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_CYCLE_COUNT, 3)
        assert driver.read_cycle_count() == 3

    def test_read_dod0(self):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_DOD0, 150)
        assert driver.read_dod0() == 150


class TestChemIdAndControlStatus:
    def test_read_chem_id(self):
        driver, transport = make_driver()
        transport.set_control_register(driver.MAC_CHEM_ID, 0x3230)
        assert driver.read_chem_id() == 0x3230

    def test_wrong_chem_id_detectable_by_caller(self):
        """A driver csak olvas — a 'wrong ChemID' döntést a hívó hozza meg
        egy elvárt érték összevetésével (SUP03-TEST-004)."""
        driver, transport = make_driver()
        expected_chem_id = 0x3230
        transport.set_control_register(driver.MAC_CHEM_ID, 0x1234)  # más, mint elvárt
        assert driver.read_chem_id() != expected_chem_id

    def test_read_control_status_sealed_bit(self):
        driver, transport = make_driver()
        transport.set_control_register(driver.MAC_CONTROL_STATUS, 0x2000)  # SS bit példa
        assert driver.read_control_status() == 0x2000

    def test_read_device_type_fw_version_hw_version(self):
        """WP-AK-07: BQ Diagnostics GUI "BQ device" mezőihez sourced Control()
        alparancsok — l. device_bq34z110.py docstring."""
        driver, transport = make_driver()
        transport.set_control_register(driver.MAC_DEVICE_TYPE, 0x0541)
        transport.set_control_register(driver.MAC_FW_VERSION, 0x0311)
        transport.set_control_register(driver.MAC_HW_VERSION, 0x0003)
        assert driver.read_device_type() == 0x0541
        assert driver.read_fw_version() == 0x0311
        assert driver.read_hw_version() == 0x0003

    def test_standard_command_and_control_subcommand_namespaces_do_not_collide(self):
        """WP-AK-06 review-lelet: STD_CMD_VOLTAGE(0x08) és MAC_CHEM_ID(0x0008)
        ugyanaz a Python int (8) — a két névtérnek EGYMÁSTÓL FÜGGETLENÜL kell
        léteznie, nem szabad, hogy egyik felülírja a másikat."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        transport.set_control_register(driver.MAC_CHEM_ID, 0x0800)
        assert driver.read_voltage_mV() == 12500
        assert driver.read_chem_id() == 0x0800


class TestUpdateStatusQmaxRa:
    """WP-AK-06: Qmax/UpdateStatus UGYANABBÓL a Data Flash blokkból
    (subclass 82 "State", block 0) olvasódik, különböző offseten — nem
    egy-egy önálló "subcommand"-ból (l. device_bq34z110.py docstring,
    regiszter-cím kereszt-ellenőrzés)."""

    @staticmethod
    def _state_block(
        qmax: int = 0, update_status: int = 0,
        avg_i_last_run: int = 0, avg_p_last_run: int = 0,
    ) -> bytes:
        block = bytearray(32)
        block[0:2] = qmax.to_bytes(2, "big")
        block[4] = update_status
        block[7:9] = avg_i_last_run.to_bytes(2, "big", signed=True)
        block[9:11] = avg_p_last_run.to_bytes(2, "big", signed=True)
        return bytes(block)

    def test_read_update_status_0x02_golden_image(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, self._state_block(update_status=0x02))
        assert driver.read_update_status() == 0x02

    def test_read_update_status_0x05_first_qmax_update(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, self._state_block(update_status=0x05))
        assert driver.read_update_status() == 0x05

    def test_read_update_status_0x06_full_learning(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, self._state_block(update_status=0x06))
        assert driver.read_update_status() == 0x06

    def test_read_update_status_raises_on_missing_block(self):
        """[WP-AK-07 kapu-javítás] korábban `read_update_status() == 0`-t
        vártunk itt — ez PONT az a "malformed reply csendesen 0 érték"
        mintázat, amit a WP-AK-07 Konszolidált terv kapuja kifejezetten
        tilt (egy hiányzó/túl rövid blokk NEM ugyanaz, mint egy valódi,
        0x00 UpdateStatus). Most explicit InstrumentInvalidReading-et vár."""
        driver, transport = make_driver()
        with pytest.raises(InstrumentInvalidReading):
            driver.read_update_status()

    def test_read_qmax(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, self._state_block(qmax=7200))
        assert driver.read_qmax() == 7200

    def test_read_qmax_raises_on_missing_block(self):
        """[WP-AK-07 kapu-javítás] l. test_read_update_status_raises_on_missing_block."""
        driver, transport = make_driver()
        with pytest.raises(InstrumentInvalidReading):
            driver.read_qmax()

    def test_read_qmax_decodes_as_signed_i2(self):
        """[regresszió, governance-v2 review-kör 1, CF-41, VERIFIED_MULTI_
        SOURCE: TI-SLUSB55B Table 20 "Qmax Cell 0" = I2, kereszt-igazolva
        Folyamatok/tervek/TRADITIONAL_LEARNING_BQ_PROVISIONING_AUDIT_2026-
        08-24.md 3. táblázatával ÉS bq_data_flash_service.py::FIELD_
        REGISTRY["qmax_cell0_initial_mAh"] (ugyanaz a subclass=82/offset=0
        pár, WRITE-oldalon már signed) — a READ-oldal korábban signed=
        False-szal dekódolt. Egy nyers 0x9C40 (unsigned 40000) bájtsorozat
        előjeles I2-ként -25536, nem 40000."""
        driver, transport = make_driver()
        raw_unsigned_qmax = 40000  # > 0x7FFF, előjeles/előjel nélküli eltérés itt jelentkezik
        block = bytearray(self._state_block())
        block[0:2] = raw_unsigned_qmax.to_bytes(2, "big", signed=False)
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, bytes(block))
        assert driver.read_qmax() == raw_unsigned_qmax - 0x10000

    def test_read_qmax_and_update_status_share_same_underlying_block(self):
        driver, transport = make_driver()
        transport.set_block(
            driver.DF_SC_STATE, driver.DF_BLOCK_STATE,
            self._state_block(qmax=7200, update_status=0x06),
        )
        assert driver.read_qmax() == 7200
        assert driver.read_update_status() == 0x06

    def test_read_avg_i_last_run_mA_signed(self):
        # WP-AK-10 (SUP03-LEARN-002 gauge log)
        driver, transport = make_driver()
        transport.set_block(
            driver.DF_SC_STATE, driver.DF_BLOCK_STATE,
            self._state_block(avg_i_last_run=-299),
        )
        assert driver.read_avg_i_last_run_mA() == -299

    def test_read_avg_p_last_run_signed(self):
        driver, transport = make_driver()
        transport.set_block(
            driver.DF_SC_STATE, driver.DF_BLOCK_STATE,
            self._state_block(avg_p_last_run=-1131),
        )
        assert driver.read_avg_p_last_run() == -1131

    def test_read_avg_i_last_run_raises_on_missing_block(self):
        driver, transport = make_driver()
        with pytest.raises(InstrumentInvalidReading):
            driver.read_avg_i_last_run_mA()

    def test_read_avg_p_last_run_raises_on_missing_block(self):
        driver, transport = make_driver()
        with pytest.raises(InstrumentInvalidReading):
            driver.read_avg_p_last_run()

    # [CRITICAL finding, 2. review-kör; forráshivatkozás frissítve F5-1,
    # review-kör 5 -- l. device_bq34z110.py DF_RA_TABLE_FLAG_BYTES kommentje]
    # a 32 bájtos blokk ELSŐ 2 bájtja a "Cell0 R_a flag" (SLUA664, bq34z100
    # háttérforrás -- CLAIM_LEDGER.yaml BQ-RA-001), NEM egy R_a érték —
    # minden lenti fixture-nek tartalmaznia kell egy REALISZTIKUS, nem-nulla
    # flag-szót (0xFF55 — elojeles int16-kent dekodolva NAGY NEGATIV szam,
    # -171 — lenne, ha a driver tevesen R_a0-kent olvasna), hogy a teszt
    # ténylegesen buktasson, ha a flag-kihagyás valaha visszaromlana.
    _RA_FLAG = bytes([0xFF, 0x55])

    def test_read_ra_table_15_values(self):
        driver, transport = make_driver()
        raw_mohm = [15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
        block = self._RA_FLAG + b"".join(v.to_bytes(2, "big", signed=True) for v in raw_mohm)
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, block)
        result = driver.read_ra_table()
        assert result == [float(v) for v in raw_mohm]

    def test_read_ra_table_negative_value_preserved(self):
        """A driver nem szűr — a negatív/invalid Ra-érték felismerése a
        hívó (SUP03-LEARN-009 'no_invalid_or_negative_values') felelőssége.
        [WP-AK-07 kapu-javítás után] a blokknak TELJES (32 bájtos, mind a 15
        értéket tartalmazó) kell lennie — egy csonka blokk most már
        InstrumentInvalidReading-et dob (l. test_read_ra_table_raises_on_missing_block)."""
        driver, transport = make_driver()
        raw_mohm = [-5, 10] + [1] * 13
        block = self._RA_FLAG + b"".join(v.to_bytes(2, "big", signed=True) for v in raw_mohm)
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, block)
        result = driver.read_ra_table()
        assert result[0] == -5.0
        assert result[1] == 10.0

    def test_read_ra_table_raises_on_missing_block(self):
        """[WP-AK-07 kapu-javítás, független review I-1 lelet] korábban a
        driver egy TELJESEN hiányzó/rövid blokkra csendesen egy üres/csonka
        listát adott vissza — ez a `qmax`/`update_status` mellett
        INKONZISZTENS módon kikerülte a kapu-szabályt (a snapshot-ban
        valid=True, error=None jelent volna meg hiányzó adatra). Most a
        driver is explicit InstrumentInvalidReading-et dob, mint a
        testvér-mezők."""
        driver, transport = make_driver()
        with pytest.raises(InstrumentInvalidReading):
            driver.read_ra_table()

    def test_read_ra_table_raises_on_partial_block(self):
        """Egy 15-nél kevesebb értéket tartalmazó (de nem teljesen üres)
        blokk is malformed — nem csak a teljesen hiányzó eset."""
        driver, transport = make_driver()
        block = self._RA_FLAG + b"".join(
            v.to_bytes(2, "big", signed=True) for v in [1, 2, 3]
        )
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, block)
        with pytest.raises(InstrumentInvalidReading):
            driver.read_ra_table()

    def test_read_ra_table_uses_subclass_88_not_89(self):
        """WP-AK-06 kereszt-ellenőrzés: subclass 88 = Cell0 R_a Table, 89 =
        xCell0 (alternatív profil, NEM exponált) — Leírások/TI_BQ/
        BQ34Z110PWR.pdf (SLUSB55B) "Table 20. Data Flash Summary" "Ra
        Tables" osztálya."""
        driver, transport = make_driver()
        cell0 = [1] * 15
        xcell0 = [99] * 15
        transport.set_block(
            88, 0, self._RA_FLAG + b"".join(v.to_bytes(2, "big", signed=True) for v in cell0)
        )
        transport.set_block(
            89, 0, self._RA_FLAG + b"".join(v.to_bytes(2, "big", signed=True) for v in xcell0)
        )
        result = driver.read_ra_table()
        assert result == [1.0] * 15

    def test_read_ra_table_skips_cell0_ra_flag_prefix(self):
        """Ha a driver tévesen a flag-szót olvasná R_a0-ként, ez az érték
        (-171, l. _RA_FLAG=0xFF55 elojeles dekodolva) jelenne meg — ehelyett
        a valódi első R_a értéknek (200) kell jönnie."""
        driver, transport = make_driver()
        raw_mohm = [200 + i for i in range(15)]
        block = self._RA_FLAG + b"".join(v.to_bytes(2, "big", signed=True) for v in raw_mohm)
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, block)
        result = driver.read_ra_table()
        assert len(result) == 15
        assert result[0] == 200.0
        assert -171.0 not in result


class TestDegenerateSealedDfBlockRejection:
    """[SCF-011 újratervezés, 2026-09-11, felhasználói döntés -- a DEFEKTUS
    HIL_VERIFIED, a FIX maga HIL_PENDING, l. device_bq34z110.py kommentje]
    Defense-in-depth driver-szintű védelem BQ-DF-SEALED-001 ellen -- l.
    CLAIM_LEDGER.yaml BQ-DF-SEALED-001: egy valós SEALED BQ34Z110-en a
    subclass 82 (State)/88 (Ra Table) data_flash_block()-olvasás PONTOSAN
    32 bájtos, de byte-azonos degenerált mintát ad vissza (21×0x00+11×0xFF)
    -- ezt a hosszalapú "túl rövid blokk" kapuk NEM fogják ki (a blokk
    hossza helyes), csak egy explicit tartalom-ellenőrzés. A kontroll
    SZÁNDÉKOSAN unseal-állapottól függetlenül fut minden ilyen olvasásnál
    (második, független biztosíték a session-szintű unseal() mellett,
    l. traditional_learning_controller.py/golden_gg_controller.py
    _run_precond())."""

    # DRIFT-HORGONY, NE cseréld a `device_bq34z110.DEGENERATE_SEALED_DF_BLOCK`
    # importjára (Kör 21 F18 MAX verifier N2): ez a FÜGGETLEN literál-másolat
    # az egyetlen hely a suite-ban, ami elkapja, ha a produkciós bájtminta
    # elsodródik a HIL-bizonyítéktól (CLAIM_LEDGER.yaml BQ-DF-SEALED-001).
    _DEGENERATE_BLOCK = b"\x00" * 21 + b"\xff" * 11

    def test_read_update_status_raises_on_degenerate_sealed_pattern(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, self._DEGENERATE_BLOCK)
        with pytest.raises(InstrumentInvalidReading):
            driver.read_update_status()

    def test_read_qmax_raises_on_degenerate_sealed_pattern(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, self._DEGENERATE_BLOCK)
        with pytest.raises(InstrumentInvalidReading):
            driver.read_qmax()

    def test_read_avg_i_last_run_raises_on_degenerate_sealed_pattern(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, self._DEGENERATE_BLOCK)
        with pytest.raises(InstrumentInvalidReading):
            driver.read_avg_i_last_run_mA()

    def test_read_avg_p_last_run_raises_on_degenerate_sealed_pattern(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, self._DEGENERATE_BLOCK)
        with pytest.raises(InstrumentInvalidReading):
            driver.read_avg_p_last_run()

    def test_read_ra_table_raises_on_degenerate_sealed_pattern(self):
        driver, transport = make_driver()
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, self._DEGENERATE_BLOCK)
        with pytest.raises(InstrumentInvalidReading):
            driver.read_ra_table()

    def test_near_miss_pattern_does_not_raise(self):
        """Egyetlen eltérő bájt (utolsó 0xFF -> 0xFE) NEM egyezik a HIL-
        igazolt degenerált mintával -- a kontroll ne legyen túl tág (ne
        utasítson el bármilyen sok-nulla/sok-0xFF blokkot, csak a PONTOS,
        HIL-en megfigyelt mintát)."""
        driver, transport = make_driver()
        near_miss = b"\x00" * 21 + b"\xff" * 10 + b"\xfe"
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, near_miss)
        # A degenerált-minta kapu nem akadályozza -- a normál dekódolás fut
        # (update_status offset=4, ami itt 0x00, egy hihető érték).
        assert driver.read_update_status() == 0x00

    def test_legitimate_all_zero_qmax_state_block_not_falsely_rejected(self):
        """Egy ténylegesen 0-lal kezdődő, de a degenerált mintától eltérő
        (nem csupa-0xFF farokkal záruló) blokk ne bukjon el hamisan -- csak
        a PONTOS 21×0x00+11×0xFF egyezés számít, nem az, hogy a blokk elején
        nullák vannak."""
        driver, transport = make_driver()
        block = bytearray(32)  # csupa nulla, kivéve...
        block[31] = 0xFE  # ...az utolsó bájt, ami elrontja a degenerált egyezést
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, bytes(block))
        assert driver.read_qmax() == 0


class TestTimeoutAndChecksum:
    def test_timeout_on_word_read(self):
        driver, transport = make_driver()
        transport.simulate_timeout = True
        with pytest.raises(InstrumentTimeoutError):
            driver.read_voltage_mV()

    def test_timeout_on_block_read(self):
        driver, transport = make_driver()
        transport.simulate_timeout = True
        with pytest.raises(InstrumentTimeoutError):
            driver.read_update_status()

    def test_checksum_error_on_block_read(self):
        driver, transport = make_driver()
        transport.simulate_checksum_error = True
        with pytest.raises(Bq34z110ChecksumError):
            driver.read_ra_table()


class TestNoWriteCapability:
    """SUP03-APP-002/003: a driver szándékosan NEM tartalmaz írási metódust."""

    def test_no_write_word_method(self):
        driver, _ = make_driver()
        assert not hasattr(driver, "write_word")

    def test_no_it_enable_method(self):
        driver, _ = make_driver()
        assert not hasattr(driver, "it_enable")

    def test_no_seal_method(self):
        driver, _ = make_driver()
        assert not hasattr(driver, "seal")
        assert not hasattr(driver, "unseal")

    def test_no_data_flash_write_method(self):
        driver, _ = make_driver()
        assert not hasattr(driver, "write_data_flash")
        assert not hasattr(driver, "write_block")


class TestR16B6F1PackConfiguration:
    """Közvetlen driver-kapu: a mock transport nem ellenőrzi a blokkhosszt."""

    @pytest.mark.parametrize("length", [31, 33])
    def test_rejects_wrong_block_length(self, length):
        driver, transport = make_driver()
        transport.set_block(64, 0, bytes.fromhex("0961" + "00" * (length - 2)))

        with pytest.raises(InstrumentInvalidReading, match="Pack Configuration"):
            driver.read_pack_configuration()

        assert transport.calls == ["read_data_flash_block(subclass=64, block=0)"]

    @pytest.mark.parametrize(
        ("raw_hex", "expected", "x10"),
        [("09E1" + "00" * 30, 0x09E1, True), ("8000" + "00" * 30, 0x8000, False)],
    )
    def test_literal_big_endian_words(self, raw_hex, expected, x10):
        # SLUSB55B Table 12: X10 az alsó bájt bit7, nem a bit15.
        # BQ-ENDIAN-001: a nyers DF-fixture nem a driver kódjából készül.
        driver, transport = make_driver()
        transport.set_block(64, 0, bytes.fromhex(raw_hex))

        value = driver.read_pack_configuration()

        assert value == expected
        assert bool(value & 0x0080) is x10
        assert transport.calls == ["read_data_flash_block(subclass=64, block=0)"]

    def test_rejects_degenerate_sealed_pattern(self):
        # BQ-DF-SEALED-001: ez a minta különben hamis X10=0 lenne.
        driver, transport = make_driver()
        transport.set_block(64, 0, bytes.fromhex("00" * 21 + "FF" * 11))

        with pytest.raises(InstrumentInvalidReading, match="SEALED"):
            driver.read_pack_configuration()

        assert transport.calls == ["read_data_flash_block(subclass=64, block=0)"]
