"""
BqDiagnosticsWorker — WP-AK-07: QObject a PicoBQTransport-alapú BQ34Z110
diagnosztikai lekérdezéshez, saját QThread-en, QTimer-alapú periodikus
pollinggal.

Eltér a meglévő TestRunnerWorker (Prog/gui/worker.py) mintától: az nem az
esemény-ciklust futtatja (self._runner.run() BLOKKOLÓ, saját állapotgépe
van), ez viszont egy gyors, ismételt, önmagában állapotmentes művelet — ide
a Qt-idiomatikus QTimer + a szál saját eseményhurokja (QThread alapértelmezett
run()-ja exec()-et hív) illik, nem egy egyedi blokkoló ciklus.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import poll_bq_diagnostics


class BqDiagnosticsWorker(QObject):
    snapshot_ready    = Signal(object)  # BqDiagnosticsSnapshot
    connection_error  = Signal(str)
    # [WP-AK-07 Critical fix, független review C-1] Cross-thread leállítás
    # KIZÁRÓLAG ezen a szignálon keresztül történhet — l. _stop_and_close()
    # docstring.
    stop_requested    = Signal()

    def __init__(
        self, driver: Bq34z110ReadOnlyDriver, serial_conn=None, poll_interval_ms: int = 2000,
    ) -> None:
        super().__init__()
        self._driver = driver
        self._serial_conn = serial_conn
        self._poll_interval_ms = poll_interval_ms
        self._timer: QTimer | None = None
        self.stop_requested.connect(self._stop_and_close)

    @Slot()
    def start_polling(self) -> None:
        """A thread.started jelre kötendő — ekkor a worker MÁR a saját
        (poller) QThread-jén fut, a QTimer is ott él, nem a GUI-szálon,
        így egy lassú/timeoutoló BQ-olvasás nem fagyasztja a GUI-t."""
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_interval_ms)
        self._timer.timeout.connect(self.poll_once)
        self._timer.start()
        self.poll_once()  # azonnali első lekérdezés, ne kelljen 1 intervallumot várni

    @Slot()
    def poll_once(self) -> None:
        """Egyetlen lekérdezés — kívülről (teszt) is közvetlenül hívható a
        QTimer-től függetlenül, szinkron módon (nincs valódi időzítő-várakozás
        a teszt-szuitban)."""
        try:
            snapshot = poll_bq_diagnostics(self._driver)
        except Exception as exc:  # noqa: BLE001 - poll_bq_diagnostics() már
            # mezőnkénti hibakezelést végez (l. Prog/src/bq_diagnostics.py
            # kapu-szabálya); ez az ág csak akkor futna, ha maga a driver/
            # transport objektum a lekérdezésen KÍVÜL dobna kivételt.
            self.connection_error.emit(str(exc))
            return
        self.snapshot_ready.emit(snapshot)

    @Slot()
    def stop_polling(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    @Slot()
    def _stop_and_close(self) -> None:
        """[WP-AK-07 Critical fix, független review C-1/C-2] Kizárólag a
        `stop_requested` szignálon keresztül hívandó — Qt AutoConnection
        MINDIG a fogadó (ez a worker) SAJÁT szálán futtatja, amikor a
        kibocsátó (GUI-szál, l. MainWindow._cleanup_bq_thread()) szála eltér
        a fogadóétól (worker a saját bq_thread-jén él). Ez váltja ki a
        korábbi, KÖZVETLEN `stop_polling()` Python-hívást a GUI-szálról —
        az egy szál-biztonsági szabálysértés volt (egy másik szálhoz tartozó
        QTimer közvetlen módosítása Qt-n kívüli, nem garantált módon).

        A soros port bezárása IS ide került (nem a MainWindow GUI-szálára) —
        így SOSEM versenyezhet egy, ezen a szálon még folyamatban lévő
        blokkoló olvasással/írással (pyserial Windows alatt nem biztonságos
        egy másik szálról zárni egy `Serial` objektumot, amíg egy blokkoló
        `read()`/`write()` rajta fut). Fennmaradó, dokumentált korlát: ha
        `poll_once()` éppen egy blokkoló hívás KÖZEPÉN tart, ez a metódus
        csak a hívás visszatérése UTÁN fut le (egyetlen szálú Qt esemény-
        hurok) — ezt korlátozza (nem szünteti meg teljesen) a
        MainWindow-oldali `max_retries=1` + rövidebb soros timeout
        beállítás a BQ diagnosztikai kapcsolatnál, ami egyetlen mező
        worst-case blokkolási idejét kb. 6s-ről kb. 1s-re csökkenti."""
        self.stop_polling()
        # MAX verifier 2. kör (2026-09-14, V2 lelet, l. CLAIM_LEDGER.yaml
        # PICO-RECONNECT-001): a konstruktorban kapott `serial_conn` egy
        # RÖGZÍTETT pillanatfelvétel a worker indulásakor — ha időközben a
        # PicoBQTransport egy soros I/O hiba után sikeresen újranyitotta a
        # portot (_reopen_serial()), ez a régi, már lecserélt handle-t
        # zárná, az ÚJ, ténylegesen aktív handle-t NYITVA hagyva. A
        # `transport.disconnect()` mindig a JELENLEGI handle-t zárja, és
        # konzisztensen törli a port-információt is — ha a transport nem
        # kínál `disconnect()`-et (pl. teszt-fake), a régi, közvetlen
        # `serial_conn.close()` út marad a tartalék.
        transport = getattr(self._driver, "transport", None)
        if transport is not None and hasattr(transport, "disconnect"):
            try:
                transport.disconnect()
            except Exception:
                pass
        elif self._serial_conn is not None:
            try:
                self._serial_conn.close()
            except Exception:
                pass
        # [WP-AK-07 Critical fix, 2. kör — a saját tesztem buktatta el az
        # 1. javítási kört] a hívó (GUI-szál) NEM hívhatja a `thread.quit()`-ot
        # rögtön a `stop_requested.emit()` UTÁN, szinkron módon — az
        # deadlockhoz vezetett volna: `QThread.wait()` a GUI-szálon
        # BLOKKOL, tehát a GUI-szál eseményhurka eközben nem pumpál, így egy
        # onnan induló KERESZTSZÁLAS `quit()`-hívás sosem jutna el a worker
        # szálhoz, amíg a `wait()` fut — holtpont. Egy köztes verzió (`stopped`
        # szignál -> GUI-szálon élő `thread.quit()` slot) ugyanebbe a
        # csapdába esett (a `quit()` maga is keresztszálas queued hívássá
        # vált volna). A helyes, Qt-dokumentáció szerint biztonságos minta:
        # a JELENLEG FUTÓ esemény-hurok a SAJÁT szálán kéri a leállítást —
        # `QThread.quit()` dokumentáltan thread-safe, de itt nincs is
        # szükség keresztszálas hívásra, mert ez a kód MÁR a worker szálán
        # fut (a stop_requested queued connection miatt).
        self.thread().quit()
