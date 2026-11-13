"""Akkuteszter — Labor műszerfal belépési pont."""
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from Prog import app_paths
from Prog.gui.main_window import MainWindow


def _install_emergency_excepthook(window: MainWindow) -> None:
    """D6 (2026-09-13, felhasználói döntés, l. DECISION_REGISTER.md
    GOV-023): egy a GUI/main-thread szálon kezeletlenül maradt kivétel
    (pl. egy panel-slot hívás közben) alapállapotban NEM vezet safe-off-
    hoz — a Qt eseményhurok elnyeli a kivételt, a `sys.excepthook`
    kiírja stderr-re, az alkalmazás pedig FUT TOVÁBB: az operátor egy
    élő, de belsőleg már megbízhatatlan állapotú sessiont láthat. A
    worker-szálon futó `TestRunner.run()`-t már véd egy saját
    try/except (`worker.py::_do_run()`); ez a hook a MÁSIK, addig
    fedetlen réteget (GUI/main-thread) fedi, CLAUDE.md §6/§10 szerint.

    Csak `main()`-ből hívva installálódik (sosem modul-import időben),
    ezért teszt alatt (ahol a tesztek nem hívják a `main()`-t) nem
    aktiválódik — egy pytest alatt elkapott kivétel kezelését nem
    befolyásolja.

    MAX independent verifier 1. kör (2026-09-13) két blokkoló leletet
    talált az eredeti verzióban, mindkettő itt javítva:

    F1 — ha AKTÍV session fut (`window._worker is not None`), a
    kezeletlen kivétel PONT akkor érheti a main-threadet, amikor a
    worker-szál ugyanazokon a drivereken dolgozik (CV/CC-szabályzás,
    mintavétel) — egy közvetlen `_perform_gui_safe_off()`-hívás innen
    konkurens, szerializálatlan eszköz-hozzáférést okozna (a driverek
    nem lockoltak), és megvakítaná a PSU/Load interlockot
    (`safety.py::check_concurrent_psu_load()`), mert az `output_
    commanded_on`/`input_commanded_on` flageket OFF-ra állítaná egy
    esetlegesen csak RÉSZBEN sikeres parancs után is, míg a worker
    tovább futtatná a saját hurkát (az `emergency_stop_requested` flag
    nélkül). Fix: aktív worker esetén NEM közvetlenül az instrumentumra
    nyúlunk, hanem a MÁR létező, a worker SAJÁT szálán futó,
    szerializált vészleállítási útra (`TestRunner.request_emergency_
    stop()` -> `_emergency_stop()` -> `_abort_active_controller()` +
    `_safe_all_off_with_uncertainty_check()`) tesszük át a döntést — ez
    a mechanizmus már HIL-ellenőrzött és a `_fault_reason`
    taxonómiáját is helyesen tartja (l. `"UNHANDLED_EXCEPTION: "`
    prefix, `test_runner.py:824` és
    `traditional_learning_controller.py::_classify_result()` —
    ugyanezt a prefixet használjuk itt is, hogy a riport ne minősítse
    ezt hamisan bizonyított BQ-tanulási kudarcnak). A közvetlen
    `_perform_gui_safe_off()` csak a NINCS aktív worker ágon marad — ez
    az eredeti, dokumentáltan biztonságos előfeltétel
    (`main_window.py::_perform_gui_safe_off()` docstringje).

    F2 — a `sys.excepthook` NEM garantál main-threadet: egy
    `moveToThread`-elt QObject worker-szálán (pl.
    `bq_calibration_worker.py`, `device_search_dialog.py`) keletkező
    kezeletlen kivétel is idekerül. A safe-off-kísérlet viszont Qt-
    widgetet mutál (`_live_panel.append_event()` ->
    `QListWidgetItem`/`scrollToBottom`), ami csak a GUI-szálról
    biztonságos. Fix: a hook telepítésekor rögzített main-thread
    identitáshoz kötött explicit szálkapu — más szálról ÉRKEZŐ
    kivételnél a safe-off-kísérletet KIHAGYJUK (csak az eredeti hook
    futhat le), nem próbálunk Qt-objektumot más szálról elérni.

    MAX independent verifier 2. kör (2026-09-13) egy harmadik, ÚJ
    leletet talált (F-A, P2): az `window._worker is not None` feltétel
    egy OBJEKTUM LÉTEZÉSÉT azonosítja a MÖGÖTTE futó tick-hurok
    ÉLETBEN LÉTÉVEL — de ez a két dolog NEM ugyanaz. Ha a worker-szál
    `run()`-ja MÁR visszatért, és a session egy resumable BQ
    checkpointnál PARKOL (`window._pending_checkpoint_result is not
    None` -- l. `main_window.py::closeEvent()` pontosan ugyanerről a,
    korábban MÁR egyszer feltárt és javított hibaosztályról szóló
    dokumentált esete), a `request_emergency_stop()` csak egy flaget
    állítana egy dormant objektumon, amit SOHA senki nem olvasna ki —
    az operátor "VÉSZLEÁLLÍTÁS kérve" üzenetet látna, de valós safe-off
    NEM történne. A fix a closeEvent() BEVÁLT, dokumentált mintáját
    replikálja: a delegálás csak akkor jogos, ha a worker ÉS a hurok is
    valóban él (a checkpoint nem parkol) — egyébként a közvetlen
    `_perform_gui_safe_off()` a helyes út (pont ennek az állapotnak az
    eredeti, dokumentált előfeltétele).
    """
    previous_hook = sys.excepthook
    main_thread_ident = threading.get_ident()

    def _emergency_hook(exc_type, exc_value, exc_tb) -> None:
        if threading.get_ident() == main_thread_ident:
            try:
                if window._worker is None and (
                    window._connect_worker is not None
                    or window._bring_up_in_progress
                ):
                    # R2-B3 (R2-SES-01, DFAM-001): a bring-up a connect-szal
                    # utan, az _on_connect_ready()/_build_runner() (resume-
                    # modal) alatt is tart -- ugyanaz a csak-flag ag.
                    # R1 B6, F-07 (DFAM-001): a bring-up (connect/identify)
                    # egy külön QThread-en fut -- a korábbi `else` ág itt a
                    # KORÁBBI manageren adott ki safe-offot (konkurens
                    # műszer-I/O), és a kapcsolódás után a session ELINDULT.
                    # Ugyanaz, mint a "Safe Off" gomb ebben a fázisban
                    # (main_window.py::_emergency_stop()): csak a flag, a
                    # lezárást a _on_connect_ready() abort-ága végzi. Nincs
                    # műszer-I/O; _on_connect_failed() is nullázza a flaget.
                    window._connect_abort_requested = True
                    window._status_bar.showMessage(
                        "VÉSZLEÁLLÍTÁS kérve (kezeletlen kivétel a GUI-szálon) — "
                        "a kapcsolódás befejezése után a műszerek azonnal "
                        "lezáródnak, a session NEM indul el."
                    )
                elif (
                    window._worker is not None
                    and window._pending_checkpoint_result is None
                ):
                    window._worker.request_emergency_stop(
                        f"UNHANDLED_EXCEPTION: {exc_value}"
                    )
                    window._status_bar.showMessage(
                        "VÉSZLEÁLLÍTÁS kérve (kezeletlen kivétel a "
                        "GUI-szálon)…"
                    )
                else:
                    attempted, result = window._perform_gui_safe_off(
                        "UNHANDLED_EXCEPTION_MAIN_THREAD"
                    )
                    uncertain = attempted and any(
                        not result.get(name, False) for name in ("Load", "PSU")
                    )
                    if uncertain:
                        # F3 (MAX verifier 1. kör): egy nem igazoltan
                        # sikeres OFF ezen az úton (nincs aktív worker,
                        # tehát nincs report.json, ami perzisztálná)
                        # sosem maradhat kizárólag az élő eseménynaplóban
                        # — a modális dialógus garantáltan látható marad.
                        QMessageBox.critical(
                            window, "VÉSZLEÁLLÍTÁS — bizonytalan safe-off",
                            "Kezeletlen hiba után a Load/PSU kikapcsolása "
                            "NEM igazoltan sikeres. Ellenőrizd a "
                            "műszereket manuálisan!",
                        )
            except Exception:
                # A másodlagos hiba is a diagnosztikai naplóba kerül;
                # az eredeti kivétel jelentése és a hook láncolása megmarad.
                logging.getLogger(__name__).exception("A kezeletlen kivétel safe-off-kísérlete sikertelen")
        logging.getLogger(__name__).critical("Kezeletlen kivétel", exc_info=(exc_type, exc_value, exc_tb))
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _emergency_hook


def main() -> None:
    try:
        log_dir = app_paths.exe_dir() / "logs"
        log_path = (log_dir / "akkuteszter.log").resolve()
        root_logger = logging.getLogger()
        if not any(
            isinstance(existing, logging.FileHandler)
            and existing.get_name() == "akkuteszter.file"
            and Path(existing.baseFilename) == log_path
            for existing in root_logger.handlers
        ):
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.set_name("akkuteszter.file")
            handler.setLevel(logging.WARNING)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"
            ))
            root_logger.addHandler(handler)
        root_logger.setLevel(logging.WARNING)
    except OSError:
        # Írásvédett telepítésnél is induljon; ahol van stderr, a hiba látszik.
        logging.getLogger(__name__).exception("A diagnosztikai napló nem nyitható meg")
    app_paths.ensure_local_config()
    app = QApplication(sys.argv)
    app.setApplicationName("Akkuteszter")
    window = MainWindow()
    _install_emergency_excepthook(window)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
