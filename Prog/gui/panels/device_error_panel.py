"""DeviceErrorPanel — eszköz SCPI error queue monitoring.
Tickenként fogadja az on_device_error callbackből érkező hibákat,
megjeleníti QListWidget-ben, és felhalmozza a session teljes ideje alatt.
"""
from __future__ import annotations
from datetime import datetime

from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QPushButton,
)

_MAX_ROWS = 1000

_DEVICE_BG: dict[str, str] = {
    "PSU":   "#fff8e1",
    "Load":  "#e8f5e9",
    "DMM_V": "#e3f2fd",
    "DMM_T": "#fce4ec",
}


class DeviceErrorPanel(QWidget):
    """Eszköz SCPI hibák felhalmozódó listája."""

    # Review-kör 4/5 GUI F5 (P2, DFAM-012 rokona): a "Törlés" gomb korábban
    # csak ezt a panelt nullázta -- l. main_window.py bekötés a kompakt
    # LivePanel-számláló szinkronban tartásához.
    cleared = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Eszköz SCPI hibanapló</b>"))
        header.addStretch()
        self._count_lbl = QLabel("0 hiba")
        header.addWidget(self._count_lbl)
        clr_btn = QPushButton("Törlés")
        clr_btn.setFixedWidth(70)
        clr_btn.clicked.connect(self.clear)
        header.addWidget(clr_btn)
        layout.addLayout(header)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        layout.addWidget(self._list)

        self._total: int = 0
        # Governance-v2 review-kör 1 (CF-29, GUI-R1-11, DFAM-012 rokona):
        # a _MAX_ROWS túlcsordulásnál korábban a legrégebbi bejegyzések
        # NÉMÁN kiestek a listából — a _count_lbl a _total-t mutatta
        # (ami helyesen tovább nőtt), de semmi nem jelezte explicit, hogy
        # a LÁTHATÓ lista rövidebb, mint a _total, azaz hogy történt
        # törlés. Ez a számláló teszi explicitté.
        self._truncated_count: int = 0

    @Slot(dict)
    def append_device_error(self, err: dict) -> None:
        ts_raw = err.get("timestamp_iso", "")
        try:
            ts = datetime.fromisoformat(ts_raw).strftime("%H:%M:%S")
        except (ValueError, TypeError):
            ts = datetime.now().strftime("%H:%M:%S")

        device = err.get("device", "?")
        error  = err.get("error", "")
        text   = f"[{ts}] {device:<6} {error}"

        item = QListWidgetItem(text)
        bg = _DEVICE_BG.get(device)
        if bg:
            item.setBackground(QColor(bg))
        self._list.addItem(item)
        self._list.scrollToBottom()

        while self._list.count() > _MAX_ROWS:
            self._list.takeItem(0)
            self._truncated_count += 1

        self._total += 1
        if self._truncated_count > 0:
            self._count_lbl.setText(
                f"{self._total} hiba ({self._truncated_count} régebbi törölve, "
                f"{self._list.count()} látható)"
            )
        else:
            self._count_lbl.setText(f"{self._total} hiba")

    def clear(self) -> None:
        """Teljes kódbázis review 16. kör (2026-09-04), GUI/UX/Állapot P3
        lelet: korábban csak a "Törlés" gombhoz kötött privát metódus volt
        — a panel semmilyen Start/session-határ eseménynél nem törlődött,
        emiatt egy korábbi, hibás session SCPI-hibái láthatók maradtak egy
        KÉSŐBBI, hibamentes session alatt is. Publikussá téve, hogy
        main_window.py::_on_connect_ready() is meghívhassa új session
        indításakor (a LivePanel.reset_plots() mintáját követve)."""
        self._list.clear()
        self._total = 0
        self._truncated_count = 0
        self._count_lbl.setText("0 hiba")
        self.cleared.emit()
