"""PySide6 offscreen tesztkörnyezet."""
import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_blocking_message_boxes(monkeypatch):
    """Review-kör 2 (2026-09-09), friss GUI/UX F1: a "Kész" popup
    (main_window.py::_on_finished()) mostantól QMessageBox.warning-ot
    használ (nem csak .information-t) DEGRADED minősítésnél -- ami
    KORÁBBAN is bekövetkezett bármilyen nem-DONE terminális státusznál
    (capacity_result_quality régóta "status != DONE" -> DEGRADED), csak
    eddig ez sosem futtatott le semmilyen popupot. Több, KORÁBBAN írt
    teszt csak a .information-t patch-elte -- egy ilyen tesztben most egy
    VALÓDI, blokkoló QMessageBox.warning() nyílna meg egy fej nélküli
    (offscreen) Qt event loopban, lefagyasztva a teljes tesztfutást (ez a
    hatás ténylegesen reprodukálódott egy -k szűrt tesztfutásnál a
    test_build_runner.py fájlban -- a pontos hibás teszt nem került külön
    izolálásra, mert a globális fixture ezt strukturálisan kizárja). Ez az
    autouse fixture
    biztonságos no-op alapértéket ad mindkét metódusnak minden GUI teszthez;
    az egyes tesztek saját, KÉSŐBBI monkeypatch.setattr()-jükkel felülírják,
    ha kifejezetten a popup tartalmát akarják ellenőrizni (a monkeypatch
    ugyanarra a célra tett későbbi setattr()-je nyer az azonos tesztfüggvényen
    belül, a teardown mindkettőt automatikusan visszaállítja)."""
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    # D6 (2026-09-13, MAX verifier 1. kör F3 javítás utáni hang-lelet): a
    # `Prog/main.py::_install_emergency_excepthook()` bizonytalan safe-off
    # esetén `QMessageBox.critical()`-t hív -- ez a fenti kettővel azonos
    # hibaosztály (valódi, blokkoló modális dialógus egy offscreen teszt
    # alatt, user-interakció nélkül -- fagyasztja a teljes tesztfutást),
    # csak egy HARMADIK QMessageBox-metódust érintve, amit az autouse
    # fixture korábban nem fedett.
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
