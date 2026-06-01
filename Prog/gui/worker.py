"""
TestRunnerWorker — egyetlen PySide6 ↔ TestRunner bridge réteg.
A TestRunner GUI-független marad; a worker fordítja signalokra a callback-eket.
"""
from __future__ import annotations
from PySide6.QtCore import QObject, Signal, Slot

from Prog.src.test_runner import TestRunner, TestPlan


class TestRunnerWorker(QObject):
    sample_ready       = Signal(dict)
    event_ready        = Signal(dict)
    status_changed     = Signal(str)
    checkpoint_reached = Signal(dict)
    finished           = Signal(object)
    fault              = Signal(str)

    def __init__(self, runner, test_plan: TestPlan) -> None:
        super().__init__()
        self._runner = runner
        self._test_plan = test_plan
        self._runner.on_sample = self.sample_ready.emit
        self._runner.on_event = self._handle_event

    def _handle_event(self, event: dict) -> None:
        self.event_ready.emit(event)
        if event.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED":
            self.checkpoint_reached.emit(event)

    @Slot()
    def run(self) -> None:
        self.status_changed.emit("RUNNING")
        try:
            result = self._runner.run(self._test_plan)
        except Exception as exc:
            self.status_changed.emit("FAULT")
            self.fault.emit(str(exc))
            return

        if result.status == "FAULT":
            self.status_changed.emit("FAULT")
            self.fault.emit(result.reason or "UNKNOWN_FAULT")
        elif result.status == "STOPPED":
            self.status_changed.emit("STOPPED")
            self.finished.emit(result)
        else:
            self.status_changed.emit("DONE")
            self.finished.emit(result)

    @Slot()
    def request_stop(self) -> None:
        self._runner.request_stop()

    @Slot(str)
    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None:
        self._runner.request_emergency_stop(reason)

    @Slot()
    def request_continue_from_checkpoint(self) -> None:
        pass  # 6B-ben implementálandó

    @property
    def runner(self):
        return self._runner
