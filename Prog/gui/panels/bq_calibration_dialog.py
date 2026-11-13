"""
BqCalibrationDialog — WP-AK-09: BQ_GAUGE_CALIBRATION_ASSISTED operátor-
vezérelt párbeszédpanel (SUP03-CAL-001 "kezelő felé lépéseket adhat").

UX-minta forrása: Prog/pico_tf1_olvas.py (TF1 repo) "[CAL 1/4]".."[CAL 4/4]"
konzol-promptjai — ugyanaz a lépés-szám és üzenet-stílus, csak Qt
dialógusként, a Prog/src/bq_gauge_calibration.py `OPERATOR_PROMPTS`
szótárából render elve.

Kölcsönös kizárás: ez a dialógus ÖNÁLLÓ soros kapcsolatot nyit (saját
`PicoBQTransport`), mert egy fizikai Pico USB-CDC portja egyszerre csak
egy klienst szolgál ki megbízhatóan — ezért a BQ Diagnosztika panelnek
(Prog/gui/panels/bq_diagnostics_panel.py) BONTOTT állapotban kell lennie a
kalibráció indításakor (a hívó — MainWindow — felelőssége ezt ellenőrizni
megnyitás előtt, l. main_window.py `_on_bq_calibration_requested`).

Szál-kezelés: 1:1 mintakövetés Prog/gui/bq_diagnostics_worker.py-ból
(WP-AK-07, 2 review-kör után stabilizálódott önmagát-leállító minta) — l.
Prog/gui/bq_calibration_worker.py docstring.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout,
)

from Prog.gui.bq_calibration_worker import BqCalibrationWorker
from Prog.src.bq_gauge_calibration import CalState, VoltageCalState


class _BqCalibrationConnectWorker(QObject):
    """Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet (F2): a
    kalibrációs dialógus konstruktora korábban szinkron, a GUI-szálon nyitotta
    a soros portot és futtatta a transport.negotiate()-et (akár ~4s worst-
    case, 2x2s retry) — ez alatt egy futó fizikai teszt Emergency Stop gombja
    késleltetve dolgozódott volna fel (CLAUDE.md §15). Ugyanaz a minta, mint
    main_window.py::_BqConnectWorker a BQ Diagnosztika "Kapcsolódás"-nál."""
    connect_ready: Signal = Signal(object, object)   # (transport, bq_serial)
    connect_failed: Signal = Signal(str)

    def __init__(self, port: str) -> None:
        super().__init__()
        self._port = port

    @Slot()
    def run(self) -> None:
        from Prog.drivers.pico_bq_transport import (
            CALIBRATION_WRITE_METHODS,
            PicoBQTransport,
        )
        from Prog.src.exceptions import InstrumentError

        # Teljes kódbázis review 2. kör (2026-08-28), Hardver/Műszervezérlés
        # P2 lelet: a soros port nyitása korábban itt, közvetlenül
        # pyserial-lel történt — most a PicoBQTransport.connect()-en
        # keresztül, CLAUDE.md §9 driver-interfész szerint.
        transport = PicoBQTransport(max_retries=2)
        try:
            transport.connect(self._port, timeout=2.0)
        except ImportError as exc:
            self.connect_failed.emit(f"A pyserial csomag nincs telepítve: {exc}")
            return
        except Exception as exc:
            self.connect_failed.emit(str(exc))
            return

        try:
            # Kör 21 F1/ARC21-3(b): a kalibráció unseal()/full_access()-t hív
            # enter_cal() előtt (l. bq_gauge_calibration.py:439-445), majd
            # enter_cal()/exit_cal()/board_offset_cal()/cc_offset_cal()/
            # cc_offset_save()/write_calibration_gain_delta()/
            # write_voltage_divider()-t, végül seal()-t cleanupként (MAX-
            # verifier B3 lelet, l. CALIBRATION_WRITE_METHODS -- a korábbi
            # lista hiányzó unseal/full_access/seal miatt a gate éppen az
            # első valódi write-nál bukott volna el, nem a kapcsolódáskor)
            # -- ha a csatlakoztatott firmware ezeket nem exponálja, itt
            # bukjon el, MIELŐTT a kalibrációs dialógus bármilyen write-
            # primitívet meghívna.
            transport.negotiate(required_write_methods=CALIBRATION_WRITE_METHODS)
        except InstrumentError as exc:
            # Teljes kódbázis review 13. kör (2026-08-31), Hardver/
            # Műszervezérlés P1 lelet — l. main_window.py::_BqConnectWorker
            # azonos fixjének docstringje ugyanerről a testvér-
            # kivételosztály hibaosztályról.
            transport.disconnect()
            self.connect_failed.emit(str(exc))
            return

        self.connect_ready.emit(transport, transport.serial)


class BqCalibrationDialog(QDialog):
    # Cross-thread hívások a workerhez - queued connection-nel wire-olva
    # (l. _wire_worker), UGYANAZ a minta mint BqDiagnosticsPanel.
    # connect_requested/disconnect_requested szignáljai.
    _start_cc_gain_requested = Signal()
    _confirm_battery_disconnected_requested = Signal()
    _submit_reference_current_requested = Signal(int)
    _abort_cc_gain_requested = Signal()
    _start_voltage_requested = Signal()
    _submit_dmm_voltage_requested = Signal(int)
    _abort_voltage_requested = Signal()

    def __init__(self, port: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"BQ gauge kalibráció (WP-AK-09) — {port}")
        self.setMinimumWidth(560)
        self._port = port
        self._thread: QThread | None = None
        self._worker: BqCalibrationWorker | None = None
        self._serial_conn = None
        self._connect_thread: QThread | None = None
        self._connect_worker: _BqCalibrationConnectWorker | None = None
        # Teljes kódbázis review 10. kör (2026-08-31), GUI/UX/Állapot P2
        # lelet: a CC Gain és a Voltage Divider kalibráció protokoll-szinten
        # ÖSSZEFÉRHETETLEN — a CC Gain egyetlen közös ENTER_CAL/EXIT_CAL+
        # UNSEAL munkamenetben fut (percekig nyitva, l. bq_gauge_
        # calibration.py modul docstring), a Voltage Divider viszont saját
        # lefutása VÉGÉN feltétel nélkül seal()-t hív — ha ez a még futó CC
        # Gain munkamenet KÖZBEN történik, SEALED állapotba viszi az
        # eszközt, a CC Gain állapotgép ezt követő lépései elutasítódnak
        # (ERROR, felesleges elveszett kísérlet). Ez a 2 flag tartja számon,
        # melyik fázis fut éppen — l. _update_start_button_availability().
        self._cc_phase_active: bool = False
        self._vd_phase_active: bool = False

        self._build_ui()
        # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet (F2):
        # a konstruktor korábban itt, szinkron végezte a soros port nyitást +
        # negotiate()-et — most _BqCalibrationConnectWorker-en, külön
        # QThread-en fut, a dialógus "Csatlakozás…" állapottal nyílik meg,
        # minden vezérlő letiltva, amíg az eredmény meg nem érkezik.
        self._set_all_controls_enabled(False)
        self._cc_status_lbl.setText("Csatlakozás…")
        self._vd_status_lbl.setText("Csatlakozás…")
        self._start_connecting()

    def _start_connecting(self) -> None:
        self._connect_thread = QThread(self)
        self._connect_worker = _BqCalibrationConnectWorker(self._port)
        self._connect_worker.moveToThread(self._connect_thread)
        self._connect_thread.started.connect(self._connect_worker.run)
        self._connect_worker.connect_ready.connect(self._on_connect_ready)
        self._connect_worker.connect_failed.connect(self._on_connect_failed)
        self._connect_thread.start()

    def _on_connect_ready(self, transport: Any, bq_serial: Any) -> None:
        self._cleanup_connect_thread()
        self._serial_conn = bq_serial
        self._thread = QThread(self)
        self._worker = BqCalibrationWorker(transport, serial_conn=bq_serial)
        self._worker.moveToThread(self._thread)
        self._wire_worker()
        self._thread.start()
        self._cc_status_lbl.setText("Nincs elindítva.")
        self._vd_status_lbl.setText("Nincs elindítva.")
        self._set_all_controls_enabled(True)
        # A Megerősítés/Referencia-áram/DMM-feszültség vezérlők csak az adott
        # kalibrációs állapotban jelennek meg (l. _on_cc_snapshot/_on_vd_snapshot)
        # — kapcsolódás után még egyik sincs látható állapotban.
        self._cc_confirm_check.setVisible(False)
        self._cc_confirm_btn.setVisible(False)
        self._ref_current_spin.setVisible(False)
        self._ref_current_btn.setVisible(False)
        self._cc_abort_btn.setEnabled(False)
        self._dmm_voltage_spin.setVisible(False)
        self._dmm_voltage_btn.setVisible(False)
        self._vd_abort_btn.setEnabled(False)
        self._cc_phase_active = False
        self._vd_phase_active = False
        self._update_start_button_availability()

    def _on_connect_failed(self, message: str) -> None:
        self._cleanup_connect_thread()
        QMessageBox.critical(self, "Kalibráció — kapcsolódási hiba", message)
        # A dialógus így is megnyílik (bezárható), de minden vezérlő
        # letiltva - a hívó felelőssége eldönteni, próbálkozik-e újra.
        self._cc_status_lbl.setText("Nem sikerült csatlakozni.")
        self._vd_status_lbl.setText("Nem sikerült csatlakozni.")
        self._set_all_controls_enabled(False)

    def _cleanup_connect_thread(self) -> None:
        if self._connect_thread:
            self._connect_thread.quit()
            self._connect_thread.wait(5000)
            self._connect_thread.deleteLater()
            self._connect_thread = None
        if self._connect_worker:
            self._connect_worker.deleteLater()
            self._connect_worker = None

    # ------------------------------------------------------------------ #
    # UI építés                                                            #
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        cc_box = QGroupBox("1. fázis — CC/Board Offset + Áram-gain kalibráció (SUP03-CAL-002/003/006)")
        cc_layout = QVBoxLayout(cc_box)
        self._cc_status_lbl = QLabel("Nincs elindítva.")
        self._cc_status_lbl.setWordWrap(True)
        cc_layout.addWidget(self._cc_status_lbl)

        self._cc_start_btn = QPushButton("Indítás")
        self._cc_start_btn.clicked.connect(self._on_cc_start_clicked)
        cc_layout.addWidget(self._cc_start_btn)

        self._cc_confirm_check = QCheckBox("Megerősítem: az akku NINCS csatlakoztatva")
        self._cc_confirm_check.setVisible(False)
        cc_layout.addWidget(self._cc_confirm_check)
        self._cc_confirm_btn = QPushButton("Megerősítés és folytatás")
        self._cc_confirm_btn.setVisible(False)
        self._cc_confirm_btn.clicked.connect(self._on_cc_confirm_clicked)
        cc_layout.addWidget(self._cc_confirm_btn)

        ref_row = QHBoxLayout()
        self._ref_current_spin = QSpinBox()
        self._ref_current_spin.setRange(0, 100000)
        self._ref_current_spin.setSuffix(" mA")
        self._ref_current_spin.setVisible(False)
        ref_row.addWidget(self._ref_current_spin)
        self._ref_current_btn = QPushButton("Referencia áram megadása")
        self._ref_current_btn.setVisible(False)
        self._ref_current_btn.clicked.connect(self._on_reference_current_clicked)
        ref_row.addWidget(self._ref_current_btn)
        cc_layout.addLayout(ref_row)

        self._cc_abort_btn = QPushButton("Megszakítás")
        self._cc_abort_btn.setEnabled(False)
        self._cc_abort_btn.clicked.connect(self._on_cc_abort_clicked)
        cc_layout.addWidget(self._cc_abort_btn)
        root.addWidget(cc_box)

        vd_box = QGroupBox("2. fázis — Voltage Divider kalibráció (SUP03-CAL-004/008, egyedi per board)")
        vd_layout = QVBoxLayout(vd_box)
        self._vd_status_lbl = QLabel("Nincs elindítva.")
        self._vd_status_lbl.setWordWrap(True)
        vd_layout.addWidget(self._vd_status_lbl)

        self._vd_start_btn = QPushButton("Indítás")
        self._vd_start_btn.clicked.connect(self._on_vd_start_clicked)
        vd_layout.addWidget(self._vd_start_btn)

        dmm_row = QHBoxLayout()
        self._dmm_voltage_spin = QSpinBox()
        self._dmm_voltage_spin.setRange(0, 100000)
        self._dmm_voltage_spin.setSuffix(" mV")
        self._dmm_voltage_spin.setVisible(False)
        dmm_row.addWidget(self._dmm_voltage_spin)
        self._dmm_voltage_btn = QPushButton("DMM feszültség megadása")
        self._dmm_voltage_btn.setVisible(False)
        self._dmm_voltage_btn.clicked.connect(self._on_dmm_voltage_clicked)
        dmm_row.addWidget(self._dmm_voltage_btn)
        vd_layout.addLayout(dmm_row)

        self._vd_abort_btn = QPushButton("Megszakítás")
        self._vd_abort_btn.setEnabled(False)
        self._vd_abort_btn.clicked.connect(self._on_vd_abort_clicked)
        vd_layout.addWidget(self._vd_abort_btn)
        root.addWidget(vd_box)

        self._close_btn = QPushButton("Bezárás")
        # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P1 lelet (F4):
        # self.accept()-et hívva a Qt QDialog::done() NEM generál
        # QCloseEvent-et (empirikusan igazolva) — a closeEvent()-ben lévő
        # _cleanup_thread() sosem futott volna le, a worker-szál + nyitott
        # soros port a folyamat végéig életben maradt volna. self.close()
        # valódi QCloseEvent-et generál, így a meglévő closeEvent() lefut.
        self._close_btn.clicked.connect(self.close)
        root.addWidget(self._close_btn)

    def _set_all_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self._cc_start_btn, self._cc_confirm_btn, self._ref_current_btn,
            self._cc_abort_btn, self._vd_start_btn, self._dmm_voltage_btn,
            self._vd_abort_btn,
        ):
            widget.setEnabled(enabled)

    # ------------------------------------------------------------------ #
    # Worker szál bekötés                                                 #
    # ------------------------------------------------------------------ #
    def _wire_worker(self) -> None:
        assert self._worker is not None
        self._start_cc_gain_requested.connect(self._worker.start_cc_gain_calibration)
        self._confirm_battery_disconnected_requested.connect(self._worker.confirm_battery_disconnected)
        self._submit_reference_current_requested.connect(self._worker.submit_reference_current)
        self._abort_cc_gain_requested.connect(self._worker.abort_cc_gain)
        self._start_voltage_requested.connect(self._worker.start_voltage_calibration)
        self._submit_dmm_voltage_requested.connect(self._worker.submit_dmm_voltage)
        self._abort_voltage_requested.connect(self._worker.abort_voltage)
        self._worker.cc_gain_snapshot_ready.connect(self._on_cc_snapshot)
        self._worker.voltage_snapshot_ready.connect(self._on_vd_snapshot)

    def reject(self) -> None:  # Qt override
        # Teljes kódbázis review 7. kör (2026-08-30), GUI/UX/Állapot P2
        # lelet #1: az Escape billentyű alapértelmezetten reject()-et
        # hív, ami — a device_search_dialog.py::reject()-nél (2. kör,
        # `bb9bd2b`) már javított mintával azonosan — Qt QDialog::done()
        # szándékos CloseNoEvent viselkedése miatt NEM vált ki
        # QCloseEvent-et, tehát a lenti closeEvent()-ben lévő védelem
        # (kapcsolódás/aktív kalibráció közbeni worker/soros port
        # takarítás) megkerülhető volt Escape-fel — csak a "Bezárás" gomb
        # lett a 4. körben self.close()-ra kötve, a reject() felülbírálás
        # kimaradt. self.close() valódi QCloseEvent-et generál, így a
        # closeEvent() mindig lefut.
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Teljes kódbázis review 4. kör (2026-08-29), GUI/UX P1 lelet: a
        # "Bezárás" gomb NEM szerepel a _set_all_controls_enabled()
        # letiltási listájában, ezért kapcsolódás közben is aktív marad —
        # ha a kezelő ekkor rákattint, a lenti _cleanup_thread() feltétel
        # nélkül hívná a _cleanup_connect_thread()-et, ami a még blokkoló
        # soros I/O-ban lévő connect-worker szálra `wait(5000)`-et hívna a
        # GUI-szálon (akár ~4s fagyás, éppen egy futó fizikai teszt
        # Emergency Stop gombjának reakcióidejét rontva — CLAUDE.md §15).
        # Ugyanez a védelem MÁR megvan `device_search_dialog.py::
        # closeEvent()`-ben (2. kör, `bb9bd2b`) — itt hiányzott.
        if self._connect_thread is not None and self._connect_thread.isRunning():
            QMessageBox.information(
                self, "Kapcsolódás folyamatban",
                "A BQ soros kapcsolódás még folyamatban van. Várj, amíg "
                "befejeződik, majd zárd be az ablakot.",
            )
            event.ignore()
            return
        # Review-kör 4 (2026-09-09), MAX verifier (a153925b3b9fbce5c) P3-2
        # kovetkezmenye (DFAM-016 sibling, l. MainWindow._cleanup_thread()
        # azonos mintaja): a _cleanup_thread() korabban feltetel nelkul
        # hivta a super().closeEvent()-et (ami tenylegesen bezarja/torli a
        # dialogust) akkor is, ha a wait(5000) timeoutolt -- ha ez egy aktiv
        # kalibraciofazis (self._cc_phase_active/self._vd_phase_active)
        # kozben tortenik, az exit_cal()+seal()/seal() abort-szekvencia
        # (l. lent) FELBESZAKADHAT, es a BQ34Z110 chip vegleg UNSEALED
        # (+CALMODE) allapotban maradhat.
        if not self._cleanup_thread():
            event.ignore()
            QMessageBox.warning(
                self, "Bezárás megszakítva",
                "A kalibrációs leállítás (BQ chip visszapecsételése) még "
                "folyamatban van. Az ablak biztonsági okból NEM zárható "
                "be, amíg ez be nem fejeződik -- várj, majd próbáld újra.\n\n"
                "NE zárd be a programot a Feladatkezelőből -- az a BQ "
                "chipet UNSEALED állapotban hagyhatja.",
            )
            return
        # Review-kör 4 (2026-09-09), MAX verifier (a7bc49f9f37a670af) O-1
        # (P2, pre-existing, ettől a commit-lánctól függetlenül is
        # megerősítve): super().closeEvent(event) a QDialog belső
        # implementációja szerint MAGA hívja a reject()-et -- mivel a fenti
        # reject() felülbírálás self.close()-t hív, ez végtelen (Qt által
        # egy szinten megállított, de "accepted=False"-tal záruló)
        # rekurziót okozott: a dialógus soha nem vált ténylegesen
        # láthatatlanná/elfogadottá, a modális exec() sosem tért vissza --
        # az egyetlen "kiút" az operátor számára a Feladatkezelős kilövés
        # lett volna, pontosan az, amit a fenti warning szövege explicit
        # tilt. A takarítás (_cleanup_thread()) maga helyesen lefutott,
        # csak a Qt-widget nem záródott be. Javítva a testvér
        # device_search_dialog.py::closeEvent() már helyes mintáját követve
        # (event.accept() közvetlenül, NEM super().closeEvent()).
        event.accept()

    def _cleanup_thread(self) -> bool:
        # UGYANAZ a mintakövetés, mint MainWindow._cleanup_bq_thread()
        # (WP-AK-07 Critical fix, 2 review-kör) - a leállítás a worker SAJÁT
        # szálán kérve (stop_requested), a GUI-szál NEM hív közvetlenül
        # semmit a workeren/timeren/soros porton.
        # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet: ha a
        # dialógus még kapcsolódás közben záródik be, a connect-worker
        # szálát is takarítani kell — nem csak a (még nem is létező)
        # BqCalibrationWorker szálát.
        self._cleanup_connect_thread()
        if self._thread is not None:
            if self._worker is not None:
                # Teljes kódbázis review 12. kör (2026-08-31), GUI/UX/
                # Állapot P1 lelet: ha egy kalibrációs fázis még aktív
                # (a BQ34Z110 chip UNSEALED+FULL_ACCESS(+CALMODE)
                # állapotban van), az abort()-ot (exit_cal()+seal() /
                # seal()) EL KELL indítani a stop_requested ELŐTT — a
                # worker a saját szálán, sorban dolgozza fel a
                # queue-olt slot-hívásokat, tehát az abort mindig
                # lefut a soros port bezárása (thread().quit()) előtt.
                # Enélkül a dialógus egyszerű ablakbezárása (X/Alt+F4/
                # Escape/"Bezárás") a chipet visszaállítás nélkül
                # UNSEALED(+CALMODE) állapotban hagyta — ugyanaz a
                # hibaosztály, mint a 10. kör P1 leletében (c95934e).
                if self._cc_phase_active:
                    self._abort_cc_gain_requested.emit()
                if self._vd_phase_active:
                    self._abort_voltage_requested.emit()
                self._worker.stop_requested.emit()
            else:
                self._thread.quit()
            # Review-kör 4 (2026-09-09), MAX verifier (a153925b3b9fbce5c)
            # P3-2 -- l. closeEvent() fenti kommentje. Timeoutnál NEM adunk
            # ki deleteLater()-t egy még futó szálra (Qt: nem biztonságos),
            # és NEM nullázzuk a referenciát -- a hívó (closeEvent())
            # eldöntheti, hogy megvárja-e az abort-szekvencia befejezését.
            if not self._thread.wait(5000):
                return False
            self._thread.deleteLater()
            self._thread = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        return True

    # ------------------------------------------------------------------ #
    # CC/Gain fázis UI-esemény kezelők                                    #
    # ------------------------------------------------------------------ #
    def _update_start_button_availability(self) -> None:
        """Teljes kódbázis review 10. kör (2026-08-31), GUI/UX/Állapot P2
        lelet — l. __init__ docstringje ugyanerről a kölcsönös kizárásról.
        Egyik fázis start gombja sem engedélyezett, amíg a MÁSIK fázis fut."""
        self._cc_start_btn.setEnabled(not self._cc_phase_active and not self._vd_phase_active)
        self._vd_start_btn.setEnabled(not self._vd_phase_active and not self._cc_phase_active)

    def _on_cc_start_clicked(self) -> None:
        # R6-B2 (R6-gui-01, DFAM-003): barmely korabbi futas (DONE, ERROR
        # vagy ABORTED; megerositessel vagy anelkul) "akku NINCS
        # csatlakoztatva" megerositese nem viheto at az uj inditasra --
        # egyszer, itt, a start-signal ELOTT torolve (a periodikus
        # snapshot-kezeles szandekosan nem nullaz).
        self._cc_confirm_check.setChecked(False)
        self._cc_phase_active = True
        self._cc_abort_btn.setEnabled(True)
        self._update_start_button_availability()
        self._start_cc_gain_requested.emit()

    def _on_cc_confirm_clicked(self) -> None:
        if not self._cc_confirm_check.isChecked():
            QMessageBox.warning(
                self, "Kalibráció",
                "Előbb jelöld be a jelölőnégyzetet — az akku csatlakoztatás-"
                "állapotának megerősítése kötelező a folytatáshoz.",
            )
            return
        self._cc_confirm_btn.setEnabled(False)
        self._cc_confirm_check.setEnabled(False)
        self._confirm_battery_disconnected_requested.emit()

    def _on_reference_current_clicked(self) -> None:
        value = self._ref_current_spin.value()
        self._ref_current_btn.setEnabled(False)
        self._ref_current_spin.setEnabled(False)
        self._submit_reference_current_requested.emit(value)

    def _on_cc_abort_clicked(self) -> None:
        self._abort_cc_gain_requested.emit()

    def _on_cc_snapshot(self, snapshot) -> None:
        lines = [f"Állapot: {snapshot.state.value}"]
        if snapshot.operator_prompt:
            lines.append(snapshot.operator_prompt)
        if snapshot.cc_offset is not None:
            lines.append(f"CC Offset: {snapshot.cc_offset}, Board Offset: {snapshot.board_offset}")
        if snapshot.new_cc_gain is not None:
            lines.append(
                f"CC Gain: {snapshot.old_cc_gain:.5f} → {snapshot.new_cc_gain:.5f}  "
                f"CC Delta: {snapshot.old_cc_delta:.0f} → {snapshot.new_cc_delta:.0f}"
            )
        if snapshot.state == CalState.ERROR:
            lines.append(f"HIBA [{snapshot.error.value}]: {snapshot.fail_reason}")
        if snapshot.cc_offset_save_retries_used:
            # HIL evidencia (2026-09-12, l. bq_gauge_calibration.py
            # CC_OFFSET_SAVE_RETRY_WAIT_S docstring): minden retry egy
            # VALÓS további Data Flash commit lehet (SOURCE_REQUIRED
            # ismételt-kiadás biztonságosság) -- ugyanaz az elv, mint a
            # cleanup_failures mezőnél: ne csak a végeredmény, az ÚT is
            # látható legyen a kezelőnek.
            lines.append(
                f"Megjegyzés: a CC Offset mentéséhez "
                f"{snapshot.cc_offset_save_retries_used}x újrapróbálkozás "
                f"kellett (Pico-BQ I2C időzítés, l. HIL dokumentáció)."
            )
        if snapshot.cleanup_failures:
            # Review-kör 5, Architektúra A-1 (P1) MAX verifier N1 lelete:
            # a cleanup_failures mezőnek korábban NEM volt production
            # fogyasztója — a chip UNSEALED/CALMODE visszazárási hibája
            # attesztálva volt a snapshotban, de a kezelő sosem látta.
            lines.append(
                "FIGYELEM — a BQ visszazárása (SEAL/EXIT_CAL) nem igazolt: "
                + "; ".join(snapshot.cleanup_failures)
            )
        self._cc_status_lbl.setText("\n".join(lines))

        self._cc_confirm_check.setVisible(snapshot.state == CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM)
        self._cc_confirm_btn.setVisible(snapshot.state == CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM)
        self._cc_confirm_btn.setEnabled(snapshot.state == CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM)
        self._cc_confirm_check.setEnabled(snapshot.state == CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM)

        awaiting_current = snapshot.state == CalState.AWAITING_REFERENCE_CURRENT
        self._ref_current_spin.setVisible(awaiting_current)
        self._ref_current_btn.setVisible(awaiting_current)
        self._ref_current_spin.setEnabled(awaiting_current)
        self._ref_current_btn.setEnabled(awaiting_current)

        terminal = snapshot.state in (CalState.DONE, CalState.ERROR, CalState.ABORTED)
        self._cc_abort_btn.setEnabled(not terminal)
        self._cc_phase_active = not terminal
        self._update_start_button_availability()

    # ------------------------------------------------------------------ #
    # Voltage fázis UI-esemény kezelők                                    #
    # ------------------------------------------------------------------ #
    def _on_vd_start_clicked(self) -> None:
        self._vd_phase_active = True
        self._vd_abort_btn.setEnabled(True)
        self._update_start_button_availability()
        self._start_voltage_requested.emit()

    def _on_dmm_voltage_clicked(self) -> None:
        value = self._dmm_voltage_spin.value()
        self._dmm_voltage_btn.setEnabled(False)
        self._dmm_voltage_spin.setEnabled(False)
        self._submit_dmm_voltage_requested.emit(value)

    def _on_vd_abort_clicked(self) -> None:
        self._abort_voltage_requested.emit()

    def _on_vd_snapshot(self, snapshot) -> None:
        lines = [f"Állapot: {snapshot.state.value}"]
        if snapshot.operator_prompt:
            lines.append(snapshot.operator_prompt)
        if snapshot.new_voltage_divider is not None:
            lines.append(
                f"Voltage Divider: {snapshot.old_voltage_divider} → {snapshot.new_voltage_divider} "
                f"(BQ={snapshot.bq_voltage_mV} mV, DMM={snapshot.dmm_voltage_mV} mV)"
            )
        if snapshot.state == VoltageCalState.ERROR:
            lines.append(f"HIBA: {snapshot.fail_reason}")
        if snapshot.cleanup_failures:
            # Review-kör 5, Architektúra A-1 (P1) MAX verifier N1 lelete --
            # l. _on_cc_snapshot() azonos hivatkozású kommentje.
            lines.append(
                "FIGYELEM — a BQ visszazárása (SEAL/EXIT_CAL) nem igazolt: "
                + "; ".join(snapshot.cleanup_failures)
            )
        self._vd_status_lbl.setText("\n".join(lines))

        awaiting_dmm = snapshot.state == VoltageCalState.AWAITING_DMM_VOLTAGE
        self._dmm_voltage_spin.setVisible(awaiting_dmm)
        self._dmm_voltage_btn.setVisible(awaiting_dmm)
        self._dmm_voltage_spin.setEnabled(awaiting_dmm)
        self._dmm_voltage_btn.setEnabled(awaiting_dmm)

        terminal = snapshot.state in (
            VoltageCalState.DONE, VoltageCalState.ERROR, VoltageCalState.ABORTED,
        )
        self._vd_abort_btn.setEnabled(not terminal)
        self._vd_phase_active = not terminal
        self._update_start_button_availability()
