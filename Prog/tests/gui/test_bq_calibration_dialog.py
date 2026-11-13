"""
BqCalibrationDialog regressziós tesztek — teljes kódbázis review 2. kör
(2026-08-28), GUI/UX P0/P1 leletek (F2/F4):

F2: a konstruktor korábban szinkron, a GUI-szálon nyitotta a soros portot és
futtatta a transport.negotiate()-et — most _BqCalibrationConnectWorker-en,
külön QThread-en fut, a dialógus "Csatlakozás…" állapottal, letiltott
vezérlőkkel nyílik meg, amíg az eredmény meg nem érkezik.

F4: a "Bezárás" gomb korábban self.accept()-et hívott, ami NEM vált ki
QCloseEvent-et (Qt QDialog::done() szándékosan CloseNoEvent móddal zár) — a
closeEvent()-ben lévő _cleanup_thread() sosem futott le. Most self.close()-ra
köt, ami valódi QCloseEvent-et generál.
"""
from __future__ import annotations

import time

from Prog.gui.panels.bq_calibration_dialog import BqCalibrationDialog
from Prog.src.bq_gauge_calibration import (
    CalState,
    CcGainCalibrationSnapshot,
    VoltageCalibrationSnapshot,
    VoltageCalState,
)
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial


def _patch_bq_serial(monkeypatch, **fake_kwargs):
    import serial  # type: ignore[import-untyped]

    fake = FakePicoSerial(**fake_kwargs)
    monkeypatch.setattr(FakePicoSerial, "close", lambda self: None, raising=False)
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
    return fake


def _pump_until(qapp, predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


class TestConnectDoesNotBlockGuiThread:
    def test_dialog_opens_with_controls_disabled_and_connecting_status(self, qapp, monkeypatch):
        """[F2] A konstruktor visszatérése UTÁN a kapcsolódás eredménye MÉG
        nem lehet kész — a vezérlők letiltva, "Csatlakozás…" státusz — ami
        bizonyítja, hogy a soros port nyitás + negotiate() nem szinkron, a
        hívó (GUI) szálon futott le."""
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            assert dlg._connect_thread is not None
            assert dlg._worker is None
            assert dlg._cc_start_btn.isEnabled() is False
            assert dlg._cc_status_lbl.text() == "Csatlakozás…"

            _pump_until(qapp, lambda: dlg._worker is not None)

            assert dlg._worker is not None
            assert dlg._connect_thread is None  # cleanup megtörtént
            assert dlg._cc_start_btn.isEnabled() is True
            assert dlg._cc_status_lbl.text() == "Nincs elindítva."
        finally:
            dlg._cleanup_thread()

    def test_dialog_connect_failure_keeps_controls_disabled(self, qapp, monkeypatch):
        """[F2] Sikertelen kapcsolódás esetén a vezérlők letiltva maradnak,
        és a hívó egyértelmű hibaüzenetet kap — nem omlik össze a dialógus."""
        import serial  # type: ignore[import-untyped]
        from PySide6.QtWidgets import QMessageBox

        def _raise_serial(*a, **k):
            raise RuntimeError("simulated port open failure")

        monkeypatch.setattr(serial, "Serial", _raise_serial)
        criticals: list = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a))

        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: len(criticals) == 1)

            assert len(criticals) == 1
            assert dlg._worker is None
            assert dlg._connect_thread is None
            assert dlg._cc_start_btn.isEnabled() is False
        finally:
            dlg._cleanup_thread()

    def test_negotiate_timeout_reports_connect_failed_not_unhandled(self, qapp, monkeypatch):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), Hardver/
        Műszervezérlés P1 lelet] negotiate() a testvér PicoBQTransportError/
        InstrumentTimeoutError kivételek BÁRMELYIKÉT dobhatja (soros I/O
        timeout esetén az utóbbit) — a korábbi szűk `except
        PicoBQTransportError` ezt nem kapta el, a dialógus véglegesen
        "Csatlakozás…" állapotban ragadt volna."""
        from PySide6.QtWidgets import QMessageBox
        _patch_bq_serial(monkeypatch, simulate_no_response=True)
        criticals: list = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a))

        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: len(criticals) == 1)

            assert len(criticals) == 1
            assert dlg._worker is None
            assert dlg._connect_thread is None
        finally:
            dlg._cleanup_thread()

    def test_negotiate_rejects_firmware_missing_calibration_write_methods(
        self, qapp, monkeypatch
    ):
        """[regresszió, Kör 21 F1/ARC21-3(b) MAX-verifier NB-E lelet] a
        testvér main_window.py::_construct_and_connect_instruments()-nek MÁR
        van diszkrimináló tesztje arra, hogy egy a szükséges write-metódusokat
        nem exponáló firmware a kapcsolódáskor bukjon el -- ennek a
        dialógusnak (a `CALIBRATION_WRITE_METHODS`-t igénylő testvér
        hívóhelynek) NEM volt. Mutáció-teszttel igazolva (a hívóhely
        `required_write_methods` argumentumának elejtésével): e teszt
        nélkül mind a 118 érintett teszt zöld marad."""
        from PySide6.QtWidgets import QMessageBox
        _patch_bq_serial(
            monkeypatch,
            results={
                "capabilities": {
                    "protocol_version": "1.0",
                    "methods": ["capabilities", "voltage_mV"],
                    "read_only": True,
                    "write_methods": [],
                }
            },
        )
        criticals: list = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a))

        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: len(criticals) == 1)

            assert len(criticals) == 1
            assert "unseal" in str(criticals[0])
            assert dlg._worker is None
            assert dlg._connect_thread is None
        finally:
            dlg._cleanup_thread()


class TestCloseButtonTriggersCleanup:
    def test_close_button_click_runs_closeevent_cleanup(self, qapp, monkeypatch):
        """[F4, regresszió] A "Bezárás" gomb self.close()-ra köt (nem
        self.accept()-re) — ez valódi QCloseEvent-et generál, ami lefuttatja
        a closeEvent()-ben lévő _cleanup_thread()-et, felszabadítva a worker-
        szálat és a soros portot. Korábban (self.accept()) ez a takarítás
        SOSEM futott le a dokumentált bezárási úton."""
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        _pump_until(qapp, lambda: dlg._worker is not None)
        assert dlg._worker is not None
        assert dlg._thread is not None

        dlg._close_btn.click()

        assert dlg._worker is None
        assert dlg._thread is None

    def test_close_button_while_still_connecting_ignores_close_no_gui_block(
        self, qapp, monkeypatch,
    ):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), GUI/UX P1
        lelet] Ha a dialógust MÉG a kapcsolódás közben zárják be, a
        closeEvent() NEM hívhatja a _cleanup_thread()-et — az a még blokkoló
        soros I/O-ban lévő connect-worker szálra `wait(5000)`-et hívna a
        GUI-szálon (akár ~4s fagyás). Ehelyett tájékoztató üzenetet mutat és
        `event.ignore()`-t hív — a connect-worker szála NEM áll le, a
        dialógus NEM záródik be. Ugyanaz a minta, mint
        `device_search_dialog.py::closeEvent()`-ben."""
        from PySide6.QtWidgets import QMessageBox

        informations: list = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: informations.append(a))
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            assert dlg._connect_thread is not None

            dlg._close_btn.click()

            assert len(informations) == 1
            assert dlg._connect_thread is not None
            assert dlg._connect_worker is not None
        finally:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._cleanup_thread()

    def test_close_button_after_connecting_completes_still_cleans_up(self, qapp, monkeypatch):
        """A kapcsolódás BEFEJEZŐDÉSE után a Bezárás gomb továbbra is
        helyesen takarít — a fenti védelem csak a kapcsolódás ALATTI
        ablakra vonatkozik, nem szünteti meg a normál bezárási utat."""
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        _pump_until(qapp, lambda: dlg._worker is not None)
        assert dlg._connect_thread is None

        dlg._close_btn.click()

        assert dlg._worker is None
        assert dlg._thread is None


class TestRejectTriggersCloseEventCleanup:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), GUI/UX/
    Állapot P2 lelet #1] Csak a "Bezárás" gomb lett self.close()-ra kötve
    (F4, fent) — a reject() (Escape) felülbírálás kimaradt, ezért Escape
    megkerülte a closeEvent()-ben lévő worker/soros port takarítást.
    Ugyanaz a minta, mint device_search_dialog.py::reject()-nél (2. kör,
    bb9bd2b), l. test_device_search.py::
    test_reject_triggers_closeevent_after_search_done."""

    def test_reject_calls_close(self, qapp, monkeypatch):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            close_calls = []
            dlg.close = lambda: close_calls.append(True)

            dlg.reject()

            assert close_calls == [True]
        finally:
            dlg._cleanup_thread()

    def test_reject_after_connecting_completes_cleans_up_worker(self, qapp, monkeypatch):
        """Escape a kapcsolódás BEFEJEZŐDÉSE után is a valódi closeEvent()
        takarítást futtatja le (self.close()-on keresztül), nem csak a
        gombkattintás — a fix előtt Escape a worker-t/soros portot élve
        hagyta volna."""
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        _pump_until(qapp, lambda: dlg._worker is not None)
        assert dlg._worker is not None
        assert dlg._thread is not None

        dlg.reject()

        assert dlg._worker is None
        assert dlg._thread is None

    def test_reject_while_still_connecting_ignores_close_no_gui_block(
        self, qapp, monkeypatch,
    ):
        """Escape a kapcsolódás ALATT ugyanazt a védelmet kapja, mint a
        "Bezárás" gomb (l. test_close_button_while_still_connecting_
        ignores_close_no_gui_block) — tájékoztató üzenet, event.ignore(),
        a connect-worker szála NEM áll le, a dialógus NEM záródik be."""
        from PySide6.QtWidgets import QMessageBox

        informations: list = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: informations.append(a))
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            assert dlg._connect_thread is not None

            dlg.reject()

            assert len(informations) == 1
            assert dlg._connect_thread is not None
            assert dlg._connect_worker is not None
        finally:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._cleanup_thread()


class TestCcVdPhaseMutualExclusion:
    """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), GUI/UX/
    Állapot P2 lelet] a CC Gain és a Voltage Divider kalibráció protokoll-
    szinten összeférhetetlen — a CC Gain egyetlen közös ENTER_CAL/EXIT_CAL+
    UNSEAL munkamenetben fut (percekig nyitva), a Voltage Divider viszont
    saját lefutása VÉGÉN feltétel nélkül seal()-t hív. Ha a kezelő mindkét
    fázist párhuzamosan indítja, a VD záró seal()-ja SEALED állapotba
    viheti az eszközt a még futó CC Gain munkamenet alatt (ERROR, elveszett
    kísérlet). Korábban egyik start gomb sem tiltotta le a másikat."""

    def test_cc_start_disables_vd_start(self, qapp, monkeypatch):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            assert dlg._vd_start_btn.isEnabled() is True

            dlg._on_cc_start_clicked()

            assert dlg._vd_start_btn.isEnabled() is False
        finally:
            dlg._cleanup_thread()

    def test_vd_start_disables_cc_start(self, qapp, monkeypatch):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            assert dlg._cc_start_btn.isEnabled() is True

            dlg._on_vd_start_clicked()

            assert dlg._cc_start_btn.isEnabled() is False
        finally:
            dlg._cleanup_thread()

    def test_vd_start_reenabled_after_cc_phase_reaches_terminal_state(self, qapp, monkeypatch):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_cc_start_clicked()
            assert dlg._vd_start_btn.isEnabled() is False

            dlg._on_cc_snapshot(CcGainCalibrationSnapshot(state=CalState.DONE))

            assert dlg._vd_start_btn.isEnabled() is True
        finally:
            dlg._cleanup_thread()

    def test_cc_start_reenabled_after_vd_phase_reaches_terminal_state(self, qapp, monkeypatch):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_vd_start_clicked()
            assert dlg._cc_start_btn.isEnabled() is False

            dlg._on_vd_snapshot(VoltageCalibrationSnapshot(state=VoltageCalState.ERROR))

            assert dlg._cc_start_btn.isEnabled() is True
        finally:
            dlg._cleanup_thread()

    def test_vd_start_stays_disabled_while_cc_phase_still_non_terminal(self, qapp, monkeypatch):
        """A CC fázis egy nem-terminális köztes snapshotja (pl.
        AWAITING_REFERENCE_CURRENT) NEM szabad, hogy visszaengedélyezze a
        VD start gombot — csak a tényleges terminális állapot (DONE/ERROR/
        ABORTED)."""
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_cc_start_clicked()

            dlg._on_cc_snapshot(
                CcGainCalibrationSnapshot(state=CalState.AWAITING_REFERENCE_CURRENT)
            )

            assert dlg._vd_start_btn.isEnabled() is False
        finally:
            dlg._cleanup_thread()


    def test_cc_snapshot_cleanup_failure_surfaces_as_warning_in_status_label(
        self, qapp, monkeypatch,
    ):
        """Review-kör 5, Architektúra A-1 (P1) MAX verifier N1 lelete: a
        snapshot `cleanup_failures` mezőjének korábban NEM volt production
        fogyasztója -- a chip UNSEALED/CALMODE visszazárási hibája
        attesztálva volt a snapshotban, de a kezelő sosem látta."""
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_cc_snapshot(
                CcGainCalibrationSnapshot(
                    state=CalState.ABORTED,
                    cleanup_failures=("SEAL_FAILED:simulated seal failure",),
                )
            )
            assert "FIGYELEM" in dlg._cc_status_lbl.text()
            assert "SEAL_FAILED:simulated seal failure" in dlg._cc_status_lbl.text()
        finally:
            dlg._cleanup_thread()

    def test_cc_snapshot_without_cleanup_failure_shows_no_warning(
        self, qapp, monkeypatch,
    ):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_cc_snapshot(CcGainCalibrationSnapshot(state=CalState.DONE))
            assert "FIGYELEM" not in dlg._cc_status_lbl.text()
        finally:
            dlg._cleanup_thread()

    def test_cc_snapshot_offset_save_retry_count_surfaces_as_note_in_status_label(
        self, qapp, monkeypatch,
    ):
        """MAX verifier + felhasználói döntés (2026-09-12): a cc_offset_
        save() retry (HIL evidencia szerint egy VALÓS további Data Flash
        commit lehet) attesztálva van a snapshotban -- ugyanaz az elv, mint
        a cleanup_failures mezőnél, ne maradjon fogyasztó nélkül."""
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_cc_snapshot(
                CcGainCalibrationSnapshot(
                    state=CalState.AWAITING_REFERENCE_CURRENT,
                    cc_offset_save_retries_used=3,
                )
            )
            assert "3x" in dlg._cc_status_lbl.text()
            assert "Megjegyzés" in dlg._cc_status_lbl.text()
        finally:
            dlg._cleanup_thread()

    def test_cc_snapshot_without_offset_save_retry_shows_no_note(
        self, qapp, monkeypatch,
    ):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_cc_snapshot(CcGainCalibrationSnapshot(state=CalState.DONE))
            assert "Megjegyzés" not in dlg._cc_status_lbl.text()
        finally:
            dlg._cleanup_thread()

    def test_vd_snapshot_cleanup_failure_surfaces_as_warning_in_status_label(
        self, qapp, monkeypatch,
    ):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_vd_snapshot(
                VoltageCalibrationSnapshot(
                    state=VoltageCalState.ABORTED,
                    cleanup_failures=("SEAL_FAILED:simulated seal failure",),
                )
            )
            assert "FIGYELEM" in dlg._vd_status_lbl.text()
            assert "SEAL_FAILED:simulated seal failure" in dlg._vd_status_lbl.text()
        finally:
            dlg._cleanup_thread()

    def test_vd_snapshot_without_cleanup_failure_shows_no_warning(
        self, qapp, monkeypatch,
    ):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._on_vd_snapshot(VoltageCalibrationSnapshot(state=VoltageCalState.DONE))
            assert "FIGYELEM" not in dlg._vd_status_lbl.text()
        finally:
            dlg._cleanup_thread()


def _trigger(calls, name):
    def _handler(params):
        calls.append(name)
        return {"status": "ok"}
    return _handler


class TestReview12thRoundCloseAbortsActiveCalibration:
    """[regresszió, Teljes kódbázis review 12. kör (2026-08-31), GUI/UX/
    Állapot P1 lelet] a dialógus ablakbezárása (a "Bezárás" gomb, amit a
    closeEvent()-en keresztül X/Alt+F4/Escape is elér) egy AKTÍV
    kalibrációs fázis közben korábban NEM hívta a controller abort()-ját
    (exit_cal()+seal() / seal()) — a BQ34Z110 chip UNSEALED(+FULL_ACCESS
    +CALMODE) állapotban maradhatott a soros port bezárása után is.
    Ugyanaz a hibaosztály, mint a 10. kör P1 leletében (c95934e)."""

    def test_close_mid_cc_gain_calibration_calls_exit_cal_and_seal(
        self, qapp, monkeypatch,
    ):
        calls: list[str] = []
        results = {
            name: _trigger(calls, name)
            for name in ("unseal", "full_access", "seal", "enter_cal", "exit_cal", "board_offset_cal")
        }
        _patch_bq_serial(monkeypatch, results=results)
        dlg = BqCalibrationDialog("COM8")
        _pump_until(qapp, lambda: dlg._worker is not None)

        dlg._on_cc_start_clicked()
        _pump_until(qapp, lambda: dlg._cc_confirm_btn.isEnabled())
        # A checkbox bejelölése nélkül _on_cc_confirm_clicked() egy VALÓS,
        # blokkoló QMessageBox.warning()-ot nyitna (self._cc_confirm_check.
        # isChecked() védelem) — ezt kell előbb bejelölni.
        dlg._cc_confirm_check.setChecked(True)
        dlg._on_cc_confirm_clicked()
        _pump_until(qapp, lambda: "enter_cal" in calls)

        dlg._close_btn.click()

        assert "exit_cal" in calls
        assert "seal" in calls
        assert dlg._worker is None

    def test_close_mid_voltage_divider_calibration_calls_seal(
        self, qapp, monkeypatch,
    ):
        calls: list[str] = []
        results = {
            name: _trigger(calls, name) for name in ("unseal", "full_access", "seal")
        }
        _patch_bq_serial(monkeypatch, results=results)
        dlg = BqCalibrationDialog("COM8")
        _pump_until(qapp, lambda: dlg._worker is not None)

        dlg._on_vd_start_clicked()
        _pump_until(qapp, lambda: "full_access" in calls)

        dlg._close_btn.click()

        assert "seal" in calls
        assert dlg._worker is None


class TestReview4thRoundCloseIgnoredWhenAbortDoesNotFinishInTime:
    """[regresszió, review-kör 4 (2026-09-09), MAX verifier
    (a153925b3b9fbce5c) P3-2, DFAM-016 sibling] a closeEvent() korábban
    feltétel nélkül hívta a super().closeEvent()-et (ami ténylegesen
    bezárja/törli a dialógust) akkor is, ha a _cleanup_thread() belsejében
    a wait(5000) timeoutolt -- egy aktív kalibrációs fázis (BQ34Z110 chip
    UNSEALED+FULL_ACCESS(+CALMODE)) exit_cal()+seal()/seal() abort-
    szekvenciája félbeszakadhatott, a chip véglegesen UNSEALED állapotban
    maradhatott. Ugyanaz a mintakövetés, mint MainWindow.closeEvent()
    S4-01 fixje."""

    def test_close_ignored_when_worker_thread_does_not_stop_in_time(
        self, qapp, monkeypatch,
    ):
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        _pump_until(qapp, lambda: dlg._worker is not None)

        class _FakeThreadStillRunning:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return False  # a szál MÉG FUT az 5s timeout után is

            def deleteLater(self):
                raise AssertionError(
                    "deleteLater() nem hívható egy még futó QThread-en"
                )

        real_worker = dlg._worker
        real_thread = dlg._thread
        dlg._worker = None  # az egyszerű (nem-abort) _thread.quit() ágat fedi
        dlg._thread = _FakeThreadStillRunning()

        from PySide6.QtWidgets import QMessageBox
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

        # takarítás: a valódi szál/worker visszaállítva, hogy a teszt ne
        # hagyjon függő QThread-et.
        dlg._worker = real_worker
        dlg._thread = real_thread
        dlg._cleanup_thread()

    def test_close_button_actually_hides_dialog_on_successful_cleanup(
        self, qapp, monkeypatch,
    ):
        """[regresszió, MAX verifier (review-kör 4, a7bc49f9f37a670af) O-1,
        P2, ettől a lánctól függetlenül is megerősített pre-existing hiba]
        a closeEvent() sikeres ágán korábban super().closeEvent(event)-et
        hívott, ami a QDialog belső implementációja szerint MAGA hívja a
        reject()-et -- mivel a reject() felülbírálás self.close()-t hív,
        ez rekurzióhoz vezetett, aminek a vége mindig event.ignore() lett:
        a dialógus SOSEM vált ténylegesen láthatatlanná, a modális exec()
        sosem tért volna vissza. Az egyetlen "kiút" az operátor számára a
        Feladatkezelős kilövés lett volna -- pontosan az, amit az S4-01
        lánc warning szövege explicit tilt."""
        _patch_bq_serial(monkeypatch)
        dlg = BqCalibrationDialog("COM8")
        dlg.show()
        _pump_until(qapp, lambda: dlg._worker is not None)
        assert dlg.isVisible() is True  # előfeltétel: a dialógus ténylegesen látszik

        dlg._close_btn.click()

        assert dlg.isVisible() is False
        assert dlg._worker is None
        assert dlg._thread is None


class TestR6B2CcConfirmCheckResetOnRestart:
    """[regresszio, Claude-Codex loop R6 B2: R6-gui-01, DFAM-003] Barmely
    korabbi futas (ABORTED, ERROR, DONE; megerositessel vagy anelkul) utan
    uj CC-inditaskor az "akku NINCS csatlakoztatva" megerosites nem
    maradhat elavultan bejelolve. Valodi lanc: dialogus gomb -> signal ->
    valodi BqCalibrationWorker (QThread) -> valodi controller ->
    PicoBQTransport -> FakePicoSerial -> snapshot-signal -> dialogus
    handler. Unit/mock/GUI-offscreen szint, nem HIL."""

    @staticmethod
    def _results(calls):
        results = {
            name: _trigger(calls, name)
            for name in ("unseal", "full_access", "seal", "enter_cal", "exit_cal",
                         "board_offset_cal", "cc_offset_cal", "cc_offset_save",
                         "write_calibration_gain_delta")
        }
        results["read_calibration_data"] = lambda params: {
            "cc_gain": 0.47095, "cc_delta": 559500.0, "cc_offset": 0, "board_offset": 0,
        }
        results["control_status"] = lambda params: 0
        results["average_current_mA"] = lambda params: 1000
        # R16-B6: a kalibracio Pack Configuration-kapuja (X10=0, 0x0961)
        # a session alatt valtozatlan fake DF-blokkbol olvas.
        pack_configuration_block = bytes.fromhex("0961" + "00" * 30)

        def data_flash_block(params):
            assert params == {"subclass": 64, "block": 0}
            return pack_configuration_block.hex()

        results["data_flash_block"] = data_flash_block
        return results

    def test_restart_after_abort_requires_fresh_confirmation(self, qapp, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        calls: list[str] = []
        _patch_bq_serial(monkeypatch, results=self._results(calls))
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[1:3]))
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            # 1. futas: megerosites -> AWAITING_REFERENCE_CURRENT -> abort.
            dlg._cc_start_btn.click()
            # Mar a legelso inditas utan is torolt (nem "toggle"-olt) a pipa.
            assert dlg._cc_confirm_check.isChecked() is False
            _pump_until(qapp, lambda: dlg._cc_confirm_btn.isEnabled())
            dlg._cc_confirm_check.setChecked(True)
            dlg._cc_confirm_btn.click()
            _pump_until(
                qapp, lambda: "AWAITING_REFERENCE_CURRENT" in dlg._cc_status_lbl.text(),
                timeout_s=10.0,
            )
            assert "AWAITING_REFERENCE_CURRENT" in dlg._cc_status_lbl.text()
            dlg._cc_abort_btn.click()
            _pump_until(qapp, lambda: "ABORTED" in dlg._cc_status_lbl.text())
            assert "ABORTED" in dlg._cc_status_lbl.text()
            assert dlg._cc_start_btn.isEnabled() is True
            unseal_before = calls.count("unseal")
            enter_cal_before = calls.count("enter_cal")

            # 2. futas: a pipa torolve; megerosites nelkul elutasitas.
            dlg._cc_start_btn.click()
            assert dlg._cc_confirm_check.isChecked() is False
            _pump_until(qapp, lambda: dlg._cc_confirm_btn.isEnabled())
            assert "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text()
            dlg._cc_confirm_btn.click()
            _pump_until(qapp, lambda: False, timeout_s=0.3)
            assert len(warnings) == 1
            assert calls.count("unseal") == unseal_before
            assert "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text()

            # A periodikus snapshot-kezeles nem nullaz: a friss pipa >2 tick
            # utan is megmarad.
            dlg._cc_confirm_check.setChecked(True)
            _pump_until(qapp, lambda: False, timeout_s=1.2)
            assert dlg._cc_confirm_check.isChecked() is True

            # Friss pipaval pontosan egy inditas.
            dlg._cc_confirm_btn.click()
            _pump_until(qapp, lambda: calls.count("enter_cal") > enter_cal_before)
            _pump_until(qapp, lambda: False, timeout_s=0.3)
            assert calls.count("unseal") == unseal_before + 1
            assert calls.count("enter_cal") == enter_cal_before + 1
            assert len(warnings) == 1
        finally:
            dlg._cleanup_thread()

    def test_restart_after_error_requires_fresh_confirmation(self, qapp, monkeypatch):
        """ERROR-ban vegzodo elso futas utan is friss megerosites kell
        (abort-kattintas nelkul). A tesztosztaly az ABORTED/ERROR/DONE utani
        friss megerositest ellenorzi; a reset pontos elhelyezeset (a
        start-handlerben, az emit elott) a kodreview igazolja (R6-B2 Codex
        reverify N2)."""
        from PySide6.QtWidgets import QMessageBox

        calls: list[str] = []
        fake = _patch_bq_serial(monkeypatch, results=self._results(calls))
        fake.errors["board_offset_cal"] = ("CAL_FAILED", "simulated")
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[1:3]))
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._cc_start_btn.click()
            _pump_until(qapp, lambda: dlg._cc_confirm_btn.isEnabled())
            dlg._cc_confirm_check.setChecked(True)
            dlg._cc_confirm_btn.click()
            _pump_until(qapp, lambda: "Állapot: ERROR" in dlg._cc_status_lbl.text())
            assert "Állapot: ERROR" in dlg._cc_status_lbl.text()
            del fake.errors["board_offset_cal"]
            unseal_before = calls.count("unseal")

            dlg._cc_start_btn.click()
            assert dlg._cc_confirm_check.isChecked() is False
            _pump_until(qapp, lambda: dlg._cc_confirm_btn.isEnabled())
            dlg._cc_confirm_btn.click()
            _pump_until(qapp, lambda: False, timeout_s=0.3)
            assert len(warnings) == 1
            assert calls.count("unseal") == unseal_before
        finally:
            dlg._cleanup_thread()

    @staticmethod
    def _assert_restart_needs_fresh_confirmation(qapp, dlg, calls, warnings):
        """Kozos masodik futas: inditas utan a pipa torolt; pipalas nelkuli
        megerositesre pontosan 1 figyelmeztetes, es nem megy ki sem
        unseal, sem board_offset_cal, sem a perzisztens cc_offset_save."""
        unseal_before = calls.count("unseal")
        board_before = calls.count("board_offset_cal")
        save_before = calls.count("cc_offset_save")
        warn_before = len(warnings)
        dlg._cc_start_btn.click()
        assert dlg._cc_confirm_check.isChecked() is False
        _pump_until(
            qapp,
            lambda: "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text()
            and dlg._cc_confirm_btn.isEnabled(),
        )
        assert "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text()
        assert dlg._cc_confirm_check.isChecked() is False
        dlg._cc_confirm_btn.click()
        _pump_until(qapp, lambda: False, timeout_s=0.5)
        assert len(warnings) - warn_before == 1
        assert calls.count("unseal") == unseal_before
        assert calls.count("board_offset_cal") == board_before
        assert calls.count("cc_offset_save") == save_before
        assert "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text()

    def test_restart_after_done_requires_fresh_confirmation(self, qapp, monkeypatch):
        """[R6-B2 verifier N1] Sikeres (DONE) futas utan -- a leggyakoribb
        valos eset, a referenciaaram-forras ekkor meg ra lehet kotve -- az
        uj inditas sem orokolheti a korabbi megerositest."""
        from PySide6.QtWidgets import QMessageBox

        import Prog.src.bq_gauge_calibration as cal

        monkeypatch.setattr(cal, "GAIN_STABILIZE_S", 0.1)
        monkeypatch.setattr(cal, "GAIN_SAMPLE_INTERVAL_S", 0.1)
        calls: list[str] = []
        _patch_bq_serial(monkeypatch, results=self._results(calls))
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[1:3]))
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            # 1. futas vegig DONE-ig (a statuszcimkere varva, nem isEnabled()-re).
            dlg._cc_start_btn.click()
            _pump_until(
                qapp, lambda: "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text(),
            )
            assert "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text()
            dlg._cc_confirm_check.setChecked(True)
            dlg._cc_confirm_btn.click()
            _pump_until(
                qapp, lambda: "AWAITING_REFERENCE_CURRENT" in dlg._cc_status_lbl.text(),
                timeout_s=10.0,
            )
            assert dlg._ref_current_btn.isEnabled()
            dlg._ref_current_spin.setValue(1000)
            dlg._ref_current_btn.click()
            _pump_until(qapp, lambda: "Állapot: DONE" in dlg._cc_status_lbl.text(), timeout_s=15.0)
            assert "Állapot: DONE" in dlg._cc_status_lbl.text()
            assert calls.count("write_calibration_gain_delta") == 1
            assert dlg._cc_start_btn.isEnabled() is True

            # 2. futas: friss megerosites nelkul nincs folytatas.
            self._assert_restart_needs_fresh_confirmation(qapp, dlg, calls, warnings)
            assert len(warnings) == 1
        finally:
            dlg._cleanup_thread()

    def test_restart_after_checked_but_unconfirmed_abort_requires_fresh_confirmation(
        self, qapp, monkeypatch,
    ):
        """[R6-B2 verifier N1] A kezelo bepipalja, de megerosites ELOTT
        megszakit: az uj inditas sem orokolheti a (soha el nem kuldott)
        pipat."""
        from PySide6.QtWidgets import QMessageBox

        calls: list[str] = []
        _patch_bq_serial(monkeypatch, results=self._results(calls))
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[1:3]))
        dlg = BqCalibrationDialog("COM8")
        try:
            _pump_until(qapp, lambda: dlg._worker is not None)
            dlg._cc_start_btn.click()
            _pump_until(
                qapp, lambda: "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text(),
            )
            assert "AWAITING_BATTERY_DISCONNECT_CONFIRM" in dlg._cc_status_lbl.text()
            dlg._cc_confirm_check.setChecked(True)
            dlg._cc_abort_btn.click()
            _pump_until(qapp, lambda: "ABORTED" in dlg._cc_status_lbl.text())
            assert "ABORTED" in dlg._cc_status_lbl.text()
            assert calls.count("unseal") == 0
            assert dlg._cc_start_btn.isEnabled() is True

            self._assert_restart_needs_fresh_confirmation(qapp, dlg, calls, warnings)
            assert len(warnings) == 1
        finally:
            dlg._cleanup_thread()
