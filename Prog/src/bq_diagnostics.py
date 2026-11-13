"""
bq_diagnostics.py — WP-AK-07 (BQ Diagnostics GUI + közös session logging):
tiszta (GUI-független), egyetlen "diagnosztikai pillanatkép" (snapshot)
összeállítása a Bq34z110ReadOnlyDriver felett.

Kapu (WP-AK-07 Konszolidált terv, szó szerint): "BQ disconnect / stale data
/ malformed reply nem válhat csendesen 0 értékké." Ezért `poll_bq_diagnostics()`
minden mezőt egyenként, egymástól függetlenül próbál olvasni, és minden
egyes mezőt egy `BqFieldReading`-be csomagol (value/valid/error/timestamp) —
egyetlen mező sikertelen olvasása SEM dönti meg a teljes snapshotot (ugyanaz
az elv, mint `InstrumentManager.read_all()`-nál), és egyetlen sikertelen
olvasás SEM ad vissza numerikus 0-t "sikeres" értékként: a `value` None
marad, `valid=False`, `error` a kivétel szövege.

Bitmezők forrása (Leírások/TI_BQ/BQ34Z110PWR.pdf, SLUSB55B — l. a fenti
"F5-1" hivatkozásokkal egyező, review-kör 5 forráshivatkozás-frissítés,
DFAM-016 6. előfordulás: az eredeti idézet egy azóta tisztán INDEX-szé
alakult TUDÁSBÁZIS.txt-re hivatkozott feloldhatatlan sorszámmal):
  - Flags() (STD_CMD_FLAGS): "Table 6. Flags Bit Definitions". Low byte
    bit7=OCVTAKEN, bit6=ISD, bit5=TDD, bit2=SOC1, bit1=SOCF, bit0=DSG;
    High byte bit7=OTC, bit6=OTD, bit5=BATHIGH, bit4=BATLOW, bit3=CHG_INH,
    bit1=FC, bit0=CHG (bit3/2 low byte és bit2 high byte RSVD).
  - CONTROL_STATUS() (MAC_CONTROL_STATUS): "Table 4. CONTROL_STATUS
    Flags". Low byte bit0=QEN, bit1=VOK, bit2=RUP_DIS, bit3=LDMD,
    bit4=SLEEP, bit5=FULLSLEEP; High byte bit1=CSV, bit2=BCA, bit3=CCA,
    bit4=CALMODE, bit5=SS, bit6=FAS. **FONTOS**: QEN/VOK/RUP_DIS a
    CONTROL_STATUS()-hoz tartozik, NEM a Flags()-hez, annak ellenére, hogy a
    WP-AK-07 Konszolidált terv GUI-listája egy közös csoportban sorolja fel
    OCVTAKEN-nel (ami viszont Flags()-ből jön) — ezt a `kb-search` skill-lel
    eredetileg a TUDÁSBÁZIS.txt-ben kereszt-ellenőriztük, ugyanabban a
    szellemben, mint a WP-AK-06 regiszter-cím kereszt-ellenőrzése.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, fields
from typing import Callable, Generic, TypeVar

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_consistency_gate import ProvisioningConsistencyReport
from Prog.src.exceptions import TestAbortedByUser

T = TypeVar("T")


@dataclass
class BqFieldReading(Generic[T]):
    """Egyetlen diagnosztikai mező eredménye — SOHA nem numerikus 0
    "hiba" jelzésére, mindig `valid=False` + `error` szöveg."""

    value: T | None
    valid: bool
    error: str | None
    timestamp_monotonic: float

    def age_s(self, now: float | None = None) -> float:
        """Mennyi ideje frissült ez a mező (freshness) — a hívó (GUI) dönti
        el, mekkora kor számít "elavultnak" (stale)."""
        return (now if now is not None else time.monotonic()) - self.timestamp_monotonic


@dataclass
class FlagsBits:
    """Flags() (STD_CMD_FLAGS) bitenkénti dekódolása — l. modul docstring."""

    dsg: bool
    socf: bool
    soc1: bool
    tdd: bool
    isd: bool
    ocvtaken: bool
    chg: bool
    fc: bool
    chg_inh: bool
    batlow: bool
    bathigh: bool
    otd: bool
    otc: bool


@dataclass
class ControlStatusBits:
    """CONTROL_STATUS() (MAC_CONTROL_STATUS) bitenkénti dekódolása — l.
    modul docstring. SS/FAS = "security" állapot nyers bitjei (a WP-AK-07
    terv "security" GUI-mezője) — szándékosan NEM összevonva egyetlen
    SEALED/UNSEALED/FULL_ACCESS enum-má, mert a datasheet-kivonat FAS
    definíciója ("FULL ACCESS SEALED") nem elég egyértelmű ehhez a
    kb-search forráshoz képest — a nyers bitek megjelenítése biztonságosabb,
    mint egy nem eléggé forrásolt interpretáció."""

    qen: bool
    vok: bool
    rup_dis: bool
    ldmd: bool
    sleep: bool
    fullsleep: bool
    csv: bool
    bca: bool
    cca: bool
    calmode: bool
    ss: bool
    fas: bool


def decode_flags(word: int) -> FlagsBits:
    return FlagsBits(
        dsg=bool(word & 0x0001),
        socf=bool(word & 0x0002),
        soc1=bool(word & 0x0004),
        tdd=bool(word & 0x0020),
        isd=bool(word & 0x0040),
        ocvtaken=bool(word & 0x0080),
        chg=bool(word & 0x0100),
        fc=bool(word & 0x0200),
        chg_inh=bool(word & 0x0800),
        batlow=bool(word & 0x1000),
        bathigh=bool(word & 0x2000),
        otd=bool(word & 0x4000),
        otc=bool(word & 0x8000),
    )


def decode_control_status(word: int) -> ControlStatusBits:
    return ControlStatusBits(
        qen=bool(word & 0x0001),
        vok=bool(word & 0x0002),
        rup_dis=bool(word & 0x0004),
        ldmd=bool(word & 0x0008),
        sleep=bool(word & 0x0010),
        fullsleep=bool(word & 0x0020),
        csv=bool(word & 0x0200),
        bca=bool(word & 0x0400),
        cca=bool(word & 0x0800),
        calmode=bool(word & 0x1000),
        ss=bool(word & 0x2000),
        fas=bool(word & 0x4000),
    )


@dataclass
class BqDiagnosticsSnapshot:
    """Egy teljes diagnosztikai lekérdezés eredménye — minden mező külön
    BqFieldReading-be csomagolva, l. modul docstring kapu-szabálya."""

    device_type: BqFieldReading[int]
    fw_version: BqFieldReading[int]
    hw_version: BqFieldReading[int]
    chem_id: BqFieldReading[int]
    control_status: BqFieldReading[int]
    voltage_mV: BqFieldReading[int]
    avg_current_mA: BqFieldReading[int]
    temperature_C: BqFieldReading[float]
    soc_percent: BqFieldReading[int]
    remaining_capacity_mAh: BqFieldReading[int]
    full_charge_capacity_mAh: BqFieldReading[int]
    flags: BqFieldReading[int]
    qmax: BqFieldReading[int]
    update_status: BqFieldReading[int]
    ra_table_mohm: BqFieldReading[list]
    # WP-AK-10 (SUP03-LEARN-002 gauge log kiegészítés) — additív, a meglévő
    # mezők/fogyasztók (pl. BqDiagnosticsPanel) változatlanok maradnak.
    cycle_count: BqFieldReading[int]
    dod0: BqFieldReading[int]
    avg_i_last_run_mA: BqFieldReading[int]
    avg_p_last_run: BqFieldReading[int]
    # Block 5 (2026-08-25, SUP03-LEARN-002): OCVTAKEN/VOK/QEN/RUP_DIS
    # kötelező gauge-log mezők. A `flags`/`control_status` nyers szavából
    # dekódolva EGY helyen (poll_bq_diagnostics()), a `decode_flags()`/
    # `decode_control_status()` már meglévő logikájával — nem duplikálva
    # a 3 korábbi, egymástól független decode-hívási helyet (GUI panel,
    # TraditionalLearningController._check_relax_bq_acceptance()). Default
    # értékkel (nem-olvasott/érvénytelen), hogy a meglévő közvetlen
    # BqDiagnosticsSnapshot(...) konstruktor-hívások (tesztek) ne törjenek.
    ocvtaken: BqFieldReading[bool] = field(
        default_factory=lambda: BqFieldReading(value=None, valid=False, error="not computed", timestamp_monotonic=0.0)
    )
    vok: BqFieldReading[bool] = field(
        default_factory=lambda: BqFieldReading(value=None, valid=False, error="not computed", timestamp_monotonic=0.0)
    )
    qen: BqFieldReading[bool] = field(
        default_factory=lambda: BqFieldReading(value=None, valid=False, error="not computed", timestamp_monotonic=0.0)
    )
    rup_dis: BqFieldReading[bool] = field(
        default_factory=lambda: BqFieldReading(value=None, valid=False, error="not computed", timestamp_monotonic=0.0)
    )
    # R16-energy-04/F1: az Avg P nyers I2 marad. A skálakontextus a B7
    # PRECOND session-eseményében szerepel; a poll nem olvassa újra.
    design_energy_scale: BqFieldReading[int] = field(
        default_factory=lambda: BqFieldReading(value=None, valid=False, error="not read", timestamp_monotonic=0.0)
    )
    pack_configuration_x10: BqFieldReading[int] = field(
        default_factory=lambda: BqFieldReading(value=None, valid=False, error="not read", timestamp_monotonic=0.0)
    )


def poll_bq_diagnostics(driver: Bq34z110ReadOnlyDriver) -> BqDiagnosticsSnapshot:
    """Egyszeri, teljes diagnosztikai lekérdezés — minden mezőt külön
    try/except-be csomagol, hogy egy transport-szintű hiba (disconnect,
    timeout, malformed reply — l. `Bq34z110ReadOnlyDriver.read_qmax()`/
    `read_update_status()` WP-AK-07 kapu-javítása) csak AZT az egy mezőt
    tegye érvénytelenné, ne a teljes snapshotot dobja el."""

    def _read(fn: Callable[[], T]) -> BqFieldReading[T]:
        ts = time.monotonic()
        try:
            value = fn()
        except TestAbortedByUser:
            raise
        except Exception as exc:  # noqa: BLE001 - szándékosan széles: bármely
            # transport/driver-szintű hiba (disconnect, timeout, malformed
            # reply) egyetlen mezőt érvénytelenít, nem a teljes snapshotot.
            return BqFieldReading(value=None, valid=False, error=str(exc), timestamp_monotonic=ts)
        return BqFieldReading(value=value, valid=True, error=None, timestamp_monotonic=ts)

    flags = _read(driver.read_flags)
    control_status = _read(driver.read_control_status)

    def _bit(source: BqFieldReading[int], extract: Callable[[int], bool]) -> BqFieldReading[bool]:
        # Block 5 (2026-08-25) — a bit a forrás-mező (flags/control_status)
        # valid/error állapotát örökli: ha maga a szó olvasása sikertelen
        # volt, a belőle levezetett bitek is érvénytelenek, NEM "hamisan
        # False"-ok (CLAUDE.md §9/§12: malformed reply nem válhat csendben
        # egy konkrét, de hamis értékké).
        if not source.valid or source.value is None:
            return BqFieldReading(
                value=None, valid=False,
                error=source.error or "source field invalid",
                timestamp_monotonic=source.timestamp_monotonic,
            )
        return BqFieldReading(
            value=extract(source.value), valid=True, error=None,
            timestamp_monotonic=source.timestamp_monotonic,
        )

    return BqDiagnosticsSnapshot(
        device_type=_read(driver.read_device_type),
        fw_version=_read(driver.read_fw_version),
        hw_version=_read(driver.read_hw_version),
        chem_id=_read(driver.read_chem_id),
        control_status=control_status,
        voltage_mV=_read(driver.read_voltage_mV),
        avg_current_mA=_read(driver.read_avg_current_mA),
        temperature_C=_read(driver.read_temperature_C),
        soc_percent=_read(driver.read_state_of_charge_percent),
        remaining_capacity_mAh=_read(driver.read_remaining_capacity_mAh),
        full_charge_capacity_mAh=_read(driver.read_full_charge_capacity_mAh),
        flags=flags,
        qmax=_read(driver.read_qmax),
        update_status=_read(driver.read_update_status),
        ra_table_mohm=_read(driver.read_ra_table),
        cycle_count=_read(driver.read_cycle_count),
        dod0=_read(driver.read_dod0),
        avg_i_last_run_mA=_read(driver.read_avg_i_last_run_mA),
        avg_p_last_run=_read(driver.read_avg_p_last_run),
        ocvtaken=_bit(flags, lambda w: decode_flags(w).ocvtaken),
        vok=_bit(control_status, lambda w: decode_control_status(w).vok),
        qen=_bit(control_status, lambda w: decode_control_status(w).qen),
        rup_dis=_bit(control_status, lambda w: decode_control_status(w).rup_dis),
    )


def format_bq_session_scale_context(report: ProvisioningConsistencyReport) -> str:
    """A B7 igazolt UNSEALED readbackje; nincs új I/O vagy profil-fallback."""
    readback = {c.field_name: c.bq_readback for c in report.comparisons}

    def _value(name: str) -> str:
        value = readback.get(name)
        return str(value) if value is not None else "not read / session context unavailable"

    return (
        "source=B7 PRECOND UNSEALED; "
        f"Design Energy Scale={_value('design_energy_scale')}; "
        f"X10={_value('pack_configuration')}"
    )


def is_snapshot_fully_invalid(snapshot: BqDiagnosticsSnapshot) -> bool:
    """HIL-D5 (2026-09-14, CF-10a/DFAM-027, CLAIM_LEDGER.yaml PICO-
    RECONNECT-001) escalation-küszöb alapja: True, ha a snapshot MINDEN
    mezője érvénytelen — ez a valós Pico-transport-kiesés (kábel-kihúzás/
    power-cycle/RPC-szerver leállt) empirikusan megfigyelt aláírása.
    Explicit KÜLÖNBÖZIK egy RÉSZLEGES érvénytelenségtől (pl. SEALED chip
    alatt a Data Flash-függő mezők — qmax/update_status/ra_table_mohm/
    avg_i_last_run_mA/avg_p_last_run — érvénytelenek, de a transport maga
    él és a többi mező sikeresen olvasható) — ez utóbbi NEM transport-
    kiesés, nem szabad escalation-t kiváltania. Egyetlen forrás
    (poll_bq_diagnostics() eredménye) ellenőrzi mindkét controllert
    (traditional_learning_controller.py/golden_gg_controller.py), hogy a
    "teljesen érvénytelen" fogalom ne térhessen el köztük."""
    return all(not getattr(snapshot, f.name).valid for f in fields(snapshot))
