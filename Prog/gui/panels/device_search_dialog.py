"""VISA eszközkereső dialógus — csak *IDN? lekérés, nincs OUTPUT ON / INPUT ON."""
from __future__ import annotations
from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTextEdit, QPushButton,
)


def _search_visa_resources() -> list[tuple[str, str]]:
    """Minden VISA resource *IDN? lekérése. Visszaad: [(resource, idn_vagy_hibaüzenet)]."""
    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
        resources = list(rm.list_resources())
    except Exception as exc:
        return [("ERROR", f"NI-VISA / pyvisa hiba: {exc} — Telepítsd a NI-VISA drivert.")]

    if not resources:
        return [("INFO", "Nincs elérhető VISA eszköz. Ellenőrizd a kábeleket és NI-VISA telepítést.")]

    results = []
    for r in resources:
        try:
            inst = rm.open_resource(r)
            inst.timeout = 2000
            idn = inst.query("*IDN?").strip()  # type: ignore[attr-defined]
            inst.close()
            results.append((r, idn))
        except Exception:
            results.append((r, "— IDN lekérés sikertelen (timeout vagy protokoll hiba)"))
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
        self._close_btn.clicked.connect(self.accept)
        layout.addWidget(self._close_btn)

    def _start_search(self) -> None:
        self._thread = QThread(self)
        self._worker = _SearchWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.result_ready.connect(self._on_result)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot(list)
    def _on_result(self, results: list) -> None:
        lines = [f"{r}\n    → {idn}" for r, idn in results]
        self._result_text.setPlainText("\n\n".join(lines))
        count = sum(1 for r, _ in results if r not in ("ERROR", "INFO"))
        self._status_label.setText(f"Kész — {count} eszköz azonosítva")
        self._close_btn.setEnabled(True)
