"""
test_bq_data_flash_service.py — Block 1 (2026-08-24, Traditional Learning
BQ34Z110 provisioning /goal): Prog/src/bq_data_flash_service.py tesztjei,
FakePicoSerial-lel (valós Pico/BQ HIL nélkül, l.
Folyamatok/tervek/TRADITIONAL_LEARNING_BQ_PROVISIONING_AUDIT_2026-08-24.md).

A test_bq_controlled_write.py mintáját követi, de a _StatefulDevice helyett
_MultiBlockDevice-ot használ (több (subclass, block) kulcsú in-memory
tartalom), mert a fő architekturális követelmény — több blokk, ill. egy
blokkon belüli több mező kezelése — ezt igényli.
"""
from __future__ import annotations

import pytest

from Prog.drivers.device_bq34z110 import DEGENERATE_SEALED_DF_BLOCK
from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.bq_controlled_write import ControlledBqWriteError
from Prog.src.bq_data_flash_service import (
    FIELD_REGISTRY,
    Bq34z110DataFlashService,
    DfFieldSpec,
    DfFieldSpecError,
    DfFieldType,
    DfFieldWriteError,
    decode_df_field,
    encode_df_field,
)
from Prog.src.exceptions import InstrumentInvalidReading
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial


class _MultiBlockDevice:
    """Több (subclass, block) kulcsú in-memory DF-tartalom — a
    test_bq_controlled_write.py _StatefulDevice-jától eltérően (ami csak EGY
    blokkot szimulál) ez teszi lehetővé a csoportosítási/multi-blokk
    teszteket."""

    def __init__(self, initial: dict[tuple[int, int], bytes]):
        self.blocks: dict[tuple[int, int], bytearray] = {
            k: bytearray(v) for k, v in initial.items()
        }
        self.write_calls = 0
        self.read_calls = 0
        # A write_fields() SAJÁT PRE-READ olvasása MINDIG a valós blokk-
        # tartalmat kell lássa (különben a patch rossz bázisra épülne) — ez
        # az override csak az ELSŐ write() HÍVÁS UTÁNI olvasásokra hat, azaz
        # kifejezetten a mögöttes pipeline WRITE UTÁNI READBACK lépését
        # szimulálja hibásnak (COMPARE FAIL), nem a PRE-READ-et.
        self.readback_override_after_write: bytes | None = None
        self._write_happened = False

    def read(self, params):
        self.read_calls += 1
        if self._write_happened and self.readback_override_after_write is not None:
            return self.readback_override_after_write.hex()
        key = (params["subclass"], params["block"])
        return bytes(self.blocks.setdefault(key, bytearray(32))).hex()

    def write(self, params):
        self.write_calls += 1
        self._write_happened = True
        key = (params["subclass"], params["block"])
        self.blocks[key] = bytearray(bytes.fromhex(params["data_hex"]))
        return {"status": "ok"}


def _make_transport(device: _MultiBlockDevice, **extra_results) -> tuple[PicoBQTransport, FakePicoSerial]:
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


# Két, ugyanabban a blokkban lévő tesztmező (SC48/block0) + egy másik blokkban
# lévő mező (SC36/block0), egy kizárólag olvasható és egy nem-writable mező —
# ISOLÁLT registry, hogy a tesztek ne a "valódi" FIELD_REGISTRY-től függjenek.
_FIELD_A = DfFieldSpec(name="field_a", subclass=48, df_offset=2, field_type=DfFieldType.I2)
_FIELD_B = DfFieldSpec(name="field_b", subclass=48, df_offset=10, field_type=DfFieldType.U2)
_FIELD_C = DfFieldSpec(name="field_c", subclass=36, df_offset=0, field_type=DfFieldType.U1)
_FIELD_OVERLAP = DfFieldSpec(name="field_overlap", subclass=48, df_offset=3, field_type=DfFieldType.I2)
_FIELD_READONLY = DfFieldSpec(
    name="field_readonly", subclass=48, df_offset=20, field_type=DfFieldType.H1, writable=False,
)
_TEST_REGISTRY = {
    "field_a": _FIELD_A, "field_b": _FIELD_B, "field_c": _FIELD_C,
    "field_overlap": _FIELD_OVERLAP, "field_readonly": _FIELD_READONLY,
}


# ---------------------------------------------------------------------- #
# Encode/decode                                                           #
# ---------------------------------------------------------------------- #

class TestEncodeDecode:
    def test_i2_negative_roundtrip(self):
        spec = DfFieldSpec(name="x", subclass=1, df_offset=0, field_type=DfFieldType.I2)
        encoded = encode_df_field(spec, -1)
        assert encoded == b"\xff\xff"
        assert decode_df_field(spec, encoded) == -1

    def test_i2_min_bitpattern(self):
        spec = DfFieldSpec(name="x", subclass=1, df_offset=0, field_type=DfFieldType.I2)
        encoded = encode_df_field(spec, -32768)
        assert encoded == b"\x80\x00"
        assert decode_df_field(spec, encoded) == -32768

    def test_u2_large_value_not_confused_with_signed(self):
        spec = DfFieldSpec(name="x", subclass=1, df_offset=0, field_type=DfFieldType.U2)
        encoded = encode_df_field(spec, 65535)
        assert encoded == b"\xff\xff"
        assert decode_df_field(spec, encoded) == 65535

    def test_u1_roundtrip(self):
        spec = DfFieldSpec(name="x", subclass=1, df_offset=0, field_type=DfFieldType.U1)
        encoded = encode_df_field(spec, 12)
        assert encoded == b"\x0c"
        assert decode_df_field(spec, encoded) == 12

    def test_encode_out_of_range_raises(self):
        spec = DfFieldSpec(name="x", subclass=1, df_offset=0, field_type=DfFieldType.I2)
        with pytest.raises(ValueError, match="értéktartománya"):
            encode_df_field(spec, 40000)

    def test_decode_too_short_block_raises_invalid_reading(self):
        spec = DfFieldSpec(name="x", subclass=1, df_offset=30, field_type=DfFieldType.I2)
        with pytest.raises(InstrumentInvalidReading):
            decode_df_field(spec, bytes(31))

    def test_block_and_offset_in_block_properties(self):
        spec = DfFieldSpec(name="cell_terminate_voltage_mV", subclass=80, df_offset=67, field_type=DfFieldType.I2)
        assert spec.block == 2
        assert spec.offset_in_block == 3


class TestCf40PerFieldMinMaxEnforcement:
    """CF-40 (governance-v2 review-kör 1, batch 5): encode_df_field() korábban
    CSAK a DfFieldType ábrázolhatósági tartományát ellenőrizte (pl. I2:
    -32768..32767), NEM a TI-SLUSB55B Table 20 szerinti, mezőnkénti,
    szűkebb Min/Max-ot — egy típus-tartományon belüli, de a TI-szemantika
    szerint érvénytelen érték (pl. Cell Design Voltage = 1000 mV, a TI
    Min=2000 mV alatt) csendben, hiba nélkül íródott volna a Data Flash-be.
    L. CLAIM_LEDGER.yaml BQ-DF-RANGE-001."""

    def test_min_value_none_by_default_no_enforcement(self):
        spec = DfFieldSpec(name="x", subclass=1, df_offset=0, field_type=DfFieldType.I2)
        # Típus-tartományon belüli, de "szokatlan" érték — min_value/max_value
        # nincs beállítva, tehát nem utasítható vissza mezőnkénti okból.
        encoded = encode_df_field(spec, -32768)
        assert decode_df_field(spec, encoded) == -32768

    def test_below_min_value_raises(self):
        spec = DfFieldSpec(
            name="cell_design_voltage_mV", subclass=48, df_offset=6,
            field_type=DfFieldType.I2, min_value=2000, max_value=5000,
        )
        with pytest.raises(ValueError, match="minimuma"):
            encode_df_field(spec, 1999)

    def test_above_max_value_raises(self):
        spec = DfFieldSpec(
            name="cell_design_voltage_mV", subclass=48, df_offset=6,
            field_type=DfFieldType.I2, min_value=2000, max_value=5000,
        )
        with pytest.raises(ValueError, match="maximuma"):
            encode_df_field(spec, 5001)

    def test_boundary_values_are_accepted(self):
        spec = DfFieldSpec(
            name="cell_design_voltage_mV", subclass=48, df_offset=6,
            field_type=DfFieldType.I2, min_value=2000, max_value=5000,
        )
        # Inkluzív határok — a TI Table 20 Min/Max saját maga is megengedett
        # érték, nem a tartományon kívüli első szám.
        assert int.from_bytes(encode_df_field(spec, 2000), "big", signed=True) == 2000
        assert int.from_bytes(encode_df_field(spec, 5000), "big", signed=True) == 5000

    def test_field_registry_real_fields_enforce_ti_table20_bounds(self):
        # A valódi FIELD_REGISTRY bejegyzésen keresztül — nem csak izolált
        # DfFieldSpec-en — 1000 mV típus-tartományon belüli, de a TI Table 20
        # Min=2000 mV alatt van cell_design_voltage_mV-re.
        spec = FIELD_REGISTRY["cell_design_voltage_mV"]
        with pytest.raises(ValueError, match="minimuma"):
            encode_df_field(spec, 1000)
        # A dokumentált alapérték (2000 mV) magának is érvényesnek kell
        # maradnia (regresszió: a jelenleg éles, fixen írt érték ne törjön).
        encode_df_field(spec, 2000)

    def test_cell_terminate_voltage_scf001_stays_unenforced(self):
        # SCF-001/BQ-DF-003: a Cell Terminate Voltage forrása önmagával
        # ellentmondásos (Min=2500 dokumentálva, de a Default=1800 a Min
        # ALATT van) — ezért ez az EGYETLEN mező, aminek NINCS min_value/
        # max_value-ja, és a projekt tényleges 1800 mV alapértéke (a
        # dokumentált 2500 mV "minimum" ALATT) nem szabad, hogy hibát dobjon.
        spec = FIELD_REGISTRY["cell_terminate_voltage_mV"]
        assert spec.min_value is None
        assert spec.max_value is None
        encode_df_field(spec, 1800)  # nem dobhat ValueError-t

    def test_number_of_series_cells_rejects_zero(self):
        # TI Table 20: Number of Series Cells Min=1 (nem 0) — egy 0 sejtszám
        # fizikailag értelmetlen, de a U1 típus-tartomány (0..255) önmagában
        # átengedné.
        spec = FIELD_REGISTRY["number_of_series_cells"]
        with pytest.raises(ValueError, match="minimuma"):
            encode_df_field(spec, 0)
        encode_df_field(spec, 1)  # a dokumentált minimum még érvényes


class TestScf007JeitaBandFields:
    """SCF-007 (2026-09-10, felhasználói döntés): korábban CSAK a T1-T2
    (0-10°C) Cell Charge Voltage mező volt regisztrálva -- a projekt labor
    teszt-hőmérséklet ablaka (15-30°C) ténylegesen a T2-T3 (10-45°C) sávba
    esik. Mindhárom JEITA sáv (T1-T2/T2-T3/T3-T4) most regisztrálva van,
    forrás: SLUSB55B Table 20, subclass=48/offset=28/30/32."""

    def test_t1t2_field_offset_unchanged(self):
        spec = FIELD_REGISTRY["cell_charge_voltage_t1t2_mV"]
        assert (spec.subclass, spec.df_offset) == (48, 28)
        assert spec.field_type == DfFieldType.U2
        assert (spec.min_value, spec.max_value) == (0, 4600)

    def test_t2t3_field_registered_at_correct_offset(self):
        spec = FIELD_REGISTRY["cell_charge_voltage_t2t3_mV"]
        assert (spec.subclass, spec.df_offset) == (48, 30)
        assert spec.field_type == DfFieldType.U2
        assert (spec.min_value, spec.max_value) == (0, 4600)

    def test_t3t4_field_registered_at_correct_offset(self):
        spec = FIELD_REGISTRY["cell_charge_voltage_t3t4_mV"]
        assert (spec.subclass, spec.df_offset) == (48, 32)
        assert spec.field_type == DfFieldType.U2
        assert (spec.min_value, spec.max_value) == (0, 4600)

    def test_all_three_bands_share_the_same_ti_table20_min_max(self):
        """A TI Table 20 mindhárom sávra azonos Min=0/Max=4600-at
        dokumentál -- egy eltérő tartomány valamelyik mezőn átírási/
        kinyerési hibára utalna."""
        specs = [
            FIELD_REGISTRY[f"cell_charge_voltage_{band}_mV"]
            for band in ("t1t2", "t2t3", "t3t4")
        ]
        assert all(s.min_value == 0 and s.max_value == 4600 for s in specs)

    def test_all_three_bands_distinct_non_overlapping_offsets(self):
        """Mind subclass=48 (Configuration/Data), egymást követő, nem
        átfedő 2-bájtos (U2) offsetek — 28, 30, 32."""
        offsets = sorted(
            FIELD_REGISTRY[f"cell_charge_voltage_{band}_mV"].df_offset
            for band in ("t1t2", "t2t3", "t3t4")
        )
        assert offsets == [28, 30, 32]

    def test_t3t4_falls_in_a_different_32_byte_block_than_t1t2_and_t2t3(self):
        """[MAX verifier P3-E javítás, 2026-09-10] `df_offset // 32` miatt a
        T1-T2 (28) és T2-T3 (30) a 0. blokkba esik, DE a T3-T4 (32) MÁR az
        1. blokkba -- tehát a 3 sáv NEM egyetlen blokk-olvasással/-írással
        kezelhető. Ez fontos, ha valaha egy jövőbeli write_fields()-alapú
        egyszeri RMW-írást terveznének mindhárom sávra: az automatikusan 2
        csoportra bomlana (a modul dokumentált `partial_groups`
        szemantikájával), nem egyetlen atomi blokk-írásra."""
        t1t2 = FIELD_REGISTRY["cell_charge_voltage_t1t2_mV"]
        t2t3 = FIELD_REGISTRY["cell_charge_voltage_t2t3_mV"]
        t3t4 = FIELD_REGISTRY["cell_charge_voltage_t3t4_mV"]
        assert t1t2.block == t2t3.block == 0
        assert t3t4.block == 1


# ---------------------------------------------------------------------- #
# Read-modify-write helyesség (fő architekturális követelmény)             #
# ---------------------------------------------------------------------- #

class TestReadModifyWrite:
    def test_single_field_write_does_not_touch_other_bytes(self):
        initial = bytes(range(32))
        device = _MultiBlockDevice({(48, 0): initial})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        report = service.write_fields({"field_a": -100}, confirm=True)

        assert report.passed is True
        written = device.blocks[(48, 0)]
        expected = bytearray(initial)
        expected[2:4] = encode_df_field(_FIELD_A, -100)
        assert bytes(written) == bytes(expected)

    def test_two_fields_same_block_single_transaction(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        report = service.write_fields({"field_a": 10, "field_b": 2000}, confirm=True)

        assert report.passed is True
        assert device.write_calls == 1
        assert len(report.groups) == 1
        written = device.blocks[(48, 0)]
        assert decode_df_field(_FIELD_A, bytes(written)) == 10
        assert decode_df_field(_FIELD_B, bytes(written)) == 2000

    def test_fields_in_different_blocks_separate_transactions(self):
        device = _MultiBlockDevice({(48, 0): bytes(32), (36, 0): bytes(32)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        report = service.write_fields({"field_a": 5, "field_c": 7}, confirm=True)

        assert report.passed is True
        assert device.write_calls == 2
        assert len(report.groups) == 2
        assert decode_df_field(_FIELD_A, bytes(device.blocks[(48, 0)])) == 5
        assert device.blocks[(36, 0)][0] == 7

    def test_overlapping_fields_raise_before_any_call(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, serial = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(DfFieldSpecError, match="átfedi"):
            service.write_fields({"field_a": 1, "field_overlap": 2}, confirm=True)
        assert serial.written_requests == []
        assert device.write_calls == 0

    def test_evidence_contains_previous_and_requested_value(self):
        initial = bytearray(32)
        initial[2:4] = encode_df_field(_FIELD_A, 999)
        device = _MultiBlockDevice({(48, 0): bytes(initial)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        report = service.write_fields({"field_a": -5}, confirm=True)

        ev = report.groups[0].fields[0]
        assert ev.field_name == "field_a"
        assert ev.previous_value == 999
        assert ev.requested_value == -5


# ---------------------------------------------------------------------- #
# Olvasás                                                                  #
# ---------------------------------------------------------------------- #

class TestRead:
    def test_read_field_decodes_correct_offset(self):
        block = bytearray(32)
        block[2:4] = encode_df_field(_FIELD_A, -321)
        device = _MultiBlockDevice({(48, 0): bytes(block)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        result = service.read_field("field_a")
        assert result.value == -321
        assert result.field_name == "field_a"

    def test_read_fields_same_block_single_read(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, _serial = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        service.read_fields(["field_a", "field_b"])
        assert device.read_calls == 1

    def test_read_unknown_field_raises(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)
        with pytest.raises(DfFieldSpecError):
            service.read_field("no_such_field")


# ---------------------------------------------------------------------- #
# Előfeltétel-gate-ek transport-hívás előtt                                #
# ---------------------------------------------------------------------- #

class TestPreconditionGates:
    def test_confirm_false_raises_before_any_call(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, serial = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)
        with pytest.raises(ValueError, match="confirm=True"):
            service.write_fields({"field_a": 1}, confirm=False)
        assert serial.written_requests == []

    def test_unknown_field_name_raises_before_any_call(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, serial = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)
        with pytest.raises(DfFieldSpecError):
            service.write_fields({"no_such_field": 1}, confirm=True)
        assert serial.written_requests == []

    def test_non_writable_field_raises_before_any_call(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, serial = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)
        with pytest.raises(DfFieldSpecError, match="nem írható"):
            service.write_fields({"field_readonly": 1}, confirm=True)
        assert serial.written_requests == []

    def test_empty_values_raises(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)
        with pytest.raises(ValueError, match="legalább egy mező"):
            service.write_fields({}, confirm=True)

    def test_out_of_range_value_raises_before_any_call(self):
        """[regresszió, "3. csoport" backlog-tétel, CF-40 P2-2 gyökér-
        megelőzés, 2026-09-13] egyetlen, önmagában érvénytelen értékű
        mezőre is igaz, hogy a tartomány-ellenőrzés a transport-hívás
        ELŐTT fut -- ugyanaz az ELŐFELTÉTEL-szintű garancia, mint a
        többi testvér itt (unknown_field/non_writable/overlap)."""
        device = _MultiBlockDevice({(36, 0): bytes(32)})
        transport, serial = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)
        with pytest.raises(DfFieldSpecError, match="érvénytelen érték"):
            service.write_fields({"field_c": 999}, confirm=True)  # U1: 0-255
        assert serial.written_requests == []


# ---------------------------------------------------------------------- #
# Hiba-öröklődés a mögöttes pipeline-ból                                   #
# ---------------------------------------------------------------------- #

class TestErrorPropagation:
    def test_pre_read_transport_error_raises_df_field_write_error(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, serial = _make_transport(device)
        serial.errors["data_flash_block"] = ("TIMEOUT", "no response")
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(DfFieldWriteError):
            service.write_fields({"field_a": 1}, confirm=True)

    def test_pre_read_timeout_is_controlled_bq_write_error_subclass(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, serial = _make_transport(device)
        serial.simulate_no_response = True
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(ControlledBqWriteError):
            service.write_fields({"field_a": 1}, confirm=True)

    def test_pipeline_compare_failure_is_report_not_exception(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, _ = _make_transport(device)
        # A readback (WRITE UTÁNI olvasás) egy MÁSIK, a szándékolttól eltérő
        # tartalmat ad vissza -> COMPARE FAIL, de NEM kivétel.
        device.readback_override_after_write = b"\xaa" * 32
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        report = service.write_fields({"field_a": 1}, confirm=True)

        assert report.passed is False
        assert report.groups[0].block_write_result.stage_reached == "COMPARE"
        assert report.stopped_at == "subclass=48 block=0"

    def test_first_group_failure_stops_before_second_group_write(self):
        device = _MultiBlockDevice({(48, 0): bytes(32), (36, 0): bytes(32)})
        transport, _ = _make_transport(device)
        device.readback_override_after_write = b"\xaa" * 32  # az 1. WRITE utáni readback bukik
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        report = service.write_fields({"field_a": 1, "field_c": 2}, confirm=True)

        assert report.passed is False
        assert len(report.groups) == 1  # a 2. csoportra meg sem próbált írni
        assert device.write_calls == 1

    def test_invalid_value_on_second_group_blocks_first_group_write_entirely(self):
        """[regresszió, D9-utáni "3. csoport" backlog-tétel, CF-40 P2-2
        gyökér-megelőzés, 2026-09-13] Review-kör 2 (2026-09-08), HW2-09
        EREDETI fixje csak azt oldotta meg, hogy a MÁR megírt első
        csoport bizonyítéka ne vesszen el (`partial_groups`) -- de az
        első csoport MAGA a chipre íródott, MIELŐTT a második csoport
        érvénytelen értéke észlelhető lett volna (l. `device.write_calls
        == 1` az eredeti, MOST JAVÍTOTT tesztben). Ez ellentmondott a
        modul saját, ELŐFELTÉTEL-szintű szerződésének (DfFieldSpecError
        docstringje: "MINDEN transport-hívás ELŐTT észlelve"). Mostantól
        az ÖSSZES kért mező érték-tartománya ELŐRE validálódik -- egy
        érvénytelen érték a MÁSODIK csoportban az ELSŐ csoport írását is
        megakadályozza, NULLA transport-hívással."""
        device = _MultiBlockDevice({(48, 0): bytes(32), (36, 0): bytes(32)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(DfFieldSpecError, match="érvénytelen érték"):
            # field_a (subclass=48) önmagában érvényes lenne; field_c
            # (subclass=36) U1 (0-255) tartományon KÍVÜLI értéket kap --
            # az előfeltétel-ellenőrzés MOST mindkettőt a write-ciklus
            # ELŐTT nézi át, tehát field_a írása meg sem kezdődik.
            service.write_fields({"field_a": 1, "field_c": 999}, confirm=True)

        assert device.write_calls == 0  # sem field_a, sem field_c nem íródott

    def test_decode_failure_on_second_group_preserves_first_group_evidence(self):
        """L. test_encode_failure_on_second_group_preserves_first_group_evidence
        -- ugyanaz a mintázat, de `decode_df_field()` oldalán: egy olyan mező,
        aminek offset+hossza túlnyúlik a 32-bájtos blokk határán (df_offset=31,
        U2 -> end=33), a `decode_df_field()`-et 'malformed reply' hibával
        dobatja MEG A PRE-READ UTÁN, MIELŐTT bármi íródna a 2. csoportra."""
        boundary_crossing = DfFieldSpec(
            name="field_boundary", subclass=36, df_offset=31, field_type=DfFieldType.U2,
        )
        registry = dict(_TEST_REGISTRY, field_boundary=boundary_crossing)
        device = _MultiBlockDevice({(48, 0): bytes(32), (36, 0): bytes(32)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, registry)

        with pytest.raises(DfFieldWriteError) as exc_info:
            service.write_fields({"field_a": 1, "field_boundary": 5}, confirm=True)

        assert device.write_calls == 1  # az első csoport ténylegesen megíródott
        partial_groups = exc_info.value.partial_groups
        assert len(partial_groups) == 1
        assert partial_groups[0].subclass == 48
        assert partial_groups[0].passed is True

    def test_first_group_pre_read_failure_has_no_partial_groups(self):
        """Ha az ELSŐ (egyetlen korábban befejezett) csoportnál hibázik a
        PRE-READ, nincs mit megőrizni -- `partial_groups` üres lista, nem
        `None` (konzisztens API a hívók felé)."""
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, serial = _make_transport(device)
        serial.errors["data_flash_block"] = ("TIMEOUT", "no response")
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(DfFieldWriteError) as exc_info:
            service.write_fields({"field_a": 1}, confirm=True)

        assert exc_info.value.partial_groups == []

    def test_device_fw_mismatch_fails_before_write(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        report = service.write_fields(
            {"field_a": 1}, confirm=True, expected_device_type=0x9999,
        )

        assert report.passed is False
        assert report.groups[0].block_write_result.stage_reached == "VERIFY_DEVICE_FW"
        assert device.write_calls == 0


# ---------------------------------------------------------------------- #
# Evidence / traceability                                                  #
# ---------------------------------------------------------------------- #

class TestEvidence:
    def test_stale_read_not_detected_in_normal_case(self):
        device = _MultiBlockDevice({(48, 0): bytes(32)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        report = service.write_fields({"field_a": 1}, confirm=True)
        assert report.groups[0].stale_read_detected is False


# ---------------------------------------------------------------------- #
# Kör 21 F18 — HW21-01: SEALED-módú degenerált blokk kapuja               #
# (SOURCE_CONFLICTS.md SCF-011 "(a)" opció, felhasználói PROJECT_POLICY    #
# döntés 2026-09-20)                                                      #
# ---------------------------------------------------------------------- #

class TestKor21F18DegenerateSealedBlockGate:
    """[HW21-01, DFAM-001 sibling-omission] A `Bq34z110ReadOnlyDriver` öt olvasó
    metódusa 2026-09-11 óta elutasítja a HIL-igazolt SEALED-módú degenerált
    DF-blokkot (21×0x00+11×0xFF), de a `Bq34z110DataFlashService` HÁROM
    olvasási hívóhelye (`read_field()`, `read_fields()`, `write_fields()`
    PRE-READ) közvetlenül a transportot hívta, kapu NÉLKÜL.

    A konkrét, fizikai következmény, amit ez megakadályoz: egy degenerált
    `subclass=49 block=0` olvasás a `cell_bl_set_volt_mV`-t `0`-ra dekódolta,
    ami átment a CF-40 `min_value=0` előellenőrzésen, és a session végén
    `passed=True`-val került vissza a FIZIKAI chipbe."""

    # 32 bájt: 21×0x00 + 11×0xFF -- a `device_bq34z110.py` EGYETLEN
    # definíciójából importálva, hogy a teszt ne egy MÁSODIK, sodródásra
    # képes másolatot rögzítsen.
    DEGENERATE = DEGENERATE_SEALED_DF_BLOCK

    def test_read_field_rejects_degenerate_block(self):
        device = _MultiBlockDevice({(48, 0): self.DEGENERATE})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(InstrumentInvalidReading) as exc_info:
            service.read_field("field_a")

        assert "BQ-DF-SEALED-001," in str(exc_info.value)
        assert "subclass=48 " in str(exc_info.value)

    def test_read_fields_rejects_degenerate_block(self):
        device = _MultiBlockDevice({(48, 0): self.DEGENERATE})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(InstrumentInvalidReading):
            service.read_fields(["field_a", "field_b"])

    def test_write_fields_pre_read_rejects_degenerate_block_and_writes_nothing(self):
        """A HÁROM hívóhely közül EZ a legsúlyosabb: a `patched` bytearray a
        pre-read tartalmából indul, tehát egy degenerált pre-read a blokk
        MINDEN MÁS mezőjét is a degenerált mintával írná vissza."""
        device = _MultiBlockDevice({(48, 0): self.DEGENERATE})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(DfFieldWriteError) as exc_info:
            service.write_fields({"field_a": 1}, confirm=True)

        assert "PRE-READ sikertelen" in str(exc_info.value)
        assert "BQ-DF-SEALED-001," in str(exc_info.value)
        assert device.write_calls == 0, "degenerált pre-read után SEMMIT nem szabad írni"

    def test_write_fields_degenerate_second_group_preserves_partial_groups(self):
        """A kapu a MEGLÉVŐ PRE-READ hibaágon keresztül lép életbe, ezért a
        már befejezett csoportok bizonyítéka (`partial_groups`) megmarad --
        nem egy új, a hívók által nem ismert kivétel-alak szivárog ki."""
        device = _MultiBlockDevice({(48, 0): bytes(32), (36, 0): self.DEGENERATE})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        with pytest.raises(DfFieldWriteError) as exc_info:
            service.write_fields({"field_a": 1, "field_c": 2}, confirm=True)

        partial_groups = exc_info.value.partial_groups
        assert len(partial_groups) == 1
        assert partial_groups[0].subclass == 48
        assert partial_groups[0].passed is True

    # ------------------------------------------------------------------ #
    # ELLEN-TESZTEK: a kapu SZŰK, nem egy általános "minden gyanús blokk"  #
    # detektor -- SCF-011 explicit korlátai.                              #
    # ------------------------------------------------------------------ #

    def test_non_evidenced_subclass_is_not_rejected(self):
        """subclass 80 ugyanabban a HIL-dumpban MÁS, ismeretlen mintát adott --
        rá a kiterjesztés SOURCE_REQUIRED/HIL_PENDING (SCF-011). Egy ottani,
        a mintával véletlenül egyező blokk LEGITIM üres/törölt blokk is lehet,
        az elutasítás hamis pozitív lenne."""
        spec80 = DfFieldSpec(name="f80", subclass=80, df_offset=0, field_type=DfFieldType.U1)
        device = _MultiBlockDevice({(80, 0): self.DEGENERATE})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, {"f80": spec80})

        result = service.read_field("f80")
        assert result.value == 0  # nem dob -- a kapu NEM terjed ki ide

    def test_non_zero_block_is_not_rejected(self):
        """Bármely block!=0 szintén SOURCE_REQUIRED/HIL_PENDING (SCF-011) --
        a HIL-bizonyíték kizárólag block=0-ra szól."""
        spec_b1 = DfFieldSpec(name="f48b1", subclass=48, df_offset=32, field_type=DfFieldType.U1)
        device = _MultiBlockDevice({(48, 1): self.DEGENERATE})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, {"f48b1": spec_b1})

        assert spec_b1.block == 1
        result = service.read_field("f48b1")
        assert result.value == 0  # nem dob

    def test_almost_degenerate_block_is_not_rejected(self):
        """EXACT-MATCH kapu, nem heurisztika: egyetlen bájt eltérés már valós
        chip-tartalomnak minősül (MAX verifier P2-5 pontosítása, 2026-09-11)."""
        almost = bytearray(self.DEGENERATE)
        almost[0] = 0x01
        device = _MultiBlockDevice({(48, 0): bytes(almost)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        result = service.read_field("field_a")
        assert result.raw_block_hex == bytes(almost).hex()

    def test_normal_block_unaffected(self):
        """Regresszió-őr: a kapu a normál úton semmit nem változtat."""
        block = bytearray(32)
        block[2:4] = (1234).to_bytes(2, "big")
        device = _MultiBlockDevice({(48, 0): bytes(block)})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport, _TEST_REGISTRY)

        assert service.read_field("field_a").value == 1234

    def test_real_registry_cell_bl_set_volt_is_gated(self):
        """A HW21-01 finding KONKRÉT mezője a VALÓS `FIELD_REGISTRY`-vel
        (subclass=49, block=0) -- a kapu nélkül ez csendben `0`-t adott volna,
        amit a session vége a fizikai chipbe írt volna vissza."""
        spec = FIELD_REGISTRY["cell_bl_set_volt_mV"]
        assert (spec.subclass, spec.block) == (49, 0)
        assert decode_df_field(spec, self.DEGENERATE) == 0, (
            "a védelem nélküli dekódolás valóban 0-t adna -- ez a finding lényege"
        )

        device = _MultiBlockDevice({(49, 0): self.DEGENERATE})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport)  # VALÓS FIELD_REGISTRY

        with pytest.raises(InstrumentInvalidReading):
            service.read_field("cell_bl_set_volt_mV")


class TestR16B7EnergyFields:
    @pytest.mark.parametrize("name,subclass,offset,kind,raw,value", [
        ("design_energy_cWh", 48, 23, DfFieldType.I2, b"\x3e\xd0", 16080),
        ("design_energy_scale", 48, 45, DfFieldType.U1, b"\x0a", 10),
        ("pack_configuration", 64, 0, DfFieldType.U2, b"\x09\x61", 0x0961),
        ("load_select", 80, 0, DfFieldType.U1, b"\x01", 1),
        ("load_mode", 80, 1, DfFieldType.U1, b"\x00", 0),
    ])
    def test_ti_layout_and_literal_wire_bytes(self, name, subclass, offset, kind, raw, value):
        # SLUSB55B Rev.B Table 20 p39–41; a nyers fixture nem az encoderből származik.
        spec = FIELD_REGISTRY[name]
        assert (spec.subclass, spec.df_offset, spec.field_type) == (subclass, offset, kind)
        data = bytearray(32)
        data[offset % 32:offset % 32 + len(raw)] = raw
        assert decode_df_field(spec, bytes(data)) == value
        assert encode_df_field(spec, value) == raw

    @pytest.mark.parametrize("value", [0, 2, 9, 11])
    def test_scale_only_accepts_ti_enumeration_before_io(self, value):
        device = _MultiBlockDevice({})
        transport, _ = _make_transport(device)
        service = Bq34z110DataFlashService(transport)
        with pytest.raises(DfFieldSpecError):
            service.write_fields({"design_energy_scale": value}, confirm=True)
        assert (device.read_calls, device.write_calls) == (0, 0)

    def test_pack_configuration_cannot_be_overwritten(self):
        device = _MultiBlockDevice({})
        transport, _ = _make_transport(device)
        with pytest.raises(DfFieldSpecError, match="nem írható"):
            Bq34z110DataFlashService(transport).write_fields({"pack_configuration": 0}, confirm=True)
        assert (device.read_calls, device.write_calls) == (0, 0)

    def test_second_energy_block_failure_retains_evidence_and_stops(self, monkeypatch):
        from types import SimpleNamespace

        import Prog.src.bq_data_flash_service as service_module

        device = _MultiBlockDevice({})
        transport, _ = _make_transport(device)
        calls = []

        def controlled(transport, subclass, block, data32, **kwargs):
            calls.append((subclass, block))
            return SimpleNamespace(passed=len(calls) == 1, backup_bytes_hex=None)

        monkeypatch.setattr(service_module, "write_single_df_block_controlled", controlled)
        report = Bq34z110DataFlashService(transport).write_fields(
            {"design_energy_cWh": 16080, "design_energy_scale": 10, "load_mode": 0},
            confirm=True,
        )
        assert not report.passed
        assert calls == [(48, 0), (48, 1)]
        assert len(report.groups) == 2
        assert report.groups[0].passed
        assert not report.groups[1].passed
