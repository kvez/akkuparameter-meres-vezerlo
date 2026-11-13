"""
PicoProvisioningDialog — "Pico előkészítése BQ34Z110-hez" párbeszédpanel.

Felhasználói igény: aki az Akkuteszter szoftvert használja és van egy
Pico-ja, csatlakoztassa, írja be a COM portot, és a szoftver azonosítsa,
töltse fel rá a Pico-oldali szervert, indítsa el a szolgáltatást, a
bekötéshez pedig adjon útmutatót — külön letöltés nélkül.

SZÁLKEZELÉS (CLAUDE.md §15, `feedback_gui_signal_chain_testing`): MINDEN
soros I/O külön QThread-en fut (`_PicoIdentifyWorker` / `_PicoProvisionWorker`)
— a GUI-szál a teljes, akár 10-30 másodperces feltöltés alatt is válaszképes
marad. A GUI-szál SOSEM hívja közvetlenül a soros portot; a worker→signal→
handler lánc a `bq_calibration_dialog.py::_BqCalibrationConnectWorker`
bevált mintáját követi.

KÖLCSÖNÖS KIZÁRÁS: ez a dialógus ÖNÁLLÓ soros kapcsolatot nyit ugyanarra a
fizikai portra, amit a BQ Diagnosztika / gauge-kalibráció / egy futó BQ
session használ — ezért a hívó (MainWindow) felelőssége ellenőrizni, hogy
egyik sem aktív, MIELŐTT megnyitná (l. main_window.py::
`_on_pico_provisioning_requested`, ugyanaz a kizárási készlet, mint
`_on_bq_calibration_requested`-nél).

BIZTONSÁG: a feltöltés FIZIKAI, NEM VISSZAVONHATÓ írás a Pico
fájlrendszerére — ezért a "Feltöltés és indítás" gomb csak egy explicit
megerősítő jelölőnégyzet + egy megerősítő párbeszéd után fut le, és a
szolgáltatás a `PicoProvisioningService` saját `confirm=True` kapuján
keresztül indul (ugyanaz a minta, mint a BQ write-primitíveknél).
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from Prog.src.pico_provisioning_service import (
    PicoProvisioningService,
    format_identify_result,
    get_wiring_instructions,
    list_serial_ports,
)


class _PicoIdentifyWorker(QObject):
    """Csak-olvasó azonosítás a worker szálon."""

    finished: Signal = Signal(object)  # IdentifyResult

    def __init__(self, service: PicoProvisioningService, port: str) -> None:
        super().__init__()
        self._service = service
        self._port = port

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.identify(self._port)
        except Exception as exc:  # noqa: BLE001 — a dialógus SOSEM ragadhat be
            # A service minden ismert hibaágon eredményt ad vissza; egy
            # váratlan kivétel is szignállá kell váljon, különben a
            # "Azonosítás folyamatban…" állapot véglegessé válna.
            from Prog.src.pico_provisioning_service import (
                IdentifyResult,
                ProvisionErrorCode,
            )

            result = IdentifyResult(
                ok=False, port=self._port, is_micropython=False,
                error_code=ProvisionErrorCode.UNEXPECTED_ERROR,
                message=f"Váratlan hiba az azonosítás közben: {exc}",
            )
        self.finished.emit(result)


class _PicoProvisionWorker(QObject):
    """A teljes feltöltés/indítás a worker szálon, élő progress-jelzéssel.

    A `progress` szignál a WORKER szálról hívódik meg (a service
    progress_callback-jéből), a Qt queued connection-ön keresztül ér a
    GUI-szálra — ezért NEM szabad benne semmilyen widget-hívás."""

    progress: Signal = Signal(str)
    finished: Signal = Signal(object)  # ProvisionResult

    def __init__(self, service: PicoProvisioningService, port: str) -> None:
        super().__init__()
        self._service = service
        self._port = port

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.provision(
                self._port, self.progress.emit, confirm=True,
            )
        except Exception as exc:  # noqa: BLE001 — l. _PicoIdentifyWorker.run()
            from Prog.src.pico_provisioning_service import (
                ProvisionErrorCode,
                ProvisionResult,
                ProvisionStep,
            )

            result = ProvisionResult(
                ok=False, port=self._port, step=ProvisionStep.OPEN_PORT,
                error_code=ProvisionErrorCode.UNEXPECTED_ERROR,
                message=f"Váratlan hiba az előkészítés közben: {exc}",
            )
        self.finished.emit(result)


class PicoProvisioningDialog(QDialog):
    def __init__(
        self,
        parent=None,
        service: PicoProvisioningService | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pico előkészítése BQ34Z110-hez")
        self.setMinimumWidth(720)
        self._service = service if service is not None else PicoProvisioningService()
        self._thread: QThread | None = None
        self._worker: QObject | None = None
        self._last_identify: Any = None
        self._last_provision: Any = None

        self._build_ui()
        self._refresh_detected_ports()

    # ------------------------------------------------------------------ #
    # UI építés                                                           #
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        intro = QLabel(
            "Csatlakoztass egy MicroPython-os Raspberry Pi Pico-t USB-n, írd be a "
            "COM portját, majd azonosítsd. Az előkészítés feltölti rá a "
            "BQ34Z110-szolgáltatást, és el is indítja — ezután a BQ Diagnosztika "
            "fül közvetlenül csatlakozhat hozzá."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        port_box = QGroupBox("1. lépés — COM port")
        port_layout = QVBoxLayout(port_box)
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("COM port:"))
        self._port_edit = QLineEdit()
        self._port_edit.setPlaceholderText("pl. COM8")
        self._port_edit.setMaximumWidth(160)
        port_row.addWidget(self._port_edit)
        self._identify_btn = QPushButton("Azonosítás")
        self._identify_btn.clicked.connect(self._on_identify_clicked)
        port_row.addWidget(self._identify_btn)
        port_row.addStretch()
        port_layout.addLayout(port_row)
        self._detected_ports_lbl = QLabel()
        self._detected_ports_lbl.setWordWrap(True)
        port_layout.addWidget(self._detected_ports_lbl)
        root.addWidget(port_box)

        provision_box = QGroupBox("2. lépés — Feltöltés és indítás")
        provision_layout = QVBoxLayout(provision_box)
        warning = QLabel(
            "FIGYELEM: ez FIZIKAI, NEM VISSZAVONHATÓ írás a Pico "
            "fájlrendszerére. A meglévő pico_bq_service.py / pico_bq_rpc.py / "
            "main.py FELÜLÍRÓDIK (a régi példányokról előbb biztonsági mentés "
            "készül a Pico-n *.bak_… néven). A művelet alatt ne húzd ki a Pico-t."
        )
        warning.setWordWrap(True)
        provision_layout.addWidget(warning)
        self._confirm_check = QCheckBox(
            "Megértettem, és megerősítem a Pico tartalmának felülírását."
        )
        self._confirm_check.stateChanged.connect(self._update_provision_button)
        provision_layout.addWidget(self._confirm_check)
        self._provision_btn = QPushButton("Feltöltés és indítás")
        self._provision_btn.setEnabled(False)
        self._provision_btn.clicked.connect(self._on_provision_clicked)
        provision_layout.addWidget(self._provision_btn)
        root.addWidget(provision_box)

        self._status_lbl = QLabel("Készen áll.")
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(160)
        root.addWidget(self._log_view)

        # A bekötési útmutató MINDIG látható (nem csak siker után) — a
        # felhasználó a bekötés ELŐTT is meg tudja nézni.
        wiring_box = QGroupBox("Bekötési útmutató")
        wiring_layout = QVBoxLayout(wiring_box)
        self._wiring_view = QPlainTextEdit()
        self._wiring_view.setReadOnly(True)
        self._wiring_view.setPlainText(get_wiring_instructions())
        self._wiring_view.setMinimumHeight(200)
        wiring_layout.addWidget(self._wiring_view)
        root.addWidget(wiring_box)

        self._close_btn = QPushButton("Bezárás")
        # self.close() valódi QCloseEvent-et generál, így a closeEvent()-ben
        # lévő "folyamatban lévő művelet" védelem lefut (l.
        # bq_calibration_dialog.py azonos mintája, 2. review-kör F4 lelet).
        self._close_btn.clicked.connect(self.close)
        root.addWidget(self._close_btn)

    def _refresh_detected_ports(self) -> None:
        ports = list_serial_ports()
        if ports:
            self._detected_ports_lbl.setText(
                "Észlelt soros portok: "
                + "; ".join(f"{device} ({desc})" if desc else device for device, desc in ports)
            )
        else:
            self._detected_ports_lbl.setText(
                "Észlelt soros portok: (nem sikerült lekérdezni — írd be a portot kézzel)"
            )

    # ------------------------------------------------------------------ #
    # Gomb-kezelők                                                        #
    # ------------------------------------------------------------------ #
    def _current_port(self) -> str:
        return self._port_edit.text().strip()

    def _update_provision_button(self) -> None:
        self._provision_btn.setEnabled(
            self._confirm_check.isChecked() and self._thread is None
        )

    def _set_busy(self, busy: bool) -> None:
        self._port_edit.setEnabled(not busy)
        self._identify_btn.setEnabled(not busy)
        self._confirm_check.setEnabled(not busy)
        self._provision_btn.setEnabled(
            not busy and self._confirm_check.isChecked()
        )

    def _on_identify_clicked(self) -> None:
        port = self._current_port()
        if not port:
            QMessageBox.warning(
                self, "Pico előkészítés",
                "Előbb írd be a COM portot (pl. COM8).",
            )
            return
        if self._thread is not None:
            return
        self._append_log(f"--- Azonosítás: {port} ---")
        self._status_lbl.setText("Azonosítás folyamatban…")
        self._set_busy(True)
        worker = _PicoIdentifyWorker(self._service, port)
        worker.finished.connect(self._on_identify_finished)
        self._start_worker(worker)

    def _on_provision_clicked(self) -> None:
        port = self._current_port()
        if not port:
            QMessageBox.warning(
                self, "Pico előkészítés",
                "Előbb írd be a COM portot (pl. COM8).",
            )
            return
        if self._thread is not None:
            return
        if not self._confirm_check.isChecked():
            QMessageBox.warning(
                self, "Pico előkészítés",
                "Előbb jelöld be a megerősítő négyzetet — a művelet felülírja "
                "a Pico tartalmát.",
            )
            return
        # R7-B1: a felülírási kérdés alapértelmezett válasza No.
        answer = QMessageBox.question(
            self, "Pico előkészítés — megerősítés",
            f"A(z) {port} porton lévő Pico fájljai FELÜLÍRÓDNAK, és elindul "
            f"rajta a BQ34Z110-szolgáltatás.\n\nBiztosan folytatod?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._append_log("Az előkészítést a felhasználó megszakította "
                             "(megerősítés visszautasítva).")
            return
        self._append_log(f"--- Előkészítés: {port} ---")
        self._status_lbl.setText("Előkészítés folyamatban… (ne húzd ki a Pico-t)")
        self._set_busy(True)
        worker = _PicoProvisionWorker(self._service, port)
        worker.progress.connect(self._append_log)
        worker.finished.connect(self._on_provision_finished)
        self._start_worker(worker)

    # ------------------------------------------------------------------ #
    # Worker életciklus                                                   #
    # ------------------------------------------------------------------ #
    def _start_worker(self, worker: QObject) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)  # type: ignore[attr-defined]
        self._thread = thread
        self._worker = worker
        thread.start()

    def _finish_worker(self) -> bool:
        """A worker BEFEJEZTE a munkáját (a `finished` szignál már
        kézbesítve) — a szál kiléptethető. A `quit()` az eseményhurkot
        állítja le, a `wait()` a GUI-szálon csak a már befejezett run()
        visszatérésére vár, ezért nem okoz érdemi blokkolást.

        Kör 21 F12 (F21-G5, DFAM-016+DFAM-001): korábban `wait(5000)`
        timeoutjánál a metódus CSENDBEN `None`-t adott vissza, `self.
        _thread`-et élve/beállítva hagyva -- a hívók (`_on_identify_
        finished()`/`_on_provision_finished()`) EZT NEM ELLENŐRIZTÉK,
        feltétel nélkül `_set_busy(False)`-t hívtak, ami az Azonosítás
        gombot/port mezőt/checkboxot TÉVESEN újra engedélyezte (a
        `_provision_btn`-t a MEGLÉVŐ `_update_provision_button()` már
        eleve `self._thread is None`-hoz kötötte, tehát az VÁLTOZATLANUL
        letiltva maradt). MAX verifier korrekciója (Kör 21 F12): a
        `self._thread` FELÜLÍRÁSA egy második `_start_worker()`-rel A
        GYAKORLATBAN NEM történhetett meg -- `_on_identify_clicked()`
        (`:260-261`) és `_on_provision_clicked()` (`:277-278`) MINDKETTŐ
        `if self._thread is not None: return`-tel indul, tehát egy
        kattintás a téves újra-engedélyezés UTÁN is csendben no-op
        maradt volna. A valódi reziduális kockázat NEM az elárvított
        szál, hanem a félrevezető, "kész" állapotot sugalló, de
        ténylegesen semmit nem tevő UI. `closeEvent()` (l. ott) MOST
        defenzíven szintén ellenőrzi ezt a visszatérési értéket -- egy
        VALÓS QThread-en ez a False-ág a gyakorlatban ott sem érhető el
        (a `closeEvent()` saját, MEGELŐZŐ `isRunning()` kapuja és a
        `wait()` ugyanazt a mutex-vezérelt állapotot nézi, tehát a kettő
        majdnem kizárja egymást -- ELTÉRŐEN `bq_calibration_dialog.py::
        closeEvent()`-től, ahol a MEGELŐZŐ kapu egy MÁSIK objektumon,
        `self._connect_thread`-en fut, ott a False-ág valóban elérhető).
        Ugyanaz a mintakövetés, mint `bq_calibration_dialog.py::
        _cleanup_thread()`-nél (ott már `bool`-t ad vissza).
        Most `bool`-t ad vissza: `False`-nál a hívó MEGŐRZI a busy
        állapotot és tájékoztatja a kezelőt, `self._thread` VÁLTOZATLANUL
        él a következő próbálkozásig."""
        if self._thread is not None:
            self._thread.quit()
            if not self._thread.wait(5000):
                # Nem tudjuk biztonságosan eldobni egy még futó szálra a
                # deleteLater()-t — meghagyjuk a referenciát, és a
                # closeEvent() majd újra megkísérli (ugyanaz az elv, mint
                # bq_calibration_dialog.py::_cleanup_thread()).
                return False
            self._thread.deleteLater()
            self._thread = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        return True

    # ------------------------------------------------------------------ #
    # Eredmény-kezelők                                                    #
    # ------------------------------------------------------------------ #
    def _on_identify_finished(self, result: Any) -> None:
        self._last_identify = result
        if not self._finish_worker():
            # Kör 21 F12 (F21-G5): a szál nem állt le az 5s-es wait()
            # alatt -- MEGŐRIZZÜK a busy állapotot (gombok letiltva
            # maradnak), hogy ne mutasson a felület téves "kész, próbálj
            # újra" állapotot egy nem-igazolt leállású szál mellett (l.
            # _finish_worker() docstringje a pontos, MAX verifier által
            # korrigált kockázat-leírásért).
            self._status_lbl.setText(
                "A szál leállítása még folyamatban — várj, ne indíts új "
                "műveletet."
            )
            return
        self._append_log(format_identify_result(result))
        self._status_lbl.setText(result.message)
        self._set_busy(False)
        self._update_provision_button()

    def _on_provision_finished(self, result: Any) -> None:
        self._last_provision = result
        if not self._finish_worker():
            # Kör 21 F12 (F21-G5): l. _on_identify_finished() azonos
            # hivatkozású kommentje.
            self._status_lbl.setText(
                "A szál leállítása még folyamatban — várj, ne indíts új "
                "műveletet."
            )
            return
        if result.ok:
            self._status_lbl.setText(result.message)
            self._append_log("")
            self._append_log(
                "A Pico készen áll. Zárd be ezt az ablakot, és a BQ "
                "Diagnosztika fülön csatlakozz ugyanerre a portra."
            )
        else:
            self._status_lbl.setText(
                f"SIKERTELEN [{result.error_code}] a(z) {result.step} lépésnél — "
                f"{result.message}"
            )
            failure_text = (
                f"Az előkészítés megállt a(z) {result.step} lépésnél.\n\n"
                f"Hibakód: {result.error_code}\n\n{result.message}"
            )
            # Kör 20 Batch E11 (F20-G3, DFAM-018+DFAM-026+DFAM-021, a
            # main_window.py:1852-1862 (Cell BL residue) mintáját követve):
            # korábban a hiba-ág KIZÁRÓLAG result.step/error_code/message-t
            # jelenítette meg -- a result.uploaded (az EGYETLEN, a
            # provision() saját, per-fájlos SHA256-igazolása UTÁN feltöltött
            # állapotot hordozó adat, l. pico_provisioning_service.py::
            # provision() 491-496. sor) SOHA nem jutott el a kezelőig. A
            # kezelő "megállt X lépésnél" üzenetet kapott, de nem tudta meg,
            # hogy a Pico flash-ére MÁR felkerültek fájlok, vagy hogy
            # léteznek-e .bak_ biztonsági mentések.
            if result.uploaded:
                residue_lines = [
                    "",
                    (
                        f"A hiba ELŐTT {len(result.uploaded)} fájl már felkerült és "
                        f"SHA256-tal igazoltan sikeresen feltöltődött a Pico flash-ére:"
                    ),
                ]
                for f in result.uploaded:
                    if f.backup_name:
                        residue_lines.append(
                            f"  • {f.name}  (korábbi verzió mentve: {f.backup_name})"
                        )
                    else:
                        residue_lines.append(f"  • {f.name}  (nem volt korábbi verzió)")
                residue_lines.append(
                    "Ezeket NEM kell kézzel törölni -- egy újabb próbálkozás "
                    "(provision() teljes 9 lépése elölről) a fenti fájlokat "
                    "biztonsági mentés után felülírja."
                )
                failure_text += "\n" + "\n".join(residue_lines)
            QMessageBox.critical(self, "Pico előkészítés — hiba", failure_text)
            if result.uploaded:
                self._append_log("")
                self._append_log(
                    f"A hiba ELŐTT {len(result.uploaded)} fájl már felkerült a "
                    f"Pico flash-ére:"
                )
                for f in result.uploaded:
                    backup_note = f.backup_name or "nincs korábbi verzió"
                    self._append_log(f"  {f.name} (backup: {backup_note})")
        self._set_busy(False)
        self._update_provision_button()

    def _append_log(self, text: str) -> None:
        self._log_view.appendPlainText(text)

    # ------------------------------------------------------------------ #
    # Bezárás                                                             #
    # ------------------------------------------------------------------ #
    def reject(self) -> None:  # Qt override
        # Az Escape alapértelmezetten reject()-et hív, ami a Qt QDialog::done()
        # CloseNoEvent viselkedése miatt NEM vált ki QCloseEvent-et — így a
        # lenti védelem megkerülhető lenne (l. bq_calibration_dialog.py /
        # device_search_dialog.py azonos, már javított mintája).
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.information(
                self, "Művelet folyamatban",
                "A Pico-val folyó művelet még tart. Várd meg, amíg befejeződik "
                "— egy megszakított feltöltés csonka fájlt hagyhat az eszközön.",
            )
            event.ignore()
            return
        # Kör 21 F12 (F21-G5): a fenti `isRunning()` ellenőrzés (ugyanazon
        # self._thread objektumon, ugyanazon mutex-vezérelt állapoton) a
        # gyakorlatban majdnem kizárja, hogy az alábbi wait(5000) False-t
        # adjon -- a bq_calibration_dialog.py::closeEvent() itt ELTÉRŐ
        # mintát követ: OTT a megelőző kapu egy MÁSIK objektumon
        # (self._connect_thread) fut, tehát ott a False-ág valóban
        # elérhető. Itt a defenzív ellenőrzés tudatosan konzervatív
        # (elméleti/jövőbeli refaktor elleni védelem), nem egy ma
        # reprodukálható hibautat zár le -- l. _finish_worker() docstringje.
        if not self._finish_worker():
            event.ignore()
            QMessageBox.warning(
                self, "Bezárás megszakítva",
                "A szál leállítása még folyamatban van. Az ablak NEM "
                "zárható be, amíg ez be nem fejeződik -- várj, majd "
                "próbáld újra.",
            )
            return
        # event.accept() közvetlenül (NEM super().closeEvent()): a QDialog
        # belső implementációja reject()-et hívna, ami a fenti felülbírálás
        # miatt visszahívná a close()-t (rekurzió) — l. bq_calibration_dialog.py
        # MAX verifier O-1 lelete.
        event.accept()
