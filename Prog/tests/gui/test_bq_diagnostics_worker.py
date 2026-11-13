"""BqDiagnosticsWorker unit tesztek — mock driverrel, valódi QTimer/QThread
nélkül (poll_once() közvetlen hívása, ugyanaz a minta mint test_worker.py).
A TestBqDiagnosticsWorkerThreadSafety osztály KIVÉTEL — az VALÓDI QThread-et
indít, l. ott a docstringet."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QThread

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.gui.bq_diagnostics_worker import BqDiagnosticsWorker
from Prog.tests.mock_drivers.mock_bq34z110_transport import MockBq34z110Transport


def make_driver() -> tuple[Bq34z110ReadOnlyDriver, MockBq34z110Transport]:
    transport = MockBq34z110Transport()
    driver = Bq34z110ReadOnlyDriver(transport=transport)
    return driver, transport


class TestBqDiagnosticsWorkerSignals:
    def test_poll_once_emits_snapshot_ready(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        worker = BqDiagnosticsWorker(driver)
        snapshots = []
        worker.snapshot_ready.connect(snapshots.append)
        worker.poll_once()
        assert len(snapshots) == 1
        assert snapshots[0].voltage_mV.value == 12500

    def test_poll_once_with_timeout_still_emits_snapshot_not_connection_error(self):
        """poll_bq_diagnostics() mezőnkénti hibakezelést végez — egy transport-
        szintű timeout NEM okoz teljes worker-szintű connection_error-t, csak
        a snapshot egyes mezői lesznek invalid=True (l. bq_diagnostics.py
        kapu-szabálya)."""
        driver, transport = make_driver()
        transport.simulate_timeout = True
        worker = BqDiagnosticsWorker(driver)
        snapshots = []
        errors = []
        worker.snapshot_ready.connect(snapshots.append)
        worker.connection_error.connect(errors.append)
        worker.poll_once()
        assert len(snapshots) == 1
        assert len(errors) == 0
        assert snapshots[0].voltage_mV.valid is False

    def test_connection_error_emitted_if_polling_itself_raises(self, qapp):
        """Ha maga a poll_bq_diagnostics() hívás (nem egy mező) dob kivételt
        — pl. driver=None konstrukciós hiba — a worker connection_error-t
        emittál, nem omlik el csendben."""
        worker = BqDiagnosticsWorker(driver=None)  # type: ignore[arg-type]
        errors = []
        snapshots = []
        worker.connection_error.connect(errors.append)
        worker.snapshot_ready.connect(snapshots.append)
        worker.poll_once()
        assert len(errors) == 1
        assert len(snapshots) == 0

    def test_start_polling_starts_timer_and_polls_immediately(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        worker = BqDiagnosticsWorker(driver, poll_interval_ms=5000)
        snapshots = []
        worker.snapshot_ready.connect(snapshots.append)
        worker.start_polling()
        assert len(snapshots) == 1, "start_polling() should poll immediately, not wait a full interval"
        assert worker._timer is not None
        assert worker._timer.isActive()
        worker.stop_polling()
        assert not worker._timer.isActive()


class _FakeSerialForThreadTest:
    """Minimál soros-port fake, ami rögzíti, MELYIK OS-szálon hívták a
    close()-t — a WP-AK-07 Critical fix (független review C-1/C-2)
    igazolásához kell, hogy a bezárás TÉNYLEGESEN a worker saját szálán
    történjen, ne a hívó (GUI-szerű) száion."""

    def __init__(self) -> None:
        self.close_calls = 0
        self.close_thread_ident: int | None = None

    def close(self) -> None:
        self.close_calls += 1
        self.close_thread_ident = threading.get_ident()


class TestBqDiagnosticsWorkerThreadSafety:
    """[WP-AK-07 Critical fix, független review C-1/C-2] VALÓDI QThread-et
    indít (nem csak a QObject-et hívja közvetlenül, mint a fenti osztályok)
    — ez pontosan az a szál-életciklus-út, amit a review hiányolt
    (test-lefedettségi rés, l. I-2 lelet)."""

    def test_stop_requested_closes_serial_on_worker_thread_not_caller_thread(self, qapp):
        driver, transport = make_driver()
        fake_serial = _FakeSerialForThreadTest()
        # Nagy poll_interval_ms, hogy a QTimer NE fusson újra a teszt alatt —
        # csak az azonnali start_polling()-beli első poll_once() történjen meg.
        worker = BqDiagnosticsWorker(driver, serial_conn=fake_serial, poll_interval_ms=60000)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.start_polling)
        # [WP-AK-07 Critical fix, 2. kör] NEM kötünk semmit a `thread.quit()`-ra
        # itt — a worker a takarítás VÉGÉN saját magát kéri leállni
        # (`BqDiagnosticsWorker._stop_and_close()`: `self.thread().quit()`).
        # Két KORÁBBI verzió itt hibás volt: (a) a hívó szálról közvetlenül,
        # szinkron `thread.quit()` rögtön `stop_requested.emit()` után —
        # EMPIRIKUSAN reprodukált verseny, `close_calls==0` lett; (b) egy
        # `stopped` szignál -> GUI-szálon élő `thread.quit()` slot —
        # HOLTPONTHOZ vezetett volna éles kódban (l. main_window.py
        # _cleanup_bq_thread() docstring), mert `thread.wait()` blokkolja a
        # hívó szálat, ami alatt az nem tudna egy keresztszálas queued
        # `quit()`-hívást fogadni.
        thread.start()
        # Determinisztikus szinkron: meg kell várni, hogy a szál TÉNYLEG
        # elindult, ÉS start_polling() (ami a worker szálán fut le) már
        # létrehozta a QTimer-t — enélkül a rögtön utána hívott
        # stop_requested.emit()+quit() még azelőtt lefuthatna, hogy a szál
        # eseményhurka egyáltalán elindult volna (flaky race, nem a
        # tesztelt viselkedés hibája). `worker._timer` egyszerű attribútum-
        # pollozással (nem szignál-alapú szinkronnal) — a `thread.started`
        # jel keresztszálas kézbesítési időzítése Qt/PySide6-ban nem
        # garantáltan azonnali, ez a legmegbízhatóbb minta itt.
        deadline = time.monotonic() + 2.0
        while worker._timer is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker._timer is not None, "a worker szálnak 2s-en belül el kell indulnia"

        caller_thread_ident = threading.get_ident()
        try:
            # Ugyanaz a minta, mint MainWindow._cleanup_bq_thread() — a
            # stop_requested szignál emittálása a hívó (itt: teszt/fő) szálról,
            # a tényleges leállítás/bezárás/thread.quit() a worker szálán
            # történik, önmagától (l. BqDiagnosticsWorker._stop_and_close()).
            worker.stop_requested.emit()
            finished_in_time = thread.wait(5000)
        finally:
            thread.deleteLater()

        assert finished_in_time, "a worker szálnak 5s-en belül le kell állnia"
        assert fake_serial.close_calls == 1, "close()-nak pontosan egyszer kell lefutnia"
        assert fake_serial.close_thread_ident is not None
        assert fake_serial.close_thread_ident != caller_thread_ident, (
            "a close()-nak a WORKER szálán kell lefutnia, nem a hívó szálán — "
            "ez pontosan a C-1/C-2 review-lelet által kifogásolt szál-biztonsági "
            "szabálysértés hiányát igazolja"
        )


class TestStopAndCloseUsesLiveTransportHandle:
    """MAX verifier 2. kör (2026-09-14, V2 lelet, CLAIM_LEDGER.yaml PICO-
    RECONNECT-001): a konstruktorban kapott `serial_conn` egy RÖGZÍTETT
    pillanatfelvétel — ha a transport időközben (egy soros I/O hiba után,
    l. PicoBQTransport._reopen_serial()) egy ÚJ, más handle-re váltott, a
    régi `serial_conn.close()` a MÁR LECSERÉLT, halott objektumot zárná,
    az ÚJ, ténylegesen aktív handle-t nyitva hagyva. `_stop_and_close()`-
    nak a transport JELENLEGI handle-jét kell zárnia, ha a transport ezt
    támogatja (`disconnect()` metódus)."""

    class _FakeTransportWithDisconnect:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        def read_standard_command_word(self, command: int) -> int:
            return 0

        def read_control_subcommand_word(self, subcommand: int) -> int:
            return 0

        def read_data_flash_block(self, subclass: int, block: int) -> bytes:
            return b"\x00" * 32

        def disconnect(self) -> None:
            self.disconnect_calls += 1

    def test_prefers_transport_disconnect_over_stale_serial_conn(self):
        fake_transport = self._FakeTransportWithDisconnect()
        driver = Bq34z110ReadOnlyDriver(transport=fake_transport)
        stale_serial = _FakeSerialForThreadTest()
        worker = BqDiagnosticsWorker(driver, serial_conn=stale_serial, poll_interval_ms=60000)

        worker._stop_and_close()

        assert fake_transport.disconnect_calls == 1
        assert stale_serial.close_calls == 0, (
            "a régi, elmentett serial_conn-t NEM szabad zárni, ha a transport "
            "maga fel tudja ajánlani a JELENLEGI handle-t a disconnect()-en keresztül"
        )

    def test_falls_back_to_serial_conn_when_transport_has_no_disconnect(self):
        """A meglévő MockBq34z110Transport (és a Bq34z110Transport Protocol
        általában) NEM deklarál disconnect()-et -- ilyenkor a régi,
        közvetlen serial_conn.close() útnak KELL maradnia, ne törjön a
        rengeteg meglévő, ezt feltételező teszt/hívóhely."""
        driver, _transport = make_driver()
        fake_serial = _FakeSerialForThreadTest()
        worker = BqDiagnosticsWorker(driver, serial_conn=fake_serial, poll_interval_ms=60000)

        worker._stop_and_close()

        assert fake_serial.close_calls == 1
