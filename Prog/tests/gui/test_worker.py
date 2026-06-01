"""TestRunnerWorker unit tesztek — Mock TestRunner-rel."""
from __future__ import annotations
import pytest
from PySide6.QtCore import QObject, Signal, QThread

from Prog.gui.worker import TestRunnerWorker
from Prog.src.test_runner import TestResult, TestPlan


class _MockRunner:
    """Mock TestRunner — azonnal visszatér, callback-eket hívja."""
    def __init__(self, result: TestResult, emit_sample: bool = True):
        self._result = result
        self._emit = emit_sample
        self.on_sample = None
        self.on_event = None
        self.stop_requested = False
        self.emergency_stop_requested = False
        self.emergency_stop_reason = ""

    def run(self, plan) -> TestResult:
        if self._emit and self.on_sample:
            self.on_sample({"event_code": None, "battery_voltage_V": 12.5, "elapsed_s": 1.0})
        if self._emit and self.on_event:
            self.on_event({"event_code": "TEST_EVENT", "event_message": "ok"})
        return self._result

    def request_stop(self):
        self.stop_requested = True

    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP"):
        self.emergency_stop_requested = True
        self.emergency_stop_reason = reason


class TestWorkerSignals:
    def test_finished_signal_emitted_on_done(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        results = []
        worker.finished.connect(results.append)
        worker.run()
        assert len(results) == 1
        assert results[0].status == "DONE"

    def test_fault_signal_emitted_on_fault(self, qapp):
        mock_runner = _MockRunner(TestResult(status="FAULT", reason="DMM_LOST"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        faults = []
        worker.fault.connect(faults.append)
        worker.run()
        assert len(faults) == 1
        assert "DMM_LOST" in faults[0]

    def test_sample_ready_emitted(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"), emit_sample=True)
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        samples = []
        worker.sample_ready.connect(samples.append)
        worker.run()
        assert len(samples) == 1
        assert samples[0]["battery_voltage_V"] == 12.5

    def test_event_ready_emitted(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"), emit_sample=True)
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        events = []
        worker.event_ready.connect(events.append)
        worker.run()
        assert any(e.get("event_code") == "TEST_EVENT" for e in events)

    def test_status_changed_running_then_done(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        statuses = []
        worker.status_changed.connect(statuses.append)
        worker.run()
        assert statuses[0] == "RUNNING"
        assert statuses[-1] == "DONE"

    def test_status_changed_fault(self, qapp):
        mock_runner = _MockRunner(TestResult(status="FAULT", reason="X"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        statuses = []
        worker.status_changed.connect(statuses.append)
        worker.run()
        assert "FAULT" in statuses

    def test_status_changed_stopped(self, qapp):
        mock_runner = _MockRunner(TestResult(status="STOPPED", reason="USER"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        statuses = []
        worker.status_changed.connect(statuses.append)
        worker.run()
        assert "STOPPED" in statuses

    def test_request_stop_delegates_to_runner(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker.request_stop()
        assert mock_runner.stop_requested is True

    def test_request_emergency_stop_delegates(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker.request_emergency_stop("USER_EMERGENCY_STOP")
        assert mock_runner.emergency_stop_requested is True
        assert mock_runner.emergency_stop_reason == "USER_EMERGENCY_STOP"

    def test_runner_on_sample_wired(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        assert mock_runner.on_sample is not None

    def test_runner_on_event_wired(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        assert mock_runner.on_event is not None

    def test_worker_exception_emits_fault(self, qapp):
        class _CrashRunner(_MockRunner):
            def run(self, plan):
                raise RuntimeError("instrument disconnected")

        mock_runner = _CrashRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        faults = []
        worker.fault.connect(faults.append)
        worker.run()
        assert len(faults) == 1
        assert "instrument disconnected" in faults[0]
