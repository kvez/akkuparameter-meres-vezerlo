"""CheckpointPanel — BQ kézi checkpoint kezelő panel."""
from __future__ import annotations

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
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
            "FONTOS: Ez a checkpoint a BQ mérési folyamat tervezett kezelői megállópontja "
            "— NEM teljesítmény-kiesés utáni folytatás, és NEM tartalmaz crash-recovery adatot.\n"
            "Végezd el a BQ kézi ellenőrzést, pipáld ki a teendőket, "
            "majd nyomj Teszt lezárása gombot."
        )
        info.setWordWrap(True)
        info.setStyleSheet("padding: 6px;")
        root.addWidget(info)

        # Kör 18 (2026-09-16), hardver/instrument-control reviewer P2-2
        # (DFAM-018+DFAM-001): a fenti statikus szöveg feltétel nélkül
        # állítja, hogy "PSU OFF, Load OFF" -- ez az EGYETLEN élő-GUI
        # felület volt, ami sosem vette figyelembe a tényleges safe-off
        # eredményt (a session-vég popup és az indítási dialógus már
        # megkapta ezt a mintát R17-03/R17-17-ben, l. main_window.py). A
        # panel a hosszú, órákig/napokig tartó kézi BQ-munka TELJES ideje
        # alatt látható -- ha itt hamis a "biztonságos" állítás, az sokkal
        # tovább téveszt, mint egy tranziens popup. SZÁNDÉKOSAN KÜLÖN
        # widget, nem a statikus `info` szövegébe olvasztva (DFAM-018
        # semantic-collapse-avoidance).
        self._safe_off_warning_lbl = QLabel("")
        self._safe_off_warning_lbl.setWordWrap(True)
        self._safe_off_warning_lbl.setStyleSheet(
            "background-color: #ffebee; color: #b71c1c; font-weight: bold; "
            "padding: 6px; border-radius: 4px;"
        )
        self._safe_off_warning_lbl.setVisible(False)
        root.addWidget(self._safe_off_warning_lbl)

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
        # Teljes kódbázis review 17. kör (2026-09-05), GUI/UX/Állapot P3
        # lelet: set_continuing() letiltja a gombot, de show_checkpoint()
        # sosem engedélyezte újra — jelenleg nincs élő útvonal (a
        # bq_learning_physical() terv egyetlen MANUAL_CHECKPOINT lépése
        # mindig terminális), de a widget önmagában így hibás lenne, ha
        # egy jövőbeli terv több checkpointot tartalmazna.
        self._close_btn.setEnabled(True)
        # R1 B6, F-08: egy korábbi set_session_finalized() után az ÉLŐ
        # checkpointnál a Safe Off ismét hatásos.
        self._emstop_btn.setEnabled(True)
        self._emstop_btn.setToolTip("")

        charge = event.get("total_charge_ah")
        disch  = event.get("total_discharge_ah")
        self._charge_lbl.setText(f"{charge:.4f} Ah" if charge is not None else "–")
        self._disch_lbl.setText(f"{disch:.4f} Ah"  if disch  is not None else "–")

        # Kör 18 (2026-09-16), hardver/instrument-control reviewer P2-2:
        # l. _build_ui()::_safe_off_warning_lbl kommentje. Hiányzó kulcs
        # (régi sémájú/direkt tesztelt event dict) -- nincs bizonyíték,
        # tehát figyelmeztetünk (fail-closed), nem hallgatunk.
        safe_off_result = event.get("safe_off_result")
        uncertain_devices = [
            n for n in ("PSU", "Load")
            if not (safe_off_result or {}).get(n, False)
        ]
        if uncertain_devices:
            self._safe_off_warning_lbl.setText(
                "FIGYELEM — a biztonságos lekapcsolás NEM igazolt: "
                + ", ".join(uncertain_devices)
                + ". A kimenet ENERGIZÁLT maradhatott a kézi BQ-munka "
                "megkezdése előtt; ellenőrizd fizikailag a műszereket. "
                "(A kikapcsolás a parancseredmény vagy a safe-off utáni "
                "állapotellenőrzés alapján nem igazolt.)"
            )
            self._safe_off_warning_lbl.setVisible(True)
        else:
            self._safe_off_warning_lbl.setVisible(False)

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

    def checklist_state(self) -> dict[str, bool]:
        """Release-audit 2026-08-23 (2.17): a checklist korábban dekoratív
        volt — a bejelölés semmit nem kapuzott és sehova nem naplózódott.
        Ez a metódus teszi lehetővé, hogy a hívó (MainWindow) a "Teszt
        lezárása" gombnál auditálható módon rögzítse az állapotot."""
        return {cb.text(): cb.isChecked() for cb in self._checkboxes}

    def set_session_finalized(self) -> None:
        """R1 B6, F-08 (DFAM-001): a session lezárult (MainWindow.
        _finalize_terminal_session()), a műszer-kapcsolatok bontva -- innen
        a Safe Off hatástalan lenne, ezért tiltva. A safe-off bizonytalanság
        jelzése (_safe_off_warning_lbl) és a "Teszt lezárása" gomb
        változatlan."""
        self._emstop_btn.setEnabled(False)
        self._emstop_btn.setToolTip(
            "A session lezárult, a műszer-kapcsolatok bontva — innen safe-off "
            "nem adható ki. Ellenőrizd fizikailag a műszereket."
        )

    def set_continuing(self) -> None:
        """Folytatás indításakor hívódik — gombok inaktiválva, Safe Off megmarad."""
        self._continue_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._emstop_btn.setEnabled(True)
        self._header_lbl.setText(
            "BQ_LEARNING_PHYSICAL — folytatás indítása..."
        )
