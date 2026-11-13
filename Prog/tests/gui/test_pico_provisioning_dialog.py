"""
PicoProvisioningDialog GUI tesztek.

Fókusz (`feedback_gui_signal_chain_testing`): a TELJES worker→signal→handler
lánc — hogy a soros I/O nem a GUI-szálon fut, a progress élőben megjelenik,
a hibaág nem ragasztja be a dialógust, és a szál minden úton takarítva lesz.

A `PicoProvisioningService` itt egy FAKE raw-REPL soros porttal van
injektálva (nincs se hardver, se pyserial-függés) — a szolgáltatás saját,
részletes tesztjei: Prog/tests/test_pico_provisioning_service.py.
"""
from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtWidgets import QMessageBox

from Prog.gui.panels.pico_provisioning_dialog import PicoProvisioningDialog
from Prog.src.pico_provisioning_service import (
    FIRMWARE_FILES,
    PicoProvisioningService,
    ProvisionErrorCode,
)
from Prog.tests.mock_drivers.fake_pico_raw_repl_serial import FakePicoRawReplSerial


class _FakeTransport:
    def __init__(self, negotiate_error=None):
        self._negotiate_error = negotiate_error
        self.disconnect_calls = 0

    def connect(self, port, timeout=1.0):
        return None

    def negotiate(self):
        if self._negotiate_error is not None:
            raise self._negotiate_error
        return {"protocol_version": "1.0", "methods": ["capabilities"]}

    def disconnect(self):
        self.disconnect_calls += 1


@pytest.fixture(autouse=True)
def _no_blocking_question_or_critical(monkeypatch):
    """A GUI-conftest az information/warning/critical hívásokat teszi
    no-op-pá (D6, 2026-09-13 óta a critical-t is) — ez a dialógus
    `question()`-t is használ, amely egyébként VALÓDI modális ablakot
    nyitna egy offscreen eseményhurokban és megfagyasztaná a
    tesztfutást. A `critical` itt lokálisan is le van nullázva (a
    conftest default-ját idempotensen megerősítve) — a modul-lokális
    `monkeypatch.setattr()` a conftest autouse fixture UTÁN fut, tehát
    önmagában is elegendő lenne, de nem árt az explicit redundancia."""
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)


def _service(firmware_dir, fake_serial, transport=None):
    return PicoProvisioningService(
        firmware_dir=firmware_dir,
        serial_factory=lambda port: fake_serial,
        transport_factory=lambda: transport if transport is not None else _FakeTransport(),
        post_reset_wait_s=0.0,
        sleep=lambda seconds: None,
        exec_timeout_s=1.0,
    )


@pytest.fixture
def firmware_dir(tmp_path):
    (tmp_path / "pico_bq_service.py").write_bytes(b"# service\n" + b"S" * 1200)
    (tmp_path / "pico_bq_rpc.py").write_bytes(b"# rpc\n" + b"R" * 800)
    (tmp_path / "main.py").write_bytes(b"# main\nmain()\n")
    return tmp_path


def _pump_until(qapp, predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


class TestDialogConstruction:
    def test_wiring_instructions_are_visible_from_the_start(self, qapp, firmware_dir):
        """A bekötési útmutató NEM csak siker után jelenik meg — a
        felhasználónak a bekötés ELŐTT kell."""
        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, FakePicoRawReplSerial())
        )
        try:
            text = dlg._wiring_view.toPlainText()
            assert "GP4" in text and "P3/SDA" in text
            assert "GP5" in text and "P4/SCL" in text
            assert "GND" in text and "VSS" in text
            assert "FORRÁSOK:" in text
        finally:
            dlg.close()

    def test_provision_button_requires_explicit_confirmation(self, qapp, firmware_dir):
        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, FakePicoRawReplSerial())
        )
        try:
            assert dlg._provision_btn.isEnabled() is False
            dlg._confirm_check.setChecked(True)
            assert dlg._provision_btn.isEnabled() is True
            dlg._confirm_check.setChecked(False)
            assert dlg._provision_btn.isEnabled() is False
        finally:
            dlg.close()

    def test_identify_without_port_warns_and_starts_no_thread(self, qapp, firmware_dir):
        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, FakePicoRawReplSerial())
        )
        try:
            dlg._port_edit.setText("   ")
            dlg._on_identify_clicked()
            assert dlg._thread is None
        finally:
            dlg.close()


class TestIdentifySignalChain:
    def test_identify_runs_off_gui_thread_and_reports_result(self, qapp, firmware_dir):
        fake = FakePicoRawReplSerial(files={})
        dlg = PicoProvisioningDialog(service=_service(firmware_dir, fake))
        try:
            dlg._port_edit.setText("COM8")
            dlg._on_identify_clicked()
            # A kattintás VISSZATÉRÉSEKOR még nincs eredmény — ez bizonyítja,
            # hogy a soros I/O nem a GUI-szálon futott le szinkron módon.
            assert dlg._thread is not None
            assert dlg._last_identify is None
            assert dlg._identify_btn.isEnabled() is False

            _pump_until(qapp, lambda: dlg._last_identify is not None)

            assert dlg._last_identify is not None
            assert dlg._last_identify.ok is True
            assert dlg._last_identify.is_micropython is True
            assert dlg._thread is None           # a szál takarítva
            assert dlg._identify_btn.isEnabled() is True
            assert "micropython" in dlg._log_view.toPlainText().lower()
        finally:
            dlg.close()

    def test_identify_failure_reenables_controls(self, qapp, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, fail_raw_repl_entry=True)
        dlg = PicoProvisioningDialog(service=_service(firmware_dir, fake))
        try:
            dlg._port_edit.setText("COM8")
            dlg._on_identify_clicked()
            _pump_until(qapp, lambda: dlg._last_identify is not None)

            assert dlg._last_identify.ok is False
            assert dlg._last_identify.error_code == ProvisionErrorCode.RAW_REPL_ENTRY_FAILED
            # A dialógus NEM ragadt be: újra lehet próbálkozni.
            assert dlg._thread is None
            assert dlg._identify_btn.isEnabled() is True
            assert dlg._port_edit.isEnabled() is True
        finally:
            dlg.close()


class TestProvisionSignalChain:
    def test_successful_provision_streams_progress_and_finishes(self, qapp, firmware_dir):
        fake = FakePicoRawReplSerial(files={})
        transport = _FakeTransport()
        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, fake, transport)
        )
        try:
            dlg._port_edit.setText("COM8")
            dlg._confirm_check.setChecked(True)
            dlg._on_provision_clicked()
            assert dlg._thread is not None

            _pump_until(qapp, lambda: dlg._last_provision is not None)

            result = dlg._last_provision
            assert result is not None and result.ok is True, getattr(result, "message", "")
            # Az élő progress ténylegesen átjött a szálhatáron.
            log = dlg._log_view.toPlainText()
            for marker in ("1/9", "5/9", "6/9", "9/9", "KÉSZ"):
                assert marker in log
            assert set(fake.files) >= set(FIRMWARE_FILES)
            assert fake.soft_reset_count == 1
            assert transport.disconnect_calls == 1
            assert dlg._thread is None
        finally:
            dlg.close()

    def test_hash_mismatch_shows_error_and_does_not_start_service(self, qapp, firmware_dir):
        fake = FakePicoRawReplSerial(files={}, corrupt_upload=True)
        transport = _FakeTransport()
        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, fake, transport)
        )
        try:
            dlg._port_edit.setText("COM8")
            dlg._confirm_check.setChecked(True)
            dlg._on_provision_clicked()
            _pump_until(qapp, lambda: dlg._last_provision is not None)

            result = dlg._last_provision
            assert result.ok is False
            assert result.error_code == ProvisionErrorCode.SHA256_MISMATCH_ABORTED
            assert "SIKERTELEN" in dlg._status_lbl.text()
            assert fake.soft_reset_count == 0
            assert transport.disconnect_calls == 0
            # Újrapróbálható állapot.
            assert dlg._thread is None
            assert dlg._identify_btn.isEnabled() is True
        finally:
            dlg.close()

    def test_failure_after_successful_uploads_names_the_residue_in_log_and_dialog(
        self, qapp, firmware_dir, monkeypatch
    ):
        """[Kör 20 Batch E11, F20-G3, DFAM-018+DFAM-026+DFAM-021] Ha a hiba
        AZUTÁN történik, hogy mind a 3 fájl sikeresen feltöltődött és
        SHA256-tal igazolódott (itt: VERIFY_SERVICE lépés hibázik), a
        kezelőnek meg kell tudnia, hogy a Pico flash-ére MÁR felkerültek
        fájlok -- korábban a hiba-ág csak a step/error_code/message-t
        jelenítette meg, a result.uploaded-et sosem."""
        fake = FakePicoRawReplSerial(files={})
        transport = _FakeTransport(negotiate_error=RuntimeError("timeout"))
        captured_critical_text = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda self_, title, text: captured_critical_text.append(text),
        )
        dlg = PicoProvisioningDialog(service=_service(firmware_dir, fake, transport))
        try:
            dlg._port_edit.setText("COM8")
            dlg._confirm_check.setChecked(True)
            dlg._on_provision_clicked()
            _pump_until(qapp, lambda: dlg._last_provision is not None)

            result = dlg._last_provision
            assert result.ok is False
            assert result.error_code == ProvisionErrorCode.SERVICE_START_VERIFICATION_FAILED
            # Előfeltétel: a diszkriminációhoz mind a 3 fájlnak fel kellett
            # töltődnie -- ha ez nem igaz, a teszt maga tautologikus lenne.
            assert len(result.uploaded) == len(FIRMWARE_FILES)

            log = dlg._log_view.toPlainText()
            for f in FIRMWARE_FILES:
                assert f in log

            assert len(captured_critical_text) == 1
            dialog_text = captured_critical_text[0]
            for f in FIRMWARE_FILES:
                assert f in dialog_text
            assert "3 fájl" in dialog_text
        finally:
            dlg.close()

    def test_failure_before_any_upload_does_not_mention_residue(
        self, qapp, firmware_dir, monkeypatch
    ):
        """Counter-test: ha a hiba MÉG az első feltöltés ELŐTT történik
        (result.uploaded üres), a hiba-üzenet NE tartalmazzon
        residue-szöveget -- a kapu nem lehet szélesebb, mint amit a
        result.uploaded ténylegesen igazol."""
        fake = FakePicoRawReplSerial(files={}, corrupt_upload=True)
        captured_critical_text = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda self_, title, text: captured_critical_text.append(text),
        )
        dlg = PicoProvisioningDialog(service=_service(firmware_dir, fake))
        try:
            dlg._port_edit.setText("COM8")
            dlg._confirm_check.setChecked(True)
            dlg._on_provision_clicked()
            _pump_until(qapp, lambda: dlg._last_provision is not None)

            result = dlg._last_provision
            assert result.ok is False
            assert result.uploaded == ()

            assert len(captured_critical_text) == 1
            assert "felkerült" not in captured_critical_text[0]
        finally:
            dlg.close()

    def test_declined_confirmation_does_not_touch_hardware(
        self, qapp, firmware_dir, monkeypatch
    ):
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        fake = FakePicoRawReplSerial(files={})
        dlg = PicoProvisioningDialog(service=_service(firmware_dir, fake))
        try:
            dlg._port_edit.setText("COM8")
            dlg._confirm_check.setChecked(True)
            dlg._on_provision_clicked()

            assert dlg._thread is None
            assert fake.files == {}
            assert fake.entered_raw_repl is False
            assert "megszakította" in dlg._log_view.toPlainText()
        finally:
            dlg.close()


class _GatedSerial(FakePicoRawReplSerial):
    """Determinisztikusan "még fut" állapotban tartható fake: az ELSŐ
    `read()` megvárja a kaput. Így a closeEvent-védelem versenyhelyzet
    nélkül tesztelhető (nem `sleep`-re/időzítésre alapozva)."""

    def __init__(self, **kwargs) -> None:
        # A szülő `@dataclass`-generált __init__-je NEM hív __post_init__-et
        # (a szülő nem definiál ilyet), ezért explicit konstruktor kell.
        super().__init__(**kwargs)
        self.gate = threading.Event()
        self._gate_used = False

    def read(self, size: int = 1) -> bytes:
        if not self._gate_used:
            self._gate_used = True
            self.gate.wait(timeout=10.0)
        return super().read(size)


class TestCloseProtection:
    def test_close_is_blocked_while_a_worker_runs(self, qapp, firmware_dir):
        """Egy megszakított feltöltés csonka fájlt hagyhat az eszközön —
        a dialógus ezért NEM zárható, amíg a művelet tart."""
        fake = _GatedSerial(files={})
        dlg = PicoProvisioningDialog(service=_service(firmware_dir, fake))
        try:
            dlg.show()
            dlg._port_edit.setText("COM8")
            dlg._confirm_check.setChecked(True)
            dlg._on_provision_clicked()
            _pump_until(qapp, lambda: dlg._thread is not None and dlg._thread.isRunning())
            assert dlg._thread is not None and dlg._thread.isRunning()

            dlg.close()
            # A bezárás VISSZAUTASÍTVA: az ablak látható marad, a szál él.
            assert dlg.isVisible() is True
            assert dlg._thread is not None and dlg._thread.isRunning()
            assert dlg._last_provision is None

            fake.gate.set()
            _pump_until(qapp, lambda: dlg._last_provision is not None)
            assert dlg._last_provision is not None and dlg._last_provision.ok is True
            # Befejezés UTÁN már zárható.
            dlg.close()
            assert dlg.isVisible() is False
            assert dlg._thread is None
        finally:
            fake.gate.set()
            dlg.close()

    def test_close_after_completion_cleans_up_thread(self, qapp, firmware_dir):
        fake = FakePicoRawReplSerial(files={})
        dlg = PicoProvisioningDialog(service=_service(firmware_dir, fake))
        dlg._port_edit.setText("COM8")
        dlg._on_identify_clicked()
        _pump_until(qapp, lambda: dlg._last_identify is not None)

        dlg.close()
        assert dlg._thread is None
        assert dlg._worker is None


class _FakeThreadStillRunning:
    """[Kör 21 F12, F21-G5] Ugyanaz a determinisztikus stub-minta, mint
    `test_bq_calibration_dialog.py::TestReview4thRoundCloseIgnoredWhen
    AbortDoesNotFinishInTime` -- a `wait(5000)` a valós QThread API-t
    hívná, itt csak False-t ad vissza, mintha a szál MÉG FUTNA.

    `isRunning()` SZÁNDÉKOSAN False -- ez a pico_provisioning_dialog.py::
    closeEvent() SAJÁT, ELSŐ `isRunning()`-kapuját (más forgatókönyv:
    egy AKTÍV, még be sem fejezett műveletet fed) hagyja átfolyni a
    `_finish_worker()`-en át tesztelt, MÁSODIK ágba. FONTOS: ez a
    kombináció (`isRunning()=False`, `wait()=False`) egy VALÓS QThreaden
    ugyanazon `self._thread` objektumon SZINTE ELÉRHETETLEN (a két
    hívás ugyanazt a mutex-vezérelt állapotot nézi) -- a stub
    SZÁNDÉKOSAN önellentmondó állapotot épít, hogy a defenzív,
    konzervatív második kaput izoláltan tesztelje, NEM egy ma valós
    QThreaddel reprodukálható forgatókönyvet szimulál."""

    def quit(self):
        pass

    def isRunning(self):
        return False

    def wait(self, timeout_ms):
        return False

    def deleteLater(self):
        raise AssertionError(
            "deleteLater() nem hívható egy még futó QThread-en"
        )


class TestKor21BatchF12FinishWorkerTimeoutKeepsBusyState:
    """Kör 21 F12 (F21-G5, DFAM-016+DFAM-001): `_finish_worker()` korábban
    CSENDBEN `None`-t adott vissza, ha a `wait(5000)` timeoutolt, `self.
    _thread`-et élve/beállítva hagyva -- a hívók (`_on_identify_finished`/
    `_on_provision_finished`) EZT NEM ELLENŐRIZTÉK, feltétel nélkül
    `_set_busy(False)`-t hívtak, visszaengedélyezve az Azonosítás/
    Előkészítés gombokat egy MÉG ÉLŐ szál mellett."""

    def test_identify_finished_with_stuck_thread_keeps_busy_state(
        self, qapp, firmware_dir
    ):
        from Prog.src.pico_provisioning_service import IdentifyResult

        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, FakePicoRawReplSerial())
        )
        try:
            dlg._port_edit.setText("COM8")
            dlg._set_busy(True)  # a valódi _on_identify_clicked() is ezt tenné
            dlg._thread = _FakeThreadStillRunning()
            dlg._worker = None

            dlg._on_identify_finished(
                IdentifyResult(ok=True, port="COM8", is_micropython=True, message="OK")
            )

            assert dlg._thread is not None, (
                "a szál referenciáját meg kell tartani, amíg a wait() nem "
                "igazolja a leállást"
            )
            assert dlg._identify_btn.isEnabled() is False, (
                "a gombok NEM engedélyezhetők vissza, amíg a szál "
                "leállítása bizonytalan"
            )
            assert dlg._provision_btn.isEnabled() is False
        finally:
            dlg._thread = None  # takarítás: a fake stub nem valódi QThread
            dlg._worker = None
            dlg.close()

    def test_provision_finished_with_stuck_thread_keeps_busy_state(
        self, qapp, firmware_dir
    ):
        from Prog.src.pico_provisioning_service import ProvisionResult

        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, FakePicoRawReplSerial())
        )
        try:
            dlg._port_edit.setText("COM8")
            dlg._confirm_check.setChecked(True)
            dlg._set_busy(True)
            dlg._thread = _FakeThreadStillRunning()
            dlg._worker = None

            dlg._on_provision_finished(
                ProvisionResult(ok=True, port="COM8", step="DONE", message="OK")
            )

            assert dlg._thread is not None
            assert dlg._identify_btn.isEnabled() is False
            assert dlg._provision_btn.isEnabled() is False
        finally:
            dlg._thread = None
            dlg._worker = None
            dlg.close()

    def test_stuck_thread_blocks_a_second_worker_start(self, qapp, firmware_dir):
        """Az archívum TEST-specifikációja szerint egy timeoutolt szál
        NEM engedhet egy második `_start_worker()`-t -- ez a meglévő
        `if self._thread is not None: return` kapun ÁT strukturálisan
        garantált (MÁR A FIX ELŐTT is így volt, ez a kapu ezt a batchet
        MEGELŐZŐEN is létezett és véd), de korábban SOHA nem volt
        explicit tesztje. Ez a teszt NEM ezt a batchet, hanem ezt a
        MEGLÉVŐ, KORÁBBI invariánst rögzíti karakterizáló tesztként."""
        from Prog.src.pico_provisioning_service import IdentifyResult

        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, FakePicoRawReplSerial())
        )
        try:
            dlg._port_edit.setText("COM8")
            dlg._set_busy(True)
            stuck_thread = _FakeThreadStillRunning()
            dlg._thread = stuck_thread
            dlg._worker = None

            dlg._on_identify_finished(
                IdentifyResult(ok=True, port="COM8", is_micropython=True, message="OK")
            )
            assert dlg._thread is stuck_thread

            dlg._on_identify_clicked()
            assert dlg._thread is stuck_thread, (
                "a self._thread is not None korai return-nek a MÁSODIK "
                "kattintást is no-op-ként kellett volna elnyelnie"
            )
        finally:
            dlg._thread = None
            dlg._worker = None
            dlg.close()

    def test_close_event_blocked_when_finish_worker_reports_stuck_thread(
        self, qapp, firmware_dir, monkeypatch
    ):
        dlg = PicoProvisioningDialog(
            service=_service(firmware_dir, FakePicoRawReplSerial())
        )
        try:
            dlg._thread = _FakeThreadStillRunning()
            dlg._worker = None

            warning_calls = []
            monkeypatch.setattr(
                QMessageBox, "warning",
                lambda *a, **k: warning_calls.append(a) or None,
            )

            class _FakeEvent:
                def __init__(self):
                    self.ignored = False
                    self.accepted = False

                def ignore(self):
                    self.ignored = True

                def accept(self):
                    self.accepted = True

            event = _FakeEvent()
            dlg.closeEvent(event)

            assert event.ignored is True
            assert event.accepted is False
            assert len(warning_calls) == 1
            assert dlg._thread is not None
        finally:
            dlg._thread = None
            dlg._worker = None
            dlg.close()


class TestMainWindowMenuGuards:
    """A menüpont kölcsönös kizárása (main_window.py::
    `_on_pico_provisioning_requested`).

    A dialógus MODÁLIS és ÖNÁLLÓ soros kapcsolatot nyit — ha egy fizikai
    teszt vagy egy másik BQ-kapcsolat fut, a megnyitása port-versengést
    okozna, ÉS elérhetetlenné tenné a Vészleállítás gombot (CLAUDE.md §15).
    A teszt a metódust egy minimál állapot-stubon hívja, hogy a MainWindow
    teljes (pyqtgraph-os) felépítése nélkül is determinisztikus legyen."""

    @staticmethod
    def _call_with(monkeypatch, **state):
        from types import SimpleNamespace

        import Prog.gui.panels.pico_provisioning_dialog as dialog_module
        from Prog.gui.main_window import MainWindow

        opened: list[object] = []
        monkeypatch.setattr(
            dialog_module, "PicoProvisioningDialog",
            lambda parent=None: opened.append(parent) or SimpleNamespace(exec=lambda: 0),
        )
        stub = SimpleNamespace(
            _worker=None, _bq_worker=None, _session_owns_bq_transport=False,
            _bq_connect_worker=None, _connect_worker=None,
        )
        for key, value in state.items():
            setattr(stub, key, value)
        MainWindow._on_pico_provisioning_requested(stub)
        return opened

    def test_opens_when_nothing_else_is_active(self, qapp, monkeypatch):
        assert len(self._call_with(monkeypatch)) == 1

    @pytest.mark.parametrize(
        "blocking_state",
        [
            {"_worker": object()},                     # futó fizikai teszt
            {"_bq_worker": object()},                  # BQ Diagnosztika kapcsolat
            {"_session_owns_bq_transport": True},      # BQ tanítási session
            {"_bq_connect_worker": object()},          # BQ kapcsolódás folyamatban
            {"_connect_worker": object()},             # műszeres bring-up folyamatban
        ],
    )
    def test_blocked_while_another_activity_owns_the_port_or_gui(
        self, qapp, monkeypatch, blocking_state
    ):
        assert self._call_with(monkeypatch, **blocking_state) == []


class TestR7B1ProvisionConfirmationDefaultNo:
    """R7-B1: valódi QMessageBox a hívó gombjaival/alapértékével, fake I/O,
    worker -> signal -> handler.

    A statikus QMessageBox.question() beágyazott eseményhurkot futtat; a
    teljes GUI-suite-ban egy korábbi teszt után a beágyazott hurkok azonnal
    visszatérnek (TINFRA), ezért a teszt a question()-t egy olyan
    csomagolóra cseréli, amely a KAPOTT argumentumokkal (ha hiányoznak: a Qt
    statikus alapértékeivel, Yes|No és NoButton) valódi QMessageBox-ot épít,
    és arra küldi a billentyűt / kattintást hurok nélkül."""

    @pytest.fixture(autouse=True)
    def _no_blocking_question_or_critical(self):
        """A modul Yes-stubja helyett a lenti csomagoló fusson."""

    @pytest.mark.parametrize("action", ["return", "yes", "escape"])
    def test_real_confirmation_controls_provision(
        self, qapp, firmware_dir, monkeypatch, action
    ):
        from unittest.mock import Mock

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        monkeypatch.setattr(
            "Prog.gui.panels.pico_provisioning_dialog.list_serial_ports",
            list,
        )
        fake = FakePicoRawReplSerial(files={})
        service = _service(firmware_dir, fake)
        provision = Mock(wraps=service.provision)
        monkeypatch.setattr(service, "provision", provision)
        dlg = PicoProvisioningDialog(service=service)
        start_worker = Mock(wraps=dlg._start_worker)
        monkeypatch.setattr(dlg, "_start_worker", start_worker)
        shown = []

        def question(
            parent, title, text,
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            default_button=QMessageBox.StandardButton.NoButton,
        ):
            box = QMessageBox(parent)
            box.setWindowTitle(title)
            box.setText(text)
            box.setStandardButtons(buttons)
            if default_button != QMessageBox.StandardButton.NoButton:
                box.setDefaultButton(default_button)
            box.show()
            qapp.processEvents()
            if action == "yes":
                QTest.mouseClick(
                    box.button(QMessageBox.StandardButton.Yes),
                    Qt.MouseButton.LeftButton,
                )
            else:
                key = Qt.Key.Key_Return if action == "return" else Qt.Key.Key_Escape
                QTest.keyClick(box, key)
            shown.append(box.isVisible())
            clicked = box.clickedButton()
            answer = (
                box.standardButton(clicked) if clicked is not None
                else QMessageBox.StandardButton.NoButton
            )
            box.deleteLater()
            return answer

        monkeypatch.setattr(QMessageBox, "question", question)
        try:
            dlg._port_edit.setText("COM8")
            dlg._confirm_check.setChecked(True)
            initial_status = dlg._status_lbl.text()
            dlg.show()
            dlg._provision_btn.click()

            assert shown == [False]
            if action == "yes":
                _pump_until(qapp, lambda: dlg._last_provision is not None)
                assert dlg._last_provision is not None and dlg._last_provision.ok is True
                start_worker.assert_called_once()
                provision.assert_called_once()
                assert provision.call_args.args[0] == "COM8"
                assert provision.call_args.kwargs == {"confirm": True}
                assert set(fake.files) >= set(FIRMWARE_FILES)
                assert fake.soft_reset_count == 1
            else:
                start_worker.assert_not_called()
                provision.assert_not_called()
                assert dlg._last_provision is None
                assert fake.files == {}
                assert fake.entered_raw_repl is False
                assert "megerősítés visszautasítva" in dlg._log_view.toPlainText()
                assert dlg._status_lbl.text() == initial_status
            assert dlg._thread is None
            assert dlg._worker is None
            assert dlg._provision_btn.isEnabled() is True
        finally:
            _pump_until(qapp, lambda: dlg._thread is None)
            dlg._finish_worker()
            dlg.close()
