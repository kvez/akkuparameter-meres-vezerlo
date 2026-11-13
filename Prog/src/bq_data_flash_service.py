"""
bq_data_flash_service.py — Block 1 (2026-08-24, Traditional Learning BQ34Z110
provisioning /goal): mező-szintű (nem csak nyers 32-bájtos blokk) read-modify-
write Data Flash szolgáltatás a battery-profile-ból származtatott BQ34Z110
mezőkhöz.

Forrás: Folyamatok/tervek/TRADITIONAL_LEARNING_BQ_PROVISIONING_AUDIT_2026-08-24.md
(DF FIELD MAP, TI SLUSB55B Table 20 + TF1_basic.c kereszt-ellenőrizve).

Architektúrális határ (CLAUDE.md 9. szakasz): ez a modul a bq_controlled_write.py
ALÁ épül — a write_single_df_block_controlled() teljes CONFIRM->IDENTIFY->
VERIFY DEVICE/FW->BACKUP->HASH->WRITE->READBACK->COMPARE->CHECKSUM->
STATUS_VERIFY pipeline-ját hívja, NEM duplikálja. ELŐFELTÉTEL: a hívónak MÁR
UNSEALED/FULL ACCESS állapotba kell hoznia a chipet, mielőtt write_fields()-et
hív — ez a réteg NEM hív automatikusan unseal()/full_access()/seal()-t
(ugyanaz az elv, mint write_single_df_block_controlled()-nél).

TOCTOU-korlát (dokumentált, nem megoldott): a write_fields() saját PRE-READ
olvasása és a mögöttes pipeline saját BACKUP-olvasása között technikailag
módosulhat a blokk tartalma — ezt HIL nélkül nem lehet kizárni. A
`stale_read_detected` mező ezt legalább láthatóvá teszi (nem állítja meg a
folyamatot — a végső COMPARE úgyis a ténylegesen írt tartalmat ellenőrzi).

Range-enforcement: encode_df_field() a TÍPUS ábrázolhatóságát (I2: -32768..
32767, U2: 0..65535, U1/H1: 0..255) ÉS -- ha a DfFieldSpec megadja -- a
TI-SLUSB55B Table 20 szerinti mezőnkénti Min/Max-ot is ellenőrzi.
FRISSÍTÉS (2026-09-08, governance-v2 review-kör 1, CF-40/BQ-DF-RANGE-001,
JAVÍTVA): a korábbi indoklás TÉVESEN általánosította MINDEN mezőre a Cell
Terminate Voltage min>default anomáliáját (SCF-001/BQ-DF-003); ténylegesen
csak EZ az egy mező (cell_terminate_voltage_mV, subclass=80/offset=67)
ellentmondásos a forrásban (Table 20: Min=2500 mV, Max=3700 mV, Default=1800
mV -- a dokumentált alapérték a dokumentált minimum ALATT van). A gauge NEM
utasít vissza tartományon kívüli írást, és a hibás érték "hardware failure"-t
okozhat -- l. BQ-DF-RANGE-001. (A perzisztencia/POR kérdésben a TI-SLUSB55B
forrás önmagával ellentmondásos -- "persistent" + "POR does resolve the
fault" egy mondaton belül --, ezt a CF-40 MAX verifier tárta fel 2026-09-08-
án; l. CLAIM_LEDGER.yaml BQ-DF-RANGE-001 P2-1 korrekció, nem enforce-olási
döntés alapja.) A `FIELD_REGISTRY`
összes többi mezőjének Min/Max-a forrással alátámasztott (önállóan, torzulás-
mentesen kinyerve: `pdftotext -f 25 -l 45 -table "Leírások/TI_BQ/
BQ34Z110PWR.pdf"`, ugyanazzal a `-table` móddal, amit a CF-41 MAX independent
verifier azonosított és amit a CF-40 fixere is használt) és MOST enforce-olva
van a `DfFieldSpec.min_value`/`max_value` opcionális mezőin keresztül. Kizárólag
`cell_terminate_voltage_mV` marad `min_value=None`/`max_value=None` (csak a
típus-tartomány érvényes rá) -- ez SZÁNDÉKOS, a SOURCE_CONFLICT (BQ-DF-003)
miatt, NEM hiányzó adat.

Cell BL/BH Set Volt Threshold: a mechanizmus (mező-regisztráció, encode/decode,
csoportos write) itt megvan, de a KONKRÉT, végleges érték SOURCE_REQUIRED
(audit SOURCE CONFLICT #2) — ezt a Block 3 (Cell BL tranzakciós kezelés)
dönti el, felhasználói jóváhagyással.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from Prog.drivers.device_bq34z110 import DEGENERATE_SEALED_DF_BLOCK
from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.bq_controlled_write import (
    ControlledBqWriteError,
    ControlledWriteResult,
    write_single_df_block_controlled,
)
from Prog.src.exceptions import InstrumentError, InstrumentInvalidReading

# [Kör 21 F18, HW21-01, PROJECT_POLICY döntés -- SOURCE_CONFLICTS.md SCF-011
# "Kiterjesztett záró mondat", (a) opció, felhasználói döntés 2026-09-20]
# Azok a (subclass, block=0) párok, amelyekre a SEALED-módú degenerált
# bájtminta HIL-EVIDENCIÁZOTT (CLAIM_LEDGER.yaml BQ-DF-SEALED-001: egyetlen
# valós COM10-dumpban mind a hat subclass block=0-ja BYTE-AZONOS mintát adott).
#
# A kapu SZÁNDÉKOSAN ERRE A HALMAZRA SZŰKÍTETT, nem minden olvasásra:
#   * subclass 80 (cell_terminate_voltage_mV) ugyanabban a HIL-dumpban MÁS,
#     ismeretlen mintát adott -- rá a kiterjesztés SCF-011 szerint TOVÁBBRA IS
#     SOURCE_REQUIRED/HIL_PENDING, ezért NINCS benne (egyébként is block=2);
#   * bármely block!=0 szintén SOURCE_REQUIRED/HIL_PENDING (a registryben ilyen
#     a subclass 48 / df_offset 32 mező, block=1) -- ezért a kapu a block==0
#     feltételt is megköveteli.
# Egy nem-evidenciázott (subclass, block) páron a minta elvi előfordulása egy
# LEGITIM, üres/törölt blokk is lehet: ott az elutasítás HAMIS POZITÍV lenne,
# ami egy írási tranzakciót indokolatlanul blokkolna. Ne bővítsd ezt a halmazt
# új HIL-bizonyíték és SCF-011-frissítés nélkül.
_HIL_EVIDENCED_DEGENERATE_DF_SUBCLASSES = frozenset({36, 48, 49, 64, 82, 88})


class DfFieldType(Enum):
    I2 = "I2"  # signed 16-bit, big-endian
    U2 = "U2"  # unsigned 16-bit, big-endian
    U1 = "U1"  # unsigned 8-bit
    H1 = "H1"  # unsigned 8-bit (státuszbájt, jellemzően csak olvasható)


_TYPE_RANGES: dict[DfFieldType, tuple[int, int]] = {
    DfFieldType.I2: (-32768, 32767),
    DfFieldType.U2: (0, 65535),
    DfFieldType.U1: (0, 255),
    DfFieldType.H1: (0, 255),
}


@dataclass(frozen=True)
class DfFieldSpec:
    """Egy battery-profile-eredetű (vagy egyéb) BQ34Z110 Data Flash mező
    leírása. `df_offset` a TI SLUSB55B Table 20 szerinti, SUBCLASS-RELATÍV
    offset (pl. Cell Terminate Voltage -> 67), NEM a blokkon belüli offset —
    a `block`/`offset_in_block` property-k számolják ki (67 // 32 = 2 blokk,
    67 % 32 = 3 blokkon belüli offset), hogy a hívónak sose kelljen kézzel
    számolnia."""

    name: str
    subclass: int
    df_offset: int
    field_type: DfFieldType
    writable: bool = True
    unit: str = ""
    source_note: str = ""
    min_value: int | None = None
    max_value: int | None = None

    @property
    def block(self) -> int:
        return self.df_offset // 32

    @property
    def offset_in_block(self) -> int:
        return self.df_offset % 32

    @property
    def byte_length(self) -> int:
        return 1 if self.field_type in (DfFieldType.U1, DfFieldType.H1) else 2


def decode_df_field(spec: DfFieldSpec, block32: bytes) -> int:
    """[malformed reply nem válhat csendben 0-vá — ugyanaz az elv, mint
    device_bq34z110.py::read_qmax()/read_update_status()]"""
    start = spec.offset_in_block
    end = start + spec.byte_length
    if len(block32) < end:
        raise InstrumentInvalidReading(
            f"Bq34z110DataFlashService: '{spec.name}' Data Flash blokk túl "
            f"rövid ({len(block32)} bájt, subclass={spec.subclass} "
            f"block={spec.block}) — malformed reply, nem 0"
        )
    raw = block32[start:end]
    if spec.field_type is DfFieldType.I2:
        return int.from_bytes(raw, byteorder="big", signed=True)
    if spec.field_type is DfFieldType.U2:
        return int.from_bytes(raw, byteorder="big", signed=False)
    return raw[0]


def encode_df_field(spec: DfFieldSpec, value: int) -> bytes:
    if spec.name == "design_energy_scale" and value not in (1, 10):
        raise ValueError("Design Energy Scale: csak 1 vagy 10 (SLUSB55B Rev.B p17)")
    lo, hi = _TYPE_RANGES[spec.field_type]
    if not (lo <= value <= hi):
        raise ValueError(
            f"encode_df_field: '{spec.name}' ({spec.field_type.value}) "
            f"értéktartománya {lo}..{hi}, kapott: {value}"
        )
    if spec.min_value is not None and value < spec.min_value:
        raise ValueError(
            f"encode_df_field: '{spec.name}' TI Table 20 szerinti minimuma "
            f"{spec.min_value}, kapott: {value}"
        )
    if spec.max_value is not None and value > spec.max_value:
        raise ValueError(
            f"encode_df_field: '{spec.name}' TI Table 20 szerinti maximuma "
            f"{spec.max_value}, kapott: {value}"
        )
    if spec.field_type is DfFieldType.I2:
        return value.to_bytes(2, byteorder="big", signed=True)
    if spec.field_type is DfFieldType.U2:
        return value.to_bytes(2, byteorder="big", signed=False)
    return bytes([value])


# Kör 19 D10/SRC19-07 (2026-09-18) PONTOSÍTÁS: a fejléc korábban azt
# állította, hogy minden mező source_note-ja a TRADITIONAL_LEARNING_BQ_
# PROVISIONING_AUDIT_2026-08-24.md "3. DF FIELD MAP" szakaszára hivatkozik
# -- ez ELAVULT állítás, CF-40 óta minden EGYES source_note közvetlenül
# a SLUSB55B Table 20-at idézi (pdftotext -f 25 -l 45 -table kinyeréssel),
# nem az auditot. Az eredeti audit-dokumentum továbbra is a KIINDULÓPONT
# volt, de a jelenlegi source_note-ok elsődleges hivatkozása a Table 20,
# nem az audit-sor -- l. lent minden egyes mező saját source_note-ja.
FIELD_REGISTRY: dict[str, DfFieldSpec] = {
    "design_energy_cWh": DfFieldSpec(
        name="design_energy_cWh", subclass=48, df_offset=23, field_type=DfFieldType.I2,
        unit="mWh/cWh", min_value=0, max_value=32767,
        source_note="SLUSB55B Rev.B p39 Table 20; p17 Table 10: Scale=10 mellett cWh (Q-9).",
    ),
    "design_energy_scale": DfFieldSpec(
        name="design_energy_scale", subclass=48, df_offset=45, field_type=DfFieldType.U1,
        min_value=1, max_value=10,
        source_note="SLUSB55B Rev.B p40 Table 20: csak 1 vagy 10; Q-9 elvárás 10.",
    ),
    "pack_configuration": DfFieldSpec(
        name="pack_configuration", subclass=64, df_offset=0, field_type=DfFieldType.U2,
        writable=False, unit="flags", min_value=0, max_value=65535,
        source_note="SLUSB55B Rev.B p20 Table 12, p40 Table 20: H2, X10 maszk 0x0080; csak olvasás.",
    ),
    "load_select": DfFieldSpec(
        name="load_select", subclass=80, df_offset=0, field_type=DfFieldType.U1,
        min_value=0, max_value=255,
        source_note="SLUSB55B Rev.B p41 Table 20; p17 Table 8/9. Alapérték 1.",
    ),
    "load_mode": DfFieldSpec(
        name="load_mode", subclass=80, df_offset=1, field_type=DfFieldType.U1,
        min_value=0, max_value=255,
        source_note="SLUSB55B Rev.B p41 Table 20; p16 Load Mode: 0=CC, 1=CP. Alapérték 0.",
    ),
    "design_capacity_mAh": DfFieldSpec(
        name="design_capacity_mAh", subclass=48, df_offset=21, field_type=DfFieldType.I2,
        unit="mAh", min_value=0, max_value=32767,
        source_note=(
            "SLUSB55B Table 20 (subclass=48/Data, offset=21, Min=0, Max=32767, "
            "Default=4000 mAh; önállóan kinyerve `pdftotext -f 25 -l 45 -table`, "
            "CF-40, governance-v2 2026-09-08). Forrás battery_profile.py oldalon: "
            "Q-9 PROJECT_POLICY: explicit C10_capacity_Ah, fallback nélkül, NEM nominal_capacity_Ah "
            "közvetlenül — l. audit 3.5 szakasz (FIAMM: 'Design Capacity = C10 kapacitás')."
        ),
    ),
    "qmax_cell0_initial_mAh": DfFieldSpec(
        name="qmax_cell0_initial_mAh", subclass=82, df_offset=0, field_type=DfFieldType.I2,
        unit="mAh", min_value=0, max_value=32767,
        source_note=(
            "SLUSB55B Table 20 (subclass=82/State, offset=0, Min=0, Max=32767, "
            "Default=4000 mAh — l. CLAIM_LEDGER.yaml BQ-QMAX-001, MAX independent "
            "verifier 2026-09-08 által `pdftotext -table` móddal torzulásmentesen "
            "megerősítve). Q-23 PROJECT_POLICY: kezdőérték = C20 (FG20721: 7200 mAh × p), a BQ learning "
            "felülírja -- ehhez DEVICE-SPECIFIC forrás is van (Kör 19 D10/SRC19-06, "
            "saját pdftotext-kinyerés, 2026-09-18): SLUSB55B (BQ34Z110PWR.pdf) "
            "nyomtatott 29. oldal, STEP 5 'Review and Modify the Data Flash Gas "
            "Gauging Configuration...': 'Qmax Cell 0: Start with the C-rate value "
            "of your battery.' A korábban itt idézett SLUA664 ('before an "
            "optimization cycle... must be set to datasheet capacity') bq34z100-"
            "családos, CSAK HÁTTÉR (nem device-specific) -- l. taper_current_mA "
            "azonos hivatkozású source_note-ja ugyanerről a megkülönböztetésről."
        ),
    ),
    "cell_design_voltage_mV": DfFieldSpec(
        name="cell_design_voltage_mV", subclass=48, df_offset=6, field_type=DfFieldType.I2,
        unit="mV", min_value=2000, max_value=5000,
        source_note=(
            "SLUSB55B Table 20 (subclass=48/Data, offset=6, Min=2000, Max=5000, "
            "Default=2000 mV; önállóan kinyerve `pdftotext -f 25 -l 45 -table`, "
            "CF-40, governance-v2 2026-09-08) — ólomsav cella, fix 2000mV "
            "(TF1_basic.c egyezik)."
        ),
    ),
    "cell_charge_voltage_t1t2_mV": DfFieldSpec(
        name="cell_charge_voltage_t1t2_mV", subclass=48, df_offset=28, field_type=DfFieldType.U2,
        unit="mV", min_value=0, max_value=4600,
        source_note=(
            "SLUSB55B Table 20 (subclass=48/Data, offset=28, 'Cell Charge Voltage "
            "T1-T2', Min=0, Max=4600, Default=2200 mV; önállóan kinyerve "
            "`pdftotext -f 25 -l 45 -table`, CF-40, governance-v2 2026-09-08) — a "
            "VALÓDI 'Charging Voltage' DF-mező (NEM SC36, l. audit SOURCE CONFLICT "
            "#1: TF1 saját doksija tévesen SC36-ra hivatkozik). Forrás: "
            "charge_voltage_per_cell_V (FIAMM boost 2.40V), FIAMM temperature-"
            "compensation formulával (-2,5 mV/cella/°C, 20°C referencia, "
            "Engineering_Manual_AGM_EN.txt 877-880/910-911. sor) a JEITA T1-T2 "
            "sáv (0-10°C, SLUSB55B Table 20 subclass=48/offset=37-40) KÖZÉPpontjára "
            "(5°C) számolva -- l. SCF-007/CLAIM_LEDGER.yaml BQ-JEITA-001, "
            "felhasználói döntés 2026-09-10 a sáv-közép reprezentatív pontról. "
            "Q-26/Q-44/Q-48 (2): a termekre is; a hideg oldali emelest a szigorubb felhasznaloi "
            "2,400 V/cella toltesi plafon vagja; az abszolut safety-limit 2,425 marad."
        ),
    ),
    "cell_charge_voltage_t2t3_mV": DfFieldSpec(
        name="cell_charge_voltage_t2t3_mV", subclass=48, df_offset=30, field_type=DfFieldType.U2,
        unit="mV", min_value=0, max_value=4600,
        source_note=(
            "SLUSB55B Table 20 (subclass=48/Data, offset=30, 'Cell Charge Voltage "
            "T2-T3', Min=0, Max=4600, Default=2450 mV; önállóan kinyerve "
            "`pdftotext -f 25 -l 45 -table`, 2026-09-10 SCF-007 újra-ellenőrzés). "
            "Ez a MŰKÖDÉSILEG releváns sáv (10-45°C) a projekt labor teszt-"
            "hőmérséklet ablakára (15-30°C, l. reference_normativ_kovetelmenyrendszer)"
            " -- a T2-T3 sáv tartalmazza a FIAMM 20°C referenciapontot, tehát nincs "
            "extra kompenzacio; a profilreferenciat a Q-26/Q-44/Q-48 szerinti "
            "2,400 V/cella plafon korlatozza. Az Op Config B JEITA-bit (gyari alap: engedelyezve) "
            "esetén ez a sáv aktiválódik a mért hőmérséklet 10-45°C közötti "
            "tartományában; ha a JEITA-bit ki lenne kapcsolva, EZ a sáv lenne az "
            "EGYETLEN érvényes érték minden hőmérsékleten (SLUSB55B Op Config B "
            "Table leírás, 'JEITA' bit: 'When not set, values in T2-T3 ranges... "
            "are used regardless of temperature')."
        ),
    ),
    "cell_charge_voltage_t3t4_mV": DfFieldSpec(
        name="cell_charge_voltage_t3t4_mV", subclass=48, df_offset=32, field_type=DfFieldType.U2,
        unit="mV", min_value=0, max_value=4600,
        source_note=(
            "SLUSB55B Table 20 (subclass=48/Data, offset=32, 'Cell Charge Voltage "
            "T3-T4', Min=0, Max=4600, Default=2350 mV; önállóan kinyerve "
            "`pdftotext -f 25 -l 45 -table`, 2026-09-10 SCF-007 újra-ellenőrzés). "
            "Forrás: charge_voltage_per_cell_V (FIAMM boost 2.40V), FIAMM "
            "temperature-compensation formulával a JEITA T3-T4 sáv (45-55°C) "
            "KÖZÉPpontjára (50°C) számolva -- l. cell_charge_voltage_t1t2_mV "
            "azonos hivatkozasu source_note-ja ugyanerrol az elvrol; a kozos "
            "profilformula Q-26/Q-44/Q-48 toltesi plafonja ebben a savban is ervenyes."
        ),
    ),
    "taper_current_mA": DfFieldSpec(
        name="taper_current_mA", subclass=36, df_offset=0, field_type=DfFieldType.I2,
        unit="mA", min_value=0, max_value=1000,
        source_note=(
            "SLUSB55B Table 20, subclass 36 (»Charge«, taper/termination "
            "mezőcsoport; a »Charge Termination« elnevezés bq34z100 "
            "(SLUSAU1C Table 8-1) nomenklatúra, csak háttér — l. F4-3, "
            "BQ-DF-001; VITATOTT — l. SCF-014 [Kör 19 D10/SRC19-08, "
            "2026-09-18: a szubclass=36 pontos nomenklatúrája nyitott "
            "REVIEW STOP, l. SOURCE_CONFLICTS.md SCF-014 -- ez a mutató NEM "
            "dönt egyik olvasat mellett sem, a subclass/offset/type/range "
            "azonos mindkét olvasat alatt, futásidő-semleges]), offset=0, "
            "Min=0, Max=1000, Default=100 mA; önállóan kinyerve "
            "`pdftotext -f 25 -l 45 -table`, CF-40, governance-v2 "
            "2026-09-08). Forrás: effective_taper_A (FIAMM: 0.03×C10)."
        ),
    ),
    "cell_terminate_voltage_mV": DfFieldSpec(
        name="cell_terminate_voltage_mV", subclass=80, df_offset=67, field_type=DfFieldType.I2,
        unit="mV",
        source_note=(
            "SLUSB55B Table 20. Forrás: terminate_voltage_per_cell_V (FIAMM 1.80V). "
            "SZÁNDÉKOSAN NINCS min_value/max_value beállítva (l. BQ-DF-003/"
            "SCF-001, CLAIM_LEDGER.yaml): a TI Table 20 ezt a mezőt Min=2500mV/"
            "Max=3700mV/Default=1800mV-ként dokumentálja — a dokumentált "
            "alapérték a dokumentált minimum ALATT van, tehát a forrás önmagával "
            "ellentmondásos. HIL_PENDING: vizuális PDF-ellenőrzés ajánlott HIL "
            "előtt, mielőtt bármelyik határ enforce-olásra kerülne."
        ),
    ),
    "number_of_series_cells": DfFieldSpec(
        name="number_of_series_cells", subclass=64, df_offset=7, field_type=DfFieldType.U1,
        unit="db", min_value=1, max_value=100,
        source_note=(
            "SLUSB55B Table 20 (subclass=64/Registers, offset=7, Min=1, Max=100, "
            "Default=6; önállóan kinyerve `pdftotext -f 25 -l 45 -table`, CF-40, "
            "governance-v2 2026-09-08) + TF1_basic.c-ből megerősítve (SC64/off7, "
            "BQ_SC_REG). Forrás: cell_count."
        ),
    ),
    "cell_bl_set_volt_mV": DfFieldSpec(
        name="cell_bl_set_volt_mV", subclass=49, df_offset=9, field_type=DfFieldType.I2,
        unit="mV", min_value=0, max_value=5000,
        source_note=(
            "SLUSB55B Table 20 (subclass=49/Discharge, offset=9, 'Cell BL Set "
            "Volt', Min=0, Max=5000, Default=1850 mV; önállóan kinyerve "
            "`pdftotext -f 25 -l 45 -table`, CF-40, governance-v2 2026-09-08) + "
            "TF1_basic.c (BQ_CELL_BL_VOLT=1950mV, aktív, readback-verify-jal "
            "ellenőrzött érték). A KONKRÉT, végleges Akkuteszter-érték (normál "
            "ÉS ideiglenes learning-küszöb) SOURCE_REQUIRED — l. audit SOURCE "
            "CONFLICT #2, Block 3 dönti el. Itt csak a mechanizmus (mező-"
            "regisztráció, encode/decode, csoportos write, MOST már Min/Max "
            "range-enforcement is) épül."
        ),
    ),
    "cell_bh_set_volt_mV": DfFieldSpec(
        name="cell_bh_set_volt_mV", subclass=49, df_offset=14, field_type=DfFieldType.I2,
        unit="mV", min_value=0, max_value=5000,
        source_note=(
            "SLUSB55B Table 20 (subclass=49/Discharge, offset=14, 'Cell BH Set "
            "Volt', Min=0, Max=5000, Default=2450 mV; önállóan kinyerve "
            "`pdftotext -f 25 -l 45 -table`, CF-40, governance-v2 2026-09-08) + "
            "TF1_basic.c (BQ_CELL_BH_VOLT=2500mV, aktív érték). L. "
            "cell_bl_set_volt_mV megjegyzés."
        ),
    ),
    # UpdateStatus SZÁNDÉKOSAN KIMARAD: már lefedi
    # Bq34z110ReadOnlyDriver.read_update_status()/bq_diagnostics.py — nem
    # duplikáljuk a felelősséget, és SUP03-LEARN-007 szerint ez a mező
    # egyébként sem battery-profile-eredetű, csak a BQ saját folyamata
    # állíthatja.
}


@dataclass
class DfFieldReadResult:
    field_name: str
    spec: DfFieldSpec
    value: int
    raw_block_hex: str


@dataclass
class DfFieldWriteEvidence:
    field_name: str
    spec: DfFieldSpec
    previous_value: int
    requested_value: int


@dataclass
class DfBlockGroupWriteResult:
    subclass: int
    block: int
    fields: list[DfFieldWriteEvidence]
    pre_read_block_hex: str
    patched_block_hex: str
    stale_read_detected: bool
    block_write_result: ControlledWriteResult
    passed: bool


@dataclass
class DfFieldsWriteReport:
    passed: bool
    groups: list[DfBlockGroupWriteResult] = field(default_factory=list)
    requested_fields: list[str] = field(default_factory=list)
    stopped_at: str | None = None


class DfFieldSpecError(ValueError):
    """Ismeretlen mezőnév, write-tiltott (writable=False) mezőre írási
    kísérlet, két kért mező bájt-tartományának átfedése, vagy (CF-40 P2-2,
    2026-09-13) egy kért érték Min/Max/type-range sértése — ELŐFELTÉTEL-
    szintű hiba, MINDEN transport-hívás ELŐTT észlelve (semmi nem íródik)."""


class DfFieldWriteError(ControlledBqWriteError):
    """A write_fields() ALATT hívott write_single_df_block_controlled() egy
    WRITE ELŐTTI (PRE-READ/IDENTIFY/BACKUP) lépése hibázott a transport
    szintjén — a WRITE MEG SEM TÖRTÉNT. ÖRÖKÖL ControlledBqWriteError-ból,
    hogy egy meglévő `except ControlledBqWriteError` (pl. jövőbeli
    BqLearningProvisioner) változtatás nélkül elkapja ezt is.

    Review-kör 2 (2026-09-08), HW2-09 (CF-40 P2-2 kiegészítő szempont):
    `partial_groups` -- ha ez a hiba a `write_fields()` egy TÖBB csoportot
    érintő hívása közben, NEM az első csoportnál történt (pl. `decode_df_
    field()`/`encode_df_field()` a MÁSODIK csoportnál dob, miután az ELSŐ
    csoport írása MÁR sikeresen lezajlott a chipen), a korábban sikeresen
    befejezett csoportok `DfBlockGroupWriteResult` bizonyítéka itt NEM
    veszik el -- a kivétel maga hordozza tovább, a hívó (ha érdekli)
    `except DfFieldWriteError as exc: exc.partial_groups` formában érheti
    el. Alapértelmezett üres lista, ha nem volt korábban befejezett
    csoport (pl. az ELSŐ csoportnál hibázott) -- ez a leggyakoribb eset a
    két mai éles hívóhelynél (traditional_learning_controller.py), amik
    mindig pontosan egy mezőt/csoportot írnak."""

    def __init__(
        self, message: str, *, partial_groups: list[DfBlockGroupWriteResult] | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_groups: list[DfBlockGroupWriteResult] = (
            list(partial_groups) if partial_groups else []
        )


class Bq34z110DataFlashService:
    """Mező-szintű read-modify-write DF-szolgáltatás. ELŐFELTÉTEL (mint
    write_single_df_block_controlled()): a hívó MÁR UNSEALED/FULL ACCESS
    állapotba hozta a chipet — ez a réteg NEM hív unseal()/full_access()/
    seal()-t automatikusan."""

    def __init__(
        self,
        transport: PicoBQTransport,
        field_registry: dict[str, DfFieldSpec] | None = None,
    ) -> None:
        self._transport = transport
        self._registry = field_registry if field_registry is not None else FIELD_REGISTRY

    def get_field_spec(self, name: str) -> DfFieldSpec:
        spec = self._registry.get(name)
        if spec is None:
            raise DfFieldSpecError(f"Bq34z110DataFlashService: ismeretlen mező: {name!r}")
        return spec

    def _reject_degenerate_sealed_df_block(
        self, block: bytes, *, subclass: int, block_id: int, context: str,
    ) -> None:
        """[Kör 21 F18, HW21-01, DFAM-001] A `Bq34z110ReadOnlyDriver` öt olvasó
        metódusa 2026-09-11 óta elutasítja a SEALED-módú degenerált DF-blokkot,
        DE ez a szolgáltatás egy MÁSODIK, önállóan karbantartott olvasási út,
        amely KÖZVETLENÜL a transportot hívja -- a kapu ide sosem került át
        (klasszikus sibling-omission). Következmény, amíg hiányzott: egy
        degenerált `subclass=49 block=0` olvasás a `cell_bl_set_volt_mV` mezőt
        csendben `0`-ra dekódolta, ez átment a CF-40 `min_value=0`
        visszaállíthatósági előellenőrzésen, és a session végén `passed=True`-val
        került vissza a FIZIKAI chipbe -- a readback-verify csak azt igazolja,
        hogy a `0` HELYESEN íródott ki, nem azt, hogy a `0` HELYES érték volt.

        A kapu hatóköre PROJECT_POLICY döntés (SCF-011, (a) opció) --
        l. `_HIL_EVIDENCED_DEGENERATE_DF_SUBCLASSES` fenti kommentjét.
        """
        if block_id != 0 or subclass not in _HIL_EVIDENCED_DEGENERATE_DF_SUBCLASSES:
            return
        if block != DEGENERATE_SEALED_DF_BLOCK:
            return
        raise InstrumentInvalidReading(
            f"Bq34z110DataFlashService: {context} a HIL-igazolt SEALED-módú "
            f"degenerált Data Flash mintát adta vissza (21×0x00+11×0xFF, "
            f"subclass={subclass} block={block_id}) — l. CLAIM_LEDGER.yaml "
            f"BQ-DF-SEALED-001, SOURCE_CONFLICTS.md SCF-011. Ez NEM valós "
            f"chip-tartalom (SLUSB55B: SEALED módban a DataFlashBlock() "
            f"Manufacturer Information Block-ra vált át), a dekódolt érték "
            f"NEM használható fel sem kiírásra, sem visszaállításra — a chip "
            f"UNSEAL-állapotát ellenőrizni kell."
        )

    def read_field(self, name: str) -> DfFieldReadResult:
        spec = self.get_field_spec(name)
        block32 = self._transport.read_data_flash_block(spec.subclass, spec.block)
        self._reject_degenerate_sealed_df_block(
            block32, subclass=spec.subclass, block_id=spec.block,
            context=f"read_field({name!r})",
        )
        value = decode_df_field(spec, block32)
        return DfFieldReadResult(
            field_name=name, spec=spec, value=value, raw_block_hex=block32.hex(),
        )

    def read_fields(self, names: Iterable[str]) -> dict[str, DfFieldReadResult]:
        names = list(names)
        specs = {name: self.get_field_spec(name) for name in names}
        block_cache: dict[tuple[int, int], bytes] = {}
        results: dict[str, DfFieldReadResult] = {}
        for name in names:
            spec = specs[name]
            key = (spec.subclass, spec.block)
            if key not in block_cache:
                fresh_block = self._transport.read_data_flash_block(*key)
                # A kapu közvetlenül a friss olvasás után, a cache-be írás
                # ELŐTT fut -- degenerált blokk így nem kerül a cache-be.
                self._reject_degenerate_sealed_df_block(
                    fresh_block, subclass=spec.subclass, block_id=spec.block,
                    context=f"read_fields({name!r})",
                )
                block_cache[key] = fresh_block
            block32 = block_cache[key]
            results[name] = DfFieldReadResult(
                field_name=name, spec=spec,
                value=decode_df_field(spec, block32), raw_block_hex=block32.hex(),
            )
        return results

    def write_fields(
        self,
        values: dict[str, int],
        *,
        confirm: bool,
        expected_device_type: int | None = None,
        expected_fw_version: int | None = None,
    ) -> DfFieldsWriteReport:
        if not confirm:
            raise ValueError("Bq34z110DataFlashService.write_fields() requires confirm=True")
        if not values:
            raise ValueError("write_fields: legalább egy mező kötelező")

        requested_fields = list(values.keys())

        # --- ELŐFELTÉTEL-ELLENŐRZÉSEK, minden transport-hívás előtt ---
        specs: dict[str, DfFieldSpec] = {}
        for name in requested_fields:
            spec = self.get_field_spec(name)
            if not spec.writable:
                raise DfFieldSpecError(
                    f"Bq34z110DataFlashService: '{name}' nem írható (writable=False)"
                )
            specs[name] = spec

        # --- csoportosítás (subclass, block) szerint, a values dict
        # beszúrási sorrendjét megőrizve (első előfordulás dönti a csoport
        # feldolgozási sorrendjét) ---
        groups: dict[tuple[int, int], list[str]] = {}
        for name in requested_fields:
            spec = specs[name]
            groups.setdefault((spec.subclass, spec.block), []).append(name)

        # --- átfedés-ellenőrzés csoportonként, MINDEN transport-hívás előtt ---
        for (subclass, block), field_names in groups.items():
            occupied: list[tuple[int, int, str]] = []
            for name in field_names:
                spec = specs[name]
                start, end = spec.offset_in_block, spec.offset_in_block + spec.byte_length
                for o_start, o_end, o_name in occupied:
                    if start < o_end and o_start < end:
                        raise DfFieldSpecError(
                            f"Bq34z110DataFlashService: '{name}' és '{o_name}' "
                            f"bájt-tartománya átfedi egymást subclass={subclass} "
                            f"block={block}-ban"
                        )
                occupied.append((start, end, name))

        # --- érték-tartomány ellenőrzés, MINDEN transport-hívás előtt
        # (CF-40 P2-2, l. DfFieldWriteError docstringje és
        # DECISION_REGISTER.md -- a "3. csoport" backlog-tétel): az
        # encode_df_field() Min/Max/type-range ellenőrzése korábban
        # KIZÁRÓLAG a lenti csoportonkénti write-ciklus BELÜL futott --
        # egy TÖBB csoportot érintő híváskor a MÁSODIK (vagy további)
        # csoport érvénytelen értéke esetén az ELSŐ csoport ekkorra MÁR
        # ki volt írva az eszközre, mielőtt a hiba észlelhető lett volna
        # -- ellentmondva a modul saját, ELŐFELTÉTEL-szintű szerződésének
        # (l. DfFieldSpecError docstringje: "MINDEN transport-hívás ELŐTT
        # észlelve"). Itt az encode_df_field() tisztán validál (az
        # eredmény bytes-ját elvetjük) -- a tényleges patch-elés a lenti
        # ciklusban, a pre-read blokk felett, változatlanul megtörténik.
        # Ma production-ben nem elérhető út (a két éles hívóhely
        # mindig egy mezőt/csoportot ír), de egy jövőbeli, több mezős
        # híváskor (pl. BqLearningProvisioner) ez a gyökér-megelőzés.
        for name in requested_fields:
            try:
                encode_df_field(specs[name], values[name])
            except ValueError as exc:
                raise DfFieldSpecError(
                    f"Bq34z110DataFlashService: '{name}' érvénytelen érték: {exc}"
                ) from exc

        # --- csoportonkénti read-modify-write ---
        group_results: list[DfBlockGroupWriteResult] = []
        overall_passed = True
        stopped_at: str | None = None

        for (subclass, block), field_names in groups.items():
            try:
                pre_read = self._transport.read_data_flash_block(subclass, block)
                # [Kör 21 F18, HW21-01] A HÁROM olvasási hívóhely közül EZ a
                # legsúlyosabb: a `patched` bytearray a pre-read tartalmából
                # indul, tehát egy degenerált pre-read esetén a blokk MINDEN
                # MÁS mezője is a degenerált mintával íródna vissza a fizikai
                # chipbe -- nem csak az éppen írt mező romlana el. A kapu
                # SZÁNDÉKOSAN a `try`-on BELÜL van: az `InstrumentInvalidReading`
                # az `InstrumentError` leszármazottja, így a meglévő PRE-READ
                # hibaág alakítja `DfFieldWriteError`-rá, megőrizve a már
                # befejezett csoportok `partial_groups` bizonyítékát.
                self._reject_degenerate_sealed_df_block(
                    pre_read, subclass=subclass, block_id=block,
                    context="write_fields PRE-READ",
                )
            except InstrumentError as exc:
                raise DfFieldWriteError(
                    f"Bq34z110DataFlashService: PRE-READ sikertelen "
                    f"subclass={subclass} block={block}: {exc}",
                    partial_groups=group_results,
                ) from exc

            patched = bytearray(pre_read)
            evidence: list[DfFieldWriteEvidence] = []
            # Review-kör 2 (2026-09-08), HW2-09 (CF-40 P2-2 kiegészítő
            # szempont): `decode_df_field()`/`encode_df_field()` MINDKETTŐ
            # dobhat (malformed/túl rövid blokk, ill. tartományon kívüli
            # érték) -- korábban ez a ciklus nem volt védve, tehát egy
            # MÁSODIK (vagy további) csoportnál bekövetkező hiba a MÁR
            # sikeresen befejezett KORÁBBI csoportok `group_results`
            # bizonyítékát elveszítette volna (a kivétel a return elé
            # száll ki). A `DfFieldWriteError` most `partial_groups`-ban
            # hordozza tovább az addig befejezett csoportokat -- ugyanaz
            # az elv, mint a fenti PRE-READ hibaágnál.
            try:
                for name in field_names:
                    spec = specs[name]
                    previous_value = decode_df_field(spec, bytes(pre_read))
                    requested_value = values[name]
                    encoded = encode_df_field(spec, requested_value)
                    patched[spec.offset_in_block:spec.offset_in_block + spec.byte_length] = encoded
                    evidence.append(DfFieldWriteEvidence(
                        field_name=name, spec=spec,
                        previous_value=previous_value, requested_value=requested_value,
                    ))
            # MAX verifier follow-up (P3-3): `DfFieldSpecError` is a
            # `ValueError` subclass -- ez a bare `ValueError` MOST ártalmatlan
            # (a get_field_spec()/writable/overlap ellenőrzések a fenti
            # ELŐFELTÉTEL-szakaszban, ezen try előtt lezajlanak), de egy
            # jövőbeli módosítás, ami egy spec-lekérdezést IDE mozgatna, ezt
            # a hibát is csendben `DfFieldWriteError`-ré alakítaná át.
            except (InstrumentInvalidReading, ValueError) as exc:
                raise DfFieldWriteError(
                    f"Bq34z110DataFlashService: decode/encode sikertelen "
                    f"subclass={subclass} block={block}: {exc}",
                    partial_groups=group_results,
                ) from exc

            try:
                block_write_result = write_single_df_block_controlled(
                    self._transport, subclass, block, bytes(patched),
                    confirm=True,
                    expected_device_type=expected_device_type,
                    expected_fw_version=expected_fw_version,
                )
            except ControlledBqWriteError as exc:
                raise DfFieldWriteError(
                    f"Bq34z110DataFlashService: write_fields subclass={subclass} "
                    f"block={block} mezők={field_names}: {exc}",
                    partial_groups=group_results,
                ) from exc

            stale_read_detected = (
                block_write_result.backup_bytes_hex is not None
                and block_write_result.backup_bytes_hex != pre_read.hex()
            )

            group_result = DfBlockGroupWriteResult(
                subclass=subclass, block=block, fields=evidence,
                pre_read_block_hex=pre_read.hex(),
                patched_block_hex=bytes(patched).hex(),
                stale_read_detected=stale_read_detected,
                block_write_result=block_write_result,
                passed=block_write_result.passed,
            )
            group_results.append(group_result)

            if not block_write_result.passed:
                overall_passed = False
                stopped_at = f"subclass={subclass} block={block}"
                break

        return DfFieldsWriteReport(
            passed=overall_passed, groups=group_results,
            requested_fields=requested_fields, stopped_at=stopped_at,
        )


__all__ = [
    "FIELD_REGISTRY",
    "Bq34z110DataFlashService",
    "DfBlockGroupWriteResult",
    "DfFieldReadResult",
    "DfFieldSpec",
    "DfFieldSpecError",
    "DfFieldType",
    "DfFieldWriteError",
    "DfFieldWriteEvidence",
    "DfFieldsWriteReport",
    "decode_df_field",
    "encode_df_field",
]
