"""VISA eszközkereső dialógus — csak *IDN? lekérés, nincs OUTPUT ON / INPUT ON."""
from __future__ import annotations
from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QMessageBox, QVBoxLayout, QLabel, QTextEdit, QPushButton,
)


def _search_visa_resources() -> list[tuple[str, str, bool]]:
    """Minden VISA resource *IDN? lekérése. Visszaad: [(resource, idn_vagy_hibaüzenet, ok)].

    Kör 19 D8f (GUI-R19-05, DFAM-018 4. előfordulása ebben a körben): a
    harmadik, `ok` mező KÜLÖN a resource-tól -- korábban a hívó a resource
    NEVÉT nézte ("ERROR"/"INFO" szűrése) annak eldöntésére, hogy egy
    bejegyzés SIKERES azonosítás volt-e, de egy VALÓS resource sikertelen
    *IDN? lekérdezése (timeout/protokollhiba, l. lent) a resource nevét
    VÁLTOZATLANUL megtartja -- csak az `idn` mezőbe kerül hibaüzenet. Egy
    egész rack időtúllépő műszere emiatt "N eszköz azonosítva"-ként
    jelent meg, holott egyikük sem válaszolt érdemben."""
    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
        resources = list(rm.list_resources())
    except Exception as exc:
        return [("ERROR", f"NI-VISA / pyvisa hiba: {exc} — Telepítsd a NI-VISA drivert.", False)]

    if not resources:
        return [("INFO", "Nincs elérhető VISA eszköz. Ellenőrizd a kábeleket és NI-VISA telepítést.", False)]

    results = []
    for r in resources:
        try:
            inst = rm.open_resource(r)
            inst.timeout = 2000
            idn = inst.query("*IDN?").strip()  # type: ignore[attr-defined]
            inst.close()
            results.append((r, idn, True))
        except Exception:
            results.append((r, "— IDN lekérés sikertelen (timeout vagy protokoll hiba)", False))
    return results


class _SearchWorker(QObject):
    result_ready: Signal = Signal(list)
    finished: Signal = Signal()

    @Slot()
    def run(self) -> None:
        results = _search_visa_resources()
        self.result_ready.emit(results)
        self.finished.emit()


class DeviceSearchDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("VISA eszközök keresése")
        self.setMinimumWidth(640)
        self.setMinimumHeight(300)
        self._thread: QThread | None = None
        self._worker: _SearchWorker | None = None
        self._build_ui()
        self._start_search()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self._status_label = QLabel("Keresés folyamatban… (néhány másodperc)")
        layout.addWidget(self._status_label)
        self._result_text = QTextEdit()
        self._result_text.setReadOnly(True)
        self._result_text.setFontFamily("Courier New")
        self._result_text.setPlaceholderText("Az eredmények itt jelennek meg.")
        layout.addWidget(self._result_text)
        self._close_btn = QPushButton("Bezár")
        self._close_btn.setEnabled(False)
        # F4-mintakövetés (l. reject() docstringje): self.close() valódi
        # QCloseEvent-et generál, self.accept() nem.
        self._close_btn.clicked.connect(self.close)
        layout.addWidget(self._close_btn)

    def _start_search(self) -> None:
        self._thread = QThread(self)
        self._worker = _SearchWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.result_ready.connect(self._on_result)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        # R1 F-30: a nullázó slot a deleteLater ELŐTT kötve -- mindkét
        # fogadó GUI-affinitású, a queued hívások kapcsolási sorrendben
        # futnak, így a referencia a DeferredDelete feldolgozása előtt
        # nullázódik (l. _on_thread_finished()).
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot()
    def _on_thread_finished(self) -> None:
        """R1 F-30 (NEW_FAMILY qt_object_deleted_but_python_reference_
        retained): a QThread a `finished` jelre deleteLater()-rel törlődik;
        a megtartott Python-referencia miatt egy befejezett keresés UTÁNI
        bezárás a closeEvent()-ben a TÖRÖLT C++ objektumon hívta az
        isRunning()-ot (közvetlen hívásnál RuntimeError, a valódi close()/
        Escape/"Bezár" láncon processz-összeomlás). A referencia KIZÁRÓLAG
        itt, a szál tényleges befejezésekor nullázódik -- result_ready-nél
        NEM, különben a futó keresést védő bezárási kapu megkerülhető
        lenne.

        A self._worker-t szándékosan NEM nullázzuk: a worker Python-
        tulajdonú, a worker-szálon él és ott törlődik (deleteLater a szál
        befejezésekor); az utolsó Python-referencia GUI-szálbeli eldobása
        közvetlen, szálközi C++ delete-et váltana ki, versenyben a worker-
        szál DeferredDelete-jével. A _worker a _start_search() után sehol
        nem dereferálódik."""
        self._thread = None

    @Slot(list)
    def _on_result(self, results: list) -> None:
        lines = [f"{r}\n    → {idn}" for r, idn, _ok in results]
        self._result_text.setPlainText("\n\n".join(lines))
        # Kör 19 D8f (GUI-R19-05, DFAM-018): l. _search_visa_resources()
        # docstringje -- az `ok` mező alapján számolunk, NEM a resource
        # nevéből ("ERROR"/"INFO" szűrés) következtetve. Egy sikertelen
        # *IDN? lekérdezésű VALÓS resource emiatt MOST nem számít
        # "azonosítottnak", és a sikertelen darabszám külön is látszik.
        identified = sum(1 for _r, _idn, ok in results if ok)
        failed = sum(1 for _r, _idn, ok in results if not ok and _r not in ("ERROR", "INFO"))
        status = f"Kész — {identified} eszköz azonosítva"
        if failed:
            status += f" ({failed} sikertelen lekérdezés)"
        self._status_label.setText(status)
        self._close_btn.setEnabled(True)

    def reject(self) -> None:  # Qt override
        # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P3 lelet (F5):
        # az Escape billentyű alapértelmezetten reject()-et hív, ami — a
        # bq_calibration_dialog.py "Bezárás" gomb F4 leletéhez hasonlóan —
        # Qt QDialog::done() szándékos CloseNoEvent viselkedése miatt NEM
        # vált ki QCloseEvent-et, tehát a lentebbi closeEvent()-ben lévő
        # védelem megkerülhető volt. self.close() valódi QCloseEvent-et
        # generál, így a closeEvent() mindig lefut.
        self.close()

    def closeEvent(self, event) -> None:  # Qt override
        # A keresés maga véges idejű (néhány VISA *IDN? lekérdezés,
        # jellemzően pár másodperc) és önmagát leállítja — itt csak annyit
        # kell garantálni, hogy a dialógus bezárásakor ne maradjon futó
        # szál egy már megsemmisített dialógusra hivatkozva.
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.information(
                self, "Keresés folyamatban",
                "A VISA eszközkeresés még folyamatban van. Várj, amíg "
                "befejeződik, majd zárd be az ablakot.",
            )
            event.ignore()
            return
        event.accept()
