"""
bq_diagnostics.py unit tesztek — WP-AK-07: BqFieldReading kapu-szabály
(malformed reply sosem 0), Flags()/CONTROL_STATUS() bit-dekódolás,
poll_bq_diagnostics() teljes snapshot mock transporttal.
"""
from __future__ import annotations

import dataclasses

import pytest

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import (
    BqDiagnosticsSnapshot,
    BqFieldReading,
    decode_control_status,
    decode_flags,
    is_snapshot_fully_invalid,
    poll_bq_diagnostics,
)
from Prog.tests.mock_drivers.mock_bq34z110_transport import MockBq34z110Transport


def make_driver() -> tuple[Bq34z110ReadOnlyDriver, MockBq34z110Transport]:
    transport = MockBq34z110Transport()
    driver = Bq34z110ReadOnlyDriver(transport=transport)
    return driver, transport


class TestDecodeFlags:
    def test_ocvtaken_bit7_low_byte(self):
        # OCVTAKEN = low byte bit 7 = 0x0080 — Leírások/TI_BQ/BQ34Z110PWR.pdf
        # (SLUSB55B) Table 6 (Flags Bit Definitions).
        bits = decode_flags(0x0080)
        assert bits.ocvtaken is True
        assert bits.dsg is False
        assert bits.chg is False

    def test_high_byte_bits(self):
        # CHG(bit8) + FC(bit9) + OTC(bit15) egyszerre.
        bits = decode_flags(0x8300)
        assert bits.chg is True
        assert bits.fc is True
        assert bits.otc is True
        assert bits.otd is False

    def test_all_zero_word_is_all_false(self):
        bits = decode_flags(0x0000)
        assert not any([
            bits.dsg, bits.socf, bits.soc1, bits.tdd, bits.isd, bits.ocvtaken,
            bits.chg, bits.fc, bits.chg_inh, bits.batlow, bits.bathigh,
            bits.otd, bits.otc,
        ])


class TestDecodeControlStatus:
    def test_qen_vok_rup_dis_low_byte(self):
        # QEN=bit0, VOK=bit1, RUP_DIS=bit2 — Leírások/TI_BQ/BQ34Z110PWR.pdf
        # (SLUSB55B) Table 4 (CONTROL_STATUS Flags, NEM a Flags()-hez
        # tartozik, l. bq_diagnostics.py modul docstring).
        bits = decode_control_status(0x0007)
        assert bits.qen is True
        assert bits.vok is True
        assert bits.rup_dis is True
        assert bits.ldmd is False

    def test_ss_fas_security_bits_high_byte(self):
        # SS=bit13 (SEALED), FAS=bit14.
        bits = decode_control_status(0x2000)
        assert bits.ss is True
        assert bits.fas is False

    def test_qen_vok_rup_dis_do_not_alias_flags_ocvtaken_bit(self):
        """A CONTROL_STATUS() QEN(bit0)/VOK(bit1)/RUP_DIS(bit2) NEM ugyanaz
        a bitmező, mint a Flags() OCVTAKEN(bit7) — a két dekóder egymástól
        FÜGGETLEN szót kap, nem szabad, hogy összekeverve hibás eredményt
        adjanak (l. WP-AK-06 "KÉT KÜLÖN CÍMTÉR" tanulságának analógiája)."""
        flags_word = 0x0080  # csak OCVTAKEN
        control_status_word = 0x0007  # csak QEN/VOK/RUP_DIS
        flags = decode_flags(flags_word)
        cs = decode_control_status(control_status_word)
        assert flags.ocvtaken is True
        assert cs.qen is True and cs.vok is True and cs.rup_dis is True
        # a másik dekóder nem "szivárogtatja át" a bitet
        assert decode_control_status(flags_word).qen is False
        assert decode_flags(control_status_word).ocvtaken is False


class TestPollBqDiagnosticsHappyPath:
    def test_all_fields_valid_on_success(self):
        driver, transport = make_driver()
        transport.set_control_register(driver.MAC_DEVICE_TYPE, 0x0541)
        transport.set_control_register(driver.MAC_FW_VERSION, 0x0311)
        transport.set_control_register(driver.MAC_HW_VERSION, 0x0003)
        transport.set_control_register(driver.MAC_CHEM_ID, 0x0800)
        transport.set_control_register(driver.MAC_CONTROL_STATUS, 0x2003)
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        transport.set_register(driver.STD_CMD_AVERAGE_CURRENT, 1400)
        transport.set_register(driver.STD_CMD_TEMPERATURE, 2982)
        transport.set_register(driver.STD_CMD_STATE_OF_CHARGE, 77)
        transport.set_register(driver.STD_CMD_REMAINING_CAPACITY, 5000)
        transport.set_register(driver.STD_CMD_FULL_CHARGE_CAPACITY, 10000)
        transport.set_register(driver.STD_CMD_FLAGS, 0xC080)
        transport.set_register(driver.STD_CMD_CYCLE_COUNT, 5)
        transport.set_register(driver.STD_CMD_DOD0, 100)
        state_block = bytearray(32)
        state_block[0:2] = (7200).to_bytes(2, "big")
        state_block[4] = 0x06
        state_block[7:9] = (-299).to_bytes(2, "big", signed=True)
        state_block[9:11] = (-1131).to_bytes(2, "big", signed=True)
        transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, bytes(state_block))
        ra_block = bytes([0xFF, 0x55]) + b"".join(
            v.to_bytes(2, "big", signed=True) for v in range(1, 16)
        )
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, ra_block)

        snapshot = poll_bq_diagnostics(driver)

        assert snapshot.device_type.valid is True
        assert snapshot.device_type.value == 0x0541
        assert snapshot.voltage_mV.value == 12500
        assert snapshot.qmax.value == 7200
        assert snapshot.update_status.value == 0x06
        assert snapshot.ra_table_mohm.value == [float(v) for v in range(1, 16)]
        assert snapshot.cycle_count.value == 5
        assert snapshot.dod0.value == 100
        assert snapshot.avg_i_last_run_mA.value == -299
        assert snapshot.avg_p_last_run.value == -1131
        # Block 5 (2026-08-25, SUP03-LEARN-002): flags=0xC080 -> OCVTAKEN
        # (bit7=0x0080) set; control_status=0x2003 -> QEN(bit0)/VOK(bit1) set,
        # RUP_DIS(bit2) clear.
        assert snapshot.ocvtaken.value is True
        assert snapshot.qen.value is True
        assert snapshot.vok.value is True
        assert snapshot.rup_dis.value is False
        for field_name in (
            "device_type", "fw_version", "hw_version", "chem_id", "control_status",
            "voltage_mV", "avg_current_mA", "temperature_C", "soc_percent",
            "remaining_capacity_mAh", "full_charge_capacity_mAh", "flags",
            "qmax", "update_status", "ra_table_mohm",
            "cycle_count", "dod0", "avg_i_last_run_mA", "avg_p_last_run",
            "ocvtaken", "vok", "qen", "rup_dis",
        ):
            reading = getattr(snapshot, field_name)
            assert reading.valid is True, f"{field_name} should be valid"
            assert reading.error is None, f"{field_name} should have no error"


class TestPollBqDiagnosticsGateNeverSilentZero:
    """WP-AK-07 kapu: "BQ disconnect / stale data / malformed reply nem
    válhat csendesen 0 értékké." — egyetlen mező hibája se dőljön el 0-ra,
    és ne dobja el a TELJES snapshotot."""

    def test_timeout_on_one_field_does_not_crash_whole_snapshot(self):
        driver, transport = make_driver()
        transport.simulate_timeout = True
        snapshot = poll_bq_diagnostics(driver)  # nem szabad kivételt dobnia
        assert snapshot.voltage_mV.valid is False
        assert snapshot.voltage_mV.value is None
        assert snapshot.voltage_mV.error is not None
        assert snapshot.qmax.valid is False
        assert snapshot.qmax.value is None

    def test_malformed_qmax_block_reported_as_invalid_not_zero(self):
        """A driver WP-AK-07 kapu-javítása után read_qmax() egy hiányzó/túl
        rövid blokkra InstrumentInvalidReading-et dob — ezt a pollernek
        valid=False + error-ként kell rögzítenie, a value SOHA nem lehet 0."""
        driver, transport = make_driver()
        # A State blokk NINCS beállítva -> read_qmax()/read_update_status()
        # rövid (üres) blokkot kap, ami most már kivételt dob (l.
        # device_bq34z110.py WP-AK-07 kapu-javítás).
        snapshot = poll_bq_diagnostics(driver)
        assert snapshot.qmax.valid is False
        assert snapshot.qmax.value is None
        assert snapshot.qmax.value != 0
        assert snapshot.update_status.valid is False
        assert snapshot.update_status.value is None

    def test_missing_ra_table_block_reported_as_invalid_not_silently_empty(self):
        """[Független review I-1 lelet, javítva] KORÁBBAN ugyanerre a
        hiányzó blokkra a ra_table_mohm mező valid=True, value=[] lett volna
        — inkonzisztensen a testvér qmax/update_status mezőkkel szemben,
        amik helyesen valid=False-ot kaptak. Most mindhárom mezőnek
        EGYSÉGESEN invalid-nak kell lennie ugyanarra a hiányzó State/Ra
        Table blokk-alaphelyzetre."""
        driver, transport = make_driver()
        snapshot = poll_bq_diagnostics(driver)
        assert snapshot.ra_table_mohm.valid is False
        assert snapshot.ra_table_mohm.value is None
        assert snapshot.ra_table_mohm.error is not None

    def test_checksum_error_on_ra_table_reported_not_crashed(self):
        driver, transport = make_driver()
        transport.simulate_checksum_error = True
        snapshot = poll_bq_diagnostics(driver)
        assert snapshot.ra_table_mohm.valid is False
        assert snapshot.ra_table_mohm.value is None

    def test_partial_failure_other_fields_still_valid(self):
        """Ha CSAK a Data Flash blokk hibás (üres), a standard command /
        Control() mezőknek attól még sikeresen kell olvasódniuk — a
        mezőnkénti hibaelkülönítés lényege."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        transport.set_control_register(driver.MAC_CHEM_ID, 0x0800)
        snapshot = poll_bq_diagnostics(driver)
        assert snapshot.voltage_mV.valid is True
        assert snapshot.voltage_mV.value == 12500
        assert snapshot.chem_id.valid is True
        assert snapshot.qmax.valid is False


class TestDecodedBitFieldsInSnapshot:
    """Block 5 (2026-08-25, SUP03-LEARN-002): OCVTAKEN/VOK/QEN/RUP_DIS mint
    önálló, dekódolt BqFieldReading[bool] mező a snapshotban — a forrás-szó
    (flags/control_status) valid/error állapotát öröklik, SOHA nem "hamisan
    False" egy sikertelen olvasásnál."""

    def test_ocvtaken_invalid_when_flags_read_fails(self):
        driver, transport = make_driver()
        transport.simulate_timeout = True
        snapshot = poll_bq_diagnostics(driver)
        assert snapshot.ocvtaken.valid is False
        assert snapshot.ocvtaken.value is None
        assert snapshot.ocvtaken.error is not None

    def test_vok_qen_rup_dis_invalid_when_control_status_read_fails(self):
        driver, transport = make_driver()
        transport.simulate_timeout = True
        snapshot = poll_bq_diagnostics(driver)
        for field_name in ("vok", "qen", "rup_dis"):
            reading = getattr(snapshot, field_name)
            assert reading.valid is False
            assert reading.value is None

    def test_flags_and_control_status_failures_are_independent(self):
        """Ha CSAK a flags olvasás hibázik (pl. STD_CMD névtér-szintű hiba),
        a control_status-ból levezetett bitek (VOK/QEN/RUP_DIS) attól még
        érvényesek maradjanak — ugyanaz az elv, mint a mezőnkénti
        hibaelkülönítés máshol a modulban."""
        driver, transport = make_driver()
        transport.set_control_register(driver.MAC_CONTROL_STATUS, 0x0003)  # QEN+VOK

        def _raise_standard(command):
            raise RuntimeError("simulated standard-command-word failure")

        transport.read_standard_command_word = _raise_standard  # type: ignore[method-assign]

        snapshot = poll_bq_diagnostics(driver)
        assert snapshot.flags.valid is False
        assert snapshot.ocvtaken.valid is False
        assert snapshot.control_status.valid is True
        assert snapshot.vok.valid is True
        assert snapshot.vok.value is True
        assert snapshot.qen.valid is True
        assert snapshot.qen.value is True
        assert snapshot.rup_dis.valid is True
        assert snapshot.rup_dis.value is False


class TestBqFieldReadingAge:
    def test_age_s_reflects_elapsed_time(self):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        snapshot = poll_bq_diagnostics(driver)
        age_now = snapshot.voltage_mV.age_s(now=snapshot.voltage_mV.timestamp_monotonic + 5.0)
        assert age_now == pytest.approx(5.0)


def _valid_reading(value):
    return BqFieldReading(value=value, valid=True, error=None, timestamp_monotonic=0.0)


def _invalid_reading(error="simulated"):
    return BqFieldReading(value=None, valid=False, error=error, timestamp_monotonic=0.0)


class TestIsSnapshotFullyInvalid:
    """HIL-D5 (2026-09-14, CF-10a/DFAM-027 escalation-küszöb, CLAIM_
    LEDGER.yaml PICO-RECONNECT-001): a `traditional_learning_controller.py`/
    `golden_gg_controller.py` `_check_bq_transport_escalation()` erre a
    helperre épül — kritikus, hogy MEGKÜLÖNBÖZTESSE a teljes transport-
    kiesést (minden mező invalid) egy legitim RÉSZLEGES érvénytelenségtől
    (pl. SEALED chip alatt csak a Data Flash-függő mezők, l. HIL-D5 valós
    mérése 2026-09-14: 18/23 valid, 5 invalid SEALED miatt)."""

    def _all_fields_snapshot(self, *, all_valid: bool, invalid_field_names: frozenset[str] = frozenset()) -> BqDiagnosticsSnapshot:
        kwargs = {}
        for f in dataclasses.fields(BqDiagnosticsSnapshot):
            if not all_valid and (not invalid_field_names or f.name in invalid_field_names):
                kwargs[f.name] = _invalid_reading()
            else:
                kwargs[f.name] = _valid_reading(1)
        return BqDiagnosticsSnapshot(**kwargs)

    def test_all_fields_valid_is_not_fully_invalid(self):
        snapshot = self._all_fields_snapshot(all_valid=True)
        assert is_snapshot_fully_invalid(snapshot) is False

    def test_all_fields_invalid_is_fully_invalid(self):
        snapshot = self._all_fields_snapshot(all_valid=False)
        assert is_snapshot_fully_invalid(snapshot) is True

    def test_sealed_chip_partial_invalidity_is_not_fully_invalid(self):
        """A HIL-D5-ön ténylegesen megfigyelt minta: SEALED chip alatt 5
        DF-függő mező invalid, a többi 18 valid — ez NEM transport-kiesés,
        NEM szabad escalation-t kiváltania."""
        sealed_df_fields = frozenset({
            "qmax", "update_status", "ra_table_mohm",
            "avg_i_last_run_mA", "avg_p_last_run",
        })
        snapshot = self._all_fields_snapshot(all_valid=False, invalid_field_names=sealed_df_fields)
        # A nem-felsorolt mezők valid=True-ra kerülnek a helper alapértelmezése miatt.
        assert is_snapshot_fully_invalid(snapshot) is False

    def test_all_but_one_field_invalid_is_not_fully_invalid(self):
        """Éles határeset: 1 valid mező is elég ahhoz, hogy NE minősüljön
        teljes kiesésnek -- a küszöb szigorúan "MINDEN mező invalid"."""
        all_but_device_type = frozenset(
            f.name for f in dataclasses.fields(BqDiagnosticsSnapshot) if f.name != "device_type"
        )
        snapshot = self._all_fields_snapshot(all_valid=False, invalid_field_names=all_but_device_type)
        assert is_snapshot_fully_invalid(snapshot) is False


# R16-B20 / energy-04: nyers Avg P és külön, mezőnként minősített skálakontextus.
@pytest.mark.parametrize("scale", [1, 10])
@pytest.mark.parametrize("pack_word,x10", [(0x0961, 0), (0x09E1, 1)])
@pytest.mark.parametrize("missing", [None, "scale", "x10"])
def test_r16b20_avg_p_scale_context_csv(scale, pack_word, x10, missing):
    import csv
    import io
    import threading

    from Prog.src.logger import BQ_DIAGNOSTICS_COLUMNS, CSV_COLUMNS, Logger

    driver, transport = make_driver()
    state = bytearray(32)
    state[9:11] = (-1131).to_bytes(2, "big", signed=True)
    transport.set_block(82, 0, bytes(state))
    from Prog.src.bq_consistency_gate import (
        DfFieldComparison,
        ProvisioningConsistencyReport,
        ProvisioningGateDecision,
    )
    from Prog.src.bq_diagnostics import format_bq_session_scale_context

    # Session-readback, szándékosan eltérő profile_expected értékkel.
    # A teljes Pack Configuration szó helyett a B20 kapu 0/1 bitet jelent.
    assert (pack_word >> 7) & 1 == x10
    report = ProvisioningConsistencyReport(
        decision=ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH,
        reason="test session readback",
        comparisons=tuple(
            DfFieldComparison(name, -1, None if absent else value, unit, 0, False)
            for name, value, unit, absent in (
                ("design_energy_scale", scale, "", missing == "scale"),
                ("pack_configuration", x10, "bit", missing == "x10"),
            )
        ),
    )
    message = format_bq_session_scale_context(report)
    for label, value, absent in (
        ("Design Energy Scale", scale, missing == "scale"),
        ("X10", x10, missing == "x10"),
    ):
        expected = "not read / session context unavailable" if absent else str(value)
        assert f"{label}={expected}" in message.split("; ")
    assert message.startswith("source=B7 PRECOND UNSEALED; ")

    snapshot = poll_bq_diagnostics(driver)
    assert snapshot.avg_p_last_run.value == -1131
    # A periodikus snapshot nem állít friss olvasást a session-kontextusról.
    for name in ("design_energy_scale", "pack_configuration_x10"):
        reading = getattr(snapshot, name)
        assert reading.value is None
        assert reading.valid is False
        assert reading.error == "not read"
        assert reading.timestamp_monotonic == 0.0

    # A valódi szerializáló memóriapufferbe ír, fájl és SQLite nélkül.
    buf = io.StringIO(newline="")
    logger = Logger.__new__(Logger)
    logger._bq_diag_lock = threading.Lock()
    logger._bq_diag_file = buf
    logger._bq_diag_writer = csv.DictWriter(buf, fieldnames=BQ_DIAGNOSTICS_COLUMNS)
    logger._bq_diag_writer.writeheader()
    logger.log_bq_diagnostics(snapshot)
    buf.seek(0)
    reader = csv.DictReader(buf)
    row = next(reader)
    assert row["avg_p_last_run"] == "-1131"
    assert row["avg_p_last_run_valid"] == "True"
    assert row["avg_p_last_run_error"] == ""
    for name in ("design_energy_scale", "pack_configuration_x10"):
        assert row[name] == ""
        assert row[f"{name}_valid"] == "False"
        assert row[f"{name}_error"] == "not read"
        assert float(row[f"{name}_age_s"]) >= 0
    legacy = (
        "device_type", "fw_version", "hw_version", "chem_id", "control_status",
        "voltage_mV", "avg_current_mA", "temperature_C", "soc_percent",
        "remaining_capacity_mAh", "full_charge_capacity_mAh", "flags",
        "qmax", "update_status", "ra_table_mohm",
        "cycle_count", "dod0", "avg_i_last_run_mA", "avg_p_last_run",
        "ocvtaken", "vok", "qen", "rup_dis",
    )
    legacy_columns = ["timestamp_iso"] + [
        col for name in legacy
        for col in (name, f"{name}_valid", f"{name}_error", f"{name}_age_s")
    ]
    assert reader.fieldnames == legacy_columns + [
        "design_energy_scale", "design_energy_scale_valid",
        "design_energy_scale_error", "design_energy_scale_age_s",
        "pack_configuration_x10", "pack_configuration_x10_valid",
        "pack_configuration_x10_error", "pack_configuration_x10_age_s",
    ]
    assert "design_energy_scale" not in CSV_COLUMNS
    assert "pack_configuration_x10" not in CSV_COLUMNS


def test_r16b20_old_snapshot_constructor_marks_scale_unknown():
    driver, _transport = make_driver()
    snapshot = poll_bq_diagnostics(driver)
    old_kwargs = {
        f.name: getattr(snapshot, f.name)
        for f in dataclasses.fields(snapshot)
        if f.name not in ("design_energy_scale", "pack_configuration_x10")
    }
    old_style = BqDiagnosticsSnapshot(**old_kwargs)
    for name in ("design_energy_scale", "pack_configuration_x10"):
        reading = getattr(old_style, name)
        assert reading.value is None
        assert reading.valid is False
        assert reading.error == "not read"


@pytest.mark.parametrize("timeout", [False, True])
def test_r16b20f1_poll_keeps_original_io_budget_without_writes(timeout):
    from unittest.mock import Mock

    driver, transport = make_driver()
    transport.simulate_timeout = timeout
    spy = Mock(wraps=driver)
    snapshot = poll_bq_diagnostics(spy)

    assert [call[0] for call in spy.method_calls] == [
        "read_flags", "read_control_status", "read_device_type", "read_fw_version",
        "read_hw_version", "read_chem_id", "read_voltage_mV", "read_avg_current_mA",
        "read_temperature_C", "read_state_of_charge_percent",
        "read_remaining_capacity_mAh", "read_full_charge_capacity_mAh",
        "read_qmax", "read_update_status", "read_ra_table", "read_cycle_count",
        "read_dod0", "read_avg_i_last_run_mA", "read_avg_p_last_run",
    ]
    assert len(transport.calls) == 19
    assert all(call.startswith("read_") for call in transport.calls)
    assert not any("subclass=48" in call or "subclass=64" in call for call in transport.calls)
    assert snapshot.design_energy_scale.error == "not read"
    assert snapshot.pack_configuration_x10.error == "not read"
    if timeout:
        assert is_snapshot_fully_invalid(snapshot)


def test_r16b20f1_missing_session_context_is_explicit():
    from Prog.src.bq_consistency_gate import (
        ProvisioningConsistencyReport,
        ProvisioningGateDecision,
    )
    from Prog.src.bq_diagnostics import format_bq_session_scale_context

    report = ProvisioningConsistencyReport(
        ProvisioningGateDecision.BLOCKED_BQ_DF_READBACK_FAILED, "unavailable", (),
    )
    assert format_bq_session_scale_context(report) == (
        "source=B7 PRECOND UNSEALED; "
        "Design Energy Scale=not read / session context unavailable; "
        "X10=not read / session context unavailable"
    )


@pytest.mark.parametrize("method", [
    "read_flags", "read_control_status", "read_device_type", "read_fw_version",
    "read_hw_version", "read_chem_id", "read_voltage_mV", "read_avg_current_mA",
    "read_temperature_C", "read_state_of_charge_percent", "read_remaining_capacity_mAh",
    "read_full_charge_capacity_mAh", "read_qmax", "read_update_status", "read_ra_table",
    "read_cycle_count", "read_dod0", "read_avg_i_last_run_mA", "read_avg_p_last_run",
])
def test_r16b25_poll_preserves_user_abort(monkeypatch, method):
    from unittest.mock import Mock

    from Prog.src.exceptions import TestAbortedByUser

    driver, _transport = make_driver()
    aborted = TestAbortedByUser("B25 first poll abort")
    read = Mock(side_effect=aborted)
    monkeypatch.setattr(driver, method, read)
    with pytest.raises(TestAbortedByUser) as caught:
        poll_bq_diagnostics(driver)
    assert caught.value is aborted
    read.assert_called_once()
