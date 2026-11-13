"""DeviceErrorPanel unit tesztek.

[regresszió, governance-v2 review-kör 1, CF-29, GUI-R1-11, DFAM-012
rokona] a _MAX_ROWS túlcsordulásnál korábban a legrégebbi bejegyzések
NÉMÁN kiestek a QListWidget-ből -- a _count_lbl a _total-t mutatta
(ami helyesen tovább nőtt), de semmi nem jelezte explicit, hogy a
LÁTHATÓ lista rövidebb, mint a _total, azaz hogy történt törlés.
"""
from __future__ import annotations

from Prog.gui.panels.device_error_panel import _MAX_ROWS, DeviceErrorPanel


def _err(i: int) -> dict:
    return {"timestamp_iso": "2026-09-08T10:00:00", "device": "PSU", "error": f"err{i}"}


class TestCF29TruncationVisibility:
    def test_count_label_plain_below_max_rows(self, qapp):
        panel = DeviceErrorPanel()
        panel.append_device_error(_err(0))
        panel.append_device_error(_err(1))
        assert panel._count_lbl.text() == "2 hiba"

    def test_no_truncation_note_while_under_limit(self, qapp):
        panel = DeviceErrorPanel()
        for i in range(_MAX_ROWS):
            panel.append_device_error(_err(i))
        assert panel._truncated_count == 0
        assert panel._count_lbl.text() == f"{_MAX_ROWS} hiba"

    def test_truncation_note_appears_once_limit_exceeded(self, qapp):
        panel = DeviceErrorPanel()
        for i in range(_MAX_ROWS + 5):
            panel.append_device_error(_err(i))
        assert panel._truncated_count == 5
        assert panel._total == _MAX_ROWS + 5
        assert panel._list.count() == _MAX_ROWS
        text = panel._count_lbl.text()
        assert text == f"{_MAX_ROWS + 5} hiba (5 régebbi törölve, {_MAX_ROWS} látható)"

    def test_list_widget_never_exceeds_max_rows(self, qapp):
        panel = DeviceErrorPanel()
        for i in range(_MAX_ROWS + 50):
            panel.append_device_error(_err(i))
        assert panel._list.count() == _MAX_ROWS

    def test_clear_resets_truncated_count(self, qapp):
        panel = DeviceErrorPanel()
        for i in range(_MAX_ROWS + 5):
            panel.append_device_error(_err(i))
        panel.clear()
        assert panel._truncated_count == 0
        assert panel._total == 0
        assert panel._count_lbl.text() == "0 hiba"


class TestReviewRound4GuiF5ClearedSignal:
    """[regresszió, review-kör 4/5 GUI F5 (P2, DFAM-012 rokona)] a "Törlés"
    gomb korábban csak a HÁTTÉRFÜL saját listáját/számlálóját nullázta --
    a LivePanel kompakt "Eszközhiba" számlálója semmilyen módon nem
    értesült erről, tartósan eltérő (törlés előtti) értéket mutatva egy
    mid-session Törlés-kattintás után. `clear()` mostantól egy `cleared`
    jelet is kiad, amit main_window.py köt be a LivePanel-hez."""

    def test_clear_emits_cleared_signal(self, qapp):
        panel = DeviceErrorPanel()
        panel.append_device_error(_err(0))
        calls: list = []
        panel.cleared.connect(lambda: calls.append(True))

        panel.clear()

        assert calls == [True]
