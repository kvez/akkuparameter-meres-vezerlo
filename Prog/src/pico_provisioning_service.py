"""
PicoProvisioningService — egy "üres" (gyári/friss MicroPython) Raspberry Pi
Pico előkészítése BQ34Z110-kommunikációra, közvetlenül az Akkuteszter GUI-ból.

CÉL (felhasználói követelmény): aki az Akkuteszter szoftvert használja és van
egy Pico-ja, ne kelljen külön letöltenie/kézzel feltöltenie a Pico-oldali
szervert — a szoftver TARTALMAZZA (l. `Prog/pico_firmware/`), és egyetlen
menüpontból feltölti + elindítja rajta a szolgáltatást, a bekötéshez pedig
útmutatót ad.

RÉTEGHATÁR (CLAUDE.md §9): ez a modul a raw-REPL *provisioning* protokollt
beszéli (MicroPython Ctrl-A/Ctrl-B/Ctrl-D bootloader-szintű REPL), NEM a
futó szolgáltatás JSON-Lines RPC protokollját. A kettő KÖLCSÖNÖSEN
KIZÁRÓLAGOS ugyanazon a fizikai porton:
  - amíg a `main.py`/`serve_forever()` fut, a port JSON-Lines RPC-t beszél
    (`Prog/drivers/pico_bq_transport.py`),
  - a provisioning ezt Ctrl-C-vel megszakítja, raw-REPL-be lép, ír, majd
    soft-reset-tel visszaadja a portot a szolgáltatásnak.
A záró élő visszaigazolást (`verify_service_running()`) SZÁNDÉKOSAN a MÁR
MEGLÉVŐ, éles `PicoBQTransport.negotiate()` végzi egy ÚJ soros kapcsolaton —
így a siker-kritérium pontosan az, amit a GUI később ténylegesen használni
fog, nem egy provisioning-specifikus, önmagát igazoló ellenőrzés.

FORRÁS / BIZONYÍTOTTSÁG
- A raw-REPL szekvencia (Ctrl-C ×2 → Ctrl-A → kód + Ctrl-D → `OK<stdout>
  \\x04<stderr>\\x04`) ebben a projektben már HIL-lel bizonyított módszer
  (2026-09-12, TF1 Pico-firmware fizikai feltöltése; memória:
  `reference_pico_hw_repl_teszt_menet`, `feedback_pico_firmware_requires_
  physical_reupload`). Ez a modul ugyanazt a szekvenciát kódolja, nem újat.
- A feltöltési lánc (backup → base64-chunk írás → SHA256 bit-pontos verify
  → import-check → soft reset → élő negotiate()) ugyanabból a memóriából
  származó, kötelező eljárás — a SHA256 eltérésnél a provisioning MEGÁLL,
  és NEM indítja el a szolgáltatást csonka/hibás fájllal.

HIL-STÁTUSZ: az eredeti feltöltési lánc 2026-09-14-én valós Picon
végigfutott (CLAIM_LEDGER: PICO-PROVISIONING-001). Az R8-B4 cleanup-
újraindítás bizonyítéka unit/mock; ennek valós ellenőrzése HIL_PENDING.

BIZTONSÁG: a `provision()` FIZIKAI, NEM VISSZAVONHATÓ írás egy eszköz
fájlrendszerére, ezért — a projekt minden más roncsoló műveletével azonos
mintát követve (`bq_df_write(confirm=True)`, `PicoBQTransport.unseal(
confirm=...)`) — EXPLICIT `confirm=True` kulcsszó-paramétert követel meg,
alapértelmezés nélkül. Az `identify()` nem ír fájlt, de megszakítja a futó
szolgáltatást; az igazolt saját firmware-t best-effort soft resettel indítja.
"""
from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from Prog import app_paths

# --------------------------------------------------------------------------- #
# Protokoll-konstansok (MicroPython raw REPL)                                  #
# --------------------------------------------------------------------------- #
_CTRL_A = b"\x01"  # raw REPL-be lépés
_CTRL_B = b"\x02"  # vissza friendly REPL-be
_CTRL_C = b"\x03"  # futó program megszakítása (KeyboardInterrupt)
_CTRL_D = b"\x04"  # raw REPL-ben: a beírt blokk futtatása / kimenet-határoló

_RAW_REPL_BANNER = b"raw REPL; CTRL-B to exit\r\n>"

#: A Pico flash gyökerébe feltöltendő fájlok, a feltöltés SORRENDJÉBEN.
#: A `main.py` SZÁNDÉKOSAN UTOLSÓ: ha egy korábbi fájl feltöltése/hash-
#: ellenőrzése megbukik, a provisioning megáll, és a Pico-n MÉG NINCS olyan
#: `main.py`, ami egy hiányzó/csonka modult próbálna importálni induláskor.
FIRMWARE_FILES: tuple[str, ...] = (
    "pico_bq_service.py",
    "pico_bq_rpc.py",
    "main.py",
)

#: Egy raw-REPL exec-blokkban átvitt NYERS bájtok száma. Base64-kódolva ez
#: ~1,37×, plusz a wrapper — jóval a MicroPython raw-REPL gyakorlati
#: blokkmérete alatt marad, és a ~60 KB-os teljes csomagot ~60 körfordulóból
#: viszi át.
_UPLOAD_CHUNK_BYTES = 1024

#: A Pico-oldali SHA256 számítás olvasási blokkmérete (MicroPython-ban a
#: `hashlib.sha256` csak `update()`/`digest()`-et támogat — a `_sha256_hex()`
#: helper mintája a `pico_firmware/pico_bq_service.py`-ban).
_DEVICE_HASH_READ_BYTES = 512


# --------------------------------------------------------------------------- #
# Hibakódok — minden hibaág EGYÉRTELMŰEN megnevezi, MELYIK lépésnél állt meg  #
# --------------------------------------------------------------------------- #
class ProvisionErrorCode:
    PORT_OPEN_FAILED = "PORT_OPEN_FAILED"
    RAW_REPL_ENTRY_FAILED = "RAW_REPL_ENTRY_FAILED"
    IDENTIFY_FAILED = "IDENTIFY_FAILED"
    NOT_MICROPYTHON = "NOT_MICROPYTHON"
    FIRMWARE_SOURCE_MISSING = "FIRMWARE_SOURCE_MISSING"
    BACKUP_FAILED = "BACKUP_FAILED"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    SHA256_MISMATCH_ABORTED = "SHA256_MISMATCH_ABORTED"
    IMPORT_CHECK_FAILED = "IMPORT_CHECK_FAILED"
    SOFT_RESET_FAILED = "SOFT_RESET_FAILED"
    SERVICE_START_VERIFICATION_FAILED = "SERVICE_START_VERIFICATION_FAILED"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


class ProvisionStep:
    """A `provision()` lépés-azonosítói — a `ProvisionResult.step` mezőbe
    kerülnek, hogy a hiba helye a GUI-ból is egyértelmű legyen."""

    OPEN_PORT = "1_OPEN_PORT"
    RAW_REPL = "2_RAW_REPL"
    IDENTIFY = "3_IDENTIFY"
    BACKUP = "4_BACKUP"
    UPLOAD = "5_UPLOAD"
    VERIFY_HASH = "6_VERIFY_HASH"
    IMPORT_CHECK = "7_IMPORT_CHECK"
    SOFT_RESET = "8_SOFT_RESET"
    VERIFY_SERVICE = "9_VERIFY_SERVICE"
    DONE = "DONE"


class PicoProvisioningError(Exception):
    """Belső, lépés-címkézett hiba — a `provision()` alakítja `ProvisionResult`-tá.

    SZÁNDÉKOSAN nem `InstrumentError` leszármazott: ez NEM egy mérőműszer-
    kommunikációs hiba, hanem egy eszköz-előkészítési (bring-up) hiba, és
    nem szabad, hogy a mérési workflow-k `except InstrumentError` ágaiba
    csendben beleessen."""

    def __init__(self, step: str, code: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Injektálható soros port (l. `PicoBQTransport.SerialLike` azonos mintája)     #
# --------------------------------------------------------------------------- #
class RawReplSerialLike(Protocol):
    """Minimál soros-port interfész a raw-REPL protokollhoz — valós hardveren
    `pyserial.Serial` (natívan implementálja mindet), teszthez
    `FakePicoRawReplSerial` (l. Prog/tests/mock_drivers/
    fake_pico_raw_repl_serial.py).

    Eltér a `pico_bq_transport.SerialLike`-tól: ott soronkénti (`readline()`)
    JSON-forgalom megy, itt bájt-pontos (`read()`/`read_until()`) vezérlő-
    karakteres protokoll — ez a két KÜLÖN protokoll KÜLÖN interfésze, nem
    duplikáció."""

    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


SerialFactory = Callable[[str], RawReplSerialLike]
TransportFactory = Callable[[], Any]
ProgressCallback = Callable[[str], None]


def _default_serial_factory(port: str) -> RawReplSerialLike:
    import serial as pyserial  # type: ignore[import-untyped]  # lazy: csak valós használatkor kell

    ser = pyserial.Serial(port, baudrate=115200, timeout=5)
    # A pyserial portnyitás után a MicroPython USB CDC-nek rövid idő kell,
    # mire fogadja a vezérlő-karaktereket (a HIL-bizonyított
    # `reference_pico_hw_repl_teszt_menet` mintája ugyanezt a 0,3 s-ot
    # használja).
    time.sleep(0.3)
    return ser


def _default_transport_factory() -> Any:
    from Prog.drivers.pico_bq_transport import PicoBQTransport

    return PicoBQTransport(max_retries=2)


# --------------------------------------------------------------------------- #
# Eredmény-típusok                                                             #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UploadedFile:
    name: str
    size_bytes: int
    sha256: str
    backup_name: str | None  # None, ha a Picon nem volt korábbi példány


@dataclass(frozen=True)
class IdentifyResult:
    """Fájlírás nélküli azonosítás eredménye; a szolgáltatásfutást nem igazolja."""

    ok: bool
    port: str
    is_micropython: bool
    message: str
    implementation: str | None = None       # sys.implementation.name, pl. "micropython"
    version: str | None = None              # os.uname().release
    machine: str | None = None              # os.uname().machine, pl. "Raspberry Pi Pico with RP2040"
    existing_files: tuple[str, ...] = ()    # a flash gyökerének tartalma
    already_provisioned: bool = False       # mindhárom FIRMWARE_FILES már fent van
    error_code: str | None = None


@dataclass(frozen=True)
class ProvisionResult:
    ok: bool
    port: str
    step: str
    message: str
    log: tuple[str, ...] = ()
    uploaded: tuple[UploadedFile, ...] = ()
    protocol_version: str | None = None
    error_code: str | None = None


# --------------------------------------------------------------------------- #
# Bekötési útmutató                                                            #
# --------------------------------------------------------------------------- #
#: A bekötési útmutató soronként, emberi olvasásra. FORRÁSOK soronként
#: megjelölve — a TI-adatlapból származó láb-számok és a projekt saját,
#: bizonyítottan működő GP4/GP5 kiosztása KÜLÖN van nevezve, hogy a
#: felhasználó lássa, melyik állítás mire épül.
WIRING_INSTRUCTIONS_LINES: tuple[str, ...] = (
    "BEKÖTÉS — Raspberry Pi Pico  ↔  BQ34Z110 gas gauge",
    "",
    "1) I2C adat- és órajel:",
    "     Pico GP4  (fizikai 6. láb)   ──  BQ34Z110  P3/SDA  (14. láb)",
    "     Pico GP5  (fizikai 7. láb)   ──  BQ34Z110  P4/SCL  (13. láb)",
    "",
    "2) Közös referencia (KÖTELEZŐ):",
    "     Pico GND  (pl. 38. láb)      ──  BQ34Z110  VSS     (8. láb)",
    "",
    "3) Felhúzó (pull-up) ellenállások — KÖTELEZŐ, mert az SDA/SCL",
    "   nyitott nyelőjű (open-drain), magától nem tud magas szintre menni:",
    "     SDA  ──[ 4,7 kΩ ]──  Pico 3V3(OUT)  (36. láb)",
    "     SCL  ──[ 4,7 kΩ ]──  Pico 3V3(OUT)  (36. láb)",
    "   Ha a gauge-panelen MÁR VAN felhúzás, ne tegyél be másodikat.",
    "   Érték: a projekt bevált NYÁK-ja 4,7 kΩ-ot használ; a TI adatlap",
    "   10 kΩ-ot ad „typical”-ként — mindkettő érvényes (kisebb érték",
    "   nagyobb buszkapacitásnál biztonságosabb).",
    "",
    "4) A BQ34Z110 SAJÁT tápellátása (REGIN / REG25 / BAT / SRP / SRN):",
    "   ezt a gauge-panel saját kapcsolási rajza határozza meg (tipikusan",
    "   az akkucsomagból) — ez az útmutató NEM írja elő és nem is tudja",
    "   ellenőrizni.",
    "   FIGYELEM: NE tápláld a gauge-panelt a Pico 3V3 kimenetéről",
    "   anélkül, hogy a panel rajzából igazoltad volna, hogy ez megengedett.",
    "",
    "5) A Pico a PC USB-portjáról kapja a tápot — a provisioning alatt",
    "   csak az USB-kábelt kell csatlakoztatni.",
    "",
    "MIT NE:",
    "  • Ne köss MÁS I2C-eszközt ugyanarra a buszra: a szolgáltatás a",
    "    BQ34Z110 fix 0x55 címét használja, idegen slave csendes",
    "    adatsérülést vagy időtúllépést okozhat.",
    "  • Ne futtass párhuzamosan más programot (pl. Thonny REPL-t) ugyanazon",
    "    a COM porton — a Pico USB CDC portját egyszerre egy kliens",
    "    szolgálhatja ki megbízhatóan.",
    "",
    "FORRÁSOK:",
    "  • GP4=SDA / GP5=SCL / 4,7 kΩ felhúzás 3,3 V-ra: a projekt",
    "    bizonyítottan működő Pico-BQ34Z110 összeállítása",
    "    (Prog/pico_firmware/pico_bq_service.py, `I2C(0, sda=Pin(4),",
    "    scl=Pin(5), freq=100000)`).",
    "  • BQ34Z110 lábszámok (P3/SDA=14, P4/SCL=13, VSS=8) és a 10 kΩ",
    "    „typical” felhúzás: TI bq34z110 adatlap, Table 1 „External Pin",
    "    Functions”.",
    "  • A 3,3 V-os felhúzás megengedett: TI bq34z110 adatlap, Recommended",
    "    Operating Conditions — VIH(OD) 1,2…6 V; Absolute Maximum Ratings —",
    "    open-drain I/O (SDA, SCL, HDQ) max. 5,5 V.",
)

WIRING_INSTRUCTIONS: str = "\n".join(WIRING_INSTRUCTIONS_LINES)


def get_wiring_instructions() -> str:
    """A GUI-ban megjelenítendő bekötési útmutató (l. `WIRING_INSTRUCTIONS`)."""
    return WIRING_INSTRUCTIONS


# --------------------------------------------------------------------------- #
# Szolgáltatás                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class PicoProvisioningService:
    """Raw-REPL alapú Pico-előkészítés. Minden külső függősége injektálható,
    hogy hardver nélkül, determinisztikusan tesztelhető legyen."""

    firmware_dir: Path | None = None
    serial_factory: SerialFactory = _default_serial_factory
    transport_factory: TransportFactory = _default_transport_factory
    #: A soft reset után ennyit várunk, mielőtt ÚJ kapcsolattal ellenőriznénk
    #: a szolgáltatást — a MicroPython újraindulás + USB CDC újraenumerálás
    #: ideje. Injektálható, hogy a tesztek ne aludjanak.
    post_reset_wait_s: float = 2.0
    sleep: Callable[[float], None] = time.sleep
    exec_timeout_s: float = 20.0
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    # ------------------------------------------------------------------ #
    # Publikus API                                                        #
    # ------------------------------------------------------------------ #
    def identify(self, port: str) -> IdentifyResult:
        """Fájlírás nélküli azonosítás a MicroPython saját REPL-válaszából.

        A raw REPL megszakítja a futó szolgáltatást. R8-B4: csak igazolt saját
        firmware és hiányzó boot.py/boot.mpy mellett próbáljuk újraindítani.
        A soft reset kiadása nem igazolja a szolgáltatás futását.
        """
        ser: RawReplSerialLike | None = None
        entered_raw_repl = False
        cleanup_note = ""
        try:
            try:
                ser = self.serial_factory(port)
            except Exception as exc:
                return IdentifyResult(
                    ok=False, port=port, is_micropython=False,
                    error_code=ProvisionErrorCode.PORT_OPEN_FAILED,
                    message=f"A(z) {port} port nem nyitható meg: {exc}",
                )
            self._enter_raw_repl(ser)
            entered_raw_repl = True
            info = self._read_device_info(ser)
            implementation = info.get("implementation")
            is_micropython = implementation == "micropython"
            files = tuple(info.get("files", ()))
            already = all(name in files for name in FIRMWARE_FILES)
            if not is_micropython:
                result = IdentifyResult(
                    ok=False, port=port, is_micropython=False,
                    implementation=implementation, version=info.get("version"),
                    machine=info.get("machine"), existing_files=files,
                    error_code=ProvisionErrorCode.NOT_MICROPYTHON,
                    message=(
                        f"A(z) {port} porton válaszoló eszköz nem MicroPython "
                        f"(sys.implementation.name={implementation!r}). Telepíts rá "
                        f"MicroPython firmware-t (.uf2), mielőtt előkészítenéd."
                    ),
                )
            else:
                result = IdentifyResult(
                    ok=True, port=port, is_micropython=True,
                    implementation=implementation, version=info.get("version"),
                    machine=info.get("machine"), existing_files=files,
                    already_provisioned=already,
                    message=(
                        f"MicroPython eszköz azonosítva: {info.get('machine')} "
                        f"(verzió: {info.get('version')}). "
                        + (
                            "A BQ-szolgáltatás fájljai MÁR fent vannak — az előkészítés "
                            "felülírja őket (a régiek biztonsági mentésbe kerülnek)."
                            if already
                            else "A BQ-szolgáltatás fájljai még NINCSENEK fent."
                        )
                    ),
                )
        except PicoProvisioningError as exc:
            result = IdentifyResult(
                ok=False, port=port, is_micropython=False,
                error_code=exc.code, message=exc.message,
            )
        finally:
            try:
                if ser is not None:
                    cleanup_note = self._cleanup_raw_repl(
                        ser, entered_raw_repl, allow_reset=True,
                    )
            finally:
                _close_quietly(ser)
        return replace(result, message=f"{result.message} {cleanup_note}")

    def provision(
        self,
        port: str,
        progress_callback: ProgressCallback | None = None,
        *,
        confirm: bool,
    ) -> ProvisionResult:
        """A teljes, 9 lépéses előkészítés. FIZIKAI, NEM VISSZAVONHATÓ írás —
        ezért `confirm=True` kötelező (l. modul-docstring "BIZTONSÁG").

        Minden hibaágon GARANTÁLT a soros port lezárása (`finally`), és az
        eredmény megnevezi a lépést (`step`) és a hibaokot (`error_code`)."""
        log: list[str] = []

        def emit(text: str) -> None:
            log.append(text)
            if progress_callback is not None:
                progress_callback(text)

        if not confirm:
            return ProvisionResult(
                ok=False, port=port, step=ProvisionStep.OPEN_PORT,
                error_code=ProvisionErrorCode.NOT_CONFIRMED,
                message=(
                    "provision() requires confirm=True — ez fizikai, nem "
                    "visszavonható írás a Pico fájlrendszerére."
                ),
                log=tuple(log),
            )

        # A forrásfájlokat MÉG a port megnyitása ELŐTT beolvassuk: ha
        # hiányoznak/olvashatatlanok, ne nyúljunk hozzá a hardverhez.
        try:
            sources = self._load_firmware_sources()
        except PicoProvisioningError as exc:
            emit(f"HIBA [{exc.code}]: {exc.message}")
            return ProvisionResult(
                ok=False, port=port, step=exc.step, error_code=exc.code,
                message=exc.message, log=tuple(log),
            )

        uploaded: list[UploadedFile] = []
        step = ProvisionStep.OPEN_PORT
        ser: RawReplSerialLike | None = None
        entered_raw_repl = False
        # R8-B4: már a bizonytalan kimenetelű módosítási kísérlet is tiltja a resetet.
        modification_attempted = False
        cleanup_note = ""
        try:
            emit(f"1/9 — {port} port megnyitása…")
            try:
                ser = self.serial_factory(port)
            except Exception as exc:
                raise PicoProvisioningError(
                    ProvisionStep.OPEN_PORT, ProvisionErrorCode.PORT_OPEN_FAILED,
                    f"A(z) {port} port nem nyitható meg: {exc}",
                ) from exc

            step = ProvisionStep.RAW_REPL
            emit("2/9 — raw REPL (a futó szolgáltatás megszakítása, ha van)…")
            self._enter_raw_repl(ser)
            entered_raw_repl = True

            step = ProvisionStep.IDENTIFY
            emit("3/9 — MicroPython azonosítása…")
            info = self._read_device_info(ser)
            if info.get("implementation") != "micropython":
                raise PicoProvisioningError(
                    ProvisionStep.IDENTIFY, ProvisionErrorCode.NOT_MICROPYTHON,
                    f"Az eszköz nem MicroPython (sys.implementation.name="
                    f"{info.get('implementation')!r}) — az előkészítés leáll.",
                )
            emit(f"      {info.get('machine')} / {info.get('version')}")
            existing = set(info.get("files", ()))

            step = ProvisionStep.BACKUP
            suffix = self._backup_suffix()
            backups: dict[str, str | None] = {}
            emit(f"4/9 — meglévő fájlok mentése (*.{suffix})…")
            for name in FIRMWARE_FILES:
                if name in existing:
                    backup_name = f"{name}.{suffix}"
                    modification_attempted = True
                    self._backup_file(ser, name, backup_name)
                    backups[name] = backup_name
                    emit(f"      {name} → {backup_name}")
                else:
                    # NEM hiba: egy üres/friss Picon nincs mit menteni.
                    backups[name] = None
                    emit(f"      {name}: nincs korábbi példány, nincs mit menteni")

            for index, name in enumerate(FIRMWARE_FILES, start=1):
                data = sources[name]
                step = ProvisionStep.UPLOAD
                emit(
                    f"5/9 — feltöltés ({index}/{len(FIRMWARE_FILES)}): {name} "
                    f"({len(data)} bájt)…"
                )
                modification_attempted = True
                self._upload_file(ser, name, data, emit)

                step = ProvisionStep.VERIFY_HASH
                local_hash = hashlib.sha256(data).hexdigest()
                emit(f"6/9 — SHA256 ellenőrzés: {name}…")
                device_hash = self._device_sha256(ser, name)
                if device_hash != local_hash:
                    raise PicoProvisioningError(
                        ProvisionStep.VERIFY_HASH,
                        ProvisionErrorCode.SHA256_MISMATCH_ABORTED,
                        f"SHA256 eltérés a(z) {name} fájlnál — helyi "
                        f"{local_hash}, Picon {device_hash}. Az előkészítés "
                        f"MEGÁLL, a szolgáltatás NEM indul el csonka/hibás "
                        f"fájllal. A régi fájlok biztonsági mentése megmaradt "
                        f"a Picon (*.{suffix}).",
                    )
                emit(f"      OK  {local_hash[:16]}…")
                uploaded.append(
                    UploadedFile(
                        name=name, size_bytes=len(data), sha256=local_hash,
                        backup_name=backups.get(name),
                    )
                )

            step = ProvisionStep.IMPORT_CHECK
            emit("7/9 — modulok importálhatóságának ellenőrzése…")
            self._import_check(ser)

            step = ProvisionStep.SOFT_RESET
            emit("8/9 — soft reset (a main.py önállóan elindul)…")
            self._soft_reset(ser)
            _close_quietly(ser)
            ser = None
            self.sleep(self.post_reset_wait_s)

            step = ProvisionStep.VERIFY_SERVICE
            emit("9/9 — élő visszaigazolás új kapcsolaton (negotiate)…")
            capabilities = self._verify_service(port)
            protocol_version = None
            if isinstance(capabilities, dict):
                raw_version = capabilities.get("protocol_version")
                protocol_version = raw_version if isinstance(raw_version, str) else None
            emit(f"      A szolgáltatás fut (protokoll {protocol_version}).")

            step = ProvisionStep.DONE
            emit("KÉSZ — a Pico előkészítve a BQ34Z110-kommunikációra.")
            return ProvisionResult(
                ok=True, port=port, step=ProvisionStep.DONE,
                message=(
                    f"A(z) {port} porton lévő Pico előkészítve: "
                    f"{len(uploaded)} fájl feltöltve és SHA256-tal igazolva, "
                    f"a szolgáltatás fut (protokoll {protocol_version})."
                ),
                log=tuple(log), uploaded=tuple(uploaded),
                protocol_version=protocol_version,
            )
        except PicoProvisioningError as exc:
            emit(f"HIBA [{exc.code}] a(z) {exc.step} lépésnél: {exc.message}")
            result = ProvisionResult(
                ok=False, port=port, step=exc.step, error_code=exc.code,
                message=exc.message, log=tuple(log), uploaded=tuple(uploaded),
            )
        except Exception as exc:  # noqa: BLE001 — a GUI-nak MINDIG eredményt adunk
            # Bármely váratlan kivétel (pl. a soros könyvtár OSError-je egy
            # eddig nem kezelt ponton) ugyanúgy lezárt porttal és megnevezett
            # lépéssel kell visszatérjen — a workert nem szabad némán
            # elszállítani (a dialógus különben örökre "folyamatban"
            # állapotban ragadna).
            message = f"Váratlan hiba a(z) {step} lépésnél: {exc}"
            emit(message)
            result = ProvisionResult(
                ok=False, port=port, step=step,
                error_code=ProvisionErrorCode.UNEXPECTED_ERROR,
                message=message, log=tuple(log), uploaded=tuple(uploaded),
            )
        finally:
            try:
                if ser is not None:
                    cleanup_note = self._cleanup_raw_repl(
                        ser, entered_raw_repl, allow_reset=not modification_attempted,
                    )
            finally:
                _close_quietly(ser)
        if cleanup_note:
            result = replace(
                result, message=f"{result.message} {cleanup_note}",
                log=(*result.log, cleanup_note),
            )
        return result

    def verify_service_running(self, port: str) -> bool:
        """Igaz, ha a porton MÁR FUT a BQ-szolgáltatás, és a protokoll-verziója
        egyezik a PC-oldali klienssel. A MEGLÉVŐ, éles `PicoBQTransport.
        negotiate()`-et használja — l. modul-docstring."""
        try:
            self._verify_service(port)
        except PicoProvisioningError:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Forrásfájlok                                                        #
    # ------------------------------------------------------------------ #
    def _firmware_dir(self) -> Path:
        if self.firmware_dir is not None:
            return self.firmware_dir
        return app_paths.pico_firmware_dir()

    def _load_firmware_sources(self) -> dict[str, bytes]:
        """A fájlokat BINÁRISAN olvassuk: a SHA256 csak akkor bizonyít
        bit-pontos átvitelt, ha sem itt, sem a Picon nincs sorvég-konverzió."""
        directory = self._firmware_dir()
        sources: dict[str, bytes] = {}
        for name in FIRMWARE_FILES:
            path = directory / name
            try:
                sources[name] = path.read_bytes()
            except OSError as exc:
                raise PicoProvisioningError(
                    ProvisionStep.OPEN_PORT,
                    ProvisionErrorCode.FIRMWARE_SOURCE_MISSING,
                    f"A feltöltendő Pico-firmware fájl nem olvasható: {path} ({exc})",
                ) from exc
        return sources

    def _backup_suffix(self) -> str:
        return "bak_" + time.strftime("%Y%m%d_%H%M%S", time.localtime())

    # ------------------------------------------------------------------ #
    # raw REPL primitívek                                                 #
    # ------------------------------------------------------------------ #
    def _enter_raw_repl(self, ser: RawReplSerialLike) -> None:
        """Ctrl-C ×2 (megszakít bármi futót — köztük a `serve_forever()`-t),
        majd Ctrl-A a raw REPL-be. HIL-bizonyított szekvencia."""
        try:
            ser.write(b"\r" + _CTRL_C + _CTRL_C)
            self.sleep(0.2)
            ser.reset_input_buffer()
            ser.write(b"\r" + _CTRL_A)
            data = ser.read_until(_RAW_REPL_BANNER)
        except Exception as exc:
            raise PicoProvisioningError(
                ProvisionStep.RAW_REPL, ProvisionErrorCode.RAW_REPL_ENTRY_FAILED,
                f"Soros I/O hiba a raw REPL-be lépés közben: {exc}",
            ) from exc
        if b"raw REPL" not in data:
            raise PicoProvisioningError(
                ProvisionStep.RAW_REPL, ProvisionErrorCode.RAW_REPL_ENTRY_FAILED,
                "Nem sikerült raw REPL-be lépni — a porton nem MicroPython "
                f"REPL válaszol (kapott: {data!r}). Ellenőrizd a COM portot és "
                "hogy nem használja-e más program (pl. Thonny).",
            )

    def _cleanup_raw_repl(
        self, ser: RawReplSerialLike, entered_raw_repl: bool, *, allow_reset: bool,
    ) -> str:
        """R8-B4: best-effort reset csak azonosított, módosítatlan saját kódhoz."""
        note = "A Pico BQ-szolgáltatása leállhatott; húzd ki és dugd vissza."
        if entered_raw_repl and allow_reset:
            with suppress(Exception):
                info = self._read_device_info(ser)
                files = info.get("files")
                if (
                    info.get("implementation") == "micropython"
                    and files is not None
                    and all(name in files for name in FIRMWARE_FILES)
                    and "boot.py" not in files
                    and "boot.mpy" not in files
                ):
                    sources = self._load_firmware_sources()
                    if all(
                        self._device_sha256(ser, name)
                        == hashlib.sha256(sources[name]).hexdigest()
                        for name in FIRMWARE_FILES
                    ):
                        self._soft_reset(ser)
                        return (
                            "Soft reset kiadva; a BQ-szolgáltatás futása "
                            "nincs élőben ellenőrizve."
                        )
        if entered_raw_repl:
            self._exit_raw_repl(ser)
        return note

    def _exit_raw_repl(self, ser: RawReplSerialLike) -> None:
        """Best-effort visszatérés friendly REPL-be — a hívó már úgyis lezárja
        a portot, ezért egy hiba itt nem teheti tönkre a művelet eredményét."""
        try:
            ser.write(b"\r" + _CTRL_B)
        except Exception:
            pass

    def _exec(
        self, ser: RawReplSerialLike, code: str, step: str, error_code: str,
    ) -> str:
        """Egy kódblokk futtatása raw REPL-ben. Válaszformátum:
        `OK<stdout>\\x04<stderr>\\x04`. Nem üres stderr = a Picon kivétel
        keletkezett → hiba (SOSEM csendes továbbfolyás)."""
        try:
            ser.write(code.encode("utf-8") + _CTRL_D)
            ack = ser.read(2)
            # 2026-09-12, F2 (független verifier): a raw REPL egy blokk
            # lezáró `\x04\x04`-je UTÁN azonnal kiküldi a KÖVETKEZŐ blokkra
            # váró '>' promptot is. Ha ez a bájt az ELŐZŐ `_exec()` hívás
            # válasz-olvasásakor még nem érkezett meg (időzítés-függő --
            # ~70+ egymást követő exec-blokknál egy feltöltés alatt ez NEM
            # elhanyagolható valószínűségű), akkor ITT, a KÖVETKEZŐ hívás
            # 'OK' nyugtájának ELEJÉN jelenik meg — hamis "keret-szinkron
            # vesztés" hibát okozva. Legfeljebb néhány vezető '>' karaktert
            # eldobunk, mielőtt az 'OK'-t várnánk (a protokoll szerint
            # SOSEM küld a Pico többet egyetlen '>'-nál egy blokk között).
            while ack[:1] == b">":
                ack = ack[1:] + ser.read(1)
        except Exception as exc:
            raise PicoProvisioningError(
                step, error_code, f"Soros I/O hiba kódfuttatás közben: {exc}",
            ) from exc
        if ack != b"OK":
            raise PicoProvisioningError(
                step, error_code,
                f"A raw REPL nem nyugtázta a kódblokkot ('OK' helyett {ack!r}) — "
                f"keret-szinkron vesztés. Húzd ki és dugd vissza a Pico-t, majd "
                f"próbáld újra.",
            )

        deadline = self._clock() + self.exec_timeout_s
        out = bytearray()
        while self._clock() < deadline:
            try:
                chunk = ser.read(256)
            except Exception as exc:
                raise PicoProvisioningError(
                    step, error_code, f"Soros I/O hiba a válasz olvasása közben: {exc}",
                ) from exc
            out.extend(chunk)
            if out.count(_CTRL_D) >= 2:
                break
        else:
            raise PicoProvisioningError(
                step, error_code,
                f"Időtúllépés ({self.exec_timeout_s:g} s) a Pico válaszára várva.",
            )

        parts = bytes(out).split(_CTRL_D)
        stdout = parts[0].decode("utf-8", errors="replace") if parts else ""
        stderr = parts[1].decode("utf-8", errors="replace") if len(parts) > 1 else ""
        if stderr.strip():
            raise PicoProvisioningError(
                step, error_code, f"A Pico kivételt jelzett: {stderr.strip()}",
            )
        return stdout

    # ------------------------------------------------------------------ #
    # Lépések                                                             #
    # ------------------------------------------------------------------ #
    def _read_device_info(self, ser: RawReplSerialLike) -> dict[str, Any]:
        """`sys.implementation` / `os.uname()` / `os.listdir()` egy blokkban.

        A kimenetet SOR-ALAPÚ, előtaggal címkézett formában kérjük (nem
        `json`-nal), mert egy gyári MicroPython buildben a `json` modul
        jelenléte nem garantált, és mert egy csonka/torz sor így NEM tud
        csendben más mezőként értelmeződni."""
        code = (
            "import sys, os\n"
            "print('IMPL=' + str(sys.implementation.name))\n"
            "try:\n"
            "    u = os.uname()\n"
            "    print('REL=' + str(u.release))\n"
            "    print('MACH=' + str(u.machine))\n"
            "except Exception as e:\n"
            "    print('REL=?')\n"
            "    print('MACH=?')\n"
            "print('FILES=' + ','.join(os.listdir('/')))\n"
        )
        stdout = self._exec(
            ser, code, ProvisionStep.IDENTIFY, ProvisionErrorCode.IDENTIFY_FAILED,
        )
        info: dict[str, Any] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("IMPL="):
                info["implementation"] = line[len("IMPL="):]
            elif line.startswith("REL="):
                info["version"] = line[len("REL="):]
            elif line.startswith("MACH="):
                info["machine"] = line[len("MACH="):]
            elif line.startswith("FILES="):
                raw = line[len("FILES="):]
                info["files"] = tuple(p for p in raw.split(",") if p)
        if "implementation" not in info:
            raise PicoProvisioningError(
                ProvisionStep.IDENTIFY, ProvisionErrorCode.IDENTIFY_FAILED,
                f"Az eszköz nem adott értelmezhető azonosító választ: {stdout!r}",
            )
        return info

    def _backup_file(self, ser: RawReplSerialLike, name: str, backup_name: str) -> None:
        """`os.rename()` a Picon. Ha a fájl közben mégsem létezik, az NEM hiba
        (üres/friss Pico) — de MINDEN MÁS OSError igen, mert egy sikertelen
        mentés után a felülírás visszafordíthatatlan adatvesztés lenne."""
        code = (
            "import os\n"
            f"src = {name!r}\n"
            f"dst = {backup_name!r}\n"
            "try:\n"
            "    os.stat(src)\n"
            "except OSError:\n"
            "    print('NOFILE')\n"
            "else:\n"
            "    try:\n"
            "        os.remove(dst)\n"
            "    except OSError:\n"
            "        pass\n"
            "    os.rename(src, dst)\n"
            "    print('BACKED_UP')\n"
        )
        stdout = self._exec(
            ser, code, ProvisionStep.BACKUP, ProvisionErrorCode.BACKUP_FAILED,
        )
        if "BACKED_UP" not in stdout and "NOFILE" not in stdout:
            raise PicoProvisioningError(
                ProvisionStep.BACKUP, ProvisionErrorCode.BACKUP_FAILED,
                f"A(z) {name} biztonsági mentése nem igazolt (válasz: {stdout!r}) — "
                f"a felülírás elmarad, hogy a régi fájl ne vesszen el.",
            )

    def _upload_file(
        self,
        ser: RawReplSerialLike,
        name: str,
        data: bytes,
        emit: ProgressCallback,
    ) -> None:
        """Base64-chunkolt írás. Az ELSŐ chunk 'wb' (csonkít), a többi 'ab' —
        így minden chunk ÖNÁLLÓ, állapotmentes exec-blokk (nem támaszkodik egy
        korábbi blokkból nyitva maradt fájl-objektumra)."""
        chunks = _chunk(data, _UPLOAD_CHUNK_BYTES)
        total = max(1, len(chunks))
        for index, chunk in enumerate(chunks):
            mode = "wb" if index == 0 else "ab"
            # BÁJT-literál (nem str): a MicroPython `binascii.a2b_base64()`
            # bájtokat vár, és így a base64 szövegben biztosan nincs
            # kódolás-függő értelmezés sem.
            payload = base64.b64encode(chunk)
            code = (
                "import binascii\n"
                f"f = open({name!r}, {mode!r})\n"
                f"f.write(binascii.a2b_base64({payload!r}))\n"
                "f.close()\n"
            )
            self._exec(
                ser, code, ProvisionStep.UPLOAD, ProvisionErrorCode.UPLOAD_FAILED,
            )
            if total > 1 and (index + 1) % 10 == 0:
                emit(f"      {index + 1}/{total} blokk…")
        if not chunks:
            # Üres forrásfájl: hozzuk létre üresen, hogy a SHA256-ellenőrzés
            # egy létező fájlon fusson (és ne "nincs ilyen fájl" hibát adjon,
            # ami elfedné a valódi okot).
            self._exec(
                ser, f"f = open({name!r}, 'wb')\nf.close()\n",
                ProvisionStep.UPLOAD, ProvisionErrorCode.UPLOAD_FAILED,
            )

    def _device_sha256(self, ser: RawReplSerialLike, name: str) -> str:
        """SHA256 a Picon, blokkonként olvasva. `hashlib.sha256(...).hexdigest()`
        MicroPython-ban NEM létezik (csak `update()`/`digest()`), ezért
        `binascii.hexlify()` — ugyanaz a minta, mint a vendorolt
        `pico_bq_service.py::_sha256_hex()`-ben."""
        code = (
            "import hashlib, binascii\n"
            "h = hashlib.sha256()\n"
            f"f = open({name!r}, 'rb')\n"
            "while True:\n"
            f"    b = f.read({_DEVICE_HASH_READ_BYTES})\n"
            "    if not b:\n"
            "        break\n"
            "    h.update(b)\n"
            "f.close()\n"
            "print('SHA=' + binascii.hexlify(h.digest()).decode())\n"
        )
        stdout = self._exec(
            ser, code, ProvisionStep.VERIFY_HASH,
            ProvisionErrorCode.SHA256_MISMATCH_ABORTED,
        )
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("SHA="):
                return line[len("SHA="):]
        raise PicoProvisioningError(
            ProvisionStep.VERIFY_HASH, ProvisionErrorCode.SHA256_MISMATCH_ABORTED,
            f"A Pico nem adott SHA256 választ a(z) {name} fájlra: {stdout!r} — "
            f"az átvitel bit-pontossága NEM igazolható, az előkészítés megáll.",
        )

    def _import_check(self, ser: RawReplSerialLike) -> None:
        """A feltöltött modulok tényleges importálhatósága MÉG a raw-REPL
        kapcsolat alatt. Enélkül egy szintaktikai/futásidejű hiba csak a soft
        reset UTÁN derülne ki, "a szolgáltatás nem válaszol" formában — vagyis
        a valódi ok (a Pico kivétel-szövege) elveszne.

        MEGJEGYZÉS: az import ITT ténylegesen lefoglalja az I2C0 buszt (a
        `pico_bq_service` modul-szintű `_i2c = I2C(0, ...)` miatt) — ez a
        pad-es összeállításban ártalmatlan, és a rögtön utána következő soft
        reset amúgy is elengedi."""
        code = (
            "import pico_bq_service, pico_bq_rpc\n"
            "d = pico_bq_rpc.RpcDispatcher(pico_bq_rpc.build_methods(pico_bq_service))\n"
            "print('PROTO=' + str(pico_bq_rpc.PROTOCOL_VERSION))\n"
        )
        stdout = self._exec(
            ser, code, ProvisionStep.IMPORT_CHECK, ProvisionErrorCode.IMPORT_CHECK_FAILED,
        )
        if "PROTO=" not in stdout:
            raise PicoProvisioningError(
                ProvisionStep.IMPORT_CHECK, ProvisionErrorCode.IMPORT_CHECK_FAILED,
                f"A feltöltött modulok importja nem igazolt (válasz: {stdout!r}).",
            )

    def _soft_reset(self, ser: RawReplSerialLike) -> None:
        """Ctrl-B (kilépés raw REPL-ből) + Ctrl-D (soft reboot) FRIENDLY
        módban — ez indítja el önállóan a `main.py`-t (és ezen keresztül a
        szolgáltatást), a raw-REPL kapcsolattól függetlenül.

        KRITIKUS JAVÍTÁS (független verifier F1, 2026-09-12): a MicroPython
        SOHA nem futtatja le a flash gyökerében lévő `main.py`-t, ha a soft
        reboot RAW REPL módból érkezik — a raw mód a soft reboot UTÁN is
        megmarad (nem esik vissza friendly módba). Forrás: a Thonny saját,
        terepen bizonyított MicroPython backend-je
        (`C:\\Users\\KS\\Desktop\\CLI\\TF1\\Prog\\thonny-4.1.7-windows-portable\\
        Lib\\site-packages\\thonny\\plugins\\micropython\\bare_metal_backend.py`):
        a `_clear_repl()` (raw módban marad, NEM futtat user kódot,
        :515-523) és a `_soft_reboot_for_restarting_user_program()`
        (:504-513 `_ensure_normal_mode()` ELŐSZÖR, majd :519/:532
        `SOFT_REBOOT_CMD`) EXPLICIT KÉT KÜLÖN metódus — a különbség pontosan
        az, hogy előbb Ctrl-B-vel (`NORMAL_MODE_CMD = b'\\x02'`, mp_back.py:84)
        NORMÁL módba kell váltani, és csak ott ér bármit egyetlen Ctrl-D
        (`SOFT_REBOOT_CMD = b'\\x04'`, mp_back.py:86) — FRIENDLY REPL-ben ez
        a MicroPython-konvenció szerinti soft reboot, ami lefuttatja a
        `boot.py`+`main.py`-t. A korábbi implementáció egy raw-REPL-specifikus
        SZÖVEGES parancsot (`import machine; machine.soft_reset()`) küldött
        Ctrl-B NÉLKÜL — ez feltöltötte a fájlokat, de a szolgáltatás SOHA nem
        indult volna el automatikusan valós hardveren (a `VERIFY_SERVICE`
        lépés determinisztikusan elhasalt volna minden futáson).

        Egyik lépés SEM `_exec()`-kel megy (az a lezáró `\\x04\\x04`-re várna,
        ami már nem érkezik meg) — a parancsokat kiírjuk, rövid várakozással
        a módváltás között, majd a port lezárásával/újranyitásával megyünk
        tovább."""
        try:
            ser.write(b"\r" + _CTRL_B)
            self.sleep(0.3)
            ser.write(_CTRL_D)
        except Exception as exc:
            raise PicoProvisioningError(
                ProvisionStep.SOFT_RESET, ProvisionErrorCode.SOFT_RESET_FAILED,
                f"Soros I/O hiba a soft reset kiadásakor: {exc}",
            ) from exc

    def _verify_service(self, port: str) -> dict[str, Any]:
        """ÚJ soros kapcsolat + `PicoBQTransport.negotiate()`. Minden ágon
        lezárja a transportot (`finally`), hogy a port szabadon maradjon a
        GUI tényleges BQ-kapcsolatának."""
        transport = self.transport_factory()
        try:
            try:
                transport.connect(port, timeout=2.0)
            except Exception as exc:
                raise PicoProvisioningError(
                    ProvisionStep.VERIFY_SERVICE,
                    ProvisionErrorCode.SERVICE_START_VERIFICATION_FAILED,
                    f"A(z) {port} port nem nyitható meg a szolgáltatás "
                    f"ellenőrzéséhez: {exc}",
                ) from exc
            try:
                capabilities = transport.negotiate()
            except Exception as exc:
                raise PicoProvisioningError(
                    ProvisionStep.VERIFY_SERVICE,
                    ProvisionErrorCode.SERVICE_START_VERIFICATION_FAILED,
                    f"A fájlok feltöltése és SHA256-ellenőrzése SIKERES volt, de "
                    f"a szolgáltatás nem válaszolt a várt protokollal: {exc}. "
                    f"Próbáld ki- és visszadugni a Pico-t, majd futtasd az "
                    f"Azonosítást.",
                ) from exc
        finally:
            try:
                transport.disconnect()
            except Exception:
                pass
        if not isinstance(capabilities, dict):
            raise PicoProvisioningError(
                ProvisionStep.VERIFY_SERVICE,
                ProvisionErrorCode.SERVICE_START_VERIFICATION_FAILED,
                f"A szolgáltatás érvénytelen capabilities választ adott: "
                f"{capabilities!r}",
            )
        return capabilities


# --------------------------------------------------------------------------- #
# Segédfüggvények                                                              #
# --------------------------------------------------------------------------- #
def _chunk(data: bytes, size: int) -> list[bytes]:
    return [data[i:i + size] for i in range(0, len(data), size)]


def _close_quietly(ser: RawReplSerialLike | None) -> None:
    if ser is None:
        return
    try:
        ser.close()
    except Exception:
        pass


def list_serial_ports() -> tuple[tuple[str, str], ...]:
    """KÉNYELMI kiegészítés a GUI port-választásához: (port, leírás) párok a
    `pyserial.tools.list_ports`-ból.

    NEM ez az azonosítás — a kötelező, döntő azonosítás az `identify()`
    raw-REPL/MicroPython-válasz alapú ellenőrzése (l. ottani docstring). Ha a
    pyserial nincs telepítve vagy hibázik, üres listát ad (a felhasználó
    ilyenkor kézzel írja be a portot)."""
    try:
        from serial.tools import list_ports  # type: ignore[import-untyped]

        return tuple((p.device, p.description or "") for p in list_ports.comports())
    except Exception:
        return ()


def format_identify_result(result: IdentifyResult) -> str:
    """Egységes, emberi olvasásra szánt összefoglaló — a GUI és a jövőbeli
    CLI-eszközök ugyanazt a szöveget kapják."""
    lines = [result.message]
    if result.machine:
        lines.append(f"Eszköz: {result.machine}")
    if result.version:
        lines.append(f"MicroPython verzió: {result.version}")
    if result.existing_files:
        lines.append("Fájlok a Picon: " + ", ".join(result.existing_files))
    if result.error_code:
        lines.append(f"Hibakód: {result.error_code}")
    return "\n".join(lines)
