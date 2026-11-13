"""
FakePicoRawReplSerial — in-memory MicroPython raw-REPL soros port a
`Prog/src/pico_provisioning_service.py` teszteléséhez.

A VALÓS raw-REPL DRÓTPROTOKOLLT szimulálja (nem a service belső hívásait):
  • Ctrl-C ×2 + Ctrl-A  →  a "raw REPL; CTRL-B to exit\\r\\n>" banner,
  • kódblokk + Ctrl-D   →  "OK" + stdout + \\x04 + stderr + \\x04.

Ez szándékosan a `FakePicoSerial` (JSON-Lines RPC) TESTVÉRE, nem a
kiterjesztése: a két fake KÉT KÜLÖN protokollt beszél UGYANAZON a fizikai
porton (a provisioning raw-REPL-t, a futó szolgáltatás JSON-Lines RPC-t),
ezért egy közös osztályba olvasztásuk elfedné pont azt a határt, amit a
provisioning-szekvenciának tisztelnie kell.

A fake egy MINIMÁLIS MicroPython fájlrendszert is modellez (`files` dict),
hogy a backup → írás → SHA256-verify lánc VALÓDI bájtokon, valódi hash-sel
legyen ellenőrizhető — nem előre beprogramozott "helyes" válaszokon (l.
Akkuteszter memória `feedback_tautological_fixture_shared_assumption`: a
fixture ne ugyanabból a feltevésből származzon, mint a kód).
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field

RAW_REPL_BANNER = b"raw REPL; CTRL-B to exit\r\n>"


@dataclass
class FakePicoRawReplSerial:
    """Injektálható raw-REPL soros port. A `RawReplSerialLike` Protocol
    minden metódusát implementálja."""

    #: A szimulált Pico flash gyökere: fájlnév -> tartalom.
    files: dict[str, bytes] = field(default_factory=dict)
    # R8-B4: a kezdeti futás explicit; a reset utáni indulás külön injektálható.
    service_running: bool = False
    service_starts_on_reset: bool = True
    #: `sys.implementation.name` — "cpython"-ra állítva NOT_MICROPYTHON ág.
    implementation: str = "micropython"
    release: str = "1.22.2"
    machine: str = "Raspberry Pi Pico with RP2040"

    # -- hibainjektálás ------------------------------------------------- #
    fail_raw_repl_entry: bool = False
    #: Ha igaz, az `import pico_bq_service` a Picon kivételt dob.
    fail_import_check: bool = False
    #: Ha igaz, az `os.rename()` (backup) OSError-t dob a Picon.
    fail_backup: bool = False
    #: Ha igaz, a fájlírás CSENDESEN megcsonkítja az utolsó chunkot — így a
    #: SHA256-verify a VALÓDI hash-eltérésen bukik el, nem egy hamisított
    #: hash-válaszon.
    corrupt_upload: bool = False
    #: Ha igaz, a raw REPL "OK" helyett szemetet nyugtáz (keret-szinkron vesztés).
    fail_exec_ack: bool = False
    #: Ha igaz, a `write()` OSError-t dob (soros I/O hiba).
    fail_write: bool = False
    #: 2026-09-12, fuggetlen verifier F2: a valos raw REPL egy blokk zaro
    #: `\x04\x04`-je UTAN AZONNAL kikuldi a kovetkezo '>' promptot is -- ha
    #: ez KESON (kulon USB-csomagban/kesobbi olvasasnal) erkezik meg, a
    #: KOVETKEZO `_exec()` 'OK' nyugtajanak ELEJEN jelenhet meg. Alapertelmezesben
    #: (False) a fake SZINKRON modellje ezt "egyben" adja vissza (a jelenlegi
    #: hivas sajat olvasasa nyeli el, sosem szivarog at) -- ez NEM tesztelne a
    #: hibat. Ha True, a zaro '>' KESLELTETVE, csak a KOVETKEZO write()-kor
    #: kerul az RX pufferbe, pontosan reprodukalva a versenyhelyzetet.
    simulate_delayed_prompt: bool = False
    _pending_prompt: bool = field(default=False, init=False, repr=False)

    # -- megfigyelt állapot --------------------------------------------- #
    closed: bool = field(default=False, init=False)
    soft_reset_count: int = field(default=0, init=False)
    entered_raw_repl: bool = field(default=False, init=False)
    exited_raw_repl: bool = field(default=False, init=False)
    # Kör 21 F7 (HW21-02) MAX verifier N2 lelete: `exited_raw_repl` egy
    # LATCHING bool -- egy MÁSODIK Ctrl-B sem változtatna rajta, tehát
    # önmagában NEM bizonyítja az "pontosan egyszer" állítást. Ez a
    # számláló a tényleges Ctrl-B bájt előfordulásainak SZÁMÁT követi.
    exit_raw_repl_count: int = field(default=0, init=False)
    executed_blocks: list[str] = field(default_factory=list, init=False)
    #: 2026-09-12, fuggetlen verifier F1: a VALOS MicroPython csak akkor
    #: futtatja le a flash gyokerebeli main.py-t egy soft reboot utan, ha a
    #: reboot-kiuvaltas (magaban allo Ctrl-D) FRIENDLY REPL modban erkezik --
    #: raw modban kiadva (pl. egy "machine.soft_reset()"-et tartalmazo kodblokk
    #: vegen levo Ctrl-D-vel) a reset megtortenik, de a main.py NEM fut le
    #: (forras: Thonny bare_metal_backend.py _clear_repl() vs.
    #: _soft_reboot_for_restarting_user_program(), l. pico_provisioning_
    #: service.py::_soft_reset() docstringje). Ez a mezo csak akkor True,
    #: ha a modell szerint a main.py TENYLEGESEN lefutott volna.
    main_py_ran_after_reset: bool = field(default=False, init=False)
    #: Belso REPL-mod-kovetes: "raw" (Ctrl-A utan) vagy "friendly" (Ctrl-B
    #: utan / kezdeti allapot).
    _mode: str = field(default="friendly", init=False, repr=False)

    _rx: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _tx: bytearray = field(default_factory=bytearray, init=False, repr=False)

    # ------------------------------------------------------------------ #
    # RawReplSerialLike                                                    #
    # ------------------------------------------------------------------ #
    def write(self, data: bytes) -> int:
        if self.fail_write:
            raise OSError("FakePicoRawReplSerial: simulated write failure")
        if self.closed:
            raise OSError("FakePicoRawReplSerial: port is closed")
        if self._pending_prompt:
            # 2026-09-12, F2: az ELOZO blokk kesleltetett '>' promptja MOST,
            # a KOVETKEZO parancs beerkezesekor "landol" az RX pufferben --
            # pontosan a valos, idozites-fuggo versenyhelyzetet modellezve.
            self._rx.extend(b">")
            self._pending_prompt = False
        self._tx.extend(data)
        self._process_tx()
        return len(data)

    def read(self, size: int = 1) -> bytes:
        out = bytes(self._rx[:size])
        del self._rx[:size]
        return out

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        index = bytes(self._rx).find(expected)
        if index < 0:
            out = bytes(self._rx)
            self._rx.clear()
            return out
        end = index + len(expected)
        out = bytes(self._rx[:end])
        del self._rx[:end]
        return out

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ #
    # A szimulált MicroPython oldal                                        #
    # ------------------------------------------------------------------ #
    def _process_tx(self) -> None:
        """A beérkezett bájtokból kiveszi a felismert vezérlő-szekvenciákat és
        a Ctrl-D-vel lezárt kódblokkokat."""
        while True:
            buf = bytes(self._tx)
            if b"\x01" in buf:  # Ctrl-A: raw REPL-be lépés
                index = buf.index(b"\x01")
                del self._tx[:index + 1]
                if not self.fail_raw_repl_entry:
                    self.entered_raw_repl = True
                    self._mode = "raw"
                    self._rx.extend(b"\r\n" + RAW_REPL_BANNER)
                else:
                    self._rx.extend(b"\r\nMicroPython v1.22.2\r\n>>> ")
                continue
            if b"\x02" in buf:  # Ctrl-B: friendly REPL
                index = buf.index(b"\x02")
                del self._tx[:index + 1]
                self.exited_raw_repl = True
                self.exit_raw_repl_count += 1
                self._mode = "friendly"
                continue
            if b"\x04" in buf:  # Ctrl-D: a megelőző blokk futtatása
                index = buf.index(b"\x04")
                block = bytes(self._tx[:index]).decode("utf-8", errors="replace")
                del self._tx[:index + 1]
                if block == "" and self._mode == "friendly":
                    # 2026-09-12, F1: magaban allo Ctrl-D FRIENDLY modban =
                    # MicroPython soft-reboot konvencio (SOFT_REBOOT_CMD),
                    # ami TENYLEGESEN lefuttatja a main.py-t -- nincs valasz
                    # (a VALODI eszkoz is ujraindul, nem valaszol azonnal).
                    self.soft_reset_count += 1
                    self.main_py_ran_after_reset = "main.py" in self.files
                    self.service_running = (
                        self.main_py_ran_after_reset and self.service_starts_on_reset
                    )
                    continue
                self._execute(block)
                continue
            # Ctrl-C / \r és minden egyéb: nincs válasz, eldobjuk
            if b"\x03" in buf:
                self.service_running = False
                index = buf.index(b"\x03")
                del self._tx[:index + 1]
                continue
            break

    def _execute(self, block: str) -> None:
        self.executed_blocks.append(block)
        if self.fail_exec_ack:
            self._rx.extend(b"??")
            return
        if "machine.soft_reset()" in block:
            # A soft reset SOSEM ad választ — az interpreter újraindul.
            # 2026-09-12, F1: ez a RÉGI, RAW REPL-en belüli szöveges-parancs
            # út (kódblokk vége Ctrl-D-vel, MÉG raw módban) -- szándékosan
            # NEM állítja a `main_py_ran_after_reset`-et: a modell szerint
            # (l. modul docstring) a soft reset MEGTÖRTÉNIK, de a main.py
            # NEM fut le, mert a reboot raw módból érkezett.
            self.soft_reset_count += 1
            return
        self._rx.extend(b"OK")
        try:
            stdout = self._run(block)
            stderr = ""
        except _PicoException as exc:
            stdout = ""
            stderr = str(exc)
        self._rx.extend(stdout.encode("utf-8"))
        self._rx.extend(b"\x04")
        self._rx.extend(stderr.encode("utf-8"))
        self._rx.extend(b"\x04")
        # 2026-09-12, F2: a valos raw REPL a lezaro EOT-par UTAN azonnal
        # kikuldi a kovetkezo '>' promptot is.
        if self.simulate_delayed_prompt:
            self._pending_prompt = True
        else:
            self._rx.extend(b">")

    def _run(self, block: str) -> str:
        """A provisioning által kiadott KONKRÉT kódblokkok szimulálása.
        Szándékosan mintaillesztéses (nem `exec()`), hogy a teszt ne
        ugyanazt a Python-szemantikát feltételezze, mint amit a Pico ad."""
        if block.startswith("import sys, os"):
            files = ",".join(sorted(self.files))
            return (
                f"IMPL={self.implementation}\r\n"
                f"REL={self.release}\r\n"
                f"MACH={self.machine}\r\n"
                f"FILES={files}\r\n"
            )
        if "os.rename(src, dst)" in block:
            src = _literal(block, r"src = '([^']*)'")
            dst = _literal(block, r"dst = '([^']*)'")
            if src not in self.files:
                return "NOFILE\r\n"
            if self.fail_backup:
                raise _PicoException("OSError: [Errno 1] EPERM")
            self.files[dst] = self.files.pop(src)
            return "BACKED_UP\r\n"
        if "binascii.a2b_base64(" in block:
            name = _literal(block, r"open\('([^']*)', '(?:wb|ab)'\)")
            mode = _literal(block, r"open\('[^']*', '(wb|ab)'\)")
            payload = _literal(block, r"a2b_base64\(b'([^']*)'\)")
            data = base64.b64decode(payload)
            if self.corrupt_upload:
                data = data[:-1] if len(data) > 1 else b""
            if mode == "wb":
                self.files[name] = data
            else:
                self.files[name] = self.files.get(name, b"") + data
            return ""
        if "hashlib.sha256()" in block:
            name = _literal(block, r"open\('([^']*)', 'rb'\)")
            if name not in self.files:
                raise _PicoException("OSError: [Errno 2] ENOENT")
            return "SHA=" + hashlib.sha256(self.files[name]).hexdigest() + "\r\n"
        if block.startswith("import pico_bq_service, pico_bq_rpc"):
            if self.fail_import_check:
                raise _PicoException(
                    'Traceback (most recent call last):\r\n'
                    '  File "<stdin>", line 1, in <module>\r\n'
                    '  File "pico_bq_service.py", line 51, in <module>\r\n'
                    'ImportError: no module named \'machine\''
                )
            missing = [n for n in ("pico_bq_service.py", "pico_bq_rpc.py") if n not in self.files]
            if missing:
                raise _PicoException(f"ImportError: no module named {missing[0][:-3]!r}")
            return "PROTO=1.0\r\n"
        if block.startswith("f = open(") and "'wb'" in block:
            name = _literal(block, r"open\('([^']*)', 'wb'\)")
            self.files[name] = b""
            return ""
        return ""


class _PicoException(Exception):
    """A szimulált Pico-oldali kivétel — a fake ezt írja a stderr-csatornára."""


def _literal(block: str, pattern: str) -> str:
    match = re.search(pattern, block)
    if match is None:
        raise AssertionError(
            f"FakePicoRawReplSerial: a kódblokk nem illeszkedik a várt mintára "
            f"({pattern!r}):\n{block}"
        )
    return match.group(1)
