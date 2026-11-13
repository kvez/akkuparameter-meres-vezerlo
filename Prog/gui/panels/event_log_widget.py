"""EventLogWidget — scrollozható eseménylista, severity alapú színezéssel."""
from __future__ import annotations
from datetime import datetime

from PySide6.QtCore import Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QLabel,
)

_MAX_ROWS = 500

_SEVERITY_BG: dict[str, str | None] = {
    "INFO":       None,
    "WARNING":    "#fff8e1",
    "FAULT":      "#ffebee",
    "CHECKPOINT": "#fff3e0",
    # Governance-v2 review-kör 1 (CF-07/GUI-R1-13, DFAM-011, batch 4.2;
    # MAX verifier F-1 pontosítás — a forrás-hivatkozás korábban tévesen
    # test_runner.py-t is megnevezett): worker.py::CHECKPOINT_RESUME_
    # BLOCKED_BY_EMERGENCY_STOP és relax_controller.py::RELAX_
    # TEMPERATURE_DMM_FAULT explicit "severity": "CRITICAL"-t adnak — ez
    # a kulcs korábban hiányzott a mapből, ezért _infer_severity() az
    # explicit, magas prioritású jelzést CSENDBEN eldobta, és a
    # (esetlegesen félrevezető) event_code-alapú találgatásra esett
    # vissza. A FAULT-tal azonos, piros hátteret kapja. A test_runner.py
    # CONTROLLER_ABORT_FAILED (és a legtöbb hasonló belső esemény) MA
    # NEM ad explicit severity-t — ez önmagában is nyitott, dokumentált
    # követő tétel (l. WORK_STATE.json batch 4.2b).
    "CRITICAL":   "#ffebee",
}


def _infer_severity(event: dict) -> str:
    """BATCH 4.2b F-1 (2026-09-13, DECISION_REGISTER.md GOV-024, "5.
    csoport"): a legtobb produkcios hiba-esemeny (test_runner.py::
    CONTROLLER_ABORT_FAILED/SAFE_OFF_RESULT_UNCERTAIN_LOGGING_FAILED/
    stb., traditional_learning_controller.py LEARNING_*_FAILED/
    NOT_CONFIRMED/EXCEEDED/TIMEOUT esemenyei) NEM ad explicit
    `severity`-t az `on_event()` dict-ben (a perzisztens
    `logger.log_event(is_critical=True)` hivas ettol fuggetlen, azt
    mar korabban helyesen jeloltek) -- enelkul ezek a tenylegesen
    sulyos esemenyek a fallback-agon egyszeru INFO-kent (szin nelkul)
    jelentek meg. Minden hivohely kezi severity-bovitese (tucatnyi
    hely, tobb fajlban) a DFAM-026 ismetlodo mintaja lenne -- a
    fallback-heurisztika egyetlen, kozponti bovitese a teljes
    family-t lefedi (jelenlegi ES jovobeli hivohelyeket is)."""
    sev = event.get("severity")
    if sev in _SEVERITY_BG:
        return sev
    code = (event.get("event_code") or "").upper()
    if "FAULT" in code or "EMERGENCY" in code or code in {
        "BATTERY_OVERTEMPERATURE", "DISCHARGE_CONFIG_INVALID",
    }:
        return "FAULT"
    if (
        "FAILED" in code or "NOT_CONFIRMED" in code or "EXCEEDED" in code
        or "TIMEOUT" in code or "UNCERTAIN" in code
    ):
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
        # MAX independent verifier P3-B lelete (f9027e3 utóellenőrzés, batch
        # 4.3): a testvér device_error_panel.py::append_device_error() már
        # (ValueError, TypeError)-t fog el ugyanerre a mintára — itt csak
        # ValueError volt elkapva. Ma látens (a runner event dict-jei nem
        # hordoznak timestamp_iso-t, az `if ts:` rövidzár miatt), de a
        # sibling-inkonzisztencia (CLAUDE.md §8) önmagában javítandó.
        except (ValueError, TypeError):
            pass
    return datetime.now().strftime("%H:%M:%S")


class EventLogWidget(QWidget):
    """Scrollozható eseménynapló — append_event(dict) hívással bővíthető."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Eseménynapló</b>"))
        header.addStretch()
        # Review-kör 2 (2026-09-09), friss GUI/UX F4 (P2, DFAM-001/012): a
        # device_error_panel.py testvér gyűrűpufferét a CF-29 fix már
        # ellátta ezzel a jelzővel -- ez a widget (LivePanel élő
        # eseménynaplója + BqDiagnosticsPanel._session_event_log) NEM
        # kapta meg, holott ugyanaz a DFAM-011 evidence által dokumentált
        # kockázat áll fenn (tickenkénti relax-DMM-hiba esemény kiürítheti
        # az 500-soros gyűrűt egy hosszú relax alatt). A legrégebbi
        # bejegyzések eddig NÉMÁN estek ki -- semmi nem jelezte a
        # láthatóság/teljesség eltérését.
        self._total = 0
        self._truncated_count = 0
        self._count_lbl = QLabel("0 esemény")
        header.addWidget(self._count_lbl)
        layout.addLayout(header)
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
            self._truncated_count += 1

        self._list.scrollToBottom()

        self._total += 1
        if self._truncated_count > 0:
            self._count_lbl.setText(
                f"{self._total} esemény ({self._truncated_count} régebbi törölve, "
                f"{self._list.count()} látható)"
            )
        else:
            self._count_lbl.setText(f"{self._total} esemény")

    def clear(self) -> None:
        self._list.clear()
        self._total = 0
        self._truncated_count = 0
        self._count_lbl.setText("0 esemény")
