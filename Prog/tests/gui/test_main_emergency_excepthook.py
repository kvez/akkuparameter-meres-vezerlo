"""D6 (2026-09-13, felhasználói döntés, l. DECISION_REGISTER.md GOV-023)
regressziós tesztek: `Prog/main.py::_install_emergency_excepthook()`.

Korábban `Prog/main.py` egyáltalán nem telepített `sys.excepthook`-ot --
egy kezeletlen Python kivétel a GUI/main-thread szálon (pl. egy panel-slot
hívás közben) alapállapotban NEM vezetett safe-off-hoz: a Qt eseményhurok
elnyelte, a `sys.excepthook` kiírta stderr-re, és az alkalmazás FUTOTT
TOVÁBB. A worker-szálon futó `TestRunner.run()`-t már védte egy saját
try/except (`worker.py::_do_run()`); ez a hook a MÁSIK, addig fedetlen
réteget (GUI/main-thread) fedi.

MAX independent verifier 1. kör (2026-09-13) két blokkoló leletet talált
az első verzióban (F1: aktív worker melletti közvetlen safe-off konkurens
eszköz-hozzáférést és interlock-megvakítást okozhatott volna; F2: a hook
más szálról érkező kivételnél is Qt-widgetet mutált volna) -- a
`TestEmergencyExcepthookWithActiveWorker`/`TestEmergencyExcepthookThreadGate`
osztályok ezeket fedik. F4: az eredeti verzióban a `main()`-wiring
tesztelve nem volt, és az import-teszt `sys.modules`-cache miatt
tautologikus volt -- l. `TestMainInstallsHookOnRealEntryPoint` és az
`importlib.reload()`-alapú import-teszt.
"""
from __future__ import annotations

import importlib
import io
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from Prog import app_paths
from Prog.gui.main_window import MainWindow
from Prog.main import _install_emergency_excepthook

from .test_build_runner import _make_cfg, _patch_hardware


def _make_window(qapp, monkeypatch, tmp_path) -> MainWindow:
    _patch_hardware(monkeypatch)
    monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
    window = MainWindow()
    window._build_runner(_make_cfg())
    return window


class TestEmergencyExcepthookAttemptsSafeOff:
    def test_unhandled_exception_triggers_safe_all_off(self, qapp, monkeypatch, tmp_path):
        """Diszkrimináló: a régi kódon (sys.excepthook telepítése nélkül) a
        `safe_all_off` sosem hívódna meg egy kezeletlen kivétel hatására."""
        window = _make_window(qapp, monkeypatch, tmp_path)
        calls: list = []
        window._instruments.safe_all_off = lambda: (
            calls.append("safe_all_off")
            or {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
        )
        previous_hook_calls: list = []
        monkeypatch.setattr(sys, "excepthook", lambda *a: previous_hook_calls.append(a))

        _install_emergency_excepthook(window)
        try:
            raise RuntimeError("szimulált kezeletlen kivétel a GUI-szálon")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert calls == ["safe_all_off"]

    def test_unhandled_exception_logs_safe_off_performed_event(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        window._instruments.safe_all_off = lambda: {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        events: list = []
        window._live_panel.append_event = events.append
        monkeypatch.setattr(sys, "excepthook", lambda *a: None)

        _install_emergency_excepthook(window)
        try:
            raise RuntimeError("szimulált kezeletlen kivétel a GUI-szálon")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert any(e.get("event_code") == "SAFE_OFF_PERFORMED" for e in events)

    def test_does_not_suppress_original_exception_reporting(
        self, qapp, monkeypatch, tmp_path
    ):
        """A hook NEM nyelheti el az eredeti kivétel jelentését -- az
        eredeti (korábbi) excepthooknak minden esetben meg kell kapnia a
        hívást, a safe-off-kísérlettől függetlenül."""
        window = _make_window(qapp, monkeypatch, tmp_path)
        previous_hook_calls: list = []
        monkeypatch.setattr(sys, "excepthook", lambda *a: previous_hook_calls.append(a))

        _install_emergency_excepthook(window)
        exc = RuntimeError("szimulált kezeletlen kivétel a GUI-szálon")
        try:
            raise exc
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert len(previous_hook_calls) == 1
        assert previous_hook_calls[0][1] is exc

    def test_secondary_failure_in_safe_off_does_not_crash_hook_or_suppress_report(
        self, qapp, monkeypatch, tmp_path
    ):
        """D6 tervezési kérdése: 'mi történjen, ha maga a safe-off is
        dob?' -- a válasz: a hook ne omoljon össze, és az eredeti kivétel
        jelentése (az előző excepthook hívása) ettől függetlenül
        megtörténjen."""
        window = _make_window(qapp, monkeypatch, tmp_path)

        def _broken_safe_off(context: str):
            raise ValueError("szimulált másodlagos hiba a safe-off-kísérlet alatt")

        window._perform_gui_safe_off = _broken_safe_off
        previous_hook_calls: list = []
        monkeypatch.setattr(sys, "excepthook", lambda *a: previous_hook_calls.append(a))

        _install_emergency_excepthook(window)
        exc = RuntimeError("szimulált kezeletlen kivétel a GUI-szálon")
        try:
            raise exc
        except RuntimeError:
            sys.excepthook(*sys.exc_info())  # nem szabad feljebb dobnia

        assert len(previous_hook_calls) == 1
        assert previous_hook_calls[0][1] is exc

    def test_missing_instruments_never_produce_false_positive_attestation(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        window._instruments = None
        events: list = []
        window._live_panel.append_event = events.append
        monkeypatch.setattr(sys, "excepthook", lambda *a: None)

        _install_emergency_excepthook(window)
        try:
            raise RuntimeError("szimulált kezeletlen kivétel a GUI-szálon")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert any(
            e.get("event_code") == "SAFE_OFF_SKIPPED_NO_INSTRUMENTS" for e in events
        )


class TestEmergencyExcepthookWithActiveWorker:
    """F1 (MAX verifier 1. kör): aktív session alatt a hook NEM nyúlhat
    közvetlenül az instrumentumokhoz (konkurens eszköz-hozzáférés/
    interlock-megvakítás kockázata) -- a MÁR létező, a worker saját
    szálán szerializáltan futó `request_emergency_stop()`-útra kell
    támaszkodnia."""

    def test_active_worker_routes_to_runner_emergency_stop_not_direct_safe_off(
        self, qapp, monkeypatch, tmp_path
    ):
        """Diszkrimináló: a hibás (F1 előtti) verzión a
        `request_emergency_stop` sosem hívódna, és a közvetlen
        `safe_all_off` hívódna -- pont az ellentéte a helyesnek."""
        window = _make_window(qapp, monkeypatch, tmp_path)
        direct_safe_off_calls: list = []
        window._instruments.safe_all_off = lambda: (
            direct_safe_off_calls.append("safe_all_off")
            or {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
        )
        emergency_stop_calls: list = []

        class _FakeWorker:
            def request_emergency_stop(self, reason: str) -> None:
                emergency_stop_calls.append(reason)

        window._worker = _FakeWorker()
        monkeypatch.setattr(sys, "excepthook", lambda *a: None)

        _install_emergency_excepthook(window)
        try:
            raise RuntimeError("szimulált kivétel aktív session közben")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert direct_safe_off_calls == []
        assert len(emergency_stop_calls) == 1
        assert emergency_stop_calls[0].startswith("UNHANDLED_EXCEPTION: ")

    def test_active_worker_emergency_stop_reason_matches_runner_taxonomy(
        self, qapp, monkeypatch, tmp_path
    ):
        """A `traditional_learning_controller.py::_classify_result()`
        KIZÁRÓLAG a `"UNHANDLED_EXCEPTION: "` prefixet ismeri fel
        AWAITING-ként (nem FAILED-ként) -- ha ez a prefix elromlana, a
        riport hamisan bizonyított BQ-tanulási kudarcot állítana egy
        egyszerű GUI-kivétel után."""
        window = _make_window(qapp, monkeypatch, tmp_path)
        reasons: list = []

        class _FakeWorker:
            def request_emergency_stop(self, reason: str) -> None:
                reasons.append(reason)

        window._worker = _FakeWorker()
        monkeypatch.setattr(sys, "excepthook", lambda *a: None)

        _install_emergency_excepthook(window)
        try:
            raise RuntimeError("kapacitás-számítási hiba")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert reasons == ["UNHANDLED_EXCEPTION: kapacitás-számítási hiba"]

    def test_worker_exists_but_dormant_at_parked_checkpoint_uses_direct_safe_off(
        self, qapp, monkeypatch, tmp_path
    ):
        """F-A (MAX verifier 2. kör, 2026-09-13): `window._worker is not
        None` az OBJEKTUM létezését jelenti, NEM a mögötte futó tick-hurok
        élőállapotát. Ha a worker `run()`-ja MÁR visszatért és a session
        egy resumable BQ checkpointnál PARKOL
        (`_pending_checkpoint_result is not None` -- l. `main_window.py::
        closeEvent()` PONTOSAN ugyanerről a, már egyszer feltárt
        hibaosztályról szóló dokumentált esete), a `request_emergency_
        stop()` csak egy SOHA ki nem olvasott flaget állítana egy dormant
        objektumon -- az operátor hamis 'VÉSZLEÁLLÍTÁS kérve' üzenetet
        látna, de valós safe-off NEM történne. Diszkrimináló: az F-A
        előtti kódon a `request_emergency_stop` hívódna, a közvetlen
        `safe_all_off` NEM -- pont az ellentéte a helyesnek."""
        from Prog.src.test_runner import TestResult

        window = _make_window(qapp, monkeypatch, tmp_path)
        direct_safe_off_calls: list = []
        window._instruments.safe_all_off = lambda: (
            direct_safe_off_calls.append("safe_all_off")
            or {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
        )
        emergency_stop_calls: list = []

        class _DormantWorker:
            def request_emergency_stop(self, reason: str) -> None:
                emergency_stop_calls.append(reason)

        window._worker = _DormantWorker()
        window._pending_checkpoint_result = TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, total_charge_ah=0.0, total_discharge_ah=0.0,
        )
        monkeypatch.setattr(sys, "excepthook", lambda *a: None)

        _install_emergency_excepthook(window)
        try:
            raise RuntimeError("szimulált kivétel parkoló checkpointnál")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert emergency_stop_calls == []
        assert direct_safe_off_calls == ["safe_all_off"]


class TestEmergencyExcepthookUncertainSafeOffShowsCriticalDialog:
    """F-B (MAX verifier 2. kör, 2026-09-13): az F3 (1. kör) implementálva
    volt, de egyetlen teszt sem futtatta az `uncertain=True` ágat -- ha ez
    hibás lenne, a körülvevő `except Exception` elnyelte és csak stderr-re
    írta volna, pontosan azzá a "láthatatlan bizonytalan safe-off"-fá
    degradálva, amit az F3 megszüntetni hivatott."""

    def test_uncertain_safe_off_on_no_worker_branch_shows_critical_dialog(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        window._instruments.safe_all_off = lambda: {
            "Load": False, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        critical_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "critical", lambda *a, **k: critical_calls.append(a)
        )
        monkeypatch.setattr(sys, "excepthook", lambda *a: None)

        _install_emergency_excepthook(window)
        try:
            raise RuntimeError("szimulált kivétel bizonytalan safe-off-fal")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert len(critical_calls) == 1

    def test_fully_successful_safe_off_does_not_show_critical_dialog(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        window._instruments.safe_all_off = lambda: {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        critical_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "critical", lambda *a, **k: critical_calls.append(a)
        )
        monkeypatch.setattr(sys, "excepthook", lambda *a: None)

        _install_emergency_excepthook(window)
        try:
            raise RuntimeError("szimulált kivétel sikeres safe-off-fal")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert critical_calls == []


class TestEmergencyExcepthookThreadGate:
    """F2 (MAX verifier 1. kör): a `sys.excepthook` NEM garantál
    main-threadet -- egy `moveToThread`-elt worker-szálon keletkező
    kivétel is idekerülhet. A safe-off-kísérlet Qt-widgetet mutál, ami
    csak a GUI-szálról biztonságos -- más szálról érkező kivételnél a
    safe-off-kísérletet KI KELL hagyni, de az eredeti hook hívása nem
    maradhat el."""

    def test_exception_from_other_thread_skips_safe_off_but_still_chains(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        safe_off_calls: list = []
        window._instruments.safe_all_off = lambda: (
            safe_off_calls.append("safe_all_off")
            or {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
        )
        previous_hook_calls: list = []
        monkeypatch.setattr(sys, "excepthook", lambda *a: previous_hook_calls.append(a))

        _install_emergency_excepthook(window)
        current_hook = sys.excepthook

        exc = RuntimeError("szimulált kivétel egy worker-szálon")
        result: dict = {}

        def _raise_on_other_thread() -> None:
            try:
                raise exc
            except RuntimeError:
                current_hook(*sys.exc_info())
            result["done"] = True

        thread = threading.Thread(target=_raise_on_other_thread)
        thread.start()
        thread.join(timeout=5)

        assert result.get("done") is True
        assert safe_off_calls == []
        assert len(previous_hook_calls) == 1
        assert previous_hook_calls[0][1] is exc


class TestMainInstallsHookOnRealEntryPoint:
    """F4a (MAX verifier 1. kör): a `main()` TÉNYLEGES bekötését
    (`_install_emergency_excepthook(window)` hívása) korábban semmi nem
    tesztelte -- ha ez a sor kikerülne `main()`-ból, az eddigi 5+2 teszt
    (ami közvetlenül `_install_emergency_excepthook()`-ot hívja) zölden
    futna tovább."""

    def test_main_installs_the_hook(self, qapp, monkeypatch):
        """`qapp` fixture session-szinten MÁR biztosít egy élő
        `QApplication.instance()`-t -- `MainWindow()` ezt újrahasználja.
        A `main_module.QApplication`-t egy FÜGGETLEN, nem-valódi objektumra
        cseréljük (nem a `qapp`-ra magára), hogy a `main()` `app.exec()`-
        hívása SOHA ne indítson valódi Qt eseményhurkot -- az eredeti
        verzió a `qapp.exec`-et patch-elte, ami a VALÓDI Qt-metódusra
        nézve nem garantáltan fogott (a hívás élesen futtatta volna a
        hurkot, ami határidő nélkül blokkolt volna)."""
        import Prog.main as main_module

        class _FakeApp:
            def setApplicationName(self, name: str) -> None:
                pass

            def exec(self) -> int:
                return 0

        _patch_hardware(monkeypatch)
        # R16-B27: a fajlhandler sem irhat lemezre vagy maradhat a rooton.
        directory = Path("R:/R16-B27-memory")
        monkeypatch.setattr(app_paths, "exe_dir", lambda: directory)
        monkeypatch.setattr(app_paths, "ensure_local_config", lambda: None)
        monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
        stream = io.StringIO()
        opened = []

        def memory_open(handler):
            opened.append(handler)
            return stream

        monkeypatch.setattr(logging.FileHandler, "_open", memory_open)
        monkeypatch.setattr(main_module, "QApplication", lambda argv: _FakeApp())
        monkeypatch.setattr(main_module.sys, "exit", lambda code=0: None)
        original_hook = sys.excepthook
        monkeypatch.setattr(sys, "excepthook", original_hook)
        root = logging.getLogger()
        original_handlers = root.handlers
        original_level = root.level

        with monkeypatch.context() as logging_patch:
            logging_patch.setattr(root, "handlers", list(original_handlers))
            try:
                main_module.main()

                assert sys.excepthook is not original_hook
                assert len(opened) == 1
                assert opened[0] in root.handlers
                assert not any(
                    handler.get_name() == "akkuteszter.file"
                    for handler in original_handlers
                )
            finally:
                for handler in root.handlers[:]:
                    if handler not in original_handlers:
                        root.removeHandler(handler)
                        handler.close()
                root.setLevel(original_level)

        assert root.handlers is original_handlers
        assert root.level == original_level
        assert not any(
            handler.get_name() == "akkuteszter.file" for handler in root.handlers
        )
        assert opened[0].stream is None
        assert stream.closed


def _fire_hook(window, monkeypatch, message: str) -> tuple[list, RuntimeError]:
    previous_hook_calls: list = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: previous_hook_calls.append(a))
    _install_emergency_excepthook(window)
    exc = RuntimeError(message)
    try:
        raise exc
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    return previous_hook_calls, exc


class TestR1B6F07EmergencyExcepthookDuringConnect:
    """[regresszio, R1 B6, F-07, DFAM-001] connect-fazisban (`_worker is
    None`, `_connect_worker is not None`) a hook a KORABBI managerre adott ki
    safe-offot (konkurens muszer-I/O a bring-up worker-szal mellett), es a
    `_connect_abort_requested` nem allt be -- a kapcsolodas utan a session
    ELINDULT. Most a hook ugyanazt teszi, mint a "Safe Off" gomb ebben a
    fazisban: csak a flaget allitja."""

    def _window_with_old_manager_spy(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        old_calls: list = []
        window._instruments.safe_all_off = lambda: (
            old_calls.append("safe_all_off")
            or {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
        )
        return window, old_calls

    def test_connect_phase_sets_abort_flag_without_instrument_io(
        self, qapp, monkeypatch, tmp_path
    ):
        window, old_calls = self._window_with_old_manager_spy(qapp, monkeypatch, tmp_path)
        window._connect_worker = object()  # sentinel: "kapcsolodas folyamatban"
        criticals: list = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a))

        previous_hook_calls, exc = _fire_hook(window, monkeypatch, "kivetel connect kozben")

        assert window._connect_abort_requested is True
        assert old_calls == []
        assert criticals == []
        assert len(previous_hook_calls) == 1
        assert previous_hook_calls[0][1] is exc
        message = window._status_bar.currentMessage()
        assert "VÉSZLEÁLLÍTÁS" in message
        assert "session NEM indul el" in message

    def _run_connect_chain(self, qapp, monkeypatch, tmp_path, trigger: str) -> dict:
        """Valodi _InstrumentConnectWorker -> connect_ready -> _on_connect_ready
        lanc (worker.run() szinkron, nincs QThread); a veszleallitas a
        kapcsolodas KOZBEN (a run() elott) erkezik."""
        from Prog.gui.main_window import _InstrumentConnectWorker
        from Prog.src.instrument_manager import InstrumentManager

        window, old_calls = self._window_with_old_manager_spy(qapp, monkeypatch, tmp_path)
        old_manager = window._instruments
        manager_calls: list = []

        def _safe_all_off(self):
            manager_calls.append(("safe_all_off", self))
            return {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}

        def _disconnect_all(self):
            manager_calls.append(("disconnect_all", self))

        monkeypatch.setattr(InstrumentManager, "safe_all_off", _safe_all_off)
        monkeypatch.setattr(InstrumentManager, "disconnect_all", _disconnect_all)
        build_calls: list = []

        def _build_runner_spy(cfg, pre_connected=None):
            build_calls.append(cfg)
            raise RuntimeError("a session nem indulhat veszleallitas utan")

        monkeypatch.setattr(window, "_build_runner", _build_runner_spy)
        cfg = _make_cfg()
        window._pending_start_cfg = cfg
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        window._connect_worker = worker
        worker.connect_ready.connect(window._on_connect_ready)
        worker.connect_failed.connect(window._on_connect_failed)

        if trigger == "hook":
            _fire_hook(window, monkeypatch, "kivetel connect kozben")
        else:
            window._emergency_stop()
        flag_after_trigger = window._connect_abort_requested

        worker.run()

        new_managers = {id(m) for _name, m in manager_calls}
        return {
            "flag_after_trigger": flag_after_trigger,
            "old_calls": old_calls,
            "manager_call_names": [name for name, _m in manager_calls],
            "old_manager_touched": any(m is old_manager for _n, m in manager_calls),
            "single_new_manager": len(new_managers) == 1,
            "build_calls": build_calls,
            "worker_is_none": window._worker is None,
            "connect_worker_is_none": window._connect_worker is None,
            "flag_after_ready": window._connect_abort_requested,
            "start_enabled": window._live_panel._start_btn.isEnabled(),
        }

    def test_hook_chain_closes_new_connection_and_session_does_not_start(
        self, qapp, monkeypatch, tmp_path
    ):
        outcome = self._run_connect_chain(qapp, monkeypatch, tmp_path, "hook")

        assert outcome["flag_after_trigger"] is True
        assert outcome["old_calls"] == []
        assert outcome["manager_call_names"] == ["safe_all_off", "disconnect_all"]
        assert outcome["old_manager_touched"] is False
        assert outcome["single_new_manager"] is True
        assert outcome["build_calls"] == []
        assert outcome["worker_is_none"] is True
        assert outcome["connect_worker_is_none"] is True
        assert outcome["flag_after_ready"] is False
        assert outcome["start_enabled"] is True

    def test_hook_parity_with_safe_off_button_during_connect(
        self, qapp, monkeypatch, tmp_path
    ):
        hook_outcome = self._run_connect_chain(qapp, monkeypatch, tmp_path, "hook")
        button_outcome = self._run_connect_chain(qapp, monkeypatch, tmp_path, "button")

        assert hook_outcome == button_outcome


class TestR1B6F09IdleHookAfterFinalizedSession:
    """[regresszio, R1 B6, F-09, DFAM-016] egy lezart session utan a
    self._instruments a bontott managerre mutatott -- egy idle (nincs worker,
    nincs connect) kezeletlen kivetel a hookban ezen adott ki safe-offot, ami
    a lezart kapcsolatokon "nem igazolt" eredmenyt adott: hamis modalis
    CRITICAL dialogus. Most: nincs dialogus, SAFE_OFF_SKIPPED_NO_INSTRUMENTS."""

    def test_idle_hook_after_terminal_session_reports_skipped_not_false_critical(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.test_runner import TestResult

        window = _make_window(qapp, monkeypatch, tmp_path)
        manager = window._instruments
        manager.disconnect_all = lambda: None
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True,
            safe_off_result={"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True},
        ))
        # A bontott kapcsolatokon kiadott OFF nem igazolhato -- igy viselkedne
        # a lezart manager, ha a hook megis rajta probalkozna.
        stale_calls: list = []
        manager.safe_all_off = lambda: (
            stale_calls.append("safe_all_off")
            or {"Load": False, "PSU": False, "DMM_V": False, "DMM_T": False}
        )
        events: list = []
        window._live_panel.append_event = events.append
        criticals: list = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a))

        previous_hook_calls, exc = _fire_hook(window, monkeypatch, "idle kivetel")

        assert stale_calls == []
        assert criticals == []
        skipped = [e for e in events if e.get("event_code") == "SAFE_OFF_SKIPPED_NO_INSTRUMENTS"]
        assert len(skipped) == 1
        assert skipped[0]["severity"] == "CRITICAL"
        assert len(previous_hook_calls) == 1
        assert previous_hook_calls[0][1] is exc


class TestEmergencyExcepthookNotInstalledAtImportTime:
    def test_importing_main_module_does_not_change_excepthook(self, monkeypatch):
        """A hook csak `main()`-ből installálódik -- modul-import időben
        (ahogy a tesztek importálnak) NEM aktiválódik, tehát egy pytest
        alatt elkapott kivétel kezelését nem befolyásolja.

        F4b (MAX verifier 1. kör): az eredeti teszt `import Prog.main`-t
        hívott, de a modul a fájl 16. sorában MÁR importálva volt
        (collection-kor, `from Prog.main import ...`) -- a `sys.modules`
        cache miatt ez az `import` NEM futtatta újra a modul törzsét,
        tehát a teszt egy modul-import-időben hookot telepítő (HIBÁS)
        verzión is zölden futott volna. `importlib.reload()` kényszeríti
        a modul törzsének tényleges újrafutását."""
        import Prog.main as main_module

        original = sys.excepthook
        importlib.reload(main_module)

        assert sys.excepthook is original
