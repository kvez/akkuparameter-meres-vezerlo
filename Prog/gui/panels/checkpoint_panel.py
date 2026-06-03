"""CheckpointPanel — BQ kézi checkpoint kezelő panel."""
from __future__ import annotations

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QCheckBox, QFormLayout,
)


class CheckpointPanel(QWidget):
    """Megjelenik amikor MANUAL_BQ_CHECKPOINT_REACHED event érkezik."""

    continue_requested        = Signal()
    close_requested           = Signal()
    emergency_stop_requested  = Signal()

    _CHECKLIST_ITEMS = [
        "BQ eszköz csatlakoztatva",
        "bqStudio / BQ tool elindítva",
        "UpdateStatus ellenőrizve",
        "Qmax ellenőrizve",
        "Ra table ellenőrizve / mentve",
        "gg.csv / golden image export mentve",
        "Kezelői jegyzet rögzítve",
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self._header_lbl = QLabel(
            "BQ_LEARNING_PHYSICAL — kézi BQ ellenőrzési pont elérve"
        )
        self._header_lbl.setStyleSheet(
            "background-color: #fff3e0; font-weight: bold; "
            "font-size: 14px; padding: 8px; border-radius: 4px;"
        )
        root.addWidget(self._header_lbl)

        info = QLabel(
            "A mérés biztonságos állapotban megállt — PSU OFF, Load OFF.\n"
            "Végezd el a BQ kézi ellenőrzést, pipáld ki a teendőket, "
            "majd nyomj Teszt lezárása gombot."
        )
        info.setWordWrap(True)
        info.setStyleSheet("padding: 6px;")
        root.addWidget(info)

        meta_box = QGroupBox("Checkpoint adatok")
        meta_form = QFormLayout(meta_box)
        self._step_lbl     = QLabel("–")
        self._next_idx_lbl = QLabel("–")
        self._charge_lbl   = QLabel("–")
        self._disch_lbl    = QLabel("–")
        meta_form.addRow("Lépés neve:", self._step_lbl)
        meta_form.addRow("Következő lépés indexe:", self._next_idx_lbl)
        meta_form.addRow("Töltve:", self._charge_lbl)
        meta_form.addRow("Kisütve:", self._disch_lbl)
        root.addWidget(meta_box)

        check_box = QGroupBox("BQ ellenőrzési teendők")
        check_layout = QVBoxLayout(check_box)
        self._checkboxes: list[QCheckBox] = []
        for label in self._CHECKLIST_ITEMS:
            cb = QCheckBox(label)
            self._checkboxes.append(cb)
            check_layout.addWidget(cb)
        root.addWidget(check_box)

        btn_row = QHBoxLayout()
        self._continue_btn = QPushButton("Folytatás checkpointból")
        self._continue_btn.setEnabled(False)
        self._continue_btn.setToolTip(
            "Folytatás funkció (6C) — jelenleg nem elérhető."
        )
        self._close_btn = QPushButton("Teszt lezárása")
        self._close_btn.setStyleSheet(
            "background-color: #e8f5e9; font-weight: bold;"
        )
        self._emstop_btn = QPushButton("Safe Off")
        self._emstop_btn.setStyleSheet(
            "background-color: #b71c1c; color: white; font-weight: bold;"
        )

        self._continue_btn.clicked.connect(self.continue_requested)
        self._close_btn.clicked.connect(self.close_requested)
        self._emstop_btn.clicked.connect(self.emergency_stop_requested)

        btn_row.addWidget(self._continue_btn)
        btn_row.addWidget(self._close_btn)
        btn_row.addWidget(self._emstop_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)
        root.addStretch()

    @Slot(dict)
    def show_checkpoint(self, event: dict) -> None:
        """Metaadatok frissítése + checkboxok reset. Checkpoint_reached slotban hívódik."""
        self._step_lbl.setText(str(event.get("step_name", "–")))
        self._next_idx_lbl.setText(str(event.get("next_step_index", "–")))

        charge = event.get("total_charge_ah")
        disch  = event.get("total_discharge_ah")
        self._charge_lbl.setText(f"{charge:.4f} Ah" if charge is not None else "–")
        self._disch_lbl.setText(f"{disch:.4f} Ah"  if disch  is not None else "–")

        resume_possible = event.get("resume_possible", False)
        if resume_possible:
            self._continue_btn.setEnabled(True)
            self._continue_btn.setText("Folytatás checkpointból")
            self._continue_btn.setToolTip("")
            self._header_lbl.setText(
                "BQ_LEARNING_PHYSICAL — kézi ellenőrzési pont (folytatható)"
            )
        else:
            self._continue_btn.setEnabled(False)
            self._continue_btn.setText("Folytatás — nem elérhető")
            self._continue_btn.setToolTip(
                "Ez a checkpoint terminális — a teszt itt ért véget."
            )
            self._header_lbl.setText(
                "BQ_LEARNING_PHYSICAL — kézi ellenőrzési pont (terminális)"
            )

        for cb in self._checkboxes:
            cb.setChecked(False)

    def set_continuing(self) -> None:
        """Folytatás indításakor hívódik — gombok inaktiválva, Safe Off megmarad."""
        self._continue_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._emstop_btn.setEnabled(True)
        self._header_lbl.setText(
            "BQ_LEARNING_PHYSICAL — folytatás indítása..."
        )
