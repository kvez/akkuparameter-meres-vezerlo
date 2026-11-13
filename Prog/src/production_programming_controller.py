"""
production_programming_controller.py — WP-AK-13: PRODUCTION_PROGRAMMING_ASSISTED
per-unit gyártási workflow (SUP03-PROD-001..006, "Per unit flow" — Konszolidált
terv WP-AK-13 szakasza).

**NEM BEKÖTÖTT — NOT_WIRED (review-kör 4, Architektúra F6, DFAM-026
3. előfordulás)**: ezt a modult jelenleg SEHOL nem importálja/hívja élő GUI-
vagy TestRunner-kód (kizárólag a saját tesztje, `tests/test_production_
programming_controller.py`, hivatkozik rá) — nincs GUI menü/gomb, ami a
`ProductionProgrammingController`-t példányosítaná. A `main_window.py`/
`traditional_learning_controller.py` fájlnév-említései csak KOMMENTBEN
vannak (mintakövetés más kontextusban), nem tényleges import/hívás. L.
`bq_learning_verification.py` azonos szerkezetű "NEM HASZNÁLT" jelölése.
Ez a modul NEM lett törölve — a WP-AK-13 tervezett, jövőbeli GUI-bekötésre
váró, önmagában teljes és tesztelt implementációja.

Kötelező sorrend (a terv szó szerint, SUP03-PROD-004):
  identify -> image program -> readback -> voltage calibration -> unique data ->
  board test -> battery connect -> pack test -> IT_ENABLE -> verify -> seal ->
  final record

Ez egy OPERÁTOR-VEZÉRELT (nem tick-alapú fizikai ciklus) állapotgép — minden
lépés egy explicit metódushívás, a `bq_gauge_calibration.py::
VoltageCalibrationController` mintáját követve, NEM a
`TraditionalLearningController`/`GoldenGgController` `advance(dt_s)`
mintáját, mert egyik lépés sem igényel valós idejű fizikai várakozást
(a fizikai board/pack tesztek eredményét a hívó méri/attesztálja külsőleg —
l. lent).

**"image program" — újrahasznosítja a WP-AK-08 kontrollált write pipeline-t**
(`bq_controlled_write.write_single_df_block_controlled()`) BLOKKONKÉNT, nem
az alacsonyabb szintű, opakabb `PicoBQTransport.restore_df_snapshot()`-ot —
ez minden blokkhoz teljes PC-oldali kettős ellenőrzést (BACKUP/READBACK/
COMPARE/CHECKSUM) ad, konzisztensen a WP-AK-08 "kettős ellenőrzés" elvével.
Az első SIKERTELEN blokknál a pipeline megáll (nem folytatja a többi
blokkal egy már ismert hibás állapotban).

**"unique data" (SUP03-PROD-003)** — a serial number/lot code/manufacture
date "gyártásban egyedileg kezelendő" adatok, amik NEM részei a közös
image-nek. A BQ34Z110 Data Flash cím és írási eljárás ezekhez a mezőkhöz
FORRÁSOLT (TI-SLUSB55B Table 20: Serial Number SC48/off15 H2, Manufacture
Date SC48/off13 U2, Pack Lot Code SC56/off0 H2, PCB Lot Code SC56/off2 H2;
TI-SLUA665 "PRODUCTION STEP 3" Figure 8 a teljes write-szekvenciát adja) —
l. CLAIM_LEDGER.yaml PROD-UNIQ-001. Ez a lépés PROJECT_POLICY döntésből
(NEM forráshiányból) KIZÁRÓLAG a `ProductionRecord`-ba rögzíti ezeket,
explicit limitációként dokumentálva, NEM ír semmit a BQ-ba — a tényleges
gauge-írás egy külön, később ütemezett work package (2026-09-07,
governance-v2 review-kör 1, CF-11c: a korábbi "nincs forrás" indoklás
tévesen forráshiánynak állította be ezt a szándékos scope-korlátozást).

**"board test"/"pack test"** — nincs sourced, automatizált gyártási
tesztprocedúra ezekhez ebben a projektben (nincs dedikált board/pack test
hardver-interfész a kódbázisban) — ezért ezek externally-attested
eredmények: a hívó (operátor/külső tesztberendezés) méri és adja át a
PASS/FAIL eredményt, a controller csak a SORRENDET és a rögzítést
kényszeríti ki, nem a mérést.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
import json

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_controlled_write import ControlledBqWriteError, ControlledWriteResult, write_single_df_block_controlled
from Prog.src.bq_diagnostics import decode_control_status
from Prog.src.bq_gauge_calibration import VoltageCalibrationController, VoltageCalibrationSnapshot, VoltageCalState
from Prog.src.exceptions import InstrumentError
from Prog.src.ti_reference_validation import TiReferenceValidationRecord


class ProductionState(Enum):
    INIT = "INIT"
    IDENTIFY = "IDENTIFY"
    IMAGE_PROGRAM = "IMAGE_PROGRAM"
    VOLTAGE_CALIBRATION = "VOLTAGE_CALIBRATION"
    UNIQUE_DATA = "UNIQUE_DATA"
    BOARD_TEST = "BOARD_TEST"
    BATTERY_CONNECT = "BATTERY_CONNECT"
    PACK_TEST = "PACK_TEST"
    IT_ENABLE = "IT_ENABLE"
    VERIFY = "VERIFY"
    SEAL = "SEAL"
    DONE = "DONE"
    FAULT = "FAULT"


@dataclass(frozen=True)
class ImageBlockSpec:
    """Egy Data Flash blokk a programozandó "master" image-ből — a hívó
    egy korábbi, elfogadott golden-image DF-pillanatképből (WP-AK-09/10/11
    eredménye) állítja össze a listát."""
    subclass: int
    block: int
    data32: bytes

    def __post_init__(self) -> None:
        if len(self.data32) != 32:
            raise ValueError(
                f"ImageBlockSpec.data32 pontosan 32 bájt kell legyen, kapott: {len(self.data32)}"
            )


@dataclass
class ProductionRecord:
    """SUP03-PROD-006 kötelező verifikációs mezői."""
    device_id: Optional[int] = None
    firmware_version: Optional[int] = None
    image_version: str = ""
    block_results: list[ControlledWriteResult] = field(default_factory=list)
    voltage_calibration: Optional[VoltageCalibrationSnapshot] = None
    unique_data: dict = field(default_factory=dict)
    board_test_result: Optional[str] = None
    board_test_notes: str = ""
    battery_connect_confirmed: bool = False
    pack_test_result: Optional[str] = None
    pack_test_notes: str = ""
    it_enable_result: Optional[str] = None
    sealed_state: Optional[bool] = None
    limitations: list[str] = field(default_factory=list)
    ti_reference_validation: Optional[TiReferenceValidationRecord] = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "firmware_version": self.firmware_version,
            "image_version": self.image_version,
            "block_results": [asdict(r) for r in self.block_results],
            "voltage_calibration": self._voltage_calibration_dict(),
            "unique_data": self.unique_data,
            "board_test_result": self.board_test_result,
            "board_test_notes": self.board_test_notes,
            "battery_connect_confirmed": self.battery_connect_confirmed,
            "pack_test_result": self.pack_test_result,
            "pack_test_notes": self.pack_test_notes,
            "it_enable_result": self.it_enable_result,
            "sealed_state": self.sealed_state,
            "limitations": self.limitations,
            "ti_reference_validation": (
                self.ti_reference_validation.to_dict()
                if self.ti_reference_validation is not None else None
            ),
        }

    def _voltage_calibration_dict(self) -> Optional[dict]:
        if self.voltage_calibration is None:
            return None
        data = asdict(self.voltage_calibration)
        # [review-lelet] VoltageCalibrationSnapshot.state egy VoltageCalState
        # enum, amit asdict() NEM alakít át — json.dumps() enélkül
        # TypeError-t dobna ("Object of type VoltageCalState is not JSON
        # serializable"). A többi mező (int/str/None) már eleve JSON-safe.
        data["state"] = self.voltage_calibration.state.value
        return data


def write_production_record(output_dir: Path, record: ProductionRecord) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "production_record.json"
    path.write_text(json.dumps(record.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


class ProductionProgrammingController:
    """PRODUCTION_PROGRAMMING_ASSISTED — SUP03-PROD-004 per-unit flow."""

    def __init__(self, transport, image_version: str) -> None:
        self._transport = transport
        self._state = ProductionState.INIT
        self._fault_reason: str = ""
        self._record = ProductionRecord(image_version=image_version)

    @property
    def state(self) -> ProductionState:
        return self._state

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def record(self) -> ProductionRecord:
        return self._record

    def _require(self, expected: ProductionState) -> None:
        if self._state != expected:
            raise RuntimeError(
                f"Ez a lépés csak {expected.value} állapotból hívható, "
                f"jelenlegi állapot: {self._state.value}"
            )

    def _fail(self, reason: str) -> None:
        self._fault_reason = reason
        self._record.limitations.append(f"FAULT: {reason}")
        self._state = ProductionState.FAULT

    def emergency_abort(self, reason: str) -> None:
        try:
            self._transport.seal(confirm=True)
        except Exception:
            pass
        self._fail(reason)

    # ------------------------------------------------------------------ #
    # TI reference validation (WP-AK-14) — traceability, nem sorrendhez   #
    # kötött lépés: egy TI EV2400/bqStudio referencia-validáció jellemzően#
    # a gyártási VONALRA vonatkozik (egyszer elvégzett bring-up), nem     #
    # minden egyes egységre újra — ezért bármikor rögzíthető, nem része a #
    # `_require()`-rel kikényszerített per-unit sorrendnek.                #
    # ------------------------------------------------------------------ #

    def record_ti_reference_validation(self, validation: TiReferenceValidationRecord) -> None:
        self._record.ti_reference_validation = validation

    # ------------------------------------------------------------------ #
    # 1. IDENTIFY                                                          #
    # ------------------------------------------------------------------ #

    def run_identify(
        self,
        expected_device_type: Optional[int] = None,
        expected_fw_version: Optional[int] = None,
    ) -> None:
        self._require(ProductionState.INIT)
        try:
            device_type = self._transport.read_control_subcommand_word(
                Bq34z110ReadOnlyDriver.MAC_DEVICE_TYPE
            )
            fw_version = self._transport.read_control_subcommand_word(
                Bq34z110ReadOnlyDriver.MAC_FW_VERSION
            )
        except InstrumentError as exc:
            self._fail(f"IDENTIFY_FAILED: {exc}")
            return

        mismatch: dict[str, dict] = {}
        if expected_device_type is not None and device_type != expected_device_type:
            mismatch["device_type"] = {"expected": expected_device_type, "actual": device_type}
        if expected_fw_version is not None and fw_version != expected_fw_version:
            mismatch["firmware_version"] = {"expected": expected_fw_version, "actual": fw_version}
        if mismatch:
            self._fail(f"IDENTIFY_MISMATCH: {mismatch}")
            return

        self._record.device_id = device_type
        self._record.firmware_version = fw_version
        self._state = ProductionState.IDENTIFY

    # ------------------------------------------------------------------ #
    # 2/3. IMAGE PROGRAM + READBACK (write_single_df_block_controlled     #
    # már mindkettőt elvégzi blokkonként)                                  #
    # ------------------------------------------------------------------ #

    def program_image(self, blocks: list[ImageBlockSpec]) -> None:
        self._require(ProductionState.IDENTIFY)
        if not blocks:
            raise ValueError("program_image: legalább egy blokk kötelező")

        try:
            self._transport.unseal(confirm=True)
            self._transport.full_access(confirm=True)
        except InstrumentError as exc:
            # Teljes kódbázis review 4. kör (2026-08-29), Szoftver/
            # Architektúra P2 lelet: ha unseal() sikerrel lezajlik, de az
            # utána hívott full_access() hibázik, a chip fizikailag
            # UNSEALED állapotban marad — ez volt az EGYETLEN hibaág ebben
            # a fájlban, ami nem próbált best-effort reselelni, ellentétben
            # a lenti ControlledBqWriteError-ágtal és run_it_enable()/
            # run_verify()-vel. Best-effort: ha maga a seal() is hibázik
            # (pl. a chip sosem nyílt ki ténylegesen), ez nem akadályozhatja
            # a _fail() jelentését.
            try:
                self._transport.seal(confirm=True)
            except Exception:
                pass
            self._fail(f"IMAGE_PROGRAM_UNSEAL_FAILED: {exc}")
            return

        all_passed = True
        try:
            for spec in blocks:
                result = write_single_df_block_controlled(
                    self._transport, spec.subclass, spec.block, spec.data32,
                    confirm=True,
                    expected_device_type=self._record.device_id,
                    expected_fw_version=self._record.firmware_version,
                )
                self._record.block_results.append(result)
                if not result.passed:
                    all_passed = False
                    break
        except ControlledBqWriteError as exc:
            try:
                self._transport.seal(confirm=True)
            except Exception:
                pass
            # [review-lelet] a ControlledBqWriteError üzenete önmagában NEM
            # tartalmazza, melyik blokknál történt (a WP-AK-08 egyblokkos
            # hatókörében ez irreleváns volt) — több blokk esetén ez nélküle
            # nyomon-követhetetlen lenne, melyik blokk IDENTIFY/BACKUP lépése
            # bukott el. `spec` itt még a hibázó iteráció blokkjára mutat
            # (Python for-ciklus változó-öröklődése).
            self._fail(
                f"IMAGE_PROGRAM_FAILED: subclass={spec.subclass} block={spec.block}: {exc}"
            )
            return

        # A pipeline mindkét kimenetelnél (siker/hiba) reseal-t végez — a
        # VOLTAGE_CALIBRATION lépés saját maga unseal-el újra (l. modul
        # docstring, minden lépés önmagában kezeli a seal-állapotot).
        try:
            self._transport.seal(confirm=True)
        except Exception:
            pass

        if not all_passed:
            failed = self._record.block_results[-1]
            self._fail(
                f"IMAGE_PROGRAM_BLOCK_FAILED: subclass={failed.subclass} "
                f"block={failed.block} stage={failed.stage_reached} "
                f"reason={failed.fail_reason}"
            )
            return

        self._state = ProductionState.IMAGE_PROGRAM

    # ------------------------------------------------------------------ #
    # 4. VOLTAGE CALIBRATION — a meglévő VoltageCalibrationController      #
    # (WP-AK-09) újrahasznosítva, saját unseal/full_access/seal-lel        #
    # ------------------------------------------------------------------ #

    def begin_voltage_calibration(self) -> VoltageCalibrationController:
        self._require(ProductionState.IMAGE_PROGRAM)
        controller = VoltageCalibrationController(self._transport)
        controller.start()
        return controller

    def complete_voltage_calibration(self, controller: VoltageCalibrationController) -> None:
        self._require(ProductionState.IMAGE_PROGRAM)
        snapshot = controller.snapshot()
        if snapshot.state != VoltageCalState.DONE:
            self._fail(
                f"VOLTAGE_CALIBRATION_NOT_DONE: state={snapshot.state.value} "
                f"reason={snapshot.fail_reason}"
            )
            return
        self._record.voltage_calibration = snapshot
        self._state = ProductionState.VOLTAGE_CALIBRATION

    # ------------------------------------------------------------------ #
    # 5. UNIQUE DATA — SUP03-PROD-003: csak a record-ba, NEM a BQ-ba       #
    # ------------------------------------------------------------------ #

    def record_unique_data(self, serial_number: str, lot_code: str, manufacture_date: str) -> None:
        self._require(ProductionState.VOLTAGE_CALIBRATION)
        if not serial_number.strip() or not lot_code.strip() or not manufacture_date.strip():
            raise ValueError(
                "serial_number/lot_code/manufacture_date egyike sem lehet üres "
                "(SUP03-PROD-003)."
            )
        self._record.unique_data = {
            "serial_number": serial_number,
            "lot_code": lot_code,
            "manufacture_date": manufacture_date,
        }
        self._record.limitations.append(
            "SUP03-PROD-003: unique_data csak a production record-ba rögzítve — "
            "a BQ34Z110 Data Flash cím és írási eljárás ezekhez a mezőkhöz "
            "FORRÁSOLT (TI-SLUSB55B Table 20 SC48/SC56; TI-SLUA665 "
            "'PRODUCTION STEP 3'; l. CLAIM_LEDGER.yaml PROD-UNIQ-001), de "
            "PROJECT_POLICY döntésből (NEM forráshiányból) ez a lépés nem "
            "írja a BQ Data Flash-be — a tényleges gauge-írás külön, később "
            "ütemezett work package."
        )
        self._state = ProductionState.UNIQUE_DATA

    # ------------------------------------------------------------------ #
    # 6/8. BOARD TEST / PACK TEST — externally-attested eredmény           #
    # (nincs sourced automatizált tesztprocedúra ebben a projektben)       #
    # ------------------------------------------------------------------ #

    def record_board_test_result(self, passed: bool, notes: str = "") -> None:
        self._require(ProductionState.UNIQUE_DATA)
        self._record.board_test_result = "PASS" if passed else "FAIL"
        self._record.board_test_notes = notes
        if not passed:
            self._fail(f"BOARD_TEST_FAILED: {notes}")
            return
        self._state = ProductionState.BOARD_TEST

    def confirm_battery_connected(self) -> None:
        self._require(ProductionState.BOARD_TEST)
        self._record.battery_connect_confirmed = True
        self._state = ProductionState.BATTERY_CONNECT

    def record_pack_test_result(self, passed: bool, notes: str = "") -> None:
        self._require(ProductionState.BATTERY_CONNECT)
        self._record.pack_test_result = "PASS" if passed else "FAIL"
        self._record.pack_test_notes = notes
        if not passed:
            self._fail(f"PACK_TEST_FAILED: {notes}")
            return
        self._state = ProductionState.PACK_TEST

    # ------------------------------------------------------------------ #
    # 9/10/11. IT_ENABLE -> VERIFY -> SEAL                                 #
    # ------------------------------------------------------------------ #

    def run_it_enable(self) -> None:
        self._require(ProductionState.PACK_TEST)
        try:
            self._transport.unseal(confirm=True)
            self._transport.full_access(confirm=True)
            self._transport.it_enable(confirm=True)
        except InstrumentError as exc:
            try:
                self._transport.seal(confirm=True)
            except Exception:
                pass
            self._fail(f"IT_ENABLE_FAILED: {exc}")
            return
        self._record.it_enable_result = "SENT"
        self._state = ProductionState.IT_ENABLE

    def run_verify(self) -> None:
        self._require(ProductionState.IT_ENABLE)
        try:
            status_word = self._transport.read_control_subcommand_word(
                Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS
            )
        except InstrumentError as exc:
            try:
                self._transport.seal(confirm=True)
            except Exception:
                pass
            self._fail(f"VERIFY_FAILED: {exc}")
            return
        bits = decode_control_status(status_word)
        # [WP-AK-08 mintáját követve]: a QEN hiánya nem hard-fault (a
        # TraditionalLearningController._run_verify_vok_qen() ugyanígy csak
        # dokumentálja, nem állítja meg a folyamatot) — a döntő bizonyíték a
        # végleges SEAL utáni állapot, ezt a hívó (production riport) látja.
        self._record.it_enable_result = "QEN_CONFIRMED" if bits.qen else "QEN_NOT_CONFIRMED"
        if not bits.qen:
            self._record.limitations.append(
                "QEN nem 1 az IT_ENABLE után — Qmax-frissítés nem lesz "
                "engedélyezve, amíg ez nem változik."
            )
        self._state = ProductionState.VERIFY

    def run_seal(self) -> None:
        self._require(ProductionState.VERIFY)
        try:
            self._transport.seal(confirm=True)
        except InstrumentError as exc:
            self._fail(f"SEAL_FAILED: {exc}")
            return
        self._record.sealed_state = True
        self._state = ProductionState.SEAL

    # ------------------------------------------------------------------ #
    # 12. FINAL RECORD                                                     #
    # ------------------------------------------------------------------ #

    def finalize(self) -> ProductionRecord:
        self._require(ProductionState.SEAL)
        self._state = ProductionState.DONE
        return self._record


__all__ = [
    "ProductionState",
    "ImageBlockSpec",
    "ProductionRecord",
    "write_production_record",
    "ProductionProgrammingController",
]
