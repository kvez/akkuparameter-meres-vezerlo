"""R16-B27: windowed diagnosztika, valódi GUI és fájlírás nélkül."""
import importlib
import io
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import Prog.main as main_module


@pytest.fixture
def runtime(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    previous = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: previous.append(args))
    shown = []
    window = SimpleNamespace(
        _worker=None, _connect_worker=None, _bring_up_in_progress=False,
        _pending_checkpoint_result=None,
        _perform_gui_safe_off=lambda reason: (True, {"Load": True, "PSU": True}),
        show=lambda: shown.append(True),
    )
    app = SimpleNamespace(setApplicationName=lambda name: None, exec=lambda: 0)
    monkeypatch.setattr(main_module, "QApplication", lambda argv: app)
    monkeypatch.setattr(main_module, "MainWindow", lambda: window)
    monkeypatch.setattr(main_module.app_paths, "ensure_local_config", lambda: None)
    directory = Path("R:/R16-B27-memory")
    monkeypatch.setattr(main_module.app_paths, "exe_dir", lambda: directory)
    mkdir_calls = []
    monkeypatch.setattr(
        Path, "mkdir",
        lambda path, **kwargs: mkdir_calls.append((path, kwargs)),
    )
    streams = []
    opened = []

    def memory_open(handler):
        opened.append((Path(handler.baseFilename), handler.mode, handler.encoding))
        stream = io.StringIO("korábbi diagnosztika\n" if handler.mode == "a" else "")
        stream.seek(0, io.SEEK_END)
        streams.append(stream)
        return stream

    monkeypatch.setattr(logging.FileHandler, "_open", memory_open)
    try:
        yield SimpleNamespace(
            root=root, directory=directory, streams=streams, opened=opened,
            previous=previous, window=window, shown=shown, mkdir_calls=mkdir_calls,
        )
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()


def _start():
    with pytest.raises(SystemExit) as result:
        main_module.main()
    assert result.value.code == 0


@pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "dev"])
def test_main_persists_warning_error_critical_without_stderr(runtime, monkeypatch, frozen):
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(
        sys, "_MEIPASS", str(runtime.directory / "_internal"), raising=False,
    )
    runtime.root.setLevel(logging.CRITICAL)
    _start()

    files = [h for h in runtime.root.handlers if isinstance(h, logging.FileHandler)]
    assert len(files) == 1
    assert runtime.opened == [
        (runtime.directory / "logs" / "akkuteszter.log", "a", "utf-8"),
    ]
    assert runtime.mkdir_calls == [
        (runtime.directory / "logs", {"parents": True, "exist_ok": True}),
    ]
    logger = logging.getLogger("Prog.src.safety")
    logger.info("nem diagnosztikai minta")
    logger.warning("próba WARNING")
    logger.error("próba ERROR")
    logger.critical("próba CRITICAL")
    output = runtime.streams[0].getvalue()
    assert output.startswith("korábbi diagnosztika\n")
    for level in ("WARNING", "ERROR", "CRITICAL"):
        assert f"{level} Prog.src.safety: próba {level}" in output
    assert "nem diagnosztikai minta" not in output
    assert files[0].level == logging.WARNING


def test_main_logs_secondary_and_original_exceptions_without_stderr(runtime, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def failed_safe_off(reason):
        raise RuntimeError("másodlagos safe-off hiba")

    runtime.window._perform_gui_safe_off = failed_safe_off
    _start()
    try:
        raise ValueError("eredeti GUI-hiba")
    except ValueError:
        original = sys.exc_info()
        sys.excepthook(*original)

    assert len(runtime.streams) == 1
    output = runtime.streams[0].getvalue()
    assert "ERROR Prog.main: A kezeletlen kivétel safe-off-kísérlete sikertelen" in output
    assert "RuntimeError: másodlagos safe-off hiba" in output
    assert "CRITICAL Prog.main: Kezeletlen kivétel" in output
    assert "ValueError: eredeti GUI-hiba" in output
    assert "Traceback (most recent call last)" in output
    assert runtime.previous == [original]


@pytest.mark.parametrize("stage", ["mkdir", "open"])
@pytest.mark.parametrize("stderr_available", [True, False])
def test_logging_open_failure_keeps_startup_and_reports_when_possible(
    runtime, monkeypatch, stage, stderr_available,
):
    stderr = io.StringIO() if stderr_available else None
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def denied(*args, **kwargs):
        raise PermissionError("írásvédett naplóhely")

    if stage == "mkdir":
        monkeypatch.setattr(Path, "mkdir", denied)
    else:
        monkeypatch.setattr(logging.FileHandler, "_open", denied)
    # A pytest saját capture-handlerei nem részei az alkalmazás indulásának.
    runtime.root.handlers.clear()
    _start()

    assert runtime.shown == [True]
    assert not any(isinstance(h, logging.FileHandler) for h in runtime.root.handlers)
    if stderr is not None:
        assert "A diagnosztikai napló nem nyitható meg" in stderr.getvalue()
        assert "PermissionError: írásvédett naplóhely" in stderr.getvalue()


def test_import_does_not_install_file_handler(runtime):
    before = runtime.root.handlers[:]
    importlib.reload(main_module)
    assert runtime.root.handlers == before
    assert runtime.opened == []


@pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "dev"])
@pytest.mark.parametrize("reload_between", [False, True], ids=["repeat", "reload"])
def test_repeated_main_reuses_own_handler_and_records_once(
    runtime, monkeypatch, frozen, reload_between,
):
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    _start()
    first = next(h for h in runtime.root.handlers if isinstance(h, logging.FileHandler))
    runtime.root.setLevel(logging.CRITICAL)
    if reload_between:
        before = runtime.root.handlers[:]
        app_factory = main_module.QApplication
        window_factory = main_module.MainWindow
        importlib.reload(main_module)
        monkeypatch.setattr(main_module, "QApplication", app_factory)
        monkeypatch.setattr(main_module, "MainWindow", window_factory)
        assert runtime.root.handlers == before
        assert len(runtime.opened) == 1
    _start()

    files = [h for h in runtime.root.handlers if isinstance(h, logging.FileHandler)]
    assert files == [first]
    assert first.get_name() == "akkuteszter.file"
    assert len(runtime.opened) == 1
    assert len(runtime.mkdir_calls) == 1
    assert runtime.root.level == logging.WARNING
    assert first.level == logging.WARNING
    logger = logging.getLogger("Prog.tests.repeat_main")
    for level in (logging.WARNING, logging.ERROR, logging.CRITICAL):
        marker = f"R16-B27-once-{level}"
        logger.log(level, marker)
        assert runtime.streams[0].getvalue().count(marker) == 1
    assert runtime.shown == [True, True]


@pytest.mark.parametrize("other", ["foreign-same-path", "own-different-path"])
def test_main_reuses_only_own_handler_for_the_same_path(runtime, other):
    other_path = runtime.directory / "logs" / (
        "akkuteszter.log" if other == "foreign-same-path" else "other.log"
    )
    existing = logging.FileHandler(other_path, encoding="utf-8")
    existing.set_name("foreign.file" if other == "foreign-same-path" else "akkuteszter.file")
    runtime.root.addHandler(existing)
    _start()
    files = [h for h in runtime.root.handlers if isinstance(h, logging.FileHandler)]
    assert len(files) == 2
    assert existing in files
    installed = next(h for h in files if h is not existing)
    assert installed.get_name() == "akkuteszter.file"
    assert Path(installed.baseFilename) == runtime.directory / "logs" / "akkuteszter.log"
    assert len(runtime.opened) == 2
    _start()
    assert [h for h in runtime.root.handlers if isinstance(h, logging.FileHandler)] == files
    assert len(runtime.opened) == 2
