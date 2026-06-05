"""
TestRunnerWorker — egyetlen PySide6 ↔ TestRunner bridge réteg.
A TestRunner GUI-független marad; a worker fordítja signalokra a callback-eket.
"""
from __future__ import annotations
from PySide6.QtCore import QObject, Signal, Slot

from Prog.src.test_runner import TestPlan


class TestRunnerWorker(QObject):
    sample_ready       = Signal(dict)
    event_ready        = Signal(dict)
    status_changed     = Signal(str)
    checkpoint_reached = Signal(dict)
    finished           = Signal(object)
    fault              = Signal(str)
    step_changed       = Signal(dict)

    def __init__(self, runner, test_plan: TestPlan) -> None:
        super().__init__()
        self._runner = runner
        self._test_plan = test_plan
        self._runner.on_sample = self.sample_ready.emit
        self._runner.on_event = self._handle_event
        self._runner.on_step_changed = self.step_changed.emit
        self._checkpoint_next_step_index: int | None = None

    def _handle_event(self, event: dict) -> None:
        self.event_ready.emit(event)
        if event.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED":
            self._checkpoint_next_step_index = event.get("next_step_index")
            self.checkpoint_reached.emit(event)

    def _do_run(self, start_step_index: int = 0) -> None:
        self.status_changed.emit("RUNNING")
        try:
            result = self._runner.run(self._test_plan, start_step_index)
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
        elif result.status == "CHECKPOINT_STOPPED":
            self.status_changed.emit("CHECKPOINT_STOPPED")
            self.finished.emit(result)
        else:
            self.status_changed.emit("DONE")
            self.finished.emit(result)

    @Slot()
    def run(self) -> None:
        self._do_run(start_step_index=0)

    @Slot()
    def request_stop(self) -> None:
        self._runner.request_stop()

    @Slot(str)
    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None:
        self._runner.request_emergency_stop(reason)

    @Slot()
    def request_continue_from_checkpoint(self) -> None:
        if self._checkpoint_next_step_index is None:
            self.event_ready.emit({
                "event_code": "CHECKPOINT_RESUME_INVALID_STATE",
                "event_message": "Folytatás kérése érkezett, de nincs érvényes next_step_index.",
                "severity": "WARNING",
            })
            return
        self._runner.reset_control_flags()
        self._do_run(start_step_index=self._checkpoint_next_step_index)

    @property
    def runner(self):
        return self._runner
