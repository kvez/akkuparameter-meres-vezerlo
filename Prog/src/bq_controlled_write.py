"""
bq_controlled_write.py — WP-AK-08 (Controlled BQ write réteg): PC-oldali
(workflow-master) pipeline egyetlen BQ34Z110 Data Flash blokk kontrollált
írásához.

Kapu (Akkuteszter_Konszolidalt_Lezarasi_es_BQ_Integracios_Implementacios_Terv
WP-AK-08 szakasza, szó szerint): "Kontrollált egyblokkos write/readback HIL
PASS." Ez a modul a SZOFTVERES pipeline-t implementálja — a fizikai HIL-
igazolás nyitott, dokumentált tétel marad (l.
Riportok/wp_ak_08_implementacios_zarojelentes.md és TF1 repo
TF1_BLOCKED_VALIDATION_BACKLOG.md BLK-025).

Kötelező write pipeline (a terv szó szerint):
  CONFIRM -> IDENTIFY -> VERIFY DEVICE/FW -> BACKUP -> HASH -> WRITE ->
  READBACK -> COMPARE -> CHECKSUM -> STATUS VERIFY -> PASS/FAIL

Architektúrális határ (CLAUDE.md 9. szakasz): ez a modul a "workflow-döntés"
réteg (Prog/src/), NEM driver — a `PicoBQTransport`-ot
(Prog/drivers/pico_bq_transport.py) használja alacsony szintű primitívként,
semmilyen soros/JSON-protokoll-részletet nem lát közvetlenül. A transport
saját maga NEM dönt semmilyen write-sorrendről (l. pico_bq_transport.py
docstring) — ez a modul hozza meg az ÖSSZES pipeline-döntést, és a
transport minden egyes hívásának eredményét a visszaadott
`ControlledWriteResult`-ban rögzíti (traceability — soha nem csak egy
bool PASS/FAIL, mindig a mögötte álló bizonyíték is).

Hatókörszűkítés (tudatos döntés, l. session-memória
[[tf1_akkuteszter_integration]]): a Kapu SZÓ SZERINT "egyblokkos" write-ot
ír elő — ez a modul EZÉRT csak EGYETLEN Data Flash blokk kontrollált
írását orchestrálja. A multi-block DF snapshot restore
(`PicoBQTransport.restore_df_snapshot`) alacsony szinten már elérhető, de a
hozzá tartozó PC-oldali workflow-orchestráció (GUI-bekötés, multi-block
pipeline, jóváhagyási munkafolyamat) NEM része ennek a WP-nek — egy
későbbi WP hatóköre.

Kettős ellenőrzés, nem redundancia: a `pico_bq_service.py::bq_df_write()`
(TF1 repo) MÁR elvégez egy saját, Pico-oldali readback-verify-t (checksum +
byte-egyezés) ÍRÁS UTÁN, és kivételt dob eltérésnél. Ez a modul EZEN FELÜL,
FÜGGETLENÜL is elvégzi a READBACK/COMPARE/CHECKSUM lépéseket a PC oldalán —
két, egymástól független réteg védi ugyanazt az állítást ("a Data Flash
tartalma pontosan az, amit szándékoztunk írni"), mert egyik réteg
kompromittálódása (pl. egy jövőbeli Pico-oldali refaktor hibája) nem
maradhat észrevétlen, ha a PC-oldali ellenőrzés önállóan is lefut.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.exceptions import InstrumentError

# [review finding] InstrumentTimeoutError (soros I/O timeout/nincs valasz)
# az InstrumentError ALATT testverkent all a PicoBQTransportError mellett,
# NEM annak leszarmazottja (l. Prog/src/exceptions.py) - egy `except
# PicoBQTransportError` onmagaban NEM kapna el egy timeoutot. Mivel epp a
# WRITE lepesnel egy timeout a LEGSULYOSABB bizonytalansag (nem tudni,
# megtortent-e a fizikai iras), minden pipeline-lepes a kozos
# `InstrumentError` alaposztalyt kapja el, nem csak a PicoBQTransportError-t.


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ControlledWriteResult:
    """A pipeline MINDEN elért lépésének bizonyítékát hordozza — a
    `bq_diagnostics.py::BqFieldReading` / `golden_image_manifest.py`
    mintáját követve: soha nem csak egy `passed: bool`, hanem a mögötte
    álló nyers adat is (traceability). `passed` csak akkor `True`, ha a
    pipeline MINDEN lépése sikerrel lezárult — egyetlen sikertelen lépés
    is a teljes pipeline-t FAIL-be viszi, `stage_reached` jelzi, melyik
    lépésnél állt meg."""

    subclass: int
    block: int
    passed: bool
    stage_reached: str
    fail_reason: Optional[str] = None
    identify: Optional[dict[str, int]] = None
    device_fw_mismatch: Optional[dict[str, Any]] = None
    backup_bytes_hex: Optional[str] = None
    backup_sha256: Optional[str] = None
    intended_data_hex: Optional[str] = None
    intended_sha256: Optional[str] = None
    readback_bytes_hex: Optional[str] = None
    readback_sha256: Optional[str] = None
    status_after_write: Optional[dict[str, int]] = None


class ControlledBqWriteError(Exception):
    """A pipeline egy WRITE ELŐTTI lépése (IDENTIFY/BACKUP) hibázott a
    transport szintjén (protokollhiba VAGY soros I/O timeout — l. modul
    import-komment `InstrumentError` megjegyzése) — nem csak "FAIL
    eredmény", hanem kivétel, mert ezekben az esetekben nem is lehetett
    megállapítani, biztonságos-e egyáltalán megkísérelni az írást (pl. a
    BQ nem válaszol). Elkülönítve a WRITE/READBACK/STATUS_VERIFY lépések
    "FAIL eredmény" viselkedésétől, hogy a hívó egyértelműen lássa: a
    WRITE lépés MEG SEM TÖRTÉNT."""


def write_single_df_block_controlled(
    transport: PicoBQTransport,
    subclass: int,
    block: int,
    new_data32: bytes,
    *,
    confirm: bool,
    expected_device_type: Optional[int] = None,
    expected_fw_version: Optional[int] = None,
) -> ControlledWriteResult:
    """WP-AK-08 kötelező write pipeline egyetlen DF blokkra: CONFIRM ->
    IDENTIFY -> VERIFY DEVICE/FW -> BACKUP -> HASH -> WRITE -> READBACK ->
    COMPARE -> CHECKSUM -> STATUS VERIFY -> PASS/FAIL.

    `expected_device_type`/`expected_fw_version` opcionális — ha a hívó
    megadja, a VERIFY DEVICE/FW lépés eltérés esetén FAIL-lel áll meg
    ÍRÁS ELŐTT (a WRITE lépés meg sem történik). Ha egyik sincs megadva,
    az IDENTIFY lépés eredménye csak dokumentálásra kerül, nem blokkol.

    ELŐFELTÉTEL (tudatosan NEM ez a függvény végzi el): a BQ34Z110-nek MÁR
    UNSEALED/FULL ACCESS állapotban kell lennie, amikor ezt hívod — a
    függvény NEM hív automatikusan `transport.unseal()`-t, ugyanazon elv
    szerint, mint a TF1 repo `pico_bq_service.py::bq_learn_status()`
    (a `confirm=True` kapu szemantikáját tisztán tartja: egy külön,
    explicit hívás egy külön, explicit megerősítést igényel, ez a
    pipeline nem "csomagol össze" több roncsoló lépést egyetlen hívásba).
    Ha a device SEALED állapotban van, a Data Flash írás a BQ-n belül
    várhatóan nem érvényesül — ezt a COMPARE lépés fogja elkapni (a
    readback nem fog egyezni az intended data-val), NEM egy hamis PASS-t
    ad vissza.
    """
    if not confirm:
        raise ValueError("write_single_df_block_controlled() requires confirm=True")
    if len(new_data32) != 32:
        raise ValueError(
            f"write_single_df_block_controlled: new_data32 must be exactly 32 "
            f"bytes, got {len(new_data32)}"
        )

    intended_data_hex = new_data32.hex()
    intended_sha256 = _sha256_hex(new_data32)

    # --- IDENTIFY ---
    try:
        identify = {
            "device_type": transport.read_control_subcommand_word(
                Bq34z110ReadOnlyDriver.MAC_DEVICE_TYPE
            ),
            "firmware_version": transport.read_control_subcommand_word(
                Bq34z110ReadOnlyDriver.MAC_FW_VERSION
            ),
        }
    except InstrumentError as exc:
        raise ControlledBqWriteError(f"IDENTIFY sikertelen: {exc}") from exc

    # --- VERIFY DEVICE/FW ---
    mismatch: dict[str, Any] = {}
    if expected_device_type is not None and identify["device_type"] != expected_device_type:
        mismatch["device_type"] = {
            "expected": expected_device_type, "actual": identify["device_type"],
        }
    if expected_fw_version is not None and identify["firmware_version"] != expected_fw_version:
        mismatch["firmware_version"] = {
            "expected": expected_fw_version, "actual": identify["firmware_version"],
        }
    if mismatch:
        return ControlledWriteResult(
            subclass=subclass, block=block, passed=False,
            stage_reached="VERIFY_DEVICE_FW", fail_reason="device/firmware mismatch",
            identify=identify, device_fw_mismatch=mismatch,
            intended_data_hex=intended_data_hex, intended_sha256=intended_sha256,
        )

    # --- BACKUP (a cél blokk jelenlegi tartalma, ÍRÁS ELŐTT) ---
    try:
        backup_bytes = transport.read_data_flash_block(subclass, block)
    except InstrumentError as exc:
        raise ControlledBqWriteError(
            f"BACKUP (írás előtti olvasás) sikertelen: {exc}"
        ) from exc
    backup_sha256 = _sha256_hex(backup_bytes)

    # --- WRITE ---
    try:
        transport.write_data_flash_block(subclass, block, new_data32, confirm=True)
    except InstrumentError as exc:
        # A Pico-oldali bq_df_write() SAJÁT readback-verify-ja már itt
        # elbukhatott (l. TF1 repo pico_bq_service.py) — ez esetben a
        # fizikai írás MEGTÖRTÉNT, csak a checksum/readback nem egyezett.
        # Ezt a hívónak KÜLÖN kell látnia (stage_reached="WRITE"), nem
        # tűnhet el egy IDENTIFY/BACKUP-szintű, "meg sem próbáltuk" hibaként.
        return ControlledWriteResult(
            subclass=subclass, block=block, passed=False,
            stage_reached="WRITE", fail_reason=str(exc),
            identify=identify,
            backup_bytes_hex=backup_bytes.hex(), backup_sha256=backup_sha256,
            intended_data_hex=intended_data_hex, intended_sha256=intended_sha256,
        )

    # --- READBACK (PC-oldali, a Pico-oldali readback-verify-tól FÜGGETLEN
    # második ellenőrzés — l. modul docstring "kettős ellenőrzés") ---
    try:
        readback_bytes = transport.read_data_flash_block(subclass, block)
    except InstrumentError as exc:
        return ControlledWriteResult(
            subclass=subclass, block=block, passed=False,
            stage_reached="READBACK", fail_reason=str(exc),
            identify=identify,
            backup_bytes_hex=backup_bytes.hex(), backup_sha256=backup_sha256,
            intended_data_hex=intended_data_hex, intended_sha256=intended_sha256,
        )
    readback_sha256 = _sha256_hex(readback_bytes)

    # --- COMPARE ---
    if readback_bytes != new_data32:
        return ControlledWriteResult(
            subclass=subclass, block=block, passed=False,
            stage_reached="COMPARE", fail_reason="readback does not match intended data",
            identify=identify,
            backup_bytes_hex=backup_bytes.hex(), backup_sha256=backup_sha256,
            intended_data_hex=intended_data_hex, intended_sha256=intended_sha256,
            readback_bytes_hex=readback_bytes.hex(), readback_sha256=readback_sha256,
        )

    # --- CHECKSUM (SHA-256 egyezés — a byte-egyenlőség fölötti, teljes
    # tartalom szintű megerősítés) ---
    if readback_sha256 != intended_sha256:
        return ControlledWriteResult(
            subclass=subclass, block=block, passed=False,
            stage_reached="CHECKSUM", fail_reason="readback sha256 does not match intended sha256",
            identify=identify,
            backup_bytes_hex=backup_bytes.hex(), backup_sha256=backup_sha256,
            intended_data_hex=intended_data_hex, intended_sha256=intended_sha256,
            readback_bytes_hex=readback_bytes.hex(), readback_sha256=readback_sha256,
        )

    # --- STATUS VERIFY (CONTROL_STATUS() lekérdezés írás után — nem
    # állapít meg semmilyen konkrét hibajelző bitet, csak dokumentálja a
    # nyers állapotot bizonyítékként, mert a bitmezők jelentése kontextus-
    # függő, l. Prog/src/bq_diagnostics.py ControlStatusBits) ---
    try:
        status_word = transport.read_control_subcommand_word(
            Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS
        )
    except InstrumentError as exc:
        return ControlledWriteResult(
            subclass=subclass, block=block, passed=False,
            stage_reached="STATUS_VERIFY", fail_reason=str(exc),
            identify=identify,
            backup_bytes_hex=backup_bytes.hex(), backup_sha256=backup_sha256,
            intended_data_hex=intended_data_hex, intended_sha256=intended_sha256,
            readback_bytes_hex=readback_bytes.hex(), readback_sha256=readback_sha256,
        )

    # --- PASS/FAIL: minden lépés sikerrel lezárult ---
    return ControlledWriteResult(
        subclass=subclass, block=block, passed=True,
        stage_reached="PASS", fail_reason=None,
        identify=identify,
        backup_bytes_hex=backup_bytes.hex(), backup_sha256=backup_sha256,
        intended_data_hex=intended_data_hex, intended_sha256=intended_sha256,
        readback_bytes_hex=readback_bytes.hex(), readback_sha256=readback_sha256,
        status_after_write={"control_status": status_word},
    )
