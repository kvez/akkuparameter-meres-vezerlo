"""
LivePanel — kibővített állapotsor + 4 pyqtgraph grafikon + Rb panel + Start/Stop/EmStop gombok.
Slot: update_sample(dict) — minden tickben frissül.
"""
from __future__ import annotations
from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal, Slot
from Prog.gui.panels.event_log_widget import EventLogWidget
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QGridLayout, QSizePolicy,
)

_MAX_POINTS = 3600  # max 1 óra 1 s tickkel
_DVDT_WINDOW = 10   # dV/dt számítási ablak (minták száma)


def _make_plot(title: str, y_label: str, color: str) -> tuple[pg.PlotWidget, pg.PlotDataItem]:
    pw = pg.PlotWidget(title=title)
    pw.setLabel("bottom", "Eltelt idő", units="s")
    pw.setLabel("left", y_label)
    pw.showGrid(x=True, y=True, alpha=0.3)
    curve = pw.plot(pen=pg.mkPen(color=color, width=2))
    pw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)  # type: ignore[attr-defined]
    return pw, curve


class LivePanel(QWidget):
    """Real-time monitoring panel."""

    stop_requested           = Signal()
    emergency_stop_requested = Signal()
    start_requested          = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._xs:           deque[float] = deque(maxlen=_MAX_POINTS)
        self._u_batt:       deque[float] = deque(maxlen=_MAX_POINTS)
        self._i_signed:     deque[float] = deque(maxlen=_MAX_POINTS)
        self._temp:         deque[float] = deque(maxlen=_MAX_POINTS)
        self._charge_ah:    deque[float] = deque(maxlen=_MAX_POINTS)
        self._discharge_ah: deque[float] = deque(maxlen=_MAX_POINTS)
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI építés                                                            #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- Fő állapotsor (nagyobb, kibővített) ---
        status_box = QGroupBox("Állapot")
        status_grid = QGridLayout(status_box)
        status_grid.setSpacing(10)

        self._state_lbl    = self._status_label("IDLE")
        self._ubatt_lbl    = self._status_label("– V")
        self._isign_lbl    = self._status_label("– A")
        self._temp_lbl     = self._status_label("– °C")
        self._charge_lbl   = self._status_label("– Ah")
        self._disch_lbl    = self._status_label("– Ah")
        self._psumode_lbl  = self._status_label("–")
        self._udrop_lbl    = self._status_label("– V")
        self._upsu_set_lbl = self._status_label("– V")
        self._iload_set_lbl = self._status_label("– A")
        self._dvdt_lbl     = self._status_label("– mV/s")
        self._warn_lbl     = self._status_label("–")
        self._fault_lbl    = self._status_label("–")

        # Első sor: fő mérési adatok
        _ROW1 = [
            ("Állapot",     self._state_lbl),
            ("U_batt",      self._ubatt_lbl),
            ("I_signed",    self._isign_lbl),
            ("T_batt",      self._temp_lbl),
            ("Töltve",      self._charge_lbl),
            ("Kisütve",     self._disch_lbl),
            ("PSU mód",     self._psumode_lbl),
        ]
        # Második sor: beállított értékek + derivált + hibák
        _ROW2 = [
            ("Dióda esés",  self._udrop_lbl),
            ("PSU set V",   self._upsu_set_lbl),
            ("Terh. I set", self._iload_set_lbl),
            ("dV/dt",       self._dvdt_lbl),
            ("Warning",     self._warn_lbl),
            ("Fault",       self._fault_lbl),
        ]
        for col, (name, widget) in enumerate(_ROW1):
            status_grid.addWidget(QLabel(f"<b>{name}</b>"), 0, col)
            status_grid.addWidget(widget, 1, col)
        for col, (name, widget) in enumerate(_ROW2):
            status_grid.addWidget(QLabel(f"<b>{name}</b>"), 2, col)
            status_grid.addWidget(widget, 3, col)
        status_grid.setRowMinimumHeight(2, 4)  # kis elválasztás a két sor között

        root.addWidget(status_box)

        # --- Gombok ---
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("▶  Start")
        self._start_btn.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold;")
        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setEnabled(False)
        self._emstop_btn = QPushButton("⚡  Emergency Stop")
        self._emstop_btn.setStyleSheet(
            "background-color: #b71c1c; color: white; font-weight: bold;")
        self._emstop_btn.setEnabled(False)

        self._start_btn.clicked.connect(self.start_requested)
        self._stop_btn.clicked.connect(self.stop_requested)
        self._emstop_btn.clicked.connect(self.emergency_stop_requested)

        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._stop_btn)
        btn_row.addWidget(self._emstop_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        # --- Rb panel (diódaesés grafikon helyett) ---
        rb_box = QGroupBox("Belső ellenállás (Rb mérés — kisütés elején)")
        rb_box.setMinimumHeight(110)
        rb_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)  # type: ignore[attr-defined]
        rb_grid = QGridLayout(rb_box)
        rb_grid.setSpacing(14)

        self._rb1s_lbl  = self._rb_value_label("– mΩ")
        self._rb10s_lbl = self._rb_value_label("– mΩ")
        self._rb30s_lbl = self._rb_value_label("– mΩ")
        self._rb_dv_lbl = self._rb_value_label("– V")

        for col, (title, widget) in enumerate([
            ("Rb 1s", self._rb1s_lbl),
            ("Rb 10s", self._rb10s_lbl),
            ("Rb 30s", self._rb30s_lbl),
            ("ΔV (U_OCV − U_load)", self._rb_dv_lbl),
        ]):
            hdr = QLabel(f"<b>{title}</b>")
            hdr.setAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
            rb_grid.addWidget(hdr, 0, col)
            rb_grid.addWidget(widget, 1, col)
            rb_grid.setColumnStretch(col, 1)

        root.addWidget(rb_box)

        # --- 4 grafikon ---
        self._pw_ubatt, self._curve_ubatt = _make_plot(
            "Akkufeszültség", "U_batt (V)", "#4fc3f7")
        self._pw_isign, self._curve_isign = _make_plot(
            "Áram (signed)", "I_signed (A)", "#81c784")
        self._pw_temp, self._curve_temp = _make_plot(
            "Hőmérséklet", "T_batt (°C)", "#ffb74d")

        self._pw_ah = pg.PlotWidget(title="Kapacitás integrátor")
        self._pw_ah.setLabel("bottom", "Eltelt idő", units="s")
        self._pw_ah.setLabel("left", "Ah")
        self._pw_ah.showGrid(x=True, y=True, alpha=0.3)
        self._pw_ah.addLegend()
        self._curve_ch_ah = self._pw_ah.plot(
            pen=pg.mkPen(color="#4fc3f7", width=2), name="Charge Ah")
        self._curve_dch_ah = self._pw_ah.plot(
            pen=pg.mkPen(color="#ef9a9a", width=2), name="Discharge Ah")
        self._pw_ah.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)  # type: ignore[attr-defined]

        for pw in (self._pw_ubatt, self._pw_isign, self._pw_temp, self._pw_ah):
            root.addWidget(pw)

        self._event_log = EventLogWidget()
        root.addWidget(self._event_log)

    # ------------------------------------------------------------------ #
    # Segéd widgetek                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _status_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("font-size: 15px; padding: 5px;")
        return lbl

    @staticmethod
    def _rb_value_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #ce93d8; padding: 8px;")
        lbl.setAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
        return lbl

    # ------------------------------------------------------------------ #
    # Frissítés                                                            #
    # ------------------------------------------------------------------ #

    @Slot(dict)
    def update_sample(self, sample: dict) -> None:
        elapsed = sample.get("elapsed_s") or 0.0
        self._xs.append(elapsed)

        def _f(key: str) -> float:
            v = sample.get(key)
            return float(v) if v is not None else float("nan")

        u   = _f("battery_voltage_V")
        i   = _f("signed_current_A")
        t   = _f("battery_temperature_C")
        d   = _f("u_drop_V")
        ch  = _f("accumulated_charge_Ah")
        dch = _f("accumulated_discharge_Ah")

        self._u_batt.append(u)
        self._i_signed.append(i)
        self._temp.append(t)
        self._charge_ah.append(ch)
        self._discharge_ah.append(dch)

        xs = list(self._xs)
        self._curve_ubatt.setData(xs, list(self._u_batt))
        self._curve_isign.setData(xs, list(self._i_signed))
        self._curve_temp.setData(xs, list(self._temp))
        self._curve_ch_ah.setData(xs, list(self._charge_ah))
        self._curve_dch_ah.setData(xs, list(self._discharge_ah))

        # --- Állapotsor értékek ---
        self._ubatt_lbl.setText(f"{u:.3f} V"  if u == u  else "– V")
        self._isign_lbl.setText(f"{i:.3f} A"  if i == i  else "– A")
        self._temp_lbl.setText( f"{t:.1f} °C" if t == t  else "– °C")
        self._charge_lbl.setText(f"{ch:.4f} Ah"  if ch == ch   else "– Ah")
        self._disch_lbl.setText( f"{dch:.4f} Ah" if dch == dch else "– Ah")
        self._psumode_lbl.setText(sample.get("psu_mode") or "–")
        self._udrop_lbl.setText(f"{d:.3f} V" if d == d else "– V")
        self._warn_lbl.setText(sample.get("warning_flags") or "–")

        upsu = sample.get("psu_set_voltage_V")
        self._upsu_set_lbl.setText(
            f"{upsu:.3f} V" if upsu is not None and upsu > 0 else "– V")

        iload = sample.get("load_set_current_A")
        self._iload_set_lbl.setText(
            f"{iload:.3f} A" if iload is not None and iload > 0 else "– A")

        # dV/dt: utolsó _DVDT_WINDOW minta átlagos meredeksége
        u_list = list(self._u_batt)
        x_list = list(self._xs)
        n = min(_DVDT_WINDOW, len(u_list))
        if n >= 2:
            dt_win = x_list[-1] - x_list[-n]
            if dt_win > 0 and u_list[-1] == u_list[-1] and u_list[-n] == u_list[-n]:
                dvdt_mvs = (u_list[-1] - u_list[-n]) / dt_win * 1000.0
                self._dvdt_lbl.setText(f"{dvdt_mvs:+.2f} mV/s")
            else:
                self._dvdt_lbl.setText("– mV/s")
        else:
            self._dvdt_lbl.setText("– mV/s")

        # --- Rb panel ---
        rb1  = sample.get("rb_1s_mohm")
        rb10 = sample.get("rb_10s_mohm")
        rb30 = sample.get("rb_30s_mohm")
        dv   = sample.get("rb_delta_v")

        self._rb1s_lbl.setText( f"{rb1:.1f} mΩ"  if rb1  is not None else "– mΩ")
        self._rb10s_lbl.setText(f"{rb10:.1f} mΩ" if rb10 is not None else "– mΩ")
        self._rb30s_lbl.setText(f"{rb30:.1f} mΩ" if rb30 is not None else "– mΩ")
        self._rb_dv_lbl.setText(f"{dv:.3f} V"     if dv   is not None else "– V")

    @Slot(str)
    def set_status(self, status: str) -> None:
        self._state_lbl.setText(status)
        is_running = status == "RUNNING"
        self._start_btn.setEnabled(not is_running)
        self._stop_btn.setEnabled(is_running)
        self._emstop_btn.setEnabled(is_running)
        color = {
            "RUNNING":             "#e8f5e9",
            "FAULT":               "#ffebee",
            "DONE":                "#e3f2fd",
            "STOPPED":             "#fff8e1",
            "CHECKPOINT_STOPPED":  "#fff3e0",
        }.get(status, "")
        self._state_lbl.setStyleSheet(
            f"font-size: 15px; padding: 5px; background-color: {color};"
        )

    @Slot(str)
    def set_fault(self, reason: str) -> None:
        self._fault_lbl.setText(reason)
        self._fault_lbl.setStyleSheet(
            "font-size: 15px; padding: 5px; color: #b71c1c; font-weight: bold;"
        )

    @Slot(dict)
    def append_event(self, event: dict) -> None:
        self._event_log.append_event(event)

    def reset_plots(self) -> None:
        for q in (self._xs, self._u_batt, self._i_signed, self._temp,
                  self._charge_ah, self._discharge_ah):
            q.clear()
        for curve in (self._curve_ubatt, self._curve_isign, self._curve_temp,
                      self._curve_ch_ah, self._curve_dch_ah):
            curve.setData([], [])
        self._fault_lbl.setText("–")
        self._fault_lbl.setStyleSheet("font-size: 15px; padding: 5px;")
        for lbl in (self._rb1s_lbl, self._rb10s_lbl, self._rb30s_lbl):
            lbl.setText("– mΩ")
        self._rb_dv_lbl.setText("– V")
        self._udrop_lbl.setText("– V")
        self._upsu_set_lbl.setText("– V")
        self._iload_set_lbl.setText("– A")
        self._dvdt_lbl.setText("– mV/s")
        self._event_log.clear()
