"""
LivePanel — kibővített állapotsor + 4 pyqtgraph grafikon + Rb panel + Start/Stop/EmStop gombok.
Slot: update_sample(dict) — minden tickben frissül.
"""
from __future__ import annotations

import time

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from Prog.gui.panels.event_log_widget import EventLogWidget

_MAX_POINTS = 3600  # max 1 óra 1 s tickkel
# Review-kör 5 (2026-09-10), GUI/UX F7 (P2): a dV/dt/dA/dt ablak korábban
# FIX MINTASZÁM (10 minta) volt, nem idő -- a _decimate_all() (l. ott a
# docstringet) a puffert a session előrehaladtával ismétlődően megfelezi,
# hogy a TELJES session időtartama lefedett maradjon a grafikonon, tehát
# az egymást követő MINTÁK közti valós időköz a session hosszával nő. Egy
# fix 10-MINTÁS ablak emiatt egyre SZÉLESEBB VALÓS IDŐT fedett le minden
# decimálás után (pl. 2 óra után a duplájára, 4 óra után a négyszeresére)
# -- a "10 minta simítás" jelentése csendben "percekre simítás"-sá vált
# egy hosszú (15-30 órás) TRADITIONAL_LEARNING/GOLDEN_GG session végére,
# az operátor számára láthatatlanul (a címke formátuma/mértékegysége
# változatlan maradt). _DVDT_WINDOW ezért mostantól a NÉVLEGES tick-szám
# (a cél ablak-hossz alapja), a tényleges ablakot _dvdt_window_seconds()
# adja a session ACTUÁLIS konfigurált tick-rátájából (self._expected_
# tick_interval_ms) -- így az ablak IDŐBEN állandó a session teljes
# hosszán át, decimálástól függetlenül.
_DVDT_WINDOW = 10   # dV/dt/dA/dt ablak névleges tick-száma (l. fenti komment)

# Governance-v2 review-kör 1 (CF-27, GUI-R1-08, DFAM-001): a Bázis
# `bq_diagnostics_panel.py::_mark_all_stale()`/`_refresh_freshness_label()`
# mintáját követve — a Fázis/Lépés idő/Session idő korábban CSAK
# update_sample()-ből frissült, tehát mintaszünetben (pl. hosszú relax,
# ritka tick) VAGY egy terminális állapot (DONE/FAULT) UTÁN a kijelzés
# a legutóbbi értéken FAGYOTT MEG, staleness-jelzés nélkül — az operátor
# nem tudta megkülönböztetni "a lépés még mindig fut, csak lassan
# frissül" és "a kijelzés elavult, nincs friss adat" eseteket.
_LIVE_LABEL_BASE_STYLE = "font-size: 15px; padding: 5px;"
_LIVE_LABEL_STALE_STYLE = _LIVE_LABEL_BASE_STYLE + "color: #c62828; font-weight: bold;"

# Kör 21 F14 (F21-G2, DFAM-028 -- sibling pattern transferred without
# precondition): Kör 19 GUI-R19-01 a fenti generikus _LIVE_LABEL_BASE_
# STYLE-t másolta át a "friss adat visszaáll" reset-ágakra, anélkül,
# hogy észrevette volna: a 3 Rb-érték címke (_rb1s_lbl/_rb10s_lbl/
# _rb30s_lbl) SAJÁT, a többitől eltérő stílust kap __init__-ben (l.
# _rb_value_label()) -- a reset ÖRÖKRE letörölte ezt, a generikus
# 15px-es stílusra cserélve, VISSZA SOSEM állt. Ez a konstans a
# CÍMKÉK SAJÁT alapstílusát nevezi meg (nem a generikus státuszsor-
# stílust), egyetlen forrásból (CLAUDE.md §8 -- egy duplikált
# style-string-forrás megszüntetése).
_RB_LABEL_BASE_STYLE = (
    "font-size: 22px; font-weight: bold; color: #ce93d8; padding: 8px;"
)

# R17-02 (Kör 17 MAX-effort review, DFAM-001): a test_runner.py::
# _build_sample() sample dict-je (logger.py::CSV_COLUMNS alapján
# seedelve) jelenleg ezt a 7 "*_valid" kulcsot hordozhatja. A
# `_VALIDITY_MARKED_LABELS` (a LivePanel.__init__()-ben, mert widget-
# referenciát tartalmaz) csak 2-t jelöl -- a maradék 5 SZÁNDÉKOSAN
# kizárva, indoklással:
#   - psu_readback_valid/load_readback_valid: MAX verifier lelete
#     (2026-09-16) szerint az EREDETI indoklás pontatlan volt -- nem
#     minden esetben "MÁR None". Pontosítva: az adott energia-ágban
#     (CHARGE/DISCHARGE, ill. a GOLDEN_GG/TRADITIONAL_LEARNING/OCV_SOC/
#     CHEMID/IEC_DC_RESISTANCE energia-alfázisai) AKTÍV műszer flagjére
#     igaz, hogy False esetén _build_sample() MÁR signed_current_A=
#     None-t ír (test_runner.py) -- _isign_lbl már "– A"-t mutat, nincs
#     hihető stale szám. A NEM-aktív műszer flagje (pl. load_readback_
#     valid CHARGE alatt) ugyanakkor LEGITIM módon False lehet, míg
#     signed_current_A ÉRVÉNYES, nem-nulla szám -- ez MÉG ERŐSEBBEN a
#     kizárás mellett szól (jelölése egy teljesen valós értéket
#     tenne hamisan gyanússá). A generikus `else` alfázisokban (a fenti
#     4 összetett controller nem-energia lépései, ill. RELAX) mindkét
#     flag LEGITIM módon False lehet, míg signed_current_A szándékosan
#     0.0 -- jelölése permanens hamis riasztást adna (DFAM-028 sibling-
#     minta-átvitel előfeltétel nélkül).
#   - sample_valid/integration_valid/safety_valid: logger.py CSV_COLUMNS
#     dokumentált, SOSEM töltött placeholder mezői -- nincs producer,
#     nincs mit jelölni.
# Ez a séma-teljességi meta-teszt alapja (l. test_live_panel.py) --
# a meta-teszt a "ground truth" halmazt a CSV_COLUMNS-ból származtatja,
# nem egy második, kézzel karbantartott literálból.
_VALIDITY_KEYS_INTENTIONALLY_NOT_MARKED = (
    "psu_readback_valid", "load_readback_valid",
    "sample_valid", "integration_valid", "safety_valid",
)
_STALE_MULTIPLIER = 2.0
_STALE_CHECK_INTERVAL_MS = 1000
_DEFAULT_EXPECTED_TICK_INTERVAL_MS = 2000  # TestRunnerConfig.runner_tick_s alapértéke

# Kontrollerállapot → megjelenítendő fázis szöveg
# Kulcs: (step_kind, controller state értéke)
_PHASE_MAP: dict[tuple[str, str], str] = {
    # Review-kör 5 (2026-09-10), GUI/UX F8 (P3, DFAM-001+018 4.
    # előfordulása -- l. Prog/tests/gui/test_live_panel.py::
    # TestStructuralPreventionPhaseMapCoverage): a CHARGE/DISCHARGE/RELAX/
    # OCV_SOC StepKind-ok CHARGE_CC/CHARGE_CV_DMM_CONTROL/TAPER_HOLD/
    # DISCHARGE_CC_*/RELAXING/FAULT-on kívüli, MÁR RÉGÓTA létező
    # controller-állapotai (INIT/PRECHECK/PSU_PRESET/CHARGE_DONE/
    # DISCHARGE_DONE/RELAX_DONE/OCV_SOC DONE) egyáltalán nem szerepeltek
    # itt -- a fallback (_STEP_KIND_DISPLAY) generikus címkéje NEM téves
    # (nem fault-maszkolás), de a KÉSZ-állapotokat megkülönböztethetetlenné
    # tette egy ténylegesen még futó fázistól.
    ("CHARGE",    "INIT"):                  "CHARGE INIT",
    ("CHARGE",    "PRECHECK"):               "CHARGE ELŐKÉSZÍTÉS",
    ("CHARGE",    "PSU_PRESET"):             "CHARGE PSU BEÁLLÍTÁS",
    ("CHARGE",    "CHARGE_CC"):             "CHARGE CC",
    ("CHARGE",    "CHARGE_CV_DMM_CONTROL"): "CHARGE CV",
    ("CHARGE",    "TAPER_HOLD"):            "CHARGE TAPER",
    ("CHARGE",    "CHARGE_DONE"):            "CHARGE KÉSZ",
    # Governance-v2 review-kör 1 (CF-26, GUI-R1-04/05, DFAM-001+018):
    # korábban a CHARGE/DISCHARGE/OCV_SOC útvonalon a controller SAJÁT
    # FAULT állapota nem volt térképezve — a fallback (_STEP_KIND_DISPLAY)
    # ilyenkor a NORMÁL futó fázissal MEGEGYEZŐ, generikus címkét adta
    # ("CHARGE"/"DISCHARGE"/"OCV-SOC"), megkülönböztethetetlenül egy
    # ténylegesen futó fázistól — csak a külön _fault_lbl (set_fault())
    # jelezte a hibát, a Fázis mező NEM. A GOLDEN_GG/TRADITIONAL_LEARNING
    # testvér-útvonalak ezt már helyesen kezelték (l. lent). A RELAX
    # step_kind-nak (RelaxState) NINCS FAULT tagja (a relax alatt PSU/
    # Load már OFF, nincs aktív energiaátadás, amit a fault-út
    # jellemezne) — ott ez a hiány strukturálisan nem fordulhat elő.
    ("CHARGE",    "FAULT"):                 "CHARGE HIBA",
    ("DISCHARGE", "INIT"):                  "DISCHARGE INIT",
    ("DISCHARGE", "PRECHECK"):               "DISCHARGE ELŐKÉSZÍTÉS",
    ("DISCHARGE", "DISCHARGE_CC_SETUP"):    "DISCHARGE",
    ("DISCHARGE", "DISCHARGE_CC_RUN"):      "DISCHARGE",
    ("DISCHARGE", "DISCHARGE_DONE"):         "DISCHARGE KÉSZ",
    ("DISCHARGE", "FAULT"):                 "DISCHARGE HIBA",
    ("RELAX",     "INIT"):                   "RELAX INIT",
    ("RELAX",     "RELAXING"):              "RELAX",
    ("RELAX",     "RELAX_DONE"):             "RELAX KÉSZ",
    ("OCV_SOC",   "INIT"):                   "OCV-SOC INIT",
    ("OCV_SOC",   "PRECHARGE"):             "OCV-SOC CHARGE",
    ("OCV_SOC",   "PRECHARGE_RELAX"):       "OCV-SOC RELAX",
    ("OCV_SOC",   "STEP_DISCHARGE"):        "OCV-SOC DISCHARGE",
    ("OCV_SOC",   "STEP_RELAX"):            "OCV-SOC RELAX",
    ("OCV_SOC",   "IMPULSE_PREP"):          "OCV-SOC IMPULSE PREP",
    ("OCV_SOC",   "IMPULSE_ON"):            "OCV-SOC IMPULSE",
    ("OCV_SOC",   "IMPULSE_ACQUIRE"):       "OCV-SOC IMPULSE",
    # CF-26: az opcionális (fast_transient_enabled) ms-tartományú
    # diagnosztikai réteg 2 alállapota korábban szintén hiányzott — a
    # standard IMPULSE_ON/IMPULSE_ACQUIRE-hoz hasonló, "OCV-SOC IMPULSE"
    # generikus címkével jelenik meg, mert a hívó (operátor) szempontjából
    # mindkettő ugyanaz a fizikai esemény (pulzus-mérés), csak eltérő
    # időbontással — l. ocv_soc_controller.py OcvSocState docstring.
    ("OCV_SOC",   "IMPULSE_FAST_ON"):       "OCV-SOC IMPULSE",
    ("OCV_SOC",   "IMPULSE_FAST_ACQUIRE"):  "OCV-SOC IMPULSE",
    ("OCV_SOC",   "LOG_SOC_POINT"):         "OCV-SOC LOG",
    ("OCV_SOC",   "DONE"):                   "OCV-SOC KÉSZ",
    ("OCV_SOC",   "FAULT"):                 "OCV-SOC HIBA",

    # Felhasználói kérés (2026-08-23): korábban a GOLDEN_GG/TRADITIONAL_
    # LEARNING lépés a Fázis mezőben mindig csak a nyers step_kind-ot
    # mutatta ("golden_gg"/"traditional_learning"), mert ez a térkép nem
    # ismerte a controller saját belső állapotait (GoldenGgState/
    # LearningState) — holott azok már eljutottak a sample["state"]-be.
    ("GOLDEN_GG", "INIT"):                         "GOLDEN GG INIT",
    ("GOLDEN_GG", "PRECOND"):                       "GOLDEN GG ELŐKÉSZÍTÉS",
    ("GOLDEN_GG", "CONDITIONING"):                  "GOLDEN GG KONDICIONÁLÁS",
    ("GOLDEN_GG", "FULL_CHARGE"):                   "GOLDEN GG TÖLTÉS",
    ("GOLDEN_GG", "CHARGE_TERMINATION_VERIFIED"):   "GOLDEN GG TÖLTÉS ELLENŐRZÉS",
    ("GOLDEN_GG", "RELAX_2H"):                      "GOLDEN GG RELAX (2H)",
    ("GOLDEN_GG", "SYSTEM_RATE_DISCHARGE"):         "GOLDEN GG KISÜTÉS",
    ("GOLDEN_GG", "RELAX_5H"):                      "GOLDEN GG RELAX (5H)",
    ("GOLDEN_GG", "EXPORT_PACKAGE"):                "GOLDEN GG EXPORT",
    ("GOLDEN_GG", "DONE"):                          "GOLDEN GG KÉSZ",
    ("GOLDEN_GG", "FAULT"):                         "GOLDEN GG HIBA",

    ("TRADITIONAL_LEARNING", "INIT"):                     "LEARNING INIT",
    ("TRADITIONAL_LEARNING", "PRECOND"):                  "LEARNING ELŐKÉSZÍTÉS",
    ("TRADITIONAL_LEARNING", "PRECHARGE"):                "LEARNING TÖLTÉS 1",
    ("TRADITIONAL_LEARNING", "PRECHARGE_RELAX"):          "LEARNING RELAX 1",
    ("TRADITIONAL_LEARNING", "INITIAL_SNAPSHOT"):         "LEARNING KIINDULÓ SNAPSHOT",
    ("TRADITIONAL_LEARNING", "IT_ENABLE_STEP"):           "LEARNING IT ENABLE",
    ("TRADITIONAL_LEARNING", "VERIFY_VOK_QEN"):           "LEARNING VOK/QEN ELLENŐRZÉS",
    ("TRADITIONAL_LEARNING", "DISCHARGE_1"):              "LEARNING KISÜTÉS 1",
    # 2026-08-24 (BQ34Z110_learning_rebound_kisutes.txt, TI SLUSB55B STEP 8
    # rebound-kezelés bevezetése): a load levétele után külön fázis várja
    # meg és értékeli ki a feszültség-visszapattanást, mielőtt RELAX-ba
    # lépne (l. traditional_learning_controller.py::_run_rebound_check()).
    ("TRADITIONAL_LEARNING", "REBOUND_CHECK_1"):          "LEARNING REBOUND ELLENŐRZÉS 1",
    ("TRADITIONAL_LEARNING", "RELAX_1"):                  "LEARNING RELAX 2",
    ("TRADITIONAL_LEARNING", "VERIFY_UPDATE_STATUS_05"):  "LEARNING STATUS 0x05 ELLENŐRZÉS",
    ("TRADITIONAL_LEARNING", "RECHARGE"):                 "LEARNING TÖLTÉS 2",
    ("TRADITIONAL_LEARNING", "RELAX_PRE_DISCHARGE_2"):    "LEARNING RELAX 3",
    ("TRADITIONAL_LEARNING", "DISCHARGE_2"):              "LEARNING KISÜTÉS 2",
    ("TRADITIONAL_LEARNING", "REBOUND_CHECK_2"):          "LEARNING REBOUND ELLENŐRZÉS 2",
    ("TRADITIONAL_LEARNING", "RELAX_2"):                  "LEARNING RELAX 4",
    ("TRADITIONAL_LEARNING", "VERIFY_UPDATE_STATUS_06"):  "LEARNING STATUS 0x06 ELLENŐRZÉS",
    ("TRADITIONAL_LEARNING", "FINAL_SNAPSHOT"):           "LEARNING ZÁRÓ SNAPSHOT",
    ("TRADITIONAL_LEARNING", "FINAL_RECHARGE"):           "LEARNING ZÁRÓ ÚJRATÖLTÉS",
    ("TRADITIONAL_LEARNING", "DONE"):                     "LEARNING KÉSZ",
    ("TRADITIONAL_LEARNING", "FAULT"):                    "LEARNING HIBA",

    # CF-26 (governance-v2 review-kör 1, GUI-R1-04/05, DFAM-001+018): a
    # CHEMID StepKind (chemid_controller.py::ChemIdState, 11 állapot)
    # EGYÁLTALÁN nem szerepelt sem itt, sem a _STEP_KIND_DISPLAY-ben —
    # egy CHEMID session alatt a Fázis mező mindig a nyers "CHEMID"
    # step_kind stringet mutatta, a GOLDEN_GG/TRADITIONAL_LEARNING előtti
    # állapothoz hasonlóan (l. fenti 2026-08-23 kommentet).
    ("CHEMID", "INIT"):                                   "CHEMID INIT",
    ("CHEMID", "PRECHECK"):                                "CHEMID ELŐKÉSZÍTÉS",
    ("CHEMID", "ROOM_TEMPERATURE_CONDITIONING"):           "CHEMID KONDICIONÁLÁS",
    ("CHEMID", "FULL_CHARGE_MANUFACTURER_METHOD"):         "CHEMID TÖLTÉS",
    ("CHEMID", "CHARGE_TERMINATION_VERIFIED"):             "CHEMID TÖLTÉS ELLENŐRZÉS",
    ("CHEMID", "RELAX_5H_FULL"):                           "CHEMID RELAX (TELE)",
    ("CHEMID", "C20_DISCHARGE_TO_MANUFACTURER_MINIMUM"):   "CHEMID KISÜTÉS",
    ("CHEMID", "RELAX_5H_EMPTY"):                          "CHEMID RELAX (ÜRES)",
    ("CHEMID", "EXPORT_GPCCHEM_PACKAGE"):                  "CHEMID EXPORT",
    ("CHEMID", "RECHARGE_REQUIRED"):                       "CHEMID VISSZATÖLTÉS SZÜKSÉGES",
    ("CHEMID", "FAULT"):                                   "CHEMID HIBA",

    # MAX verifier lelet (governance-v2 kör 2, F4, DFAM-001+018 3.
    # előfordulás -- l. a fenti GOLDEN_GG/2026-08-23 és CHEMID/CF-26
    # kommenteket ugyanerről a hibaosztályról): az IEC_DC_RESISTANCE
    # StepKind (iec_dc_resistance_controller.py::IecDcResistanceState, 9
    # állapot) EGYÁLTALÁN nem szerepelt sem itt, sem a
    # _STEP_KIND_DISPLAY-ben -- egy IEC_DC_INTERNAL_RESISTANCE session
    # alatt (2h precharge relax, 20s@4×I10, 5perc pihenő, 5s@20×I10) a
    # Fázis mező mindig a nyers "IEC_DC_RESISTANCE" step_kind stringet
    # mutatta volna, az operátor nem látta volna, mikor van rákapcsolva a
    # terhelés.
    ("IEC_DC_RESISTANCE", "INIT"):                "IEC RB INIT",
    ("IEC_DC_RESISTANCE", "PRECHARGE"):           "IEC RB TÖLTÉS",
    ("IEC_DC_RESISTANCE", "PRECHARGE_RELAX"):     "IEC RB RELAX (TÖLTÉS UTÁN)",
    ("IEC_DC_RESISTANCE", "LOAD_1"):              "IEC RB TERHELÉS 1 (4×I10)",
    ("IEC_DC_RESISTANCE", "OPEN_CIRCUIT_REST"):   "IEC RB NYITOTT KÖRI PIHENŐ",
    ("IEC_DC_RESISTANCE", "LOAD_2"):              "IEC RB TERHELÉS 2 (20×I10)",
    ("IEC_DC_RESISTANCE", "COMPUTE_RESULT"):      "IEC RB SZÁMÍTÁS",
    ("IEC_DC_RESISTANCE", "DONE"):                "IEC RB KÉSZ",
    ("IEC_DC_RESISTANCE", "FAULT"):               "IEC RB HIBA",
}

_STEP_KIND_DISPLAY: dict[str, str] = {
    "CHARGE":                "CHARGE",
    "DISCHARGE":             "DISCHARGE",
    "RELAX":                 "RELAX",
    "OCV_SOC":               "OCV-SOC",
    "MANUAL_CHECKPOINT":     "BQ CHECKPOINT",
    "GOLDEN_GG":             "GOLDEN GG",
    "TRADITIONAL_LEARNING":  "LEARNING",
    # CF-26: fallback címke, amíg az első sample["state"] meg nem érkezik
    # (l. _PHASE_MAP fenti CHEMID bejegyzései a konkrét állapotokra).
    "CHEMID":                "CHEMID",
    # MAX verifier lelet (governance-v2 kör 2, F4) -- l. _PHASE_MAP fenti
    # IEC_DC_RESISTANCE bejegyzései a konkrét állapotokra.
    "IEC_DC_RESISTANCE":    "IEC RB",
}


def _time_window_start_index(x_list: list[float], window_s: float) -> int:
    """Review-kör 5 (2026-09-10), GUI/UX F7 (P2) -- l. _DVDT_WINDOW fenti
    komment. Visszaadja a legkorábbi indexet, ami a lista VÉGÉTŐL nézve
    legalább `window_s` másodpercet fed le -- decimálás-független IDŐABLAK,
    nem fix mintaszám. Ha a puffer még nem éri el ezt az időtávot (session
    eleje), a teljes elérhető puffert adja (0. index), ugyanúgy, ahogy a
    korábbi fix-mintaszámú ablak is kevesebb mintával dolgozott a session
    elején."""
    if not x_list:
        return 0
    target = x_list[-1] - window_s
    for i in range(len(x_list) - 1, -1, -1):
        if x_list[i] <= target:
            return i
    return 0


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
        # Teljes kódbázis review 5. kör (2026-08-29), GUI/UX P2 lelet: a
        # korábbi deque(maxlen=_MAX_POINTS) egy CSÚSZÓABLAK volt, nem
        # decimation — minden új mintánál a LEGRÉGEBBI pont véglegesen
        # eldobódott. Alapértelmezett runner_tick_s=2.0s mellett ez ~2h
        # ablakot jelentett, miközben TRADITIONAL_LEARNING/GOLDEN_GG
        # sessionök jellemzően >15-30h — a session korai fázisainak görbéi
        # véglegesen eltűntek élő nézetben (a nyers log épsége nem sérült,
        # csak a GUI-megjelenítés). Sima list, hogy _decimate_all()
        # megfelezhesse — l. update_sample() és CLAUDE.md §15 "decimation
        # VAGY korlátozott pontszám".
        self._xs:           list[float] = []
        self._u_batt:       list[float] = []
        self._i_signed:     list[float] = []
        self._temp:         list[float] = []
        self._charge_ah:    list[float] = []
        self._discharge_ah: list[float] = []
        self._step_start_perf: float = 0.0
        self._current_step_kind: str = ""
        # CF-27 (governance-v2, DFAM-001): l. modul docstring a
        # _stale_timer/_last_sample_perf indoklásáról.
        self._last_sample_perf: float | None = None
        self._expected_tick_interval_ms: int = _DEFAULT_EXPECTED_TICK_INTERVAL_MS
        # ST1 (G1-UD-7): a fast loop mintaköz-nyújtó kerete, l.
        # set_fast_loop_budget_s().
        self._fast_loop_budget_s: float = 0.0
        # Kör 19 D7 (GUI-R19-02, DFAM-018 severity-collapse + DFAM-021-
        # adjacent no-persistent-artefact): a warning_flags korábban
        # KIZÁRÓLAG a _warn_lbl szövegeként jelent meg, ami a
        # _stale_tracked_labels tagjaként MINDEN ticken BASE stílusra állt
        # vissza (_clear_staleness()) -- egy figyelmeztetés tehát sima,
        # nem-kiemelt szövegként jelent meg és tűnt el, szemben a többi
        # diagnosztikai mezővel, ami piros-félkövér stílust kap. Élő
        # eseménnyé alakítjuk ÁTMENET-alapon (bq_diagnostics_panel.py::
        # _check_field_change() bevált mintáját követve, l. update_
        # sample()): az utoljára TOVÁBBÍTOTT figyelmeztetés-kódot tároljuk,
        # hogy ugyanaz a figyelmeztetés NE spammelje az EventLogWidget
        # 500-soros gyűrűjét minden ticken, amíg fennáll.
        self._last_forwarded_warning_code: str | None = None
        # Governance-v2 review-kör 1 (CF-29, GUI-R1-11, DFAM-012 rokona):
        # a device_error_ready worker-signal korábban KIZÁRÓLAG a
        # device_error_panel.py háttérfülre volt kötve — élő nézeten
        # (ahol az operátor ténylegesen figyeli a sessiont) SEMMI nem
        # jelezte, ha SCPI hiba történt, amíg valaki külön át nem váltott
        # a háttérfülre. Qt signal több független slotot is tud fogadni,
        # ezért a meglévő device_error_panel.py kapcsolat változatlan —
        # csak egy ÚJ, kiegészítő kapcsolat kell (l. main_window.py).
        self._device_err_count: int = 0
        self._build_ui()

        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(_STALE_CHECK_INTERVAL_MS)
        self._stale_timer.timeout.connect(self._refresh_staleness)
        self._stale_timer.start()

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
        self._dvdt_lbl     = self._status_label("– mV/min")
        self._dadt_lbl     = self._status_label("– mA/min")
        self._warn_lbl     = self._status_label("–")
        self._fault_lbl    = self._status_label("–")
        self._phase_lbl      = self._status_label("–")
        self._step_info_lbl  = self._status_label("step –/–")
        self._step_time_lbl  = self._status_label("–:–:–")
        self._total_time_lbl = self._status_label("–:–:–")
        self._device_err_lbl = self._status_label("0")
        # Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói
        # döntés 2026-09-10): a PSU tényleges CV/CC regulációs állapota --
        # KÜLÖN mező a fenti _psumode_lbl-től (ami a kombinációs módot,
        # INDEPENDENT/SERIES/PARALLEL mutatja, NEM a regulációs állapotot).
        # SZÁNDÉKOSAN a worker `psu_regulation_ready` szignálján, NEM az
        # `update_sample()`-en keresztül frissül -- l. update_psu_
        # regulation() docstringje.
        self._psu_regulation_lbl = self._status_label("–")

        # Review-kör 5, GUI G5-03 (P2): korábban a staleness-jelzés
        # (_clear_staleness()/_refresh_staleness()) KIZÁRÓLAG a 3
        # idő-/fázis-címkét (_step_time_lbl/_total_time_lbl/_phase_lbl)
        # fedte le a panel 17 kijelzett mezőjéből — a fizikai mérési
        # értékek (U/I/T, PSU/Load setpontok, dV/dt/dA/dt, warning) egy
        # blokkolt tick alatt JELÖLETLENÜL fagytak be, holott
        # update_sample() MINDEGYIKÜKET ugyanabban a tick-vezérelt
        # hívásban tölti fel, mint az időt/fázist. A _rb*_lbl (pulzus-
        # ellenállás), _state_lbl/_fault_lbl (esemény-vezérelt, nem
        # tick-vezérelt) és _step_info_lbl/_device_err_lbl (számláló-
        # jellegű) SZÁNDÉKOSAN NEM tagja ennek a halmaznak -- ezek
        # legitim módon maradhatnak változatlanok több tick alatt is,
        # önmagában ez nem staleness jele. R2-13: a _psu_regulation_lbl
        # MOSTANTÓL tagja -- l. lent a saját kommentjét; a _rb*/_state/
        # _fault/_step_info/_device_err kizárása változatlan.
        self._stale_tracked_labels: tuple[QLabel, ...] = (
            self._ubatt_lbl, self._isign_lbl, self._temp_lbl,
            self._charge_lbl, self._disch_lbl, self._psumode_lbl,
            self._udrop_lbl, self._upsu_set_lbl, self._iload_set_lbl,
            self._dvdt_lbl, self._dadt_lbl, self._warn_lbl,
            self._phase_lbl, self._step_time_lbl, self._total_time_lbl,
            # R2-13 (2026-09-15, DFAM-001 sibling-omission a G5-03 fixhez
            # képest): a "PSU reguláció" címke MÁS szignálon (worker.
            # psu_regulation_ready) frissül, de UGYANABBAN a runner tick-
            # ciklusban keletkezik (test_runner.py::_poll_psu_regulation_
            # state(), a _should_poll_device_errors() kapun belül) -- tehát
            # egy blokkolt tick vagy egy terminális állapot (FAULT/DONE/
            # STOPPED) UTÁN ugyanúgy JELÖLETLENÜL fagyott be, az utolsó
            # "CV"/"CC" értéken, azt a látszatot keltve, hogy a PSU MÉG
            # szabályoz.
            self._psu_regulation_lbl,
        )

        # R17-02 (Kör 17 MAX-effort review, DFAM-001): a sample dict MÁR
        # tartalmazza a dmm_voltage_valid/dmm_temperature_valid kulcsokat
        # (test_runner.py::_build_sample(), több controller-forrásból) --
        # LivePanel korábban SOHA nem olvasta ezeket, tehát egy sikertelen
        # DMM-olvasás UTÁN a controllerek MEGŐRZÖTT (nem nullázott) utolsó-
        # jó feszültség/hőmérséklet érték HIHETŐ, FRISS mérésnek látszott.
        # A meglévő _LIVE_LABEL_STALE_STYLE-t (nem új stílust) hasznosítjuk
        # újra update_sample()-ben. A psu_readback_valid/load_readback_valid
        # és a 3, sosem töltött CSV-placeholder (sample_valid/
        # integration_valid/safety_valid) SZÁNDÉKOSAN kizárva -- l. a
        # kizárás indoklását a lenti tuple-nél.
        self._VALIDITY_MARKED_LABELS: tuple[tuple[str, QLabel], ...] = (
            ("dmm_voltage_valid", self._ubatt_lbl),
            ("dmm_temperature_valid", self._temp_lbl),
        )
        # Kör 18 Batch C14/GUI-P3-3: set_step() is _clear_staleness()-t hív
        # (élő worker-aktivitás jelzésére, l. ott), ami korábban FELTÉTEL
        # NÉLKÜL BASE stílusra állította a _VALIDITY_MARKED_LABELS
        # címkéket is -- egy lépéshatár (pl. RELAX -> CHARGE fázisváltás)
        # így letörölte a DMM-érvénytelenség jelzését, holott a mögöttes
        # utolsó-ismert érvényesség NEM változott. Ez a lista jegyzi, mely
        # _stale_tracked_labels tag van jelen ÉRVÉNYTELENSÉG miatt
        # (update_sample() tölti, lent) -- _clear_staleness() ezeket
        # kihagyja.
        self._invalid_marked_labels: list[QLabel] = []

        # Kör 19 D7 (GUI-R19-01, DFAM-001 2. előfordulása UGYANEBBEN a
        # fájlban, l. a fenti R17-02 kommentje az eredeti alakzatért):
        # test_runner.py::_build_sample() már 2026-09-XX óta tölti a
        # rb_timing_degraded kulcsot (a discharge_controller.py Rb 1s/10s/
        # 30s mintavételi időpont-toleranciájának megsértéséről), de a
        # LivePanel korábban SOHA nem olvasta -- egy időzítés-degradált Rb
        # érték UGYANÚGY nézett ki, mint egy pontos, ami metrológiailag
        # félrevezető (rules/20-metrology.md "ne állíts nagyobb
        # pontosságot, mint amit... időzítés... alátámaszt"). A
        # _rb1s_lbl/_rb10s_lbl/_rb30s_lbl SZÁNDÉKOSAN NEM tagja a
        # _stale_tracked_labels halmaznak (l. ott a kommentet) -- emiatt
        # _clear_staleness() ŐKET SOHA nem érinti, tehát a stílust itt,
        # update_sample()-ben KÖZVETLENÜL kell be- és visszaállítani (nem
        # elég csak _invalid_marked_labels-hez adni őket, mint a validity-
        # jelölésnél, mert az a lista csak a _clear_staleness()-kivételhez
        # kell -- ide mégis felvesszük, a mechanizmus egységessége/jövőbeli
        # auditálhatósága miatt, l. lent update_sample()).

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
            ("dA/dt",       self._dadt_lbl),
            ("Warning",     self._warn_lbl),
            ("Fault",       self._fault_lbl),
        ]
        _ROW3 = [
            ("Fázis",       self._phase_lbl),
            ("Lépés",       self._step_info_lbl),
            ("Lépés idő",   self._step_time_lbl),
            ("Session idő", self._total_time_lbl),
            ("Eszközhiba",  self._device_err_lbl),
            ("PSU reguláció", self._psu_regulation_lbl),
        ]
        for col, (name, widget) in enumerate(_ROW1):
            status_grid.addWidget(QLabel(f"<b>{name}</b>"), 0, col)
            status_grid.addWidget(widget, 1, col)
        for col, (name, widget) in enumerate(_ROW2):
            status_grid.addWidget(QLabel(f"<b>{name}</b>"), 2, col)
            status_grid.addWidget(widget, 3, col)
        status_grid.setRowMinimumHeight(2, 4)  # kis elválasztás a két sor között
        for col, (name, widget) in enumerate(_ROW3):
            status_grid.addWidget(QLabel(f"<b>{name}</b>"), 5, col)
            status_grid.addWidget(widget, 6, col)
        status_grid.setRowMinimumHeight(4, 6)  # elválasztó a 2. és 3. sor között

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
        lbl.setStyleSheet(_RB_LABEL_BASE_STYLE)
        lbl.setAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
        return lbl

    # ------------------------------------------------------------------ #
    # Frissítés                                                            #
    # ------------------------------------------------------------------ #

    def _decimate_all(self) -> None:
        """Amikor a puffer eléri a _MAX_POINTS méretet, minden MÁSODIK
        pontot megtartva megfelezi mind a 6 puffert (szinkron indexekkel)
        — a session TELJES időtartama (t=0-tól a jelenlegi pillanatig)
        mindig lefedett marad a grafikonon, csak a felbontás csökken a
        session előrehaladtával. Ez a tényleges decimation (nem a korábbi
        csúszóablak-alapú adatvesztés) — l. update_sample() hívási
        helyének kommentje."""
        for buf in (self._xs, self._u_batt, self._i_signed, self._temp,
                    self._charge_ah, self._discharge_ah):
            del buf[1::2]

    def update_sample(self, sample: dict) -> None:
        # CF-27 (governance-v2, DFAM-001): friss minta érkezett -- a
        # staleness-jelzés (ha aktív volt) törlődik, l. _refresh_staleness().
        self._last_sample_perf = time.perf_counter()
        self._invalid_marked_labels = []
        self._clear_staleness()
        # Kör 20 Batch E10 (F20-G1, DFAM-022 RECURRING + DFAM-001): a
        # session_elapsed_s háromutas fallback (l. :747-752 lejjebb, R2-14)
        # itt korábban hiányzott -- egy cross-process resume után a
        # kapacitás-integráló görbe x=0-tól indult újra NEM-nulla Ah
        # értékkel (fizikailag lehetetlen ugrás az origóban), miközben a
        # mellette lévő "Session idő" felirat helyesen folytonos időt
        # mutatott. A session_elapsed_s a session_elapsed_s==elapsed_s
        # (nem-resume) úton BIT-AZONOS, l. a regressziós teszt empirikus
        # igazolását -- nem levezetésből elfogadva.
        session_elapsed = sample.get("session_elapsed_s")
        elapsed = (
            session_elapsed if session_elapsed is not None
            else (sample.get("elapsed_s") or 0.0)
        )
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

        # Kör 18 (2026-09-16), GUI/UX reviewer P2-3, DFAM-001 (R17-02
        # sibling-hézag): R17-02 a _ubatt_lbl/_temp_lbl CÍMKÉKET jelöli
        # meg (lent, u/t VÁLTOZATLANUL a sticky last-good értéket
        # hordozza a stílus-jelöléshez), DE a chart-görbékbe eddig
        # ugyanez a sticky last-good szám került FELTÉTEL NÉLKÜL --
        # egy tartós DMM-kiesés alatt a grafikon egy tökéletesen sima,
        # fizikailag hihető szakaszt rajzolt, megkülönböztethetetlenül
        # egy valós, stabil mérésttől. A plot-pufferbe NaN kerül
        # érvénytelen mintánál (a `_f()` már ugyanezt teszi hiányzó
        # kulcsra, pyqtgraph natívan kezeli -- szakadást rajzol, nem
        # összekötött vonalat), a CÍMKE-logika (lent) VÁLTOZATLAN marad.
        u_plot = u if sample.get("dmm_voltage_valid", True) is not False else float("nan")
        t_plot = t if sample.get("dmm_temperature_valid", True) is not False else float("nan")

        self._u_batt.append(u_plot)
        self._i_signed.append(i)
        self._temp.append(t_plot)
        # Mellékhatás (NORMAL verifier megjegyzése, Kör 18): self._u_batt
        # a dV/dt-ablak bemenete is (lent) -- egy DMM-kiesés alatt a
        # dV/dt-jelző mostantól "– mV/min"-t mutat a fabrikált (sticky
        # értékből számolt) meredekség helyett, ami helyes és szándékos
        # következmény, nem külön hiba.
        self._charge_ah.append(ch)
        self._discharge_ah.append(dch)

        if len(self._xs) > _MAX_POINTS:
            self._decimate_all()

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
        # Kör 19 D7 (GUI-R19-02): l. __init__ kommentje. Csak ÚJ (a
        # korábban továbbítottól ELTÉRŐ), nem-üres kódnál tüzelünk -- a
        # "–"-re/None-ra váltás (figyelmeztetés megszűnt) NEM esemény
        # (ez nem maga is diagnosztikai tény, csak a hiány visszaállása).
        _warning_code = sample.get("warning_flags")
        if _warning_code and _warning_code != self._last_forwarded_warning_code:
            self._event_log.append_event({
                "event_code": "LIVE_WARNING_FLAG",
                "event_message": f"Élő figyelmeztetés: {_warning_code}",
                "severity": "WARNING",
            })
        self._last_forwarded_warning_code = _warning_code

        # R17-02 (Kör 17 MAX-effort review, DFAM-001): l. _VALIDITY_
        # MARKED_LABELS docstring-je -- a meglévő _LIVE_LABEL_STALE_STYLE-t
        # hasznosítja újra (a metódus elején _invalid_marked_labels
        # nullázva, majd _clear_staleness() BASE-re állította az összeset,
        # ha az előző minta érvényes volt). Ez a lenti ciklus tölti fel
        # újra _invalid_marked_labels-t (l. Kör 18 C14/GUI-P3-3 -- a
        # set_step() ezt a listát olvasva NEM törli a jelölést).
        # Hiányzó kulcs = érvényesnek tekintve (sample.get(key, True)),
        # SOHA nem jelölve -- egy régi sémájú/direkt tesztelt sample dict
        # nem eshet csendben "invalid"-nak.
        for _valid_key, _lbl in self._VALIDITY_MARKED_LABELS:
            if sample.get(_valid_key, True) is False:
                _lbl.setStyleSheet(_LIVE_LABEL_STALE_STYLE)
                self._invalid_marked_labels.append(_lbl)

        upsu = sample.get("psu_set_voltage_V")
        self._upsu_set_lbl.setText(
            f"{upsu:.3f} V" if upsu is not None and upsu > 0 else "– V")

        iload = sample.get("load_set_current_A")
        self._iload_set_lbl.setText(
            f"{iload:.3f} A" if iload is not None and iload > 0 else "– A")

        # dV/dt: az utolsó ~_DVDT_WINDOW névleges tick-nyi IDŐABLAK átlagos
        # meredeksége (mV/min) -- l. _DVDT_WINDOW/_time_window_start_index()
        # fenti kommentje (F7): IDŐBEN állandó ablak, nem fix mintaszám,
        # hogy a decimálás a session előrehaladtával ne tágítsa csendben.
        u_list = list(self._u_batt)
        x_list = list(self._xs)
        dvdt_window_s = _DVDT_WINDOW * (self._expected_tick_interval_ms / 1000.0)
        idx = _time_window_start_index(x_list, dvdt_window_s)
        if idx < len(x_list) - 1:
            dt_win = x_list[-1] - x_list[idx]
            if dt_win > 0 and u_list[-1] == u_list[-1] and u_list[idx] == u_list[idx]:
                dvdt_mvmin = (u_list[-1] - u_list[idx]) / dt_win * 1000.0 * 60.0
                self._dvdt_lbl.setText(f"{dvdt_mvmin:+.2f} mV/min")
            else:
                self._dvdt_lbl.setText("– mV/min")
        else:
            self._dvdt_lbl.setText("– mV/min")

        # dA/dt: ugyanaz az időablak, mint dV/dt, az előjeles áramra (mA/min).
        # Felhasználói kérés (2026-08-23) — az áram-derivált a dV/dt mellett.
        i_list = list(self._i_signed)
        if idx < len(i_list) - 1:
            dt_win_i = x_list[-1] - x_list[idx]
            if dt_win_i > 0 and i_list[-1] == i_list[-1] and i_list[idx] == i_list[idx]:
                dadt_mamin = (i_list[-1] - i_list[idx]) / dt_win_i * 1000.0 * 60.0
                self._dadt_lbl.setText(f"{dadt_mamin:+.1f} mA/min")
            else:
                self._dadt_lbl.setText("– mA/min")
        else:
            self._dadt_lbl.setText("– mA/min")

        # Fázis: kontrollerállapot + step kind kombinációja
        state = sample.get("state") or ""
        phase = _PHASE_MAP.get((self._current_step_kind, state))
        if phase is None:
            phase = _STEP_KIND_DISPLAY.get(self._current_step_kind, self._current_step_kind or "–")
        self._phase_lbl.setText(phase)

        # Teljes session eltelt idő (HH:MM:SS)
        # R2-14 (2026-09-15): SESSION-szintű (a megszakítás(ok)on átívelő)
        # idő, nem a run()-szegmens elapsed_s-e -- egy checkpoint-folytatás/
        # resume után az utóbbi 0-ról indul újra, holott a session órák/napok
        # óta tart (l. logger.py CSV_COLUMNS "session_elapsed_s" kommentje).
        # A CF-19 phase_elapsed_s mintáját követő, visszafelé kompatibilis
        # fallback: ha a mező hiányzik/None (régi minta, közvetlen teszt-
        # hívás), a korábbi elapsed_s-viselkedés marad.
        session_elapsed = sample.get("session_elapsed_s")
        total_s = int(
            session_elapsed if session_elapsed is not None
            else (sample.get("elapsed_s") or 0)
        )
        self._total_time_lbl.setText(
            f"{total_s // 3600:02d}:{(total_s % 3600) // 60:02d}:{total_s % 60:02d}"
        )

        # Aktuális lépés eltelt idő (HH:MM:SS)
        # CF-19 (governance-v2 kör 2, DFAM-023 opaque_composite_step_hides_
        # progress): az összetett controllerek (OCV_SOC/CHEMID/TRADITIONAL_
        # LEARNING/GOLDEN_GG) egy-egy, akár több tíz órás belső állapotgépet
        # futtatnak EGYETLEN opak TestStep alatt -- a self._step_start_perf
        # csak a TestStep indulásakor (set_step()) áll be, tehát korábban
        # ez a kijelzés valójában a teljes (akár több tíz órás) session-
        # idővel volt egyenértékű, nem a JELENLEGI belső fázisban (pl.
        # RELAX_2H) eltöltött idővel. Ha a minta hordozza a controller
        # phase_elapsed_s property-jét (test_runner.py::_build_sample()),
        # azt használjuk -- ez a tick-konzisztens dt_s-ekből épül, és minden
        # belső állapotváltáskor nullázódik. Sima StepKindeknél (CHARGE/
        # DISCHARGE/RELAX/stb., ahol nincs ilyen property) a régi, perf_
        # counter-alapú számítás változatlan marad.
        phase_elapsed = sample.get("phase_elapsed_s")
        if phase_elapsed is not None:
            step_s = int(phase_elapsed)
        else:
            step_s = int(time.perf_counter() - self._step_start_perf)
        self._step_time_lbl.setText(
            f"{step_s // 3600:02d}:{(step_s % 3600) // 60:02d}:{step_s % 60:02d}"
        )

        # --- Rb panel ---
        rb1  = sample.get("rb_1s_mohm")
        rb10 = sample.get("rb_10s_mohm")
        rb30 = sample.get("rb_30s_mohm")
        dv   = sample.get("rb_delta_v")

        self._rb1s_lbl.setText( f"{rb1:.1f} mΩ"  if rb1  is not None else "– mΩ")
        self._rb10s_lbl.setText(f"{rb10:.1f} mΩ" if rb10 is not None else "– mΩ")
        self._rb30s_lbl.setText(f"{rb30:.1f} mΩ" if rb30 is not None else "– mΩ")
        self._rb_dv_lbl.setText(f"{dv:.3f} V"     if dv   is not None else "– V")

        # Kör 19 D7 (GUI-R19-01): l. __init__ kommentje. A discharge_
        # controller.py::rb_timing_degraded sticky (a lépés indulásakor
        # False-ra reseteltet, utána a session HÁTRALÉVŐ részén True
        # marad, ha bármelyik célidőpont a toleranciasávon kívül esett) --
        # tehát ÚJRA-alkalmazása minden ticken idempotens degradáltnál, és
        # helyesen BASE-re áll vissza a KÖVETKEZŐ lépés indulásakor (amikor
        # a sample már friss, False rb_timing_degraded-et hordoz).
        if sample.get("rb_timing_degraded") is True:
            for _rb_lbl in (self._rb1s_lbl, self._rb10s_lbl, self._rb30s_lbl):
                _rb_lbl.setStyleSheet(_LIVE_LABEL_STALE_STYLE)
                if _rb_lbl not in self._invalid_marked_labels:
                    self._invalid_marked_labels.append(_rb_lbl)
        else:
            for _rb_lbl in (self._rb1s_lbl, self._rb10s_lbl, self._rb30s_lbl):
                _rb_lbl.setStyleSheet(_RB_LABEL_BASE_STYLE)

    @Slot(dict)
    def set_step(self, payload: dict) -> None:
        # CF-27 (governance-v2, DFAM-001): egy új lépés kezdete is élő
        # worker-aktivitást jelez, l. update_sample() azonos kommentje.
        self._last_sample_perf = time.perf_counter()
        self._clear_staleness()
        self._step_start_perf = time.perf_counter()
        self._current_step_kind = payload.get("step_kind", "")
        idx   = payload.get("step_index", 0)
        count = payload.get("step_count", 0)
        label = payload.get("step_label", "–")
        self._step_info_lbl.setText(f"step {idx + 1}/{count}: {label}")
        self._step_time_lbl.setText("00:00:00")
        self._phase_lbl.setText(_STEP_KIND_DISPLAY.get(self._current_step_kind, self._current_step_kind or "–"))

    def set_expected_tick_interval_ms(self, interval_ms: int) -> None:
        """CF-27 (governance-v2, DFAM-001): a staleness-küszöb (l.
        _refresh_staleness()) a session TÉNYLEGES runner_tick_s-éhez
        igazodik — a bq_diagnostics_panel.py::set_poll_interval_ms()
        azonos mintáját követve. Hívandó session indításkor (l.
        main_window.py::_build_runner())."""
        self._expected_tick_interval_ms = interval_ms

    def set_fast_loop_budget_s(self, budget_s: float) -> None:
        """ST1 (G1-UD-7, DFAM-028): a ChargeController fast loopja késői
        CC-ben, CV-ben és TAPER_HOLD-ban egy runner-ticken belül további
        résztickeket futtat, névlegesen min(dt_s, fast_loop_max_substeps ×
        fast_loop_period_s) időkeretben (az utolsó, már megkezdett résztick
        I/O-ja ezen túlnyúlhat), így a minták normál töltés alatt is több
        másodperces közzel érkeznek. budget_s ennek a keretnek a névleges
        felső értéke. A _refresh_staleness() küszöbe ezért
        (runner_tick_s + budget_s) × _STALE_MULTIPLIER: a normál hosszú tick
        nem jelez, a valódi elakadás igen. A main_window.py::_build_runner()
        állítja a session ChargeConfig-jából; 0.0, ha a futó lépések között
        nincs töltő lépés. Véges, nemnegatív értéket vár. A névleges
        _expected_tick_interval_ms (a dV/dt-ablak alapja) nem változik, és
        reset_plots() ezt az értéket sem törli."""
        self._fast_loop_budget_s = budget_s

    def _clear_staleness(self) -> None:
        for lbl in self._stale_tracked_labels:
            if lbl in self._invalid_marked_labels:
                continue
            lbl.setStyleSheet(_LIVE_LABEL_BASE_STYLE)

    def _refresh_staleness(self) -> None:
        """CF-27 (governance-v2, DFAM-001): l. modul docstring. A
        bq_diagnostics_panel.py::_refresh_freshness_label() azonos
        mintáját követve — QTimer-vezérelt, update_sample()-től
        FÜGGETLEN ellenőrzés, hogy a tick-vezérelt kijelzés (fázis/idő
        ÉS a fizikai mérési értékek, l. G5-03/_stale_tracked_labels)
        ne fagyhasson meg észrevétlenül mintaszünetben vagy egy
        terminális állapot (DONE/FAULT) UTÁN. A küszöb a fast-loop kerettel
        bővített névleges tick többszöröse, l. set_fast_loop_budget_s()."""
        if self._last_sample_perf is None:
            return
        age_s = time.perf_counter() - self._last_sample_perf
        threshold_s = (
            self._expected_tick_interval_ms / 1000.0 + self._fast_loop_budget_s
        ) * _STALE_MULTIPLIER
        if age_s > threshold_s:
            for lbl in self._stale_tracked_labels:
                lbl.setStyleSheet(_LIVE_LABEL_STALE_STYLE)

    def set_start_enabled(self, enabled: bool) -> None:
        """Release-audit 2026-08-23 (2.6): a Start gomb explicit tiltása a
        hardveres bring-up (VISA connect/identify, BQ negotiate) alatt —
        megelőzi a dupla-Start-ot, amíg a kapcsolódás egy külön QThread-en
        fut (l. main_window.py::_start_test()/_on_connect_ready())."""
        self._start_btn.setEnabled(enabled)

    def set_emergency_stop_enabled(self, enabled: bool) -> None:
        """Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: a
        hardveres bring-up (connect/identify, BQ negotiate) alatt korábban
        az Emergency Stop gomb is tiltva maradt (csak set_status()-tól
        függött, ami csak a worker indulása UTÁN tüzel) — a kezelőnek nem
        volt módja jelezni, ha egy elakadt kapcsolódási kísérletet meg
        akar szakítani. l. main_window.py::_start_test()/_emergency_stop()."""
        self._emstop_btn.setEnabled(enabled)

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

    @Slot(dict)
    def append_device_error(self, err: dict) -> None:
        """CF-29 (governance-v2, DFAM-012 rokona): l. __init__ indoklása.
        Nem duplikálja a device_error_panel.py teljes hibalistáját —
        csak egy kompakt élő számláló, ami jelzi, hogy TÖRTÉNT SCPI hiba,
        és az operátort a háttérfülre irányítja részletekért."""
        self._device_err_count += 1
        self._device_err_lbl.setText(str(self._device_err_count))
        self._device_err_lbl.setStyleSheet(
            "font-size: 15px; padding: 5px; color: #b71c1c; font-weight: bold;"
        )

    @Slot(dict)
    def update_psu_regulation(self, state: dict) -> None:
        """Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói
        döntés 2026-09-10): a PSU CV/CC státuszát és annak SERIES-korlátját
        jeleníti meg -- a `worker.psu_regulation_ready` szignálra kötve
        (l. main_window.py bekötés), NEM az `update_sample()` tick-
        ciklusára. TISZTÁN informatív kijelzés -- SZÁNDÉKOSAN nem kap
        piros/hiba-stílust, mert a CV state normál üzemben (töltés
        abszorpciós/float fázisa) a VÁRT, nem hiba-jellegű állapot (l.
        test_runner.py::_poll_psu_regulation_state() docstringje, a
        HW-R5-A tanulsága miatt)."""
        if not state.get("ON"):
            text = "OFF"
        elif state.get("CVCC_VALID") is False:
            text = "ON (SERIES: CV/CC n/a)"
        elif state.get("CV") and state.get("CC"):
            text = "CV+CC"
        elif state.get("CV"):
            text = "CV"
        elif state.get("CC"):
            text = "CC"
        else:
            text = "ON"
        self._psu_regulation_lbl.setText(text)

    @Slot()
    def reset_device_error_count(self) -> None:
        """Review-kör 4/5 GUI F5 (P2, DFAM-012 rokona): korábban a
        DeviceErrorPanel "Törlés" gombja csak a HÁTTÉRFÜL saját listáját/
        számlálóját nullázta -- ez a kompakt élő számláló semmilyen módon
        nem értesült róla, tartósan eltérő (a törlés előtti, kumulatív)
        értéket mutatva a háttérfül "0 hiba" állapotához képest. Külön
        metódus (nem csak a reset_plots()-ba ágyazva), hogy a
        DeviceErrorPanel.cleared jele (l. main_window.py bekötés)
        KIZÁRÓLAG ezt a számlálót nullázza, ne a teljes élő nézetet (grafikonok/
        címkék) egy sima mid-session Törlés-kattintásnál."""
        self._device_err_count = 0
        self._device_err_lbl.setText("0")
        self._device_err_lbl.setStyleSheet(_LIVE_LABEL_BASE_STYLE)

    def reset_plots(self) -> None:
        for q in (self._xs, self._u_batt, self._i_signed, self._temp,
                  self._charge_ah, self._discharge_ah):
            q.clear()
        for curve in (self._curve_ubatt, self._curve_isign, self._curve_temp,
                      self._curve_ch_ah, self._curve_dch_ah):
            curve.setData([], [])
        self._fault_lbl.setText("–")
        self._fault_lbl.setStyleSheet("font-size: 15px; padding: 5px;")
        # Review-kör 2 (2026-09-09), friss GUI/UX F8 (P3, DFAM-001 sibling-
        # omission a 17. kör fixjéhez képest): az "Állapot" címke (set_status()
        # által írt _state_lbl) korábban kimaradt a 17. kör reset-listájából
        # -- egy FAULT-tal zárult session után Start-ra kattintva a mező a
        # bring-up teljes ideje alatt (több másodperc) az ELŐZŐ session
        # terminális állapotát (pl. piros "FAULT") mutatta, mielőtt bármely
        # új adat érkezett volna.
        self._state_lbl.setText("IDLE")
        self._state_lbl.setStyleSheet("font-size: 15px; padding: 5px;")
        # Teljes kódbázis review 17. kör (2026-09-05), GUI/UX/Állapot P2
        # lelet: a státusz-sor 7 címkéje korábban nem reset-elődött itt —
        # egy új session induláskor az ELŐZŐ session utolsó értékei
        # látszottak az első update_sample() tickig, valós, de már nem
        # aktuális adatot sugallva.
        self._ubatt_lbl.setText("– V")
        self._isign_lbl.setText("– A")
        self._temp_lbl.setText("– °C")
        self._charge_lbl.setText("– Ah")
        self._disch_lbl.setText("– Ah")
        self._psumode_lbl.setText("–")
        self._psu_regulation_lbl.setText("–")
        self._warn_lbl.setText("–")
        for lbl in (self._rb1s_lbl, self._rb10s_lbl, self._rb30s_lbl):
            lbl.setText("– mΩ")
            # Kör 19 D7 (GUI-R19-01): egy korábbi session időzítés-
            # degradált stílusa ne öröklődjön át az új session első
            # update_sample()-hívásáig. Kör 21 F14 (F21-G2): a reset a
            # címkék SAJÁT (22px/bold/lila) alapstílusára áll vissza,
            # NEM a generikus 15px-es állapotsor-stílusra.
            lbl.setStyleSheet(_RB_LABEL_BASE_STYLE)
        self._rb_dv_lbl.setText("– V")
        self._udrop_lbl.setText("– V")
        self._upsu_set_lbl.setText("– V")
        self._iload_set_lbl.setText("– A")
        self._dvdt_lbl.setText("– mV/min")
        self._dadt_lbl.setText("– mA/min")
        self._phase_lbl.setText("–")
        self._step_info_lbl.setText("step –/–")
        self._step_time_lbl.setText("–:–:–")
        self._total_time_lbl.setText("–:–:–")
        # CF-29 (governance-v2, DFAM-012 rokona): új session induláskor az
        # eszközhiba-számláló is nullázódik (device_error_panel.clear()
        # mintáját követve, l. main_window.py).
        self.reset_device_error_count()
        self._step_start_perf = 0.0
        self._current_step_kind = ""
        # CF-27 (governance-v2, DFAM-001): új session induláskor a
        # staleness-állapot is nullázódik -- l. _refresh_staleness().
        self._last_sample_perf = None
        self._invalid_marked_labels = []
        self._clear_staleness()
        self._event_log.clear()
        # Kör 19 D7 (GUI-R19-02): l. __init__ kommentje -- egy előző
        # session utolsó figyelmeztetés-kódja ne nyomja el ugyanannak a
        # kódnak az ÚJ session első előfordulását.
        self._last_forwarded_warning_code = None
