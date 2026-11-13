"""
BqDiagnosticsPanel — WP-AK-07: BQ34Z110 belső állapot megjelenítése ugyanabban
az alkalmazásban, ahol a referencia műszerek (PSU/Load/DMM) élő mérése is
látszik (l. Prog/gui/panels/live_panel.py).

Kapu (WP-AK-07 Konszolidált terv): "BQ disconnect / stale data / malformed
reply nem válhat csendesen 0 értékké." — minden mező a Prog.src.bq_diagnostics.
BqFieldReading-ből render: érvénytelen mezőnél SOHA nem jelenik meg "0" vagy
egy hihető szám, csak "— (hiba)" piros színnel, tooltipben a teljes hibaüzenet.

Freshness/age: a panel EGÉSZÉRE (nem mezőnként) vezetjük — az utolsó SIKERES
snapshot óta eltelt idő 1 másodpercenként frissül egy státusz-címkében, és ha
túllépi a `_STALE_THRESHOLD_S`-t (2× a lekérdezési intervallum), a teljes
panel "ELAVULT ADAT" jelzést kap. Ez egy tudatos, dokumentált hatókör-döntés
— mezőnkénti staleness-jelzés (pl. ha csak a Ra Table olvasása lassabb, mint
a többi) egy jövőbeli finomítás, nem ennek a WP-nek a hatásköre.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from Prog.gui.panels.event_log_widget import EventLogWidget
from Prog.src.bq_consistency_gate import GATE_FIELDS, ProvisioningGateDecision
from Prog.src.bq_diagnostics import decode_control_status, decode_flags

_STALE_MULTIPLIER = 2.0
_STALE_CHECK_INTERVAL_MS = 1000
_INVALID_STYLE = "color: #c62828; font-weight: bold;"
_VALID_STYLE = "color: inherit; font-weight: normal;"


def _fmt_hex(value: int | None) -> str:
    return f"0x{value:04X}" if value is not None else "—"


# Feature (2026-08-23, felhasználói kérés): "checkpoint jelleggel" figyelt
# mezők — ha ezek ÉRTÉKE változik két egymást követő, ÉRVÉNYES (valid=True)
# snapshot között, egy időbélyeges esemény kerül a panel saját event-
# listájába. Csak a snapshot-szintű mezőket követjük itt (update_status/
# qmax); a control_status/flags-bitek dekódolása után külön kezelve.
#
# Kör 19 D8c (SW19-03, DFAM-026 declared_policy_without_consumer, SW-P3-2
# mintáját követve, commit 3e41a51): korábban ez a konstans deklarálva
# volt, de _check_field_change() két hívása (lent) kézzel, duplikáltan
# ismételte ugyanezt a (kulcs, címke) párt formátumfüggvénnyel kiegészítve
# -- SEMMI nem olvasta ténylegesen ezt a tuple-t. Repo-wide grep szerint
# EDDIG szinkronban volt (élő hiba nem volt), de ez maga a DFAM-026
# definíciója: egy deklarált szabály fogyasztó nélkül csendben
# szétdrifthetett volna. A formátumfüggvényt (harmadik tagként) is
# felvettük, hogy a hurok alább TELJESEN ebből a konstansból induljon ki,
# ne egy párhuzamos, kézzel karbantartott hívássorozatból.
_TRACKED_SNAPSHOT_FIELDS: tuple[tuple[str, str, Callable[..., str]], ...] = (
    ("update_status", "UpdateStatus", _fmt_hex),
    ("qmax", "Qmax", str),
)


# Review-kör 2 (2026-09-09), friss GUI/UX F6 (P3, DFAM-018): a mezők,
# amelyek legalább EGYIKÉNEK valid=True-nak kell lennie ahhoz, hogy egy
# update_snapshot()-hívás "sikeres frissítésnek" számítson — l.
# _snapshot_has_any_valid_field() és update_snapshot() docstringje.
_FRESHNESS_FIELDS = (
    "device_type", "fw_version", "hw_version", "chem_id",
    "voltage_mV", "avg_current_mA", "temperature_C", "soc_percent",
    "remaining_capacity_mAh", "full_charge_capacity_mAh", "qmax",
    "update_status", "flags", "control_status", "ra_table_mohm",
)


def _snapshot_has_any_valid_field(snapshot) -> bool:
    return any(
        getattr(getattr(snapshot, name, None), "valid", False)
        for name in _FRESHNESS_FIELDS
    )


class BqDiagnosticsPanel(QWidget):
    """Kapcsolódás-kérő jelzések a MainWindow felé (a tényleges soros port +
    PicoBQTransport + worker-thread életciklus a MainWindow felelőssége,
    ugyanúgy, mint az InstrumentManager-nél — a panel csak UI, nem I/O)."""

    connect_requested     = Signal(str)  # port name
    disconnect_requested  = Signal()
    # WP-AK-09: kalibrációs dialógus megnyitása - a panel MAGA nem tudja
    # csatlakoztatni (saját, önálló soros kapcsolatot igényel, l.
    # bq_calibration_dialog.py modul docstring "kölcsönös kizárás" szakasza),
    # csak a port nevet adja tovább a MainWindow-nak.
    calibration_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._poll_interval_ms = 2000
        self._last_success_perf: float | None = None
        # Kör 20 F20-G5 (DFAM-018): a live_panel.py::set_step() mintáját
        # követő MÁSODIK "élő" seed-forrás -- a kapcsolódás/session-mód
        # BELÉPÉSI pillanata, függetlenül attól, hogy a chip adott-e már
        # valaha vissza érvényes mezőt. l. _refresh_freshness_label().
        self._connected_since_perf: float | None = None
        # Kör 20 F20-G6 (DFAM-018+DFAM-040): a set_connection_error() után
        # az update_snapshot() sikeres ágának ehhez kell visszaállnia --
        # l. _refresh_freshness_label() alatti kommentet is.
        self._connected_port: str = ""
        # R17-23 (Kör 17 MAX-effort review, DFAM-001+DFAM-018): l.
        # _refresh_freshness_label() -- a poll-timeout ág korábban csak
        # a fejléc-cimkét jelolte "ELAVULT ADAT"-nak, a 19+15 ertekcimke
        # az UTOLSO JO ertéken maradt, jelöletlenül. Ez a flag a
        # _mark_all_stale() ismételt (percenkénti/óránkénti, óra-hosszú
        # Pico-kiesésnél tickenkénti) újrahívását kerüli el -- NEM a
        # helyesség feltétele, csak teljesítmény-optimalizálás.
        self._all_stale_marked: bool = False
        self._value_labels: dict[str, QLabel] = {}
        # Feature (2026-08-23): checkpoint-jellegű változás-detektálás —
        # az utolsó ÉRVÉNYES érték mezőnként, hogy csak a tényleges
        # változásra keletkezzen esemény, ne minden snapshotra.
        self._last_field_values: dict[str, object] = {}
        self._session_owned: bool = False
        # Teljes kódbázis review 23. kör (2026-09-07), GUI/UX/Állapot P3
        # lelet: a 22. kör (deddebf) csak a panel SAJÁT (bq_worker-alapú)
        # kapcsolat-állapotát kötötte be a "Gauge kalibráció…" gomb
        # enabled-állapotába — a main_window.py::_on_bq_calibration_
        # requested()-ben ellenőrzött 3 MARADÉK kölcsönös kizárási feltétel
        # (fizikai teszt fut / BQ-kapcsolódás folyamatban / bármely
        # hardveres bring-up folyamatban) továbbra sem tükröződött a
        # widget-állapotban, csak a kattintás-idejű dialógus védte. Ez a
        # mező a MainWindow-ból kapott, additív blokkoló-jelző — l.
        # set_calibration_blocked_by_other_activity()/_refresh_calibration_
        # button_enabled().
        self._calibration_blocked_by_other_activity: bool = False
        self._own_bq_connection_blocks_calibration: bool = False
        self._build_ui()

        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(_STALE_CHECK_INTERVAL_MS)
        self._stale_timer.timeout.connect(self._refresh_freshness_label)
        self._stale_timer.start()

    # ------------------------------------------------------------------ #
    # UI építés                                                            #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Pico soros port:"))
        self._port_edit = QLineEdit()
        self._port_edit.setPlaceholderText("pl. COM5")
        self._port_edit.setFixedWidth(120)
        conn_row.addWidget(self._port_edit)
        self._connect_btn = QPushButton("Kapcsolódás")
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        conn_row.addWidget(self._connect_btn)
        self._disconnect_btn = QPushButton("Bontás")
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self.disconnect_requested)
        conn_row.addWidget(self._disconnect_btn)
        self._calibration_btn = QPushButton("Gauge kalibráció…")
        self._calibration_btn.setToolTip(
            "BQ_GAUGE_CALIBRATION_ASSISTED (WP-AK-09) - önálló soros "
            "kapcsolatot nyit, ezért a BQ Diagnosztika kapcsolatnak "
            "bontva kell lennie."
        )
        self._calibration_btn.clicked.connect(self._on_calibration_clicked)
        conn_row.addWidget(self._calibration_btn)
        self._status_lbl = QLabel("Nincs kapcsolat")
        conn_row.addWidget(self._status_lbl)
        conn_row.addStretch()
        self._freshness_lbl = QLabel("")
        conn_row.addWidget(self._freshness_lbl)
        root.addLayout(conn_row)

        device_box = QGroupBox("BQ device / ChemID")
        device_grid = QGridLayout(device_box)
        for col, (key, title) in enumerate([
            ("device_type", "Device Type"), ("fw_version", "FW"),
            ("hw_version", "HW"), ("chem_id", "ChemID"),
        ]):
            device_grid.addWidget(QLabel(f"<b>{title}</b>"), 0, col)
            device_grid.addWidget(self._value_label(key), 1, col)
        root.addWidget(device_box)

        security_box = QGroupBox("Security (CONTROL_STATUS)")
        security_grid = QGridLayout(security_box)
        for col, (key, title) in enumerate([
            ("ss", "SS (SEALED)"), ("fas", "FAS"), ("calmode", "CALMODE"),
            ("cca", "CCA"), ("bca", "BCA"), ("csv", "CSV"),
        ]):
            security_grid.addWidget(QLabel(f"<b>{title}</b>"), 0, col)
            security_grid.addWidget(self._value_label(key), 1, col)
        root.addWidget(security_box)

        telem_box = QGroupBox("Telemetria")
        telem_grid = QGridLayout(telem_box)
        for col, (key, title) in enumerate([
            ("voltage_mV", "Voltage (mV)"), ("avg_current_mA", "Current (mA)"),
            ("temperature_C", "Temp (°C)"), ("soc_percent", "SOC (%)"),
            ("remaining_capacity_mAh", "RM (mAh)"), ("full_charge_capacity_mAh", "FCC (mAh)"),
        ]):
            telem_grid.addWidget(QLabel(f"<b>{title}</b>"), 0, col)
            telem_grid.addWidget(self._value_label(key), 1, col)
        root.addWidget(telem_box)

        learn_box = QGroupBox("Learning / Impedance Track állapot")
        learn_grid = QGridLayout(learn_box)
        for col, (key, title) in enumerate([
            ("qmax", "Qmax"), ("update_status", "UpdateStatus"),
            ("qen", "QEN"), ("vok", "VOK"),
            ("ocvtaken", "OCVTAKEN"), ("rup_dis", "RUP_DIS"),
        ]):
            learn_grid.addWidget(QLabel(f"<b>{title}</b>"), 0, col)
            learn_grid.addWidget(self._value_label(key), 1, col)
        root.addWidget(learn_box)

        # Block 6 (2026-08-25) — a Block 2 PROFILE<->SESSION<->BQ Data Flash
        # konzisztencia-gate eredménye (LEARNING_CONFIGURATION_MISMATCH
        # esetén ez mutatja, MELYIK mező tért el). Csak TRADITIONAL_LEARNING
        # session PRECOND lépésekor kap adatot (l. set_provisioning_
        # consistency()) — más módban/session előtt "—" placeholder marad,
        # ugyanaz a passzív minta, mint a panel többi szekciója.
        provisioning_box = QGroupBox("Provisioning konzisztencia (Block 2)")
        provisioning_layout = QVBoxLayout(provisioning_box)
        self._provisioning_status_lbl = QLabel("— (nincs adat)")
        provisioning_layout.addWidget(self._provisioning_status_lbl)
        self._provisioning_grid = QGridLayout()
        headers = ("Mező", "Profil-elvárt", "BQ-readback", "Tolerancia", "Státusz")
        for col, title in enumerate(headers):
            self._provisioning_grid.addWidget(QLabel(f"<b>{title}</b>"), 0, col)
        self._provisioning_rows: dict[str, tuple[QLabel, QLabel, QLabel, QLabel]] = {}
        for row, field_name in enumerate(GATE_FIELDS, start=1):
            self._provisioning_grid.addWidget(QLabel(field_name), row, 0)
            expected_lbl, readback_lbl, tolerance_lbl, status_lbl = (
                QLabel("—"), QLabel("—"), QLabel("—"), QLabel("—"),
            )
            self._provisioning_grid.addWidget(expected_lbl, row, 1)
            self._provisioning_grid.addWidget(readback_lbl, row, 2)
            self._provisioning_grid.addWidget(tolerance_lbl, row, 3)
            self._provisioning_grid.addWidget(status_lbl, row, 4)
            self._provisioning_rows[field_name] = (
                expected_lbl, readback_lbl, tolerance_lbl, status_lbl,
            )
        provisioning_layout.addLayout(self._provisioning_grid)
        root.addWidget(provisioning_box)

        ra_box = QGroupBox("Ra Table (Cell0, mΩ)")
        self._ra_grid = QGridLayout(ra_box)
        self._ra_labels: list[QLabel] = []
        for i in range(15):
            lbl = QLabel("—")
            lbl.setAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
            self._ra_grid.addWidget(QLabel(f"R_a{i}"), 0, i)
            self._ra_grid.addWidget(lbl, 1, i)
            self._ra_labels.append(lbl)
        root.addWidget(ra_box)

        # Feature (2026-08-23, felhasználói kérés): "checkpoint jelleggel"
        # figyelt esemény-lista — UpdateStatus/Qmax/QEN/VOK/OCVTAKEN/RUP_DIS
        # változásakor, időbélyeggel. A meglévő EventLogWidget-et
        # újrahasznosítva (élő teszt-session eseménynaplójával megegyező
        # megjelenés/severity-színezés).
        self._session_event_log = EventLogWidget()
        root.addWidget(self._session_event_log)

        root.addStretch()

    def _value_label(self, key: str) -> QLabel:
        lbl = QLabel("—")
        self._value_labels[key] = lbl
        return lbl

    # ------------------------------------------------------------------ #
    # Kapcsolódás UI                                                       #
    # ------------------------------------------------------------------ #

    def _on_connect_clicked(self) -> None:
        port = self._port_edit.text().strip()
        if port:
            self.connect_requested.emit(port)

    def _on_calibration_clicked(self) -> None:
        port = self._port_edit.text().strip()
        if port:
            self.calibration_requested.emit(port)

    @Slot(str)
    def set_connected(self, port: str) -> None:
        # R1 F-29 (DFAM-018+DFAM-001): az összehasonlítási alap
        # (_last_field_values) egy valódi kapcsolat-KEZDÉSKOR (üres ->
        # kapcsolt átmenet) ürül -- így egy session-vég utáni önálló
        # kapcsolat első olvasása sem a session utolsó értékeihez
        # hasonlít. Az update_snapshot() sikeres ágából érkező ISMÉTELT
        # hívás (F20-G6, ugyanaz az élő kapcsolat) az alapot NEM üríti,
        # különben a kapcsolat minden valódi változás-eseménye elveszne.
        if not self._connected_port:
            self._last_field_values.clear()
        self._connected_port = port
        # Kör 20 F20-G5: a KAPCSOLÓDÁS pillanata a "még nem érkezett adat"
        # ág seed-forrása (l. _refresh_freshness_label()) -- update_snapshot()
        # sikeres ága (F20-G6) is ide hívja vissza magát, ott ez a sor
        # ártalmatlanul újra beállítja UGYANAZT az élő kapcsolatot, mert
        # _last_success_perf akkor már nem None (l. ott a kommentet).
        self._connected_since_perf = time.monotonic()
        self._status_lbl.setText(f"Kapcsolódva: {port}")
        self._status_lbl.setStyleSheet("color: #2e7d32; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._port_edit.setEnabled(False)
        # Teljes kódbázis review 22. kör (2026-09-07), GUI/UX/Állapot P3
        # lelet: a "Gauge kalibráció…" gomb saját tooltipje explicit
        # előírja, hogy a BQ Diagnosztika kapcsolatnak bontva kell lennie
        # — ezt korábban KIZÁRÓLAG a main_window.py::_on_bq_calibration_
        # requested() kattintás-idejű QMessageBox.warning()-ja kényszerí-
        # tette ki, a testvér-gombokkal (_connect_btn/_disconnect_btn/
        # _port_edit) ellentétben a widget-állapot nem tükrözte.
        self._own_bq_connection_blocks_calibration = True
        self._refresh_calibration_button_enabled()

    def _reset_connection_ui(self) -> None:
        """A kapcsolat-UI (állapotcímke/gombok/frissesség) visszaállítása
        "Nincs kapcsolat"-ra — a bizonyíték-felület (event-log/
        provisioning-táblázat) NÉLKÜL. l. `set_disconnected()` (önálló
        Bontás, MINDKETTŐT végzi) és `set_session_owned(False)` (session-
        vég, review-kör 5 GUI G5-01 P2 óta CSAK ezt a helpert hívja) —
        utóbbi NEM törölheti a bizonyítékot, mert az operátor jellemzően
        PONT a session vége UTÁN akarja átnézni a lezárult session
        eredményét (event-log, provisioning-gate kimenet)."""
        self._status_lbl.setText("Nincs kapcsolat")
        self._status_lbl.setStyleSheet("")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._port_edit.setEnabled(True)
        # L. set_connected() kommentje ugyanerről a hiányzó mintakövetésről.
        self._own_bq_connection_blocks_calibration = False
        self._refresh_calibration_button_enabled()
        self._last_success_perf = None
        # Kör 20 F20-G5: a "még nem érkezett adat" seed-forrás is a
        # kapcsolat-UI-hoz tartozik -- kilépéskor nullázandó, hogy a
        # KÖVETKEZŐ kapcsolódás saját, friss "connected_since"-t kapjon,
        # ne az előző (lezárt) kapcsolat pillanatát.
        self._connected_since_perf = None
        self._connected_port = ""
        self._freshness_lbl.setText("")
        self._mark_all_stale()

    @Slot()
    def set_disconnected(self) -> None:
        self._reset_connection_ui()
        # Teljes kódbázis review 7. kör (2026-08-30), GUI/UX/Állapot P3
        # lelet: set_session_owned(active=True, ...) explicit törli
        # _last_field_values-t új session indulásakor (lentebb), de az
        # önálló (nem session-vezérelt) Kapcsolódás/Bontás gombos
        # újracsatlakozási úton ez korábban SOSEM történt meg — ha az
        # operátor egy önálló BQ Diagnosztika kapcsolatot bont, majd
        # (akár egy másik eszközzel) újra csatlakozik, a _check_field_
        # change()/_check_bit_change() az ELSŐ új snapshotot a korábbi
        # kapcsolat utolsó értékéhez hasonlította — két, időben/fizikailag
        # össze nem függő kapcsolat adatait "változásként" összemosva a
        # panel saját esemény-naplójában.
        self._last_field_values.clear()
        # Teljes kódbázis review 16. kör (2026-09-04), GUI/UX/Állapot P3
        # lelet: a fenti _last_field_values.clear() csak a hamis
        # "változás"-detektálást előzte meg — a TÉNYLEGESEN MEGJELENÍTETT
        # _session_event_log (UpdateStatus/Qmax/QEN/VOK/OCVTAKEN/RUP_DIS
        # napló) egyik helyen sem törlődött, így egy korábbi (más fizikai
        # kapcsolathoz/session-hez tartozó) bejegyzés a JELENLEGI session
        # bizonyítékaként tűnhetett fel, vizuális határ nélkül a két,
        # időben/fizikailag össze nem függő kapcsolat között.
        self._session_event_log.clear()
        # Teljes kódbázis review 20. kör (2026-09-06), GUI/UX/Állapot P2
        # lelet: a Provisioning konzisztencia (Block 2) táblázat csak a
        # set_session_owned(active=True, ...) ágán (lent) törlődött, itt
        # (önálló Bontás úton) SOHA — egy lezárult TRADITIONAL_LEARNING
        # session gate-eredménye VÉGLEGESEN megmaradt, akár egy önálló,
        # más fizikai BQ-eszközhöz való kapcsolódás alatt is.
        #
        # Review-kör 5, GUI G5-01 (P2): ez a hívás (és a fenti két
        # `.clear()`) mostantól KIZÁRÓLAG az önálló Bontás útján fut —
        # a `set_session_owned(active=False)` (session-vég) a fenti
        # `_reset_connection_ui()`-t hívja, ENNÉLKÜL, hogy a lezárult
        # session bizonyítéka (event-log, provisioning-táblázat) a
        # kezelő számára látható maradjon a session vége UTÁN is.
        self.set_provisioning_consistency(None)

    @Slot(str)
    def set_connection_error(self, message: str) -> None:
        # R1 F-29 (D-B9-2): kizárólag az önálló BqDiagnosticsWorker
        # callbackje -- bontáskor a GUI-szál a _cleanup_bq_thread() wait()-
        # jében blokkol, a sorban ragadt hibajel a set_disconnected() UTÁN
        # érkezhet, és a "Nincs kapcsolat" állapotot hamis "Kapcsolódási
        # hiba"-ra írná. Élő önálló kapcsolat nélkül (a session-tulajdonlás
        # ezt NEM pótolja) a jel eldobandó.
        if not self._connected_port:
            return
        self._status_lbl.setText(f"Kapcsolódási hiba: {message}")
        self._status_lbl.setStyleSheet(_INVALID_STYLE)
        self._mark_all_stale()

    def set_session_owned(self, active: bool, session_label: str = "") -> None:
        """Feature (2026-08-23, felhasználói kérés): amikor egy
        TRADITIONAL_LEARNING/GOLDEN_GG session birtokolja a Pico soros
        portját, a Diagnosztika fül nem tud (és nem is szabad, hogy tudjon)
        önálló kapcsolatot nyitni — de az adatok élőben megjeleníthetők a
        session saját, periodikus gauge-lekérdezéséből (l. update_snapshot()
        hívó: main_window.py gauge_snapshot_ready signal). Ebben a módban
        nincs Kapcsolódás/Bontás/Kalibráció gomb, csak passzív megjelenítés."""
        self._session_owned = active
        self._connect_btn.setEnabled(not active)
        self._disconnect_btn.setEnabled(False)
        self._refresh_calibration_button_enabled()
        self._port_edit.setEnabled(not active)
        if active:
            label = session_label or "aktív session"
            self._status_lbl.setText(f"Session vezérli ({label}) — csak olvasás")
            self._status_lbl.setStyleSheet("color: #1565c0; font-weight: bold;")
            # Kör 20 F20-G5 sibling: a session-vezérelt mód is egy "belépési
            # pillanat", ugyanúgy, mint az önálló set_connected() -- ha a
            # chip session-módban SOHA nem ad vissza érvényes mezőt, a
            # _refresh_freshness_label() ugyanúgy örökre "üres" maradna
            # nélküle.
            self._connected_since_perf = time.monotonic()
            self._last_success_perf = None
            self._last_field_values.clear()
            # Teljes kódbázis review 16. kör (2026-09-04), GUI/UX/Állapot P3
            # lelet — l. set_disconnected() azonos komment: az önálló
            # kapcsolat-váltás mellett a session-vezérelt mód belépésekor is
            # törölni kell a megjelenített eseménynaplót, ne csak a
            # változás-detektáló dict-et.
            self._session_event_log.clear()
            # Block 6 (2026-08-25): egy ÚJ session induljon a placeholder
            # állapotból — az előző session gate-eredménye ne maradjon
            # látszólag még érvényesnek egy új session PRECOND-ja előtt.
            self.set_provisioning_consistency(None)
        else:
            # Review-kör 5, GUI G5-01 (P2): korábban self.set_disconnected()
            # futott itt, ami a bizonyíték-felületet (event-log,
            # provisioning-táblázat) IS törölte a session VÉGÉN — pedig az
            # operátor jellemzően PONT ekkor akarja átnézni a lezárult
            # session eredményét. Csak a kapcsolat-UI-t állítjuk vissza; az
            # evidence a KÖVETKEZŐ session indulásakor (l. fent az
            # active=True ág) ill. egy tényleges önálló BONTÁSKOR (set_
            # disconnected()) törlődik -- egy új önálló kapcsolat INDULÁSA
            # (set_connected()) csak az összehasonlítási alapot üríti, az
            # evidence-et nem (R1 F-29, D-B9-3).
            self._reset_connection_ui()

    def _refresh_calibration_button_enabled(self) -> None:
        """A "Gauge kalibráció…" gomb enabled-állapota 3 FÜGGETLEN forrásból
        számolt, ÉS-kapcsolt feltétel — l. set_calibration_blocked_by_
        other_activity() docstringje a teljes indoklásért."""
        self._calibration_btn.setEnabled(
            not self._session_owned
            and not self._own_bq_connection_blocks_calibration
            and not self._calibration_blocked_by_other_activity
        )

    def set_calibration_blocked_by_other_activity(self, blocked: bool) -> None:
        """Teljes kódbázis review 23. kör (2026-09-07), GUI/UX/Állapot P3
        lelet: a main_window.py::_on_bq_calibration_requested() 3 MARADÉK
        kölcsönös kizárási feltétele (fizikai teszt fut / BQ-kapcsolódás
        folyamatban / bármely hardveres bring-up folyamatban — l. ott a
        kommenteket) korábban KIZÁRÓLAG a kattintás-idejű QMessageBox.
        warning()-okban élt, a widget-állapotban nem tükröződött. A
        MainWindow ezt a metódust hívja minden releváns állapotváltozásnál
        (worker/bq_connect_worker/connect_worker start/stop) egy már
        kiszámolt, összesített bool-lal."""
        self._calibration_blocked_by_other_activity = blocked
        self._refresh_calibration_button_enabled()

    # ------------------------------------------------------------------ #
    # Provisioning konzisztencia — Block 6 (2026-08-25)                    #
    # ------------------------------------------------------------------ #

    @Slot(object)
    def set_provisioning_consistency(self, report) -> None:
        """A Block 2 gate egyszeri (PRECOND-nál kapott) eredménye — NEM
        periodikus, mint update_snapshot(). `report=None`-nal is hívható
        (session-váltásnál, l. set_session_owned()), ekkor a placeholder
        "—" állapotba áll vissza."""
        if report is None:
            self._provisioning_status_lbl.setText("— (nincs adat)")
            self._provisioning_status_lbl.setStyleSheet("")
            for expected_lbl, readback_lbl, tolerance_lbl, status_lbl in self._provisioning_rows.values():
                for lbl in (expected_lbl, readback_lbl, tolerance_lbl, status_lbl):
                    lbl.setText("—")
                    lbl.setStyleSheet("")
            return

        consistent = report.decision is ProvisioningGateDecision.CONSISTENT
        self._provisioning_status_lbl.setText(f"{report.decision.value}: {report.reason}")
        self._provisioning_status_lbl.setStyleSheet(_VALID_STYLE if consistent else _INVALID_STYLE)

        comparisons_by_field = {c.field_name: c for c in report.comparisons}
        for field_name, (expected_lbl, readback_lbl, tolerance_lbl, status_lbl) in self._provisioning_rows.items():
            comparison = comparisons_by_field.get(field_name)
            if comparison is None:
                for lbl in (expected_lbl, readback_lbl, tolerance_lbl, status_lbl):
                    lbl.setText("—")
                    lbl.setStyleSheet("")
                continue
            unit = f" {comparison.unit}" if comparison.unit else ""
            expected_lbl.setText(f"{comparison.profile_expected}{unit}")
            readback_lbl.setText(
                "—" if comparison.bq_readback is None else f"{comparison.bq_readback}{unit}"
            )
            tolerance_lbl.setText(f"±{comparison.tolerance}{unit}")
            status_lbl.setText("OK" if comparison.within_tolerance else "ELTÉR")
            style = _VALID_STYLE if comparison.within_tolerance else _INVALID_STYLE
            for lbl in (expected_lbl, readback_lbl, tolerance_lbl, status_lbl):
                lbl.setStyleSheet(style)

    # ------------------------------------------------------------------ #
    # Snapshot render                                                      #
    # ------------------------------------------------------------------ #

    @Slot(object)
    def update_snapshot(self, snapshot) -> None:
        """Review-kör 2 (2026-09-09), friss GUI/UX F6 (P3, DFAM-018): a
        poll_bq_diagnostics() minden mezőt külön try/except-be csomagol,
        ezért egy fizikai Pico-lecsatlakozásnál NEM dob kivételt — egy
        teljesen érvénytelen (minden valid=False) snapshotot ad vissza.
        Korábban ez feltétel nélkül "sikeres frissítésnek" számított
        (self._last_success_perf mindig frissült), ezért a
        _refresh_freshness_label() "ELAVULT ADAT" állapota SOHA nem állt
        be egy valódi kiesésnél -- a liveness-jelző örökké "friss"-et
        mutatott, épp abban az esetben, amire tervezték. Mostantól csak
        akkor számít sikeres frissítésnek, ha legalább egy mező ténylegesen
        érvényes adatot hozott.

        R1 F-29 (DFAM-018+DFAM-001): bontáskor/session-végkor a GUI-szál a
        szál-takarítás wait()-jében blokkol, ezért egy már kibocsátott
        `snapshot_ready`/`gauge_snapshot_ready` a set_disconnected()/
        set_session_owned(False) UTÁN is kézbesülhet. Ilyenkor (sem élő
        önálló kapcsolat, sem session-tulajdonlás) a snapshot NEM renderel,
        nem frissíti a "sikeres frissítés" időbélyeget és nem mozgatja az
        összehasonlítási alapot / eseménynaplót. A session végső snapshotja
        a runner `finished` jele ELŐTT, ugyanarról a szálról érkezik (queued
        FIFO), így még a session-tulajdonlás alatt kézbesül."""
        if not self._session_owned and not self._connected_port:
            return
        if _snapshot_has_any_valid_field(snapshot):
            self._last_success_perf = time.monotonic()
            # Kör 20 F20-G6 (DFAM-018+DFAM-040): set_connection_error() a
            # _status_lbl-t piros "Kapcsolódási hiba"-ra állítja, de korábban
            # SEMMI nem állította vissza egy KÉSŐBBI, sikeres frissítésen --
            # a testvér _all_stale_marked/_refresh_freshness_label() már
            # feltétel nélkül resetel a siker-ágon (l. ott a kommentet),
            # ugyanez a minta itt. Session-vezérelt módban (l.
            # set_session_owned()) NEM hívható: felülírná a "Session vezérli
            # (…) — csak olvasás" állapotot és a letiltott gombokat.
            #
            # Kör 21 F21-G1 (DFAM-032, a fenti F20-G6 fix saját regressziója):
            # a `not self._session_owned` guard ÖNMAGÁBAN hamis-negatív egy
            # KÉSVE kézbesített snapshot esetén (Bontás/session-vég UTÁN a Qt
            # event-queue-ban ragadt `snapshot_ready`/`gauge_snapshot_ready`) --
            # `_reset_connection_ui()` ekkor MÁR törölte `_connected_port`-ot
            # ("" -re) ÉS `set_session_owned(False)` MÁR törölte
            # `_session_owned`-et is, tehát a guard átengedi a hívást egy
            # `set_connected("")`-ra: a panel HAMISAN "Kapcsolódva: " (üres
            # portnév) állapotba kerül, letiltva Connect/port/kalibráció
            # gombokat, amikor NINCS kapcsolat. Az ÚJ `and self._connected_port`
            # feltétel ezt zárja ki, DE a fenti F20-G6 forgatókönyvet (sikeres
            # frissítés EGY VALÓS, aktív kapcsolat mellett, ami visszaállítja a
            # korábbi set_connection_error()-t) VÁLTOZATLANUL átengedi, mert
            # ott `_connected_port` sosem üres.
            if not self._session_owned and self._connected_port:
                self.set_connected(self._connected_port)

        self._render(self._value_labels["device_type"], snapshot.device_type, _fmt_hex)
        self._render(self._value_labels["fw_version"], snapshot.fw_version, _fmt_hex)
        self._render(self._value_labels["hw_version"], snapshot.hw_version, _fmt_hex)
        self._render(self._value_labels["chem_id"], snapshot.chem_id, _fmt_hex)

        self._render(self._value_labels["voltage_mV"], snapshot.voltage_mV, str)
        self._render(self._value_labels["avg_current_mA"], snapshot.avg_current_mA, str)
        self._render(
            self._value_labels["temperature_C"], snapshot.temperature_C,
            lambda v: f"{v:.1f}",
        )
        self._render(self._value_labels["soc_percent"], snapshot.soc_percent, str)
        self._render(
            self._value_labels["remaining_capacity_mAh"],
            snapshot.remaining_capacity_mAh, str,
        )
        self._render(
            self._value_labels["full_charge_capacity_mAh"],
            snapshot.full_charge_capacity_mAh, str,
        )

        self._render(self._value_labels["qmax"], snapshot.qmax, str)
        self._render(self._value_labels["update_status"], snapshot.update_status, _fmt_hex)

        self._render_flags_and_control_status(snapshot)
        self._render_ra_table(snapshot.ra_table_mohm)

        for key, label, formatter in _TRACKED_SNAPSHOT_FIELDS:
            self._check_field_change(key, label, getattr(snapshot, key), formatter)

        self._refresh_freshness_label()

    def _check_field_change(self, key: str, label: str, reading, formatter) -> None:
        """Feature (2026-08-23): esemény a listába, ha egy figyelt mező
        ÉRVÉNYES értéke MEGVÁLTOZOTT az előző érvényes olvasáshoz képest.
        Az első érvényes olvasás sosem esemény (nincs mihez képest
        "változni") — csak a kiindulási érték eltárolódik."""
        if not reading.valid or reading.value is None:
            return
        previous = self._last_field_values.get(key)
        current = reading.value
        self._last_field_values[key] = current
        if previous is None or previous == current:
            return
        self._session_event_log.append_event({
            "event_code": f"BQ_{key.upper()}_CHANGED",
            "event_message": f"{label}: {formatter(previous)} → {formatter(current)}",
            "severity": "CHECKPOINT",
        })

    def _render_flags_and_control_status(self, snapshot) -> None:
        if snapshot.flags.valid:
            bits = decode_flags(snapshot.flags.value)
            self._value_labels["ocvtaken"].setText("IGEN" if bits.ocvtaken else "nem")
            self._value_labels["ocvtaken"].setStyleSheet(_VALID_STYLE)
            # R10-B2: sikeres bitolvasasnal a korabbi hiba-tooltip torlodik.
            self._value_labels["ocvtaken"].setToolTip("")
            self._check_bit_change("ocvtaken", "OCVTAKEN", bits.ocvtaken)
        else:
            self._value_labels["ocvtaken"].setText("—")
            self._value_labels["ocvtaken"].setStyleSheet(_INVALID_STYLE)
            self._value_labels["ocvtaken"].setToolTip(snapshot.flags.error or "")

        if snapshot.control_status.valid:
            cs = decode_control_status(snapshot.control_status.value)
            for key, bit in (
                ("qen", cs.qen), ("vok", cs.vok), ("rup_dis", cs.rup_dis),
                ("ss", cs.ss), ("fas", cs.fas), ("calmode", cs.calmode),
                ("cca", cs.cca), ("bca", cs.bca), ("csv", cs.csv),
            ):
                self._value_labels[key].setText("IGEN" if bit else "nem")
                self._value_labels[key].setStyleSheet(_VALID_STYLE)
                self._value_labels[key].setToolTip("")
            for key, label in (("qen", "QEN"), ("vok", "VOK"), ("rup_dis", "RUP_DIS")):
                self._check_bit_change(key, label, getattr(cs, key))
        else:
            for key in ("qen", "vok", "rup_dis", "ss", "fas", "calmode", "cca", "bca", "csv"):
                self._value_labels[key].setText("—")
                self._value_labels[key].setStyleSheet(_INVALID_STYLE)
                self._value_labels[key].setToolTip(snapshot.control_status.error or "")

    def _check_bit_change(self, key: str, label: str, value: bool) -> None:
        """Feature (2026-08-23): l. _check_field_change() docstring — ugyanaz
        a minta, bool bitekre (QEN/VOK/OCVTAKEN/RUP_DIS)."""
        previous = self._last_field_values.get(key)
        self._last_field_values[key] = value
        if previous is None or previous == value:
            return
        self._session_event_log.append_event({
            "event_code": f"BQ_{key.upper()}_CHANGED",
            "event_message": f"{label}: {'IGEN' if previous else 'nem'} → {'IGEN' if value else 'nem'}",
            "severity": "CHECKPOINT",
        })

    def _render_ra_table(self, reading) -> None:
        if reading.valid and reading.value:
            values = reading.value
            for i, lbl in enumerate(self._ra_labels):
                # R10-B2: a rovid lista hianyzo elemein se marad regi hiba-tooltip.
                lbl.setToolTip("")
                if i < len(values):
                    lbl.setText(f"{values[i]:.1f}")
                    lbl.setStyleSheet(_VALID_STYLE)
                else:
                    lbl.setText("—")
                    lbl.setStyleSheet(_INVALID_STYLE)
        else:
            for lbl in self._ra_labels:
                lbl.setText("—")
                lbl.setStyleSheet(_INVALID_STYLE)
                lbl.setToolTip(reading.error or "")

    @staticmethod
    def _render(label: QLabel, reading, formatter) -> None:
        if reading.valid and reading.value is not None:
            label.setText(formatter(reading.value))
            label.setStyleSheet(_VALID_STYLE)
            label.setToolTip("")
        else:
            label.setText("—")
            label.setStyleSheet(_INVALID_STYLE)
            label.setToolTip(reading.error or "hiba")

    def _mark_all_stale(self) -> None:
        # Teljes kódbázis review 17. kör (2026-09-05), GUI/UX/Állapot P1
        # lelet: korábban csak a stílust váltotta pirosra, a szöveget nem
        # törölte — szétkapcsolás/kapcsolódási hiba után az UTOLSÓ ÉRVÉNYES
        # eszközértékek maradtak látszólag "aktívan" kijelezve (piros
        # szöveggel), ami valós, de már nem élő adatot sugallt.
        # Teljes kódbázis review 18. kör (2026-09-05), GUI/UX/Állapot P3
        # lelet: a tooltip (_render()/_render_ra_table() egy korábbi
        # ÉRVÉNYTELEN olvasásnál konkrét, mezőspecifikus hibaüzenetet
        # állít be) szintén törlésre szorul — különben a "—" felirat
        # felett egy RÉGI, már irreleváns hibaüzenet marad látható.
        for lbl in self._value_labels.values():
            lbl.setText("—")
            lbl.setStyleSheet(_INVALID_STYLE)
            lbl.setToolTip("")
        for lbl in self._ra_labels:
            lbl.setText("—")
            lbl.setStyleSheet(_INVALID_STYLE)
            lbl.setToolTip("")

    def _refresh_freshness_label(self) -> None:
        if self._last_success_perf is None:
            # Kör 20 F20-G5 (DFAM-018): korábban ez egy csendes return volt
            # akkor is, ha a chip a kapcsolódás/session-belépés ÓTA SOHA nem
            # adott vissza egyetlen érvényes mezőt sem -- a frissesség-cimke
            # örökre üres maradt, miközben a _status_lbl zölden "Kapcsolódva"
            # / kék "Session vezérli" állapotot mutatott, minden figyelmeztetés
            # nélkül. Szándékosan MÁS szövegű, mint "ELAVULT ADAT" -- az utóbbi
            # egy KORÁBBAN meglévő adat elévülését jelenti, itt még sosem
            # volt adat.
            if self._connected_since_perf is not None:
                self._freshness_lbl.setStyleSheet(_INVALID_STYLE)
                self._freshness_lbl.setText("MÉG NEM ÉRKEZETT ÉRVÉNYES BQ-ADAT")
            return
        age_s = time.monotonic() - self._last_success_perf
        self._freshness_lbl.setText(f"utolsó frissítés: {age_s:.0f} mp")
        stale_threshold_s = (self._poll_interval_ms / 1000.0) * _STALE_MULTIPLIER
        if age_s > stale_threshold_s:
            self._freshness_lbl.setStyleSheet(_INVALID_STYLE)
            self._freshness_lbl.setText(f"ELAVULT ADAT ({age_s:.0f} mp)")
            # R17-23: a fejléc-cimke mellett a 19+15 ertekcimket is
            # elavultnak jelöli -- a panel MÁR SAJÁT (l. _mark_all_
            # stale()) mechanizmusát hasznosítja újra, amit a testvér
            # live_panel.py is átvett (CF-27). Csak egyszer, az átmenet
            # pillanatában (l. _all_stale_marked docstringje).
            if not self._all_stale_marked:
                self._all_stale_marked = True
                self._mark_all_stale()
        else:
            self._freshness_lbl.setStyleSheet("")
            # [MAX verifier lelete, 2026-09-16] EZ az egyetlen hely, ahol a
            # flag sikeres frissules utan False-ra all -- update_snapshot()
            # feltetel nelkul ezt a metodust hivja a vegen, es a frissen
            # beallitott _last_success_perf miatt ide, nem a fenti "meg
            # stale" agba fut (a poll_interval_ms sosem 0, l.
            # set_poll_interval_ms() hivohelyei), tehat egy kulon reset
            # update_snapshot()-ban redundans lenne (mutacio-tesztelve).
            self._all_stale_marked = False

    def set_poll_interval_ms(self, interval_ms: int) -> None:
        self._poll_interval_ms = interval_ms
