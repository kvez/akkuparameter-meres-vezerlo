"""
PicoProvisioningService tesztek — FAKE raw-REPL soros porttal, hardver nélkül.

BIZONYÍTÉK-SZINT: UNIT/MOCK. Ezek a tesztek a PC-oldali provisioning-logikát
igazolják (lépéssorrend, hibakezelés, port-lezárás, hash-kapu) — NEM
bizonyítják a valós Pico-n való működést (HIL_PENDING, l. a modul
docstringje).

A fake egy VALÓDI, bájt-pontos fájlrendszert modellez és VALÓDI SHA256-ot
számít a nála landolt bájtokból — a hash-kapu tesztje ezért nem tautologikus
(l. `feedback_tautological_fixture_shared_assumption`).
"""
from __future__ import annotations

import hashlib

import pytest

from Prog.src.pico_provisioning_service import (
    FIRMWARE_FILES,
    WIRING_INSTRUCTIONS,
    PicoProvisioningService,
    ProvisionErrorCode,
    ProvisionStep,
    get_wiring_instructions,
)
from Prog.tests.mock_drivers.fake_pico_raw_repl_serial import FakePicoRawReplSerial

# --------------------------------------------------------------------------- #
# Segédek                                                                      #
# --------------------------------------------------------------------------- #
SOURCES = {
    "pico_bq_service.py": b"# service\n" + b"A" * 2500 + b"\n",
    "pico_bq_rpc.py": b"# rpc\n" + b"B" * 1500 + b"\n",
    "main.py": b"# main\nmain()\n",
}


@pytest.fixture
def firmware_dir(tmp_path):
    for name, data in SOURCES.items():
        (tmp_path / name).write_bytes(data)
    return tmp_path


class FakeTransport:
    """Minimál `PicoBQTransport` helyettesítő a záró negotiate()-hez."""

    def __init__(self, capabilities=None, negotiate_error=None, connect_error=None):
        self._capabilities = capabilities if capabilities is not None else {
            "protocol_version": "1.0", "methods": ["capabilities"], "read_only": True,
        }
        self._negotiate_error = negotiate_error
        self._connect_error = connect_error
        self.connected = False
        self.disconnect_calls = 0

    def connect(self, port, timeout=1.0):
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    def negotiate(self):
        if self._negotiate_error is not None:
            raise self._negotiate_error
        return self._capabilities

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False


def _service(firmware_dir, fake_serial, transport=None, **kwargs):
    transport = transport if transport is not None else FakeTransport()
    return PicoProvisioningService(
        firmware_dir=firmware_dir,
        serial_factory=lambda port: fake_serial,
        transport_factory=lambda: transport,
        post_reset_wait_s=0.0,
        sleep=lambda seconds: None,
        exec_timeout_s=1.0,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# identify()                                                                   #
# --------------------------------------------------------------------------- #
class TestIdentify:
    def test_identifies_empty_micropython_pico(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={})
        result = _service(firmware_dir, fake).identify("COM8")

        assert result.ok is True
        assert result.is_micropython is True
        assert result.implementation == "micropython"
        assert result.machine == "Raspberry Pi Pico with RP2040"
        assert result.already_provisioned is False
        assert fake.entered_raw_repl is True
        # Csak-olvasó művelet: sem írás, sem soft reset nem történt.
        assert fake.files == {}
        assert fake.soft_reset_count == 0
        # A port minden ágon lezárul, és friendly REPL-be lépünk vissza.
        assert fake.closed is True
        assert fake.exited_raw_repl is True

    def test_reports_already_provisioned_pico(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={name: b"x" for name in FIRMWARE_FILES})
        result = _service(firmware_dir, fake).identify("COM8")

        assert result.ok is True
        assert result.already_provisioned is True
        assert set(result.existing_files) == set(FIRMWARE_FILES)

    def test_non_micropython_device_is_rejected(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, implementation="cpython")
        result = _service(firmware_dir, fake).identify("COM8")

        assert result.ok is False
        assert result.is_micropython is False
        assert result.error_code == ProvisionErrorCode.NOT_MICROPYTHON
        assert fake.closed is True

    def test_raw_repl_entry_failure_gives_clean_error_and_closes_port(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, fail_raw_repl_entry=True)
        result = _service(firmware_dir, fake).identify("COM8")

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.RAW_REPL_ENTRY_FAILED
        assert "raw REPL" in result.message
        assert fake.closed is True

    def test_port_open_failure_is_reported_not_raised(self, firmware_dir):
        def boom(port):
            raise OSError("could not open port COM99")

        service = PicoProvisioningService(
            firmware_dir=firmware_dir, serial_factory=boom,
            transport_factory=lambda: FakeTransport(), sleep=lambda s: None,
        )
        result = service.identify("COM99")

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.PORT_OPEN_FAILED
        assert "COM99" in result.message


# --------------------------------------------------------------------------- #
# provision() — sikeres teljes szekvencia                                      #
# --------------------------------------------------------------------------- #
class TestProvisionSuccess:
    def test_full_sequence_uploads_verifies_and_starts_service(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={})
        transport = FakeTransport()
        progress: list[str] = []

        result = _service(firmware_dir, fake, transport).provision(
            "COM8", progress.append, confirm=True,
        )

        assert result.ok is True, result.message
        assert result.step == ProvisionStep.DONE
        assert result.error_code is None
        assert result.protocol_version == "1.0"

        # 1) Mindhárom fájl BIT-PONTOSAN landolt a Picon.
        for name, data in SOURCES.items():
            assert fake.files[name] == data
        # 2) A jelentett hash a VALÓDI tartalom hash-e.
        assert {u.name for u in result.uploaded} == set(FIRMWARE_FILES)
        for uploaded in result.uploaded:
            assert uploaded.sha256 == hashlib.sha256(SOURCES[uploaded.name]).hexdigest()
            assert uploaded.size_bytes == len(SOURCES[uploaded.name])
            assert uploaded.backup_name is None  # üres Picon nincs mit menteni
        # 3) Soft reset történt (a main.py autostartja emiatt él).
        assert fake.soft_reset_count == 1
        # 3b) [regresszió, független verifier F1, 2026-09-12] a soft reset
        # FRIENDLY REPL módból ment ki (Ctrl-B ELŐBB, majd magában álló
        # Ctrl-D) -- enélkül a valós MicroPython SOHA nem futtatná le a
        # main.py-t (a raw mód a soft reboot UTÁN is megmarad). Ez a teszt
        # a RÉGI, csak-raw-módú szöveges "machine.soft_reset()" implementáción
        # BUKNA (main_py_ran_after_reset maradna False).
        assert fake.exited_raw_repl is True
        assert fake.main_py_ran_after_reset is True
        # 4) A záró ellenőrzés ÚJ kapcsolaton futott, és lezárta magát.
        assert transport.disconnect_calls == 1
        # 5) A port lezárva.
        assert fake.closed is True
        # 6) A progress-log minden lépést jelzett.
        assert progress == list(result.log)
        assert any("1/9" in line for line in progress)
        assert any("9/9" in line for line in progress)
        assert any("KÉSZ" in line for line in progress)

    def test_main_py_is_uploaded_last(self, firmware_dir):
        """A `main.py` SZÁNDÉKOSAN utolsó: egy megszakadt feltöltés ne hagyjon
        a Picon olyan autostart fájlt, ami hiányzó modult importálna."""
        fake = FakePicoRawReplSerial(files={})
        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is True
        assert [u.name for u in result.uploaded][-1] == "main.py"
        assert FIRMWARE_FILES[-1] == "main.py"

    def test_existing_files_are_backed_up_before_overwrite(self, firmware_dir):
        old = {name: b"REGI TARTALOM " + name.encode() for name in FIRMWARE_FILES}
        fake = FakePicoRawReplSerial(files=dict(old))

        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is True
        for uploaded in result.uploaded:
            assert uploaded.backup_name is not None
            assert uploaded.backup_name.startswith(uploaded.name + ".bak_")
            # A mentés a RÉGI bájtokat őrizte meg, nem az újakat.
            assert fake.files[uploaded.backup_name] == old[uploaded.name]
            assert fake.files[uploaded.name] == SOURCES[uploaded.name]

    def test_survives_delayed_raw_repl_prompt_between_exec_blocks(self, firmware_dir):
        """[regresszió, független verifier F2, 2026-09-12] A valós raw REPL
        egy blokk lezáró `\\x04\\x04`-je UTÁN azonnal kiküldi a következő
        blokkra váró '>' promptot is. Ha ez KÉSVE (külön USB-csomagban)
        érkezik, a KÖVETKEZŐ `_exec()` 'OK' nyugtájának ELEJÉN jelenhet meg
        -- hamis "keret-szinkron vesztés" hibát okozva egy ~70+ blokkos
        feltöltés valamelyik pontján. Ez a teszt a RÉGI (a vezető '>'-t nem
        eldobó) `_exec()`-en BUKNA."""
        fake = FakePicoRawReplSerial(files={}, simulate_delayed_prompt=True)

        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is True, result.message
        assert result.step == ProvisionStep.DONE
        for name, data in SOURCES.items():
            assert fake.files[name] == data


# --------------------------------------------------------------------------- #
# provision() — hibaágak                                                       #
# --------------------------------------------------------------------------- #
class TestProvisionFailures:
    def test_requires_explicit_confirm(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={})
        result = _service(firmware_dir, fake).provision("COM8", confirm=False)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.NOT_CONFIRMED
        # A hardverhez HOZZÁ SEM nyúltunk.
        assert fake.files == {}
        assert fake.entered_raw_repl is False
        assert fake.closed is False

    def test_sha256_mismatch_aborts_before_service_start(self, firmware_dir):
        """A legfontosabb kapu: csonka átvitel esetén NEM indul el a
        szolgáltatás, és nincs soft reset."""
        fake = FakePicoRawReplSerial(files={}, corrupt_upload=True)
        transport = FakeTransport()

        result = _service(firmware_dir, fake, transport).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.SHA256_MISMATCH_ABORTED
        assert result.step == ProvisionStep.VERIFY_HASH
        assert fake.soft_reset_count == 0            # nem indítottuk el
        assert transport.disconnect_calls == 0       # el sem jutottunk idáig
        assert "main.py" not in fake.files           # az autostart fájl nem került fel
        assert fake.closed is True                   # a port így is lezárult

    def test_raw_repl_entry_failure_reports_step_and_closes_port(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, fail_raw_repl_entry=True)
        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.RAW_REPL
        assert result.error_code == ProvisionErrorCode.RAW_REPL_ENTRY_FAILED
        assert fake.files == {}
        assert fake.closed is True

    def test_non_micropython_device_aborts_before_any_write(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, implementation="cpython")
        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.IDENTIFY
        assert result.error_code == ProvisionErrorCode.NOT_MICROPYTHON
        assert fake.files == {}

    def test_backup_failure_aborts_before_overwrite(self, firmware_dir):
        old = {name: b"NE VESZEK EL" for name in FIRMWARE_FILES}
        fake = FakePicoRawReplSerial(files=dict(old), fail_backup=True)

        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.BACKUP
        assert result.error_code == ProvisionErrorCode.BACKUP_FAILED
        # A régi tartalom sértetlen — a felülírás elmaradt.
        assert fake.files["pico_bq_service.py"] == old["pico_bq_service.py"]
        assert fake.closed is True

    def test_missing_backup_source_is_not_an_error(self, firmware_dir):
        """Üres/friss Picon nincs mit menteni — ez normál út, nem hiba."""
        fake = FakePicoRawReplSerial(files={}, fail_backup=True)
        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is True, result.message
        assert all(u.backup_name is None for u in result.uploaded)
        assert any("nincs mit menteni" in line for line in result.log)

    def test_import_check_failure_aborts_before_soft_reset(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, fail_import_check=True)
        transport = FakeTransport()

        result = _service(firmware_dir, fake, transport).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.IMPORT_CHECK
        assert result.error_code == ProvisionErrorCode.IMPORT_CHECK_FAILED
        # A Pico SAJÁT kivétel-szövege eljut a kezelőig — nem nyeljük el.
        assert "ImportError" in result.message
        assert fake.soft_reset_count == 0
        assert transport.disconnect_calls == 0

    def test_service_verification_failure_is_reported_with_uploads_kept(self, firmware_dir):
        from Prog.drivers.pico_bq_transport import PicoBQTransportError

        fake = FakePicoRawReplSerial(files={})
        transport = FakeTransport(
            negotiate_error=PicoBQTransportError("nincs válasz (timeout)")
        )

        result = _service(firmware_dir, fake, transport).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.VERIFY_SERVICE
        assert result.error_code == ProvisionErrorCode.SERVICE_START_VERIFICATION_FAILED
        # A feltöltés+hash SIKERES volt — ez az információ nem veszhet el.
        assert len(result.uploaded) == len(FIRMWARE_FILES)
        assert "SIKERES" in result.message
        # A transport minden ágon lezárult (a port szabad marad).
        assert transport.disconnect_calls == 1

    def test_exec_ack_failure_reports_frame_desync(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, fail_exec_ack=True)
        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.IDENTIFY
        assert result.error_code == ProvisionErrorCode.IDENTIFY_FAILED
        assert fake.closed is True

    def test_missing_firmware_source_aborts_before_touching_hardware(self, tmp_path):
        fake = FakePicoRawReplSerial(files={})
        # tmp_path üres — nincs feltöltendő forrás.
        result = _service(tmp_path, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.FIRMWARE_SOURCE_MISSING
        assert fake.entered_raw_repl is False
        assert fake.closed is False  # meg sem nyitottuk

    def test_serial_write_failure_does_not_escape_as_exception(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, fail_write=True)
        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.RAW_REPL
        assert result.error_code == ProvisionErrorCode.RAW_REPL_ENTRY_FAILED


# --------------------------------------------------------------------------- #
# Kör 21 F7 (HW21-02): a Pico NE ragadjon raw-REPL módban semelyik hibaágon  #
# --------------------------------------------------------------------------- #
class TestKor21BatchF7ExitsRawReplOnEveryErrorPath:
    """Kör 21 Batch F7 (HW21-02, DFAM-016+DFAM-001): korábban `_exit_raw_
    repl()` KIZÁRÓLAG `identify()` sikeres ágán és `provision()` sikeres
    (`_soft_reset()`-en át történő) ágán futott -- minden hibaág a Picot
    RAW-REPL MÓDBAN hagyta zárva: a main.py/RPC-szolgáltatás nem indul
    újra, a KÖVETKEZŐ `_call()` értelmezhetetlen raw-REPL keretezést
    kapna. A `FakePicoRawReplSerial.exited_raw_repl` mező (Ctrl-B tényleges
    kiküldését követi) a diszkriminátor -- korábban ezeken az ágakon
    végig `False` maradt volna."""

    def test_identify_read_device_info_failure_exits_raw_repl(self, firmware_dir):
        """A 3. leaky path, amit a reviewer eredetileg NEM vett észre (l.
        archívum SIBLING/ANALOGUE megjegyzése) -- fail_exec_ack a
        _read_device_info() lépését buktatja identify()-ban, MÁR raw
        REPL-be lépve."""
        fake = FakePicoRawReplSerial(files={}, fail_exec_ack=True)
        result = _service(firmware_dir, fake).identify("COM8")

        assert result.ok is False
        assert fake.entered_raw_repl is True
        assert fake.exited_raw_repl is True
        assert fake.closed is True

    def test_identify_success_still_exits_raw_repl_exactly_as_before(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={})
        result = _service(firmware_dir, fake).identify("COM8")

        assert result.ok is True
        assert fake.exited_raw_repl is True

    def test_identify_raw_repl_entry_failure_does_not_attempt_exit(self, firmware_dir):
        """Counter-teszt: ha raw REPL-be SOHA nem léptünk be, a `finally`
        NEM próbál kilépni -- a `entered_raw_repl` flag (nem `ser is not
        None` önmagában) a helyes kapu, l. FIX-INDUCED RISK az
        archívumban."""
        fake = FakePicoRawReplSerial(files={}, fail_raw_repl_entry=True)
        result = _service(firmware_dir, fake).identify("COM8")

        assert result.ok is False
        assert fake.entered_raw_repl is False
        assert fake.exited_raw_repl is False

    def test_provision_backup_failure_exits_raw_repl(self, firmware_dir):
        old = {name: b"NE VESZEK EL" for name in FIRMWARE_FILES}
        fake = FakePicoRawReplSerial(files=dict(old), fail_backup=True)

        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.BACKUP
        assert fake.exited_raw_repl is True

    def test_provision_sha256_mismatch_exits_raw_repl(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, corrupt_upload=True)

        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.VERIFY_HASH
        assert fake.exited_raw_repl is True

    def test_provision_import_check_failure_exits_raw_repl(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, fail_import_check=True)

        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.IMPORT_CHECK
        assert fake.exited_raw_repl is True

    def test_provision_non_micropython_exits_raw_repl(self, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, implementation="cpython")

        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.IDENTIFY
        assert fake.exited_raw_repl is True

    def test_provision_raw_repl_entry_failure_does_not_attempt_exit(self, firmware_dir):
        """Counter-teszt, 1:1 az identify() testvérével."""
        fake = FakePicoRawReplSerial(files={}, fail_raw_repl_entry=True)

        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert fake.entered_raw_repl is False
        assert fake.exited_raw_repl is False

    def test_provision_success_exits_raw_repl_exactly_once_via_soft_reset(
        self, firmware_dir
    ):
        """A sikeres ág a `_soft_reset()`-en (SAJÁT Ctrl-B-jén) át lép ki --
        a `finally`-beli ÚJ guard (`ser is not None`) ezen az ágon no-op,
        mert `ser`-t a `_soft_reset()` UTÁN None-ra állítottuk. [MAX
        verifier N2 lelete, 2026-09-19: az `exited_raw_repl` egy LATCHING
        bool, önmagában NEM bizonyítja az "pontosan egyszer" állítást --
        egy MÁSODIK, redundáns Ctrl-B is zölden hagyta volna ezt a
        tesztet. Az `exit_raw_repl_count` (a Ctrl-B bájt tényleges
        előfordulás-száma) a valódi diszkriminátor.] Ez a teszt azt
        igazolja, hogy a fix NEM vezet be egy MÁSODIK, redundáns kilépési
        kísérletet a sikeres úton."""
        fake = FakePicoRawReplSerial(files={})
        result = _service(firmware_dir, fake).provision("COM8", confirm=True)

        assert result.ok is True, result.message
        assert fake.exit_raw_repl_count == 1
        assert fake.soft_reset_count == 1

    def test_open_port_failure_never_attempts_raw_repl_exit(
        self, firmware_dir, monkeypatch
    ):
        """A guard `ser is not None` fele (MAX verifier N8 lelete): ha a
        port MEGNYITÁSA hibázik, `ser` a metódus teljes futása alatt None
        marad -- a `finally` ne próbáljon `_exit_raw_repl(None)`-t hívni.
        `_exit_raw_repl()` SAJÁT belső `except Exception: pass`-e egy
        `None.write()`-ből eredő `AttributeError`-t is elnyelne, tehát egy
        sima "nem dob kivételt" assert NEM diszkriminálna -- a hívást
        magát kémleljük."""
        def _raise_serial_factory(port):
            raise OSError("simulated port open failure")

        exit_calls: list = []
        service = PicoProvisioningService(
            firmware_dir=firmware_dir,
            serial_factory=_raise_serial_factory,
            transport_factory=lambda: FakeTransport(),
            post_reset_wait_s=0.0,
            sleep=lambda seconds: None,
            exec_timeout_s=1.0,
        )
        monkeypatch.setattr(
            service, "_exit_raw_repl", lambda ser: exit_calls.append(ser)
        )

        result = service.provision("COM8", confirm=True)

        assert result.ok is False
        assert result.step == ProvisionStep.OPEN_PORT
        assert exit_calls == []


# --------------------------------------------------------------------------- #
# verify_service_running()                                                     #
# --------------------------------------------------------------------------- #
class TestVerifyServiceRunning:
    def test_true_when_service_negotiates(self, firmware_dir):
        transport = FakeTransport()
        service = _service(firmware_dir, FakePicoRawReplSerial(), transport)

        assert service.verify_service_running("COM8") is True
        assert transport.disconnect_calls == 1

    def test_false_when_negotiate_fails(self, firmware_dir):
        from Prog.drivers.pico_bq_transport import PicoBQTransportError

        transport = FakeTransport(negotiate_error=PicoBQTransportError("protocol mismatch"))
        service = _service(firmware_dir, FakePicoRawReplSerial(), transport)

        assert service.verify_service_running("COM8") is False
        assert transport.disconnect_calls == 1

    def test_false_when_port_cannot_be_opened(self, firmware_dir):
        transport = FakeTransport(connect_error=OSError("access denied"))
        service = _service(firmware_dir, FakePicoRawReplSerial(), transport)

        assert service.verify_service_running("COM8") is False

    def test_false_on_malformed_capabilities(self, firmware_dir):
        transport = FakeTransport(capabilities=None)
        transport._capabilities = "nem dict"
        service = _service(firmware_dir, FakePicoRawReplSerial(), transport)

        assert service.verify_service_running("COM8") is False


# --------------------------------------------------------------------------- #
# Bekötési útmutató                                                            #
# --------------------------------------------------------------------------- #
class TestWiringInstructions:
    def test_contains_the_sourced_pin_assignment_and_ground(self):
        text = get_wiring_instructions()
        assert text == WIRING_INSTRUCTIONS
        assert "GP4" in text and "SDA" in text
        assert "GP5" in text and "SCL" in text
        assert "GND" in text and "VSS" in text
        # A felhúzás és a forrásmegjelölés kötelező eleme az útmutatónak.
        assert "4,7 kΩ" in text
        assert "FORRÁSOK:" in text

    def test_warns_against_powering_gauge_from_pico(self):
        text = get_wiring_instructions()
        assert "NE tápláld" in text


# --------------------------------------------------------------------------- #
# A ténylegesen szállított Pico-firmware fájlok                                #
# --------------------------------------------------------------------------- #
class TestVendoredFirmwarePackage:
    def test_all_firmware_files_exist_and_parse(self):
        """A vendorolt fájlok a fejléc-átírás UTÁN is szintaktikailag épek
        kell legyenek. (L. `feedback_docstring_edit_premature_triple_quote`:
        egy docstring-szerkesztés csendben elronthat egy fájlt — ez a teszt
        ezt a hibaosztályt zárja ki a szállított csomagra.)"""
        import ast

        from Prog import app_paths

        directory = app_paths.pico_firmware_dir()
        for name in FIRMWARE_FILES:
            path = directory / name
            assert path.is_file(), f"hiányzó Pico-firmware fájl: {path}"
            ast.parse(path.read_bytes(), filename=str(path))

    def test_vendored_files_declare_provenance(self):
        """A TF1-eredet és a manuális szinkron kötelezettsége a fájlban
        legyen dokumentálva — nem csak a commit-üzenetben."""
        from Prog import app_paths

        directory = app_paths.pico_firmware_dir()
        for name in ("pico_bq_service.py", "pico_bq_rpc.py"):
            text = (directory / name).read_text(encoding="utf-8")
            assert "Vendorolt masolat a TF1 repo" in text
            assert "MANUALISAN szinkronizalando" in text

    def test_service_header_has_no_tf1_panel_service_mode_warning(self):
        """A TF1-panel-specifikus „szerviz mód / soha nem kerülhet üzembe”
        figyelmeztetés NEM alkalmazható az Akkuteszter pad-es kontextusára —
        a fejlécben nem szerepelhet (l. modul-docstring "HATOKOR ES KOCKAZAT")."""
        from Prog import app_paths

        text = (app_paths.pico_firmware_dir() / "pico_bq_service.py").read_text(
            encoding="utf-8"
        )
        header = text.split('"""')[1]
        assert "SOHA nem" not in header
        assert "kerulhet uzembe" not in header
        assert "melykisules-vedelem" not in header
        # Ami VISZONT kötelező: a bekötés és a busz-kizárólagosság.
        assert "GP4" in header and "GP5" in header
        assert "NE koss mas I2C-eszkozt" in header

    def test_real_firmware_package_provisions_bit_exactly(self):
        """Végponttól végpontig a VALÓDI, szállított fájlokkal (fake soros
        porton): a Picon landolt bájtok bit-pontosan egyeznek a csomaggal."""
        from Prog import app_paths

        directory = app_paths.pico_firmware_dir()
        fake = FakePicoRawReplSerial(files={})
        service = PicoProvisioningService(
            firmware_dir=None,  # app_paths-ből oldja fel
            serial_factory=lambda port: fake,
            transport_factory=lambda: FakeTransport(),
            post_reset_wait_s=0.0,
            sleep=lambda seconds: None,
            exec_timeout_s=1.0,
        )
        result = service.provision("COM8", confirm=True)

        assert result.ok is True, result.message
        for name in FIRMWARE_FILES:
            assert fake.files[name] == (directory / name).read_bytes()
        assert fake.soft_reset_count == 1


class TestR8B4ServiceCleanup:
    WARNING = "A Pico BQ-szolgáltatása leállhatott; húzd ki és dugd vissza."

    @staticmethod
    def _service(monkeypatch, fake):
        service = _service(None, fake)
        monkeypatch.setattr(service, "_load_firmware_sources", lambda: dict(SOURCES))
        return service

    @staticmethod
    def _invoke(service, operation):
        if operation == "identify":
            return service.identify("COM8")
        return service.provision("COM8", confirm=True)

    @staticmethod
    def _live_check(fake, monkeypatch):
        from Prog.drivers.pico_bq_transport import PicoBQTransport
        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial

        rpc = FakePicoSerial(simulate_missing_ok_key=not fake.service_running)
        transport = PicoBQTransport(serial=rpc, max_retries=1)
        service = _service(None, fake, transport)
        assert fake.closed is True
        with monkeypatch.context() as patch:
            patch.setattr("serial.Serial", lambda *args, **kwargs: rpc)
            result = service.verify_service_running("COM8")
        assert rpc.closed is True
        return result

    @pytest.mark.parametrize("running", [True, False])
    def test_identify_known_firmware_restarts_without_writing(self, monkeypatch, running):
        fake = FakePicoRawReplSerial(files=dict(SOURCES), service_running=running)
        result = self._service(monkeypatch, fake).identify("COM8")

        assert result.ok is True
        assert result.already_provisioned is True
        assert fake.files == SOURCES
        assert fake.soft_reset_count == 1
        assert fake.exit_raw_repl_count == 1
        assert fake.service_running is True
        assert fake.closed is True
        assert "nincs élőben ellenőrizve" in result.message
        assert self._live_check(fake, monkeypatch) is True

    @pytest.mark.parametrize("name", [*FIRMWARE_FILES, "boot.py", "boot.mpy"])
    def test_unidentified_startup_code_is_not_rebooted(self, monkeypatch, name):
        files = dict(SOURCES)
        files[name] = b"# foreign code\n"
        fake = FakePicoRawReplSerial(files=dict(files), service_running=True)
        result = self._service(monkeypatch, fake).identify("COM8")

        assert result.ok is True
        assert result.already_provisioned is True
        assert fake.files == files
        assert fake.soft_reset_count == 0
        assert fake.main_py_ran_after_reset is False
        assert fake.service_running is False
        assert fake.closed is True
        assert self.WARNING in result.message
        assert self._live_check(fake, monkeypatch) is False

    @pytest.mark.parametrize("missing", [*FIRMWARE_FILES, "all"])
    def test_incomplete_firmware_is_not_rebooted(self, monkeypatch, missing):
        files = {} if missing == "all" else {
            name: data for name, data in SOURCES.items() if name != missing
        }
        fake = FakePicoRawReplSerial(files=dict(files), service_running=True)
        result = self._service(monkeypatch, fake).identify("COM8")

        assert result.ok is True
        assert result.already_provisioned is False
        assert fake.files == files
        assert fake.soft_reset_count == 0
        assert fake.service_running is False
        assert fake.closed is True
        assert self.WARNING in result.message

    @pytest.mark.parametrize("operation", ["identify", "provision"])
    @pytest.mark.parametrize("persistent", [False, True])
    def test_identify_error_kept_and_cleanup_uses_fresh_evidence(
        self, monkeypatch, operation, persistent,
    ):
        from Prog.src.pico_provisioning_service import PicoProvisioningError

        fake = FakePicoRawReplSerial(files=dict(SOURCES), service_running=True)
        service = self._service(monkeypatch, fake)
        original = service._read_device_info
        calls = []

        def read_info(ser):
            calls.append(ser)
            if len(calls) == 1 or persistent:
                raise PicoProvisioningError(
                    ProvisionStep.IDENTIFY, ProvisionErrorCode.IDENTIFY_FAILED, "original",
                )
            return original(ser)

        monkeypatch.setattr(service, "_read_device_info", read_info)
        result = self._invoke(service, operation)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.IDENTIFY_FAILED
        assert result.message.startswith("original")
        assert fake.files == SOURCES
        assert fake.soft_reset_count == (0 if persistent else 1)
        assert fake.service_running is (not persistent)
        assert fake.closed is True
        if persistent:
            assert self.WARNING in result.message
        if operation == "provision":
            assert result.step == ProvisionStep.IDENTIFY
            assert result.uploaded == ()
            assert result.log[-1] in result.message
        assert self._live_check(fake, monkeypatch) is (not persistent)

    @pytest.mark.parametrize("operation", ["identify", "provision"])
    def test_partial_entry_reports_interruption_without_reset(self, monkeypatch, operation):
        fake = FakePicoRawReplSerial(
            files=dict(SOURCES), service_running=True, fail_raw_repl_entry=True,
        )
        result = self._invoke(self._service(monkeypatch, fake), operation)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.RAW_REPL_ENTRY_FAILED
        assert fake.entered_raw_repl is False
        assert fake.service_running is False
        assert fake.soft_reset_count == 0
        assert fake.exit_raw_repl_count == 0
        assert fake.executed_blocks == []
        assert fake.files == SOURCES
        assert fake.closed is True
        assert self.WARNING in result.message
        if operation == "provision":
            assert self.WARNING in result.log
        assert self._live_check(fake, monkeypatch) is False

    @pytest.mark.parametrize("failure", ["files", "hash", "source", "reset"])
    def test_uncertain_cleanup_preserves_identification(self, monkeypatch, failure):
        fake = FakePicoRawReplSerial(files=dict(SOURCES), service_running=True)
        service = self._service(monkeypatch, fake)

        def fail(*args):
            raise OSError("cleanup failure")

        if failure == "files":
            original = fake._run

            def no_listing(block):
                output = original(block)
                return "\n".join(
                    line for line in output.splitlines() if not line.startswith("FILES=")
                )

            monkeypatch.setattr(fake, "_run", no_listing)
        else:
            method = {
                "hash": "_device_sha256",
                "source": "_load_firmware_sources",
                "reset": "_soft_reset",
            }[failure]
            monkeypatch.setattr(service, method, fail)
        result = service.identify("COM8")

        assert result.ok is True
        assert fake.files == SOURCES
        assert fake.soft_reset_count == 0
        assert fake.service_running is False
        assert fake.closed is True
        assert self.WARNING in result.message
        assert self._live_check(fake, monkeypatch) is False

    @pytest.mark.parametrize("backup_index", [1, 2])
    def test_backup_attempt_blocks_cleanup_reset(self, monkeypatch, backup_index):
        fake = FakePicoRawReplSerial(files=dict(SOURCES), service_running=True)
        service = self._service(monkeypatch, fake)
        original = service._backup_file
        calls = []

        def backup(ser, name, backup_name):
            calls.append(name)
            fake.fail_backup = len(calls) == backup_index
            return original(ser, name, backup_name)

        monkeypatch.setattr(service, "_backup_file", backup)
        result = service.provision("COM8", confirm=True)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.BACKUP_FAILED
        assert result.step == ProvisionStep.BACKUP
        assert len(calls) == backup_index
        assert (FIRMWARE_FILES[0] in fake.files) is (backup_index == 1)
        assert result.uploaded == ()
        assert fake.soft_reset_count == 0
        assert fake.service_running is False
        assert fake.closed is True
        assert self.WARNING in result.message
        assert self.WARNING in result.log

    @pytest.mark.parametrize(
        ("flag", "code"),
        [
            ("corrupt_upload", ProvisionErrorCode.SHA256_MISMATCH_ABORTED),
            ("fail_import_check", ProvisionErrorCode.IMPORT_CHECK_FAILED),
        ],
    )
    def test_upload_errors_do_not_restart_partial_firmware(self, monkeypatch, flag, code):
        fake = FakePicoRawReplSerial(files=dict(SOURCES), service_running=True)
        setattr(fake, flag, True)
        result = self._service(monkeypatch, fake).provision("COM8", confirm=True)

        assert result.ok is False
        assert result.error_code == code
        assert fake.soft_reset_count == 0
        assert fake.service_running is False
        assert fake.closed is True
        assert self.WARNING in result.message
        assert self.WARNING in result.log

    def test_unexpected_prewrite_error_kept_after_cleanup_reset(self, monkeypatch):
        fake = FakePicoRawReplSerial(files=dict(SOURCES), service_running=True)
        service = self._service(monkeypatch, fake)

        def fail_suffix():
            raise RuntimeError("original suffix failure")

        monkeypatch.setattr(service, "_backup_suffix", fail_suffix)
        result = service.provision("COM8", confirm=True)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.UNEXPECTED_ERROR
        assert "original suffix failure" in result.message
        assert fake.files == SOURCES
        assert fake.soft_reset_count == 1
        assert fake.service_running is True
        assert fake.closed is True
        assert result.log[-1] in result.message
        assert self._live_check(fake, monkeypatch) is True

    def test_reset_attempt_is_not_reported_as_live_service_success(self, monkeypatch):
        fake = FakePicoRawReplSerial(
            files=dict(SOURCES), service_running=True, service_starts_on_reset=False,
        )
        result = self._service(monkeypatch, fake).identify("COM8")

        assert result.ok is True
        assert fake.soft_reset_count == 1
        assert fake.service_running is False
        assert fake.closed is True
        assert "nincs élőben ellenőrizve" in result.message
        assert self._live_check(fake, monkeypatch) is False

    def test_successful_provision_still_requires_live_check(self, monkeypatch):
        fake = FakePicoRawReplSerial(files={})
        service = self._service(monkeypatch, fake)
        transport = FakeTransport(negotiate_error=RuntimeError("not running"))
        service.transport_factory = lambda: transport
        result = service.provision("COM8", confirm=True)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.SERVICE_START_VERIFICATION_FAILED
        assert fake.soft_reset_count == 1
        assert fake.exit_raw_repl_count == 1
        assert fake.closed is True
        assert transport.disconnect_calls == 1

    def test_upload_attempt_blocks_reset_even_before_write_ack(self, monkeypatch):
        from Prog.src.pico_provisioning_service import PicoProvisioningError

        fake = FakePicoRawReplSerial(files=dict(SOURCES), service_running=True)
        service = self._service(monkeypatch, fake)
        original = service._read_device_info
        calls = []

        def read_info(ser):
            info = original(ser)
            if not calls:
                info["files"] = ()
            calls.append("info")
            return info

        def fail_upload(*args):
            calls.append("upload")
            raise PicoProvisioningError(
                ProvisionStep.UPLOAD, ProvisionErrorCode.UPLOAD_FAILED, "uncertain write",
            )

        monkeypatch.setattr(service, "_read_device_info", read_info)
        monkeypatch.setattr(service, "_upload_file", fail_upload)
        result = service.provision("COM8", confirm=True)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.UPLOAD_FAILED
        assert result.step == ProvisionStep.UPLOAD
        assert calls == ["info", "upload"]
        assert fake.files == SOURCES
        assert fake.soft_reset_count == 0
        assert fake.service_running is False
        assert fake.closed is True
        assert self.WARNING in result.message
        assert self.WARNING in result.log

    def test_provision_reset_failure_is_not_retried_by_cleanup(self, monkeypatch):
        from Prog.src.pico_provisioning_service import PicoProvisioningError

        fake = FakePicoRawReplSerial(files=dict(SOURCES), service_running=True)
        service = self._service(monkeypatch, fake)
        calls = []

        def fail_reset(ser):
            calls.append(ser)
            raise PicoProvisioningError(
                ProvisionStep.SOFT_RESET, ProvisionErrorCode.SOFT_RESET_FAILED, "reset failed",
            )

        monkeypatch.setattr(service, "_soft_reset", fail_reset)
        result = service.provision("COM8", confirm=True)

        assert result.ok is False
        assert result.error_code == ProvisionErrorCode.SOFT_RESET_FAILED
        assert result.step == ProvisionStep.SOFT_RESET
        assert result.message.startswith("reset failed")
        assert calls == [fake]
        assert len(result.uploaded) == len(FIRMWARE_FILES)
        assert fake.soft_reset_count == 0
        assert fake.service_running is False
        assert fake.closed is True
        assert self.WARNING in result.message
        assert self.WARNING in result.log
