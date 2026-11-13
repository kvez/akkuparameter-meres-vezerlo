"""_search_visa_resources() unit tesztek — mock pyvisa, nincs valós műszer."""
import sys
import time
from unittest.mock import MagicMock


def _make_mock_rm(resources, idn="KEITHLEY,2220-30-1,MOCK,1.0", idn_exc=None):
    rm = MagicMock()
    rm.list_resources.return_value = list(resources)
    inst = MagicMock()
    if idn_exc:
        inst.query.side_effect = idn_exc
    else:
        inst.query.return_value = idn
    rm.open_resource.return_value = inst
    return rm


def test_search_returns_idn_for_each_resource(monkeypatch):
    mock_rm = _make_mock_rm(["USB0::INSTR", "TCPIP::INSTR"])
    mock_pyvisa = MagicMock()
    mock_pyvisa.ResourceManager.return_value = mock_rm
    monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

    from Prog.gui.panels.device_search_dialog import _search_visa_resources
    results = _search_visa_resources()

    assert len(results) == 2
    assert all("KEITHLEY" in idn for _, idn, _ok in results)
    assert all(ok is True for _, _idn, ok in results)


def test_search_handles_idn_timeout(monkeypatch):
    mock_rm = _make_mock_rm(["USB0::INSTR"], idn_exc=Exception("timeout"))
    mock_pyvisa = MagicMock()
    mock_pyvisa.ResourceManager.return_value = mock_rm
    monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

    from Prog.gui.panels.device_search_dialog import _search_visa_resources
    results = _search_visa_resources()

    assert len(results) == 1
    resource, msg, ok = results[0]
    assert "sikertelen" in msg.lower() or "timeout" in msg.lower()
    # Kör 19 D8f (GUI-R19-05, DFAM-018): a resource NEVE megmarad
    # ("USB0::INSTR"), csak az `ok` mező jelzi a sikertelenséget -- ez a
    # tényleges rekeszt, amit a régi kód (a resource nevéből, "ERROR"/
    # "INFO" szűréssel, "azonosítottnak" tekintett volna.
    assert resource == "USB0::INSTR"
    assert ok is False


def test_search_handles_pyvisa_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyvisa", None)

    import importlib
    import Prog.gui.panels.device_search_dialog as mod
    importlib.reload(mod)
    results = mod._search_visa_resources()

    assert len(results) == 1
    assert results[0][0] == "ERROR"
    assert "NI-VISA" in results[0][1] or "pyvisa" in results[0][1]
    assert results[0][2] is False


def test_search_handles_no_resources(monkeypatch):
    mock_rm = _make_mock_rm([])
    mock_pyvisa = MagicMock()
    mock_pyvisa.ResourceManager.return_value = mock_rm
    monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

    from Prog.gui.panels.device_search_dialog import _search_visa_resources
    results = _search_visa_resources()

    assert len(results) == 1
    assert results[0][0] == "INFO"
    assert results[0][2] is False


class TestD8fFailedIdnNotCountedAsIdentified:
    """[regresszió, Kör 19 D8f, GUI-R19-05, DFAM-018 4. előfordulása ebben
    a körben] a végső "Kész — N eszköz azonosítva" szöveg korábban a
    resource NEVÉBŐL ("ERROR"/"INFO" szűrés) következtetett sikerre --
    egy VALÓS resource sikertelen *IDN? lekérdezése (timeout/protokollhiba)
    a resource nevét megtartja, tehát hamisan "azonosítottnak" számított
    volna. Ez a teszt a régi kódon BUKNA: 2 timeout-oló műszer esetén a
    régi logika "Kész — 2 eszköz azonosítva"-t mutatott volna."""

    def _wait_for_close_enabled(self, qapp, dlg, timeout_s: float = 3.0) -> None:
        deadline = time.monotonic() + timeout_s
        while dlg._close_btn.isEnabled() is False and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

    def test_status_label_excludes_timed_out_resources_from_count(self, qapp, monkeypatch):
        rm = MagicMock()
        rm.list_resources.return_value = ["USB0::INSTR", "USB1::INSTR"]
        inst = MagicMock()
        inst.query.side_effect = Exception("timeout")
        rm.open_resource.return_value = inst
        mock_pyvisa = MagicMock()
        mock_pyvisa.ResourceManager.return_value = rm
        monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

        from Prog.gui.panels.device_search_dialog import DeviceSearchDialog
        dlg = DeviceSearchDialog()
        self._wait_for_close_enabled(qapp, dlg)

        assert "0 eszköz azonosítva" in dlg._status_label.text()
        assert "2 sikertelen" in dlg._status_label.text()

    def test_status_label_counts_only_successful_idn_in_mixed_result(self, qapp, monkeypatch):
        rm = MagicMock()
        rm.list_resources.return_value = ["USB0::INSTR", "USB1::INSTR"]
        ok_inst = MagicMock()
        ok_inst.query.return_value = "KEITHLEY,2220-30-1,MOCK,1.0"
        fail_inst = MagicMock()
        fail_inst.query.side_effect = Exception("timeout")
        rm.open_resource.side_effect = [ok_inst, fail_inst]
        mock_pyvisa = MagicMock()
        mock_pyvisa.ResourceManager.return_value = rm
        monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

        from Prog.gui.panels.device_search_dialog import DeviceSearchDialog
        dlg = DeviceSearchDialog()
        self._wait_for_close_enabled(qapp, dlg)

        assert "1 eszköz azonosítva" in dlg._status_label.text()
        assert "1 sikertelen" in dlg._status_label.text()

    def test_status_label_omits_failed_count_when_all_succeed(self, qapp, monkeypatch):
        rm = MagicMock()
        rm.list_resources.return_value = ["USB0::INSTR"]
        inst = MagicMock()
        inst.query.return_value = "KEITHLEY,2220-30-1,MOCK,1.0"
        rm.open_resource.return_value = inst
        mock_pyvisa = MagicMock()
        mock_pyvisa.ResourceManager.return_value = rm
        monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

        from Prog.gui.panels.device_search_dialog import DeviceSearchDialog
        dlg = DeviceSearchDialog()
        self._wait_for_close_enabled(qapp, dlg)

        assert "1 eszköz azonosítva" in dlg._status_label.text()
        assert "sikertelen" not in dlg._status_label.text()


class TestReview2ndRoundCloseEventProtection:
    """Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P3 lelet (F5): a
    "Bezár" gomb korábban letiltva volt a keresés alatt, de az Escape
    (reject()) bármikor elérhető volt, és — a bq_calibration_dialog.py F4
    leletéhez hasonlóan — NEM váltott ki QCloseEvent-et, tehát semmi nem
    várta meg/védte a még futó _SearchWorker QThread-et."""

    def _patch_slow_pyvisa(self, monkeypatch, delay_s: float = 0.3):
        def _slow_list_resources():
            time.sleep(delay_s)
            return []

        mock_rm = MagicMock()
        mock_rm.list_resources.side_effect = _slow_list_resources
        mock_pyvisa = MagicMock()
        mock_pyvisa.ResourceManager.return_value = mock_rm
        monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

    def test_close_event_blocked_while_search_running(self, qapp, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        self._patch_slow_pyvisa(monkeypatch)
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

        from Prog.gui.panels.device_search_dialog import DeviceSearchDialog
        dlg = DeviceSearchDialog()
        assert dlg._thread.isRunning() is True

        class _FakeEvent:
            def __init__(self):
                self.ignored = False

            def ignore(self):
                self.ignored = True

            def accept(self):
                pass

        event = _FakeEvent()
        dlg.closeEvent(event)
        assert event.ignored is True

        deadline = time.monotonic() + 3.0
        while dlg._thread is not None and dlg._thread.isRunning() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

    def test_reject_triggers_closeevent_after_search_done(self, qapp, monkeypatch):
        """[F4-mintakövetés] reject() (Escape) self.close()-ra köt — ez
        valódi QCloseEvent-et generál, ami lefuttatja a closeEvent()
        védelmet, nem kerüli meg azt."""
        mock_rm = MagicMock()
        mock_rm.list_resources.return_value = []
        mock_pyvisa = MagicMock()
        mock_pyvisa.ResourceManager.return_value = mock_rm
        monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

        from Prog.gui.panels.device_search_dialog import DeviceSearchDialog
        dlg = DeviceSearchDialog()

        deadline = time.monotonic() + 3.0
        while dlg._close_btn.isEnabled() is False and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert dlg._close_btn.isEnabled() is True

        close_calls = []
        dlg.close = lambda: close_calls.append(True)

        dlg.reject()

        assert close_calls == [True]


_F30_CLOSE_CHAIN_SCRIPT = '''
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication([])
QMessageBox.information = staticmethod(lambda *a, **k: None)  # sosem blokkol

import Prog.gui.panels.device_search_dialog as m

m._search_visa_resources = lambda: [("INFO", "nincs eszkoz (teszt)", False)]
dlg = m.DeviceSearchDialog()
dlg.show()
thread_ref = dlg._thread
deadline = time.monotonic() + 10.0
while time.monotonic() < deadline:
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    if dlg._close_btn.isEnabled() and not shiboken6.isValid(thread_ref):
        break
    time.sleep(0.01)
if not (dlg._close_btn.isEnabled() and not shiboken6.isValid(thread_ref)):
    print("PRECONDITION_FAILED", flush=True)
    sys.exit(3)
print("THREAD_FINISHED_AND_DELETED", flush=True)

mode = sys.argv[1]
if mode == "close":
    dlg.close()
elif mode == "reject":
    dlg.reject()
elif mode == "close_button":
    dlg._close_btn.click()
else:
    print("UNKNOWN_MODE", flush=True)
    sys.exit(4)
app.processEvents()
print(f"RETURNED visible={dlg.isVisible()}", flush=True)
sys.exit(0)
'''


class TestR1F30CloseAfterFinishedSearchThreadDeleted:
    """[regresszió, R1 F-30, NEW_FAMILY qt_object_deleted_but_python_
    reference_retained] A kereső QThread a `finished` jelre deleteLater()-rel
    törlődik, a Python-referencia (self._thread) korábban megmaradt: egy
    befejezett keresés UTÁNI bezárás (close()/Escape=reject()/"Bezár" gomb)
    a closeEvent()-ben a TÖRÖLT C++ objektumon hívta az isRunning()-ot --
    közvetlen hívásnál RuntimeError, a valódi Qt-láncon processz-összeomlás.
    A referencia MOSTANTÓL a QThread.finished-en nullázódik (a deleteLater
    ELŐTT kötött slotban), a result_ready-n NEM."""

    @staticmethod
    def _patch_instant_search(monkeypatch):
        import Prog.gui.panels.device_search_dialog as m
        monkeypatch.setattr(
            m, "_search_visa_resources", lambda: [("INFO", "nincs eszkoz (teszt)", False)],
        )
        return m

    def test_direct_close_event_after_thread_deferred_delete_is_accepted(self, qapp, monkeypatch):
        import shiboken6
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtGui import QCloseEvent

        m = self._patch_instant_search(monkeypatch)
        dlg = m.DeviceSearchDialog()
        thread_ref = dlg._thread
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            qapp.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if dlg._close_btn.isEnabled() and not shiboken6.isValid(thread_ref):
                break
            time.sleep(0.01)
        assert dlg._close_btn.isEnabled() is True
        # Nem-vákuum előfeltétel: a QThread C++ objektuma TÉNYLEGESEN törölve.
        assert not shiboken6.isValid(thread_ref)

        event = QCloseEvent()
        event.ignore()
        dlg.closeEvent(event)

        assert event.isAccepted() is True
        assert dlg._thread is None

    @staticmethod
    def _run_close_chain_in_subprocess(mode: str, tmp_path) -> None:
        """D-B9-1: a valódi close()/reject() (Escape)/"Bezár" gomb lánc
        KÜLÖN alfolyamatban (a régi kód natív összeomlása a pytest-processzt
        nem döntheti le): igazolt szálbefejezés + DeferredDelete után a
        bezárás normál visszatérés, a dialógus rejtett."""
        import os
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        script = tmp_path / "f30_close_chain.py"
        script.write_text(_F30_CLOSE_CHAIN_SCRIPT, encoding="utf-8")
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PYTHONPATH"] = str(repo_root)
        proc = subprocess.run(
            [sys.executable, str(script), mode],
            cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=60, check=False,
        )

        assert "THREAD_FINISHED_AND_DELETED" in proc.stdout, (proc.stdout, proc.stderr)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "RETURNED visible=False" in proc.stdout, (proc.stdout, proc.stderr)

    def test_real_close_after_thread_deleted_does_not_crash(self, tmp_path):
        self._run_close_chain_in_subprocess("close", tmp_path)

    def test_real_reject_escape_after_thread_deleted_does_not_crash(self, tmp_path):
        self._run_close_chain_in_subprocess("reject", tmp_path)

    def test_real_close_button_click_after_thread_deleted_does_not_crash(self, tmp_path):
        self._run_close_chain_in_subprocess("close_button", tmp_path)

    def test_close_still_blocked_when_result_arrived_but_thread_still_running(
        self, qapp, monkeypatch,
    ):
        """Az eredmény (result_ready) már megérkezett, a szál MÉG fut: a
        bezárási kapu továbbra is tilt, és a referencia NEM nullázódik
        (result_ready-nél nullázni tilos)."""
        import threading

        import shiboken6
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QCloseEvent

        m = self._patch_instant_search(monkeypatch)
        release = threading.Event()

        class _BlockingAfterResultWorker(m._SearchWorker):
            """Az örökölt, valódi run() fut a worker-szálon; a worker SAJÁT
            `finished` jelére a konstruktorban (a dialógus quit/deleteLater
            kapcsolatai ELŐTT) kötött DirectConnection hívás a worker-szálon
            blokkol -- a result_ready már kézbesült, a szál még fut. (Egy
            Python-alosztályban felülírt run() slot PySide6 alatt a GUI-
            szálon futna, ezért nem azt írjuk felül.)"""

            def __init__(self) -> None:
                super().__init__()
                self.finished.connect(
                    lambda: release.wait(10.0), Qt.ConnectionType.DirectConnection,
                )

        monkeypatch.setattr(m, "_SearchWorker", _BlockingAfterResultWorker)
        dlg = m.DeviceSearchDialog()
        thread_ref = dlg._thread
        try:
            deadline = time.monotonic() + 5.0
            while dlg._close_btn.isEnabled() is False and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.01)
            assert dlg._close_btn.isEnabled() is True  # az eredmény megérkezett
            assert thread_ref.isRunning() is True

            event = QCloseEvent()
            dlg.closeEvent(event)

            assert event.isAccepted() is False
            assert dlg._thread is thread_ref
        finally:
            release.set()
            # A worker.finished -> thread.quit kapcsolat a GUI-szálra queued:
            # pumpálni kell, NEM blokkolóan wait()-elni.
            deadline = time.monotonic() + 5.0
            while (
                shiboken6.isValid(thread_ref) and thread_ref.isRunning()
                and time.monotonic() < deadline
            ):
                qapp.processEvents()
                time.sleep(0.01)


class TestR1F30FixupConnectionOrderAndThreadFinishedBinding:
    """[regresszió, R1 B9 fixup -- V-B9-01 (Codex verifier) és P3-1 (Claude
    verifier)] Az F-30 két invariánsát pinneli, amelyet a meglévő tesztek
    nem diszkrimináltak:
    (1) a nullázó slot a QThread.finished-en a deleteLater ELŐTT kötött --
        a két queued posztolás NEM atomi, a worker-szál a kettő között
        preemptálódhat, ezért a fordított sorrend egy "QThread törölve, de
        self._thread még hivatkozik rá" ablakot nyit;
    (2) a nullázás a QThread.finished-re kötött, NEM a worker finished/
        result_ready jelére -- a worker kész, de a szál még fut: a
        referencia megmarad, a bezárás tilt.
    Produkciós kód nem változik: az ütemezést a teszt kizárólag a modul
    QThread-nevének és a worker jelének műszerezésével kényszeríti ki."""

    _GATE_TIMEOUT_S = 5.0

    def test_reference_nulled_before_thread_deferred_delete_when_emission_preempted(
        self, qapp, monkeypatch,
    ):
        """V-B9-01: egy DirectConnection-kapu a QThread.finished ELSŐ
        kapcsolata UTÁN blokkolja a worker-szálat (szimulált preempció a
        két queued posztolás között). A kapu tartása alatt a GUI csak az
        első kapcsolat posztját kézbesítheti:
        - jelenlegi sorrend (nullázó slot ELŐBB): a referencia a kapu
          tartása ALATT nullázódik, a QThread C++ objektuma még él, a
          bezárás elfogadott;
        - fordított sorrend (deleteLater ELŐBB): a QThread törlődik,
          mialatt a self._thread még hivatkozik rá -- a closeEvent()
          RuntimeError-t dob (a valódi Qt-láncon processz-összeomlás), és
          a nullázás csak a kapu elengedése UTÁN posztolódhat."""
        import threading

        import shiboken6
        from PySide6.QtCore import QCoreApplication, QEvent, Qt, QThread
        from PySide6.QtGui import QCloseEvent

        import Prog.gui.panels.device_search_dialog as m

        monkeypatch.setattr(
            m, "_search_visa_resources", lambda: [("INFO", "nincs eszkoz (teszt)", False)],
        )
        gate_entered = threading.Event()
        gate_exited = threading.Event()
        release = threading.Event()
        gate_timeout_s = self._GATE_TIMEOUT_S

        def _preempt_gate():
            # A worker-szálon fut (DirectConnection), a `finished` emisszión
            # belül, az első kapcsolat posztja UTÁN, a második ELŐTT.
            gate_entered.set()
            release.wait(gate_timeout_s)
            gate_exited.set()

        proxies = []

        class _FinishedConnectProxy:
            """A dialógus `self._thread.finished.connect(...)` hívásait
            továbbítja a valódi jelre; az ELSŐ kapcsolat után beköti a
            kaput -- így a kapu pontosan a két dialógus-kapcsolat közé
            kerül, bármelyik sorrendben is kötöttek."""

            def __init__(self, real):
                self._real = real
                self.connect_count = 0

            def connect(self, *args, **kwargs):
                result = self._real.connect(*args, **kwargs)
                self.connect_count += 1
                if self.connect_count == 1:
                    self._real.connect(_preempt_gate, Qt.ConnectionType.DirectConnection)
                return result

        def _instrumented_thread(parent=None):
            thread = QThread(parent)
            proxy = _FinishedConnectProxy(thread.finished)
            thread.finished = proxy
            proxies.append(proxy)
            return thread

        monkeypatch.setattr(m, "QThread", _instrumented_thread)
        dlg = m.DeviceSearchDialog()
        thread_ref = dlg._thread
        nulled_observed = False
        nulled_while_gate_held = None
        cpp_valid_at_null = None
        close_accepted_at_null = None
        deleted_while_referenced = False
        window_close_error = None
        try:
            # Nem-vákuum előfeltétel: a dialógus pontosan két kapcsolatot
            # köt a QThread.finished-re, a kapu a kettő között ül.
            assert len(proxies) == 1
            assert proxies[0].connect_count == 2
            deadline = time.monotonic() + 3.0 * gate_timeout_s
            while time.monotonic() < deadline:
                qapp.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                if dlg._thread is None:
                    nulled_observed = True
                    nulled_while_gate_held = gate_entered.is_set() and not gate_exited.is_set()
                    cpp_valid_at_null = shiboken6.isValid(thread_ref)
                    event = QCloseEvent()
                    event.ignore()
                    dlg.closeEvent(event)
                    close_accepted_at_null = event.isAccepted()
                    break
                if not shiboken6.isValid(thread_ref):
                    # A fordított sorrend ablaka: a QThread törölve, a
                    # referencia még nem nullázódott.
                    deleted_while_referenced = True
                    try:
                        dlg.closeEvent(QCloseEvent())
                    except RuntimeError as exc:
                        window_close_error = exc
                    break
                time.sleep(0.001)
        finally:
            release.set()
            deadline = time.monotonic() + 5.0
            while shiboken6.isValid(thread_ref) and time.monotonic() < deadline:
                qapp.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                time.sleep(0.01)

        assert gate_entered.is_set() is True  # az ütemezés ténylegesen kikényszerült
        assert deleted_while_referenced is False, window_close_error
        assert nulled_observed is True
        assert nulled_while_gate_held is True
        assert cpp_valid_at_null is True
        assert close_accepted_at_null is True
        # A kapu elengedése után a QThread is ténylegesen törlődött.
        assert not shiboken6.isValid(thread_ref)

    def test_reference_kept_when_worker_finished_but_thread_still_running(
        self, qapp, monkeypatch,
    ):
        """P3-1: a keresés kapuzott, így a dialógus SAJÁT kapcsolatai UTÁN
        köthető egy DirectConnection-tartás a worker `finished` jelére. A
        tartás alatt a worker.finished (és a korábbi result_ready) minden,
        a dialógus által kötött queued hívása már posztolva van és a GUI
        kézbesíti őket, a szál viszont még fut. Helyes (QThread.finished)
        kötésnél a referencia megmarad és a bezárás tilt; worker.finished-
        vagy result_ready-re kötött nullázásnál a referencia nullázódna, és
        a futó szál mellett a bezárás átmenne."""
        import threading

        import shiboken6
        from PySide6.QtCore import QCoreApplication, QEvent, Qt
        from PySide6.QtGui import QCloseEvent

        import Prog.gui.panels.device_search_dialog as m

        search_gate = threading.Event()
        hold_entered = threading.Event()
        release = threading.Event()

        def _gated_search():
            search_gate.wait(10.0)
            return [("INFO", "nincs eszkoz (teszt)", False)]

        def _hold():
            hold_entered.set()
            release.wait(10.0)

        monkeypatch.setattr(m, "_search_visa_resources", _gated_search)
        dlg = m.DeviceSearchDialog()
        thread_ref = dlg._thread
        try:
            # A dialógus kapcsolatai (result_ready, quit, deleteLater) UTÁN.
            dlg._worker.finished.connect(_hold, Qt.ConnectionType.DirectConnection)
            search_gate.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not (
                hold_entered.is_set() and dlg._close_btn.isEnabled()
            ):
                qapp.processEvents()
                time.sleep(0.005)
            assert hold_entered.is_set() is True
            assert dlg._close_btn.isEnabled() is True  # az eredmény megérkezett
            # A tartás ELŐTT posztolt összes queued hívás kézbesítése.
            for _ in range(5):
                qapp.processEvents()
            assert thread_ref.isRunning() is True

            event = QCloseEvent()
            dlg.closeEvent(event)

            assert dlg._thread is thread_ref
            assert event.isAccepted() is False
        finally:
            search_gate.set()
            release.set()
            deadline = time.monotonic() + 5.0
            while shiboken6.isValid(thread_ref) and time.monotonic() < deadline:
                qapp.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                time.sleep(0.01)
