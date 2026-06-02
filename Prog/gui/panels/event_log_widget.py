"""EventLogWidget — scrollozható eseménylista, severity alapú színezéssel."""
from __future__ import annotations
from datetime import datetime

from PySide6.QtCore import Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QLabel,
)

_MAX_ROWS = 500

_SEVERITY_BG: dict[str, str | None] = {
    "INFO":       None,
    "WARNING":    "#fff8e1",
    "FAULT":      "#ffebee",
    "CHECKPOINT": "#fff3e0",
}


def _infer_severity(event: dict) -> str:
    sev = event.get("severity")
    if sev in _SEVERITY_BG:
        return sev
    code = (event.get("event_code") or "").upper()
    if "FAULT" in code or "EMERGENCY" in code:
        return "FAULT"
    if "WARNING" in code or "HIGH" in code or "LOST" in code:
        return "WARNING"
    if "CHECKPOINT" in code:
        return "CHECKPOINT"
    return "INFO"


def _format_ts(event: dict) -> str:
    ts = event.get("timestamp_iso")
    if ts:
        try:
            return datetime.fromisoformat(ts).strftime("%H:%M:%S")
        except ValueError:
            pass
    return datetime.now().strftime("%H:%M:%S")


class EventLogWidget(QWidget):
    """Scrollozható eseménynapló — append_event(dict) hívással bővíthető."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(QLabel("<b>Eseménynapló</b>"))
        self._list = QListWidget()
        self._list.setMaximumHeight(150)
        self._list.setAlternatingRowColors(True)
        layout.addWidget(self._list)

    @Slot(dict)
    def append_event(self, event: dict) -> None:
        severity = _infer_severity(event)
        ts = _format_ts(event)
        code = event.get("event_code") or ""
        msg = event.get("event_message") or ""
        text = f"[{ts}] {severity:<10} {code} — {msg}"

        item = QListWidgetItem(text)
        bg = _SEVERITY_BG.get(severity)
        if bg:
            item.setBackground(QColor(bg))
        self._list.addItem(item)

        while self._list.count() > _MAX_ROWS:
            self._list.takeItem(0)

        self._list.scrollToBottom()

    def clear(self) -> None:
        self._list.clear()
