"""TestRunnerWorker unit tesztek — Mock TestRunner-rel."""
from __future__ import annotations

import pytest

from Prog.gui.worker import TestRunnerWorker
from Prog.src.test_runner import TestResult, TestPlan


class _MockRunner:
    """Mock TestRunner — azonnal visszatér, callback-eket hívja."""
    def __init__(self, result: TestResult, emit_sample: bool = True):
        self._result = result
        self._emit = emit_sample
        self.on_sample = None
        self.on_event = None
        self.on_step_changed = None
        self.on_psu_regulation = None
        self.stop_requested = False
        self.emergency_stop_requested = False
        self.emergency_stop_reason = ""
        self.reset_flags_called = False
        self.last_start_step_index = 0

    def run(self, plan, start_step_index: int = 0) -> TestResult:
        self.last_start_step_index = start_step_index
        if self._emit and self.on_sample:
            self.on_sample({"event_code": None, "battery_voltage_V": 12.5, "elapsed_s": 1.0})
        if self._emit and self.on_event:
            self.on_event({"event_code": "TEST_EVENT", "event_message": "ok"})
        if self._emit and self.on_step_changed:
            self.on_step_changed({
                "runner_status": "RUNNING",
                "step_kind": "CHARGE",
                "step_label": "charge",
                "step_index": 0,
                "step_count": 4,
            })
        if self._emit and self.on_psu_regulation:
            self.on_psu_regulation({"CV": True, "CC": False, "ON": True})
        return self._result

    def request_stop(self):
        self.stop_requested = True

    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP"):
        self.emergency_stop_requested = True
        self.emergency_stop_reason = reason

    def reset_control_flags(self) -> None:
        self.reset_flags_called = True


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

    def test_finished_signal_also_emitted_on_fault(self, qapp):
        """DATA-003 regresszió: korábban FAULT esetén CSAK a `fault` szignál
        tüzelt — a `finished` (amire MainWindow._on_finished/_write_report van
        kötve) sosem, ezért egyetlen valódi FAULT-tal záruló session sem kapott
        report.json-t. Mindkettőnek tüzelnie kell, ugyanazzal a TestResult-tal."""
        mock_runner = _MockRunner(TestResult(status="FAULT", reason="DMM_LOST"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        faults = []
        finished_results = []
        worker.fault.connect(faults.append)
        worker.finished.connect(finished_results.append)
        worker.run()
        assert len(faults) == 1
        assert len(finished_results) == 1
        assert finished_results[0].status == "FAULT"
        assert finished_results[0].reason == "DMM_LOST"

    def test_finished_signal_emitted_on_unhandled_exception(self, qapp):
        """DATA-003: a run() KÍVÜLI, worker-szintű kivétel is terminális FAULT —
        a finished szignálnak itt is tüzelnie kell egy TestResult-tal, különben
        a report.json ebben az (extrém ritka) esetben sem készülne el."""
        class _RaisingRunner(_MockRunner):
            def run(self, plan, start_step_index: int = 0):
                raise RuntimeError("simulated worker-level crash")

        worker = TestRunnerWorker(_RaisingRunner(TestResult(status="DONE")), TestPlan.characterization())
        faults = []
        finished_results = []
        worker.fault.connect(faults.append)
        worker.finished.connect(finished_results.append)
        worker.run()
        assert len(faults) == 1
        assert len(finished_results) == 1
        assert finished_results[0].status == "FAULT"
        assert "simulated worker-level crash" in finished_results[0].reason

    def test_finished_signal_emitted_before_propagating_base_exception(self, qapp):
        """[regresszió, 2026-09-15, C-04/DFAM-002 RECURRING] a fenti teszt
        (RuntimeError) csak `Exception`-t fedez -- egy `KeyboardInterrupt`
        (`BaseException`) korábban KIKERÜLTE ezt az ágat teljesen: nincs
        `finished` emit, tehát nincs report.json. A `finished` emit-nek a
        `raise` ELŐTT kell megtörténnie (worker->signal->handler teljes
        lánc, l. 60-testing.md)."""
        class _RaisingRunner(_MockRunner):
            def run(self, plan, start_step_index: int = 0):
                raise KeyboardInterrupt()

        worker = TestRunnerWorker(_RaisingRunner(TestResult(status="DONE")), TestPlan.characterization())
        finished_results = []
        worker.finished.connect(finished_results.append)

        with pytest.raises(KeyboardInterrupt):
            worker.run()

        assert len(finished_results) == 1
        assert finished_results[0].status == "FAULT"

    def test_sample_ready_emitted(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"), emit_sample=True)
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        samples = []
        worker.sample_ready.connect(samples.append)
        worker.run()
        assert len(samples) == 1
        assert samples[0]["battery_voltage_V"] == 12.5

    def test_psu_regulation_ready_emitted(self, qapp):
        """Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2): a worker
        bekötése a runner.on_psu_regulation callback-re -- KÜLÖN
        szignálon, nem a device_error_ready-n."""
        mock_runner = _MockRunner(TestResult(status="DONE"), emit_sample=True)
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        received = []
        worker.psu_regulation_ready.connect(received.append)
        worker.run()
        assert received == [{"CV": True, "CC": False, "ON": True}]

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

    def test_unknown_status_raises_instead_of_silently_reporting_done(self, qapp):
        """Kör 20 SW20-6 (DFAM-004+DFAM-018): a catch-all `else` korábban
        MINDEN ismeretlen TestResult.status-t "DONE"-ra (sikeres
        befejezésre) képezett le -- egy jövőbeli terminális status (pl.
        "DEGRADED") csendben sikeres befejezésnek tűnt volna. `TestResult.
        status` sima `str`, nincs Enum/validáció, tehát ez a mock-runneren
        keresztül közvetlenül reprodukálható.

        Kör 21 F10 (F21-G3) UTÁN: a `finished` szignál MÁR NEM marad üres --
        a `raise` előtt most `status_changed("FAULT")` + `fault` + egy
        `status="FAULT"` `finished` emit fut (a testvér-ágakkal és a
        `except BaseException` kezelővel egyező mintát követve), különben
        a GUI örökre "RUNNING"-nál ragadna és sosem készülne report.json.
        L. a `TestKor21BatchF10UnknownStatusStillEmitsFaultBeforeRaising`
        osztályt a teljes diszkrimináló lefedettségért."""
        mock_runner = _MockRunner(TestResult(status="DEGRADED"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        statuses = []
        finished_results = []
        worker.status_changed.connect(statuses.append)
        worker.finished.connect(finished_results.append)

        with pytest.raises(ValueError, match="DEGRADED"):
            worker.run()

        assert "DONE" not in statuses, (
            "egy ismeretlen status NEM jelenhet meg csendben sikeres DONE-kent"
        )
        assert len(finished_results) == 1, (
            "egy ismeretlen status eseten is fusson a finished (kulonben "
            "MainWindow._on_finished sosem hivodik, nincs report.json)"
        )
        assert finished_results[0].status == "FAULT"

    def test_known_statuses_still_reach_done_without_raising(self, qapp):
        """Counter-teszt: a fix NEM szűkíthette le a MEGLÉVŐ, dokumentált
        DONE-utat -- a négy ismert terminális status (FAULT/STOPPED/
        CHECKPOINT_STOPPED/DONE) továbbra is a saját ágára esik, raise
        nélkül."""
        for status in ("DONE", "FAULT", "STOPPED", "CHECKPOINT_STOPPED"):
            mock_runner = _MockRunner(TestResult(status=status, reason="x"))
            worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
            finished_results = []
            worker.finished.connect(finished_results.append)
            worker.run()  # nem szabad raise-elnie
            assert len(finished_results) == 1
            assert finished_results[0].status == status

    def test_request_emergency_stop_delegates(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker.request_emergency_stop("USER_EMERGENCY_STOP")
        assert mock_runner.emergency_stop_requested is True
        assert mock_runner.emergency_stop_reason == "USER_EMERGENCY_STOP"

    def test_runner_on_sample_wired(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        _worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        assert mock_runner.on_sample is not None

    def test_runner_on_event_wired(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        _worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        assert mock_runner.on_event is not None

    def test_worker_exception_emits_fault(self, qapp):
        class _CrashRunner(_MockRunner):
            def run(self, plan, start_step_index: int = 0):
                raise RuntimeError("instrument disconnected")

        mock_runner = _CrashRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        faults = []
        worker.fault.connect(faults.append)
        worker.run()
        assert len(faults) == 1
        assert "instrument disconnected" in faults[0]


class TestKor21BatchF10UnknownStatusStillEmitsFaultBeforeRaising:
    """Kör 21 Batch F10 (F21-G3, DFAM-011+DFAM-018): a `raise ValueError(...)`
    ág (Kör 20 E17) SEMMIT nem emittált -- a négy testvér-ág ÉS az `except
    BaseException` kezelő is `status_changed`+`finished`-et (FAULT-nál
    `fault`-ot is) emittál a terminális visszatérés/kivétel ELŐTT. Enélkül
    a GUI örökre "RUNNING"-nál ragadt, és `MainWindow._on_finished` (a
    riportíró) sosem futott volna -- nincs report.json."""

    def test_unknown_status_emits_status_changed_fault(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DEGRADED"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        statuses = []
        worker.status_changed.connect(statuses.append)
        with pytest.raises(ValueError, match="DEGRADED"):
            worker.run()
        assert statuses == ["RUNNING", "FAULT"]

    def test_unknown_status_emits_fault_signal(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DEGRADED"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        faults = []
        worker.fault.connect(faults.append)
        with pytest.raises(ValueError, match="DEGRADED"):
            worker.run()
        assert len(faults) == 1
        assert "DEGRADED" in faults[0]

    def test_unknown_status_emits_finished_with_fault_result_then_raises(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DEGRADED"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        finished_results = []
        worker.finished.connect(finished_results.append)
        with pytest.raises(ValueError, match="DEGRADED"):
            worker.run()
        assert len(finished_results) == 1
        assert finished_results[0].status == "FAULT"
        assert "DEGRADED" in (finished_results[0].reason or "")

    def test_emission_order_matches_except_basexception_sibling(self, qapp):
        """A `:83-100` `except BaseException` kezelő emit-sorrendje
        `status_changed`->`fault`->`finished`->`raise`. Az ismeretlen-
        status ágnak EZT KELL követnie, nem csak véletlenül helyes
        végállapotot elérnie."""
        mock_runner = _MockRunner(TestResult(status="DEGRADED"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        order = []
        worker.status_changed.connect(lambda s: order.append(("status_changed", s)))
        worker.fault.connect(lambda r: order.append(("fault", r)))
        worker.finished.connect(lambda r: order.append(("finished", r.status)))
        with pytest.raises(ValueError, match="DEGRADED"):
            worker.run()
        kinds = [entry[0] for entry in order]
        assert kinds.index("fault") < kinds.index("finished"), (
            "a fault szignalnak a finished ELOTT kell tuznie, egyezve a "
            "sibling except BaseException agakkal"
        )

    def test_known_statuses_are_unaffected_by_the_fix(self, qapp):
        """Counter-teszt: a fix NEM adhat extra `finished`/`fault` emitet a
        MÁR ISMERT terminális statusokra (FAULT/STOPPED/CHECKPOINT_STOPPED/
        DONE) -- azok a saját águkon futnak, ehhez a kódhoz sosem érnek el."""
        for status in ("DONE", "FAULT", "STOPPED", "CHECKPOINT_STOPPED"):
            mock_runner = _MockRunner(TestResult(status=status, reason="x"))
            worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
            finished_results = []
            worker.finished.connect(finished_results.append)
            worker.run()
            assert len(finished_results) == 1
            assert finished_results[0].status == status


class TestWorkerSignals6B:
    def test_worker_emits_event_ready_with_payload(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"), emit_sample=True)
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        events = []
        worker.event_ready.connect(events.append)
        worker.run()
        assert any(e.get("event_code") == "TEST_EVENT" for e in events)

    def test_worker_emits_checkpoint_reached_on_manual_checkpoint_event(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"), emit_sample=True)
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        reached = []
        worker.checkpoint_reached.connect(reached.append)
        worker._handle_event({
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
            "event_message": "checkpoint",
            "step_name": "manual_bq_checkpoint",
        })
        assert len(reached) == 1
        assert reached[0]["event_code"] == "MANUAL_BQ_CHECKPOINT_REACHED"

    def test_worker_checkpoint_reached_not_emitted_for_other_events(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        reached = []
        worker.checkpoint_reached.connect(reached.append)
        worker._handle_event({"event_code": "EMERGENCY_STOP", "event_message": "x"})
        assert len(reached) == 0

    def test_worker_emits_step_changed(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"), emit_sample=True)
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        steps = []
        worker.step_changed.connect(steps.append)
        worker.run()
        assert len(steps) >= 1
        assert steps[0]["step_label"] == "charge"
        assert steps[0]["runner_status"] == "RUNNING"

    def test_worker_emits_checkpoint_stopped_status(self, qapp):
        mock_runner = _MockRunner(TestResult(status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED"))
        worker = TestRunnerWorker(mock_runner, TestPlan.bq_learning_physical())
        statuses = []
        worker.status_changed.connect(statuses.append)
        worker.run()
        assert "CHECKPOINT_STOPPED" in statuses

    def test_worker_checkpoint_stopped_emits_finished(self, qapp):
        mock_runner = _MockRunner(TestResult(status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED"))
        worker = TestRunnerWorker(mock_runner, TestPlan.bq_learning_physical())
        finished = []
        worker.finished.connect(finished.append)
        worker.run()
        assert len(finished) == 1
        assert finished[0].status == "CHECKPOINT_STOPPED"

    def test_worker_request_continue_stub_no_exception(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker.request_continue_from_checkpoint()   # nem dob kivételt

    def test_worker_stores_checkpoint_next_step_index(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        assert worker._checkpoint_next_step_index is None
        worker._handle_event({
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
            "event_message": "checkpoint",
            "step_name": "manual_bq_checkpoint",
            # Governance-v2 review-kör (MAX verifier O-2): a VALÓS esemény
            # (test_runner.py::_run_manual_checkpoint()) MINDIG hordozza a
            # `resume_possible` mezőt -- a fixture ezt a tényleges
            # esemény-szerződést tükrözi, nem egy minimalizált feltevést.
            "resume_possible": True,
            "next_step_index": 9,
        })
        assert worker._checkpoint_next_step_index == 9

    def test_worker_does_not_store_next_step_index_for_terminal_checkpoint(self, qapp):
        """[regresszió, governance-v2 review-kör, MAX verifier O-2 (P2,
        DFAM-001 -- a korábban javított F9 alakzata)] a next_step_index
        korábban FELTÉTEL NÉLKÜL tárolódott minden MANUAL_BQ_CHECKPOINT_
        REACHED eseménynél, az esemény SAJÁT `resume_possible` mezőjének
        ellenőrzése nélkül -- egy TERMINÁLIS checkpointnál (ma az EGYETLEN
        létező eset) is eltárolt egy folytatásra alkalmatlan indexet, amit a
        request_continue_from_checkpoint() "van érvényes index" ága
        érvényesként fogadott volna el. A checkpoint_panel.py gomb-tiltása
        ma zárja a gyakorlati utat; ez a MÁSODIK, redundáns védelmi réteg."""
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.bq_learning_physical())
        worker._handle_event({
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
            "event_message": "checkpoint",
            "step_name": "manual_bq_checkpoint",
            "resume_possible": False,
            "checkpoint_is_terminal": True,
            "next_step_index": 9,
        })
        assert worker._checkpoint_next_step_index is None

    def test_terminal_checkpoint_clears_earlier_resumable_index(self, qapp):
        """A javítás explicit None-ra állít (nem csak "nem ír felül"): egy
        KORÁBBI, folytatható checkpoint indexe sem maradhat érvényben egy
        KÉSŐBBI, terminális checkpoint után."""
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.bq_learning_physical())
        worker._handle_event({
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
            "resume_possible": True,
            "next_step_index": 4,
        })
        assert worker._checkpoint_next_step_index == 4
        worker._handle_event({
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
            "resume_possible": False,
            "next_step_index": 9,
        })
        assert worker._checkpoint_next_step_index is None


class TestWorkerContinuation:
    def test_continue_emits_warning_when_no_index(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        assert worker._checkpoint_next_step_index is None
        events = []
        worker.event_ready.connect(events.append)
        worker.request_continue_from_checkpoint()
        assert any(
            e.get("event_code") == "CHECKPOINT_RESUME_INVALID_STATE"
            for e in events
        )

    def test_continue_resets_runner_flags(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker._checkpoint_next_step_index = 2
        worker.request_continue_from_checkpoint()
        assert mock_runner.reset_flags_called is True

    def test_continue_runs_with_start_index(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker._checkpoint_next_step_index = 3
        worker.request_continue_from_checkpoint()
        assert mock_runner.last_start_step_index == 3

    def test_continue_emits_running(self, qapp):
        mock_runner = _MockRunner(TestResult(status="DONE"))
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker._checkpoint_next_step_index = 2
        statuses = []
        worker.status_changed.connect(statuses.append)
        worker.request_continue_from_checkpoint()
        assert "RUNNING" in statuses

    def test_continue_blocked_when_emergency_stop_requested_at_checkpoint(self, qapp):
        """[regresszió, Teljes kódbázis review 19. kör (2026-09-06), GUI/UX/
        Állapot P1 lelet] ha az operátor a checkpointnál parkolva
        vészleállítást kért (Safe Off gomb), a run() hurok ekkor MÁR nem
        fut — reset_control_flags() korábban csendben törölte volna a
        kérést, mielőtt bármi reagálhatott volna rá, és a session válasz
        nélkül folytatódott volna."""
        mock_runner = _MockRunner(TestResult(status="DONE"))
        mock_runner.emergency_stop_requested = True
        mock_runner.emergency_stop_reason = "USER_EMERGENCY_STOP"
        worker = TestRunnerWorker(mock_runner, TestPlan.characterization())
        worker._checkpoint_next_step_index = 2
        events = []
        worker.event_ready.connect(events.append)
        finished_results = []
        worker.finished.connect(finished_results.append)

        worker.request_continue_from_checkpoint()

        assert mock_runner.reset_flags_called is False
        assert mock_runner.last_start_step_index == 0  # run() sosem hívódott
        assert any(
            e.get("event_code") == "CHECKPOINT_RESUME_BLOCKED_BY_EMERGENCY_STOP"
            for e in events
        )
        assert len(finished_results) == 1

    def test_emergency_stop_at_checkpoint_preserves_accumulated_totals(self, qapp):
        """[regresszió, Teljes kódbázis review 22. kör (2026-09-07), Session/
        Workflow/Állapotgép P1 lelet] a checkpointnál kért vészleállítás
        korábban egy csupasz TestResult(status="FAULT", reason=...)-ot
        gyártott — minden session-összeg (Ah/Wh, hibaszámlálók) alapértékre
        nullázódott, noha a checkpointig ténylegesen lezajlott ciklusok
        valós, nem-nulla összegeket halmoztak fel. A záró FAULT-eredménynek
        a MEGELŐZŐ CHECKPOINT_STOPPED eredmény összegeit kell megőriznie."""
        checkpoint_result = TestResult(
            status="CHECKPOINT_STOPPED",
            reason="MANUAL_BQ_CHECKPOINT_REACHED",
            total_charge_ah=12.5,
            total_discharge_ah=6.25,
            total_charge_wh=150.0,
            total_discharge_wh=75.0,
            resume_possible=True,
            next_step_index=9,
            communication_faults_count=2,
            extreme_dt_s_count=1,
            relax_dmm_fault_count=1,
            temperature_out_of_range_count=3,
        )
        mock_runner = _MockRunner(checkpoint_result)
        worker = TestRunnerWorker(mock_runner, TestPlan.bq_learning_physical())
        worker.run()  # CHECKPOINT_STOPPED -> _last_checkpoint_result elmentve

        mock_runner.emergency_stop_requested = True
        mock_runner.emergency_stop_reason = "USER_EMERGENCY_STOP"
        finished_results = []
        worker.finished.connect(finished_results.append)
        worker._checkpoint_next_step_index = 9

        worker.request_continue_from_checkpoint()

        assert len(finished_results) == 1
        fault_result = finished_results[0]
        assert fault_result.status == "FAULT"
        assert "EMERGENCY_STOP_AT_CHECKPOINT" in fault_result.reason
        assert fault_result.resume_possible is False
        assert fault_result.total_charge_ah == 12.5
        assert fault_result.total_discharge_ah == 6.25
        assert fault_result.total_charge_wh == 150.0
        assert fault_result.total_discharge_wh == 75.0
        assert fault_result.communication_faults_count == 2
        assert fault_result.extreme_dt_s_count == 1
        assert fault_result.relax_dmm_fault_count == 1
        assert fault_result.temperature_out_of_range_count == 3
        assert finished_results[0].status == "FAULT"
        assert "EMERGENCY_STOP_AT_CHECKPOINT" in finished_results[0].reason

    def test_emergency_stop_at_checkpoint_does_not_inherit_stale_safe_off_attestation(
        self, qapp
    ):
        """[regresszió, MAX verifier NV-1, batch 4.1 follow-up] a
        `_run_manual_checkpoint()` (V-1 fix) mostantól `safe_off_
        attempted=True` + valós `safe_off_result`-tal tér vissza a
        CHECKPOINT_STOPPED eredményben — ez a checkpoint ELÉRÉSEKOR
        (akár órákkal/napokkal a vészleállítás-kérés ELŐTT) számolt
        bizonyíték. A `dataclasses.replace()` enélkül a javítás nélkül
        VÁLTOZATLANUL örökölné ezt a régi bizonyítékot a FAULT eredménybe,
        holott a checkpointnál parkolva kért vészleállítás pillanatában
        egyetlen friss OFF parancs sem ment ki — ez egy hamis POZITÍV
        safe-off attesztáció lenne a report.json-ban."""
        checkpoint_result = TestResult(
            status="CHECKPOINT_STOPPED",
            reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True,
            next_step_index=9,
            safe_off_attempted=True,
            safe_off_result={"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True},
        )
        mock_runner = _MockRunner(checkpoint_result)
        worker = TestRunnerWorker(mock_runner, TestPlan.bq_learning_physical())
        worker.run()  # CHECKPOINT_STOPPED -> _last_checkpoint_result elmentve

        mock_runner.emergency_stop_requested = True
        mock_runner.emergency_stop_reason = "USER_EMERGENCY_STOP"
        finished_results = []
        worker.finished.connect(finished_results.append)
        worker._checkpoint_next_step_index = 9

        worker.request_continue_from_checkpoint()

        assert len(finished_results) == 1
        fault_result = finished_results[0]
        assert fault_result.status == "FAULT"
        assert fault_result.safe_off_attempted is False
        assert fault_result.safe_off_result == {}


class TestR3B3DoneTailCriticalReachesLivePanel:
    """[regresszió, R3-B3 (R3-GUI-1, P3), DFAM-018 RECURRING] DONE-tail lánc
    a VALÓDI TestRunner-ből: KeyboardInterrupt a garantált safe-off PSU
    OFF-jában -> retry safe-off -> log_event -> close -> on_event ->
    `TestRunnerWorker._handle_event()` -> `event_ready` signal -> LivePanel.
    Korábban a DONE_TAIL_INTERRUPTED élő sora INFO-ként, színtelenül jelent
    meg; a fix után pontosan egy piros CRITICAL sor. A DONE-tail sorrendjét
    és pontos hívásszámait a HEAD-en (ebbbd98) azonos injekcióval mért
    baseline rögzíti (Codex R3-fixplan kötelező kiegészítés)."""

    def test_done_tail_interrupt_shows_exactly_one_red_critical_row(
        self, qapp, tmp_path
    ):
        from Prog.gui.panels.live_panel import LivePanel
        from Prog.src.test_runner import StepKind, TestStep, TestType
        from Prog.tests.test_test_runner import _make_runner, _StubChargeCtrl

        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=2))
        injected = KeyboardInterrupt("R3-DONE-tail")
        trace: list[tuple] = []
        load = runner._instruments.load
        psu = runner._instruments.psu
        real_load_off = load.safe_off
        real_psu_off = psu.safe_off
        psu_calls = {"n": 0}

        def _load_off():
            trace.append(("Load.safe_off",))
            return real_load_off()

        def _psu_off():
            psu_calls["n"] += 1
            trace.append(("PSU.safe_off",))
            if psu_calls["n"] == 1:
                raise injected
            return real_psu_off()

        load.safe_off = _load_off
        psu.safe_off = _psu_off
        real_log_event = logger.log_event
        real_flush_all = logger.flush_all
        real_close = logger.close

        def _log_event(code, msg, is_critical=False):
            trace.append(("log_event", code, is_critical))
            return real_log_event(code, msg, is_critical=is_critical)

        def _flush_all():
            trace.append(("flush_all",))
            return real_flush_all()

        def _close():
            trace.append(("close",))
            return real_close()

        logger.log_event = _log_event
        logger.flush_all = _flush_all
        logger.close = _close

        panel = LivePanel()
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        worker = TestRunnerWorker(runner, plan)  # runner.on_event = worker._handle_event
        worker.event_ready.connect(panel.append_event)
        worker.event_ready.connect(
            lambda ev: trace.append(("event_ready", ev.get("event_code")))
        )

        with pytest.raises(KeyboardInterrupt) as ei:
            runner.run(plan)

        assert ei.value is injected
        # HEAD-baseline (ebbbd98): a DONE-tail első safe-off hívásától kezdve.
        tail = trace[trace.index(("Load.safe_off",)):]
        assert tail == [
            ("Load.safe_off",),
            ("PSU.safe_off",),  # <- itt az injektált KeyboardInterrupt
            ("Load.safe_off",),  # retry: Load OFF -> PSU OFF
            ("PSU.safe_off",),
            ("log_event", "DONE_TAIL_INTERRUPTED", True),
            ("flush_all",),  # a kritikus log_event flush-e
            ("close",),
            ("flush_all",),  # a close() saját flush-e
            ("event_ready", "DONE_TAIL_INTERRUPTED"),
        ]
        assert psu_calls["n"] == 2
        lst = panel._event_log._list
        texts = [lst.item(i).text() for i in range(lst.count())]
        tail_idx = [i for i, t in enumerate(texts) if " DONE_TAIL_INTERRUPTED " in t]
        assert len(tail_idx) == 1
        item = lst.item(tail_idx[0])
        assert "CRITICAL" in item.text()
        assert item.background().color().name() == "#ffebee"
        # Pontosan egy CRITICAL GUI-esemény a teljes élő naplóban.
        assert [t for t in texts if " CRITICAL " in t] == [item.text()]


class TestR9B3PulseFaultReachesLivePanel:
    """R9-B3: controller -> runner -> worker -> LivePanel, beagyazott hurok nelkul."""

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("source", ["baseline", "chunk"])
    @pytest.mark.parametrize("off_fails", [False, True])
    def test_voltage_evidence_reaches_fault_label(
        self, qapp, fast, source, off_fails, monkeypatch,
    ):
        from Prog.gui.panels.live_panel import LivePanel
        from Prog.tests.test_ocv_soc_controller import TestR9B3OcvSocPulseFaultReason

        ctrl, psu, load = TestR9B3OcvSocPulseFaultReason._fault_case(
            fast, source, off_fails, monkeypatch,
        )
        result = TestR9B3OcvSocPulseFaultReason._runner_fault(ctrl, psu, load)
        panel = LivePanel()
        worker = TestRunnerWorker(_MockRunner(result, emit_sample=False), TestPlan.characterization())
        faults, finished = [], []
        worker.fault.connect(panel.set_fault)
        worker.fault.connect(faults.append)
        worker.finished.connect(finished.append)
        try:
            worker.run()
            assert faults == [result.reason]
            assert finished == [result]
            text = panel._fault_lbl.text()
            assert "BATTERY_OVERVOLTAGE" in text
            assert "14.56 V > 14.55 V" in text
            if off_fails:
                assert "first OFF failed" in text
        finally:
            panel.close()


class TestR16B21bPulseStopSignal:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("emergency", [False, True])
    @pytest.mark.parametrize("tick", [0.0, 30.0])
    def test_gui_signal_worker_runner_off_before_dmm_drain(
        self, qapp, monkeypatch, fast, emergency, tick,
    ):
        from threading import Event, Thread, get_ident
        from types import SimpleNamespace
        from unittest.mock import Mock

        from PySide6.QtCore import Qt

        from Prog.gui.main_window import MainWindow
        from Prog.gui.panels.live_panel import LivePanel
        from Prog.tests.mock_drivers.fake_clock import _B21bRig

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _B21bRig.runner(monkeypatch, rig, tick)
        events = _B21bRig.trace(monkeypatch, rig)
        entered, release, both_off = Event(), Event(), Event()
        fetch = rig.dmm.fetch_timed_acquisition
        off = rig.psu.all_outputs_off
        gui_thread = get_ident()

        def blocked_fetch(**kwargs):
            entered.set()
            assert release.wait(3)
            return fetch(**kwargs)

        def psu_off():
            value = off()
            if entered.is_set() and rig.service._worker is not None:
                assert rig.service._worker.cancel.is_set()
                assert not rig.load.input_commanded_on
                assert get_ident() != gui_thread
                both_off.set()
            return value

        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", blocked_fetch)
        monkeypatch.setattr(rig.psu, "all_outputs_off", psu_off)
        worker = TestRunnerWorker(rig.runner, rig.plan)
        results = []
        worker.finished.connect(results.append, Qt.ConnectionType.DirectConnection)
        panel = LivePanel()
        window = SimpleNamespace(_worker=worker, _status_bar=Mock())
        panel.stop_requested.connect(lambda: MainWindow._stop_test(window))
        panel.emergency_stop_requested.connect(lambda: MainWindow._emergency_stop(window))
        panel.set_status("RUNNING")
        button = panel._emstop_btn if emergency else panel._stop_btn
        thread = Thread(target=worker.run)
        try:
            thread.start()
            assert entered.wait(2)
            button.click()
            qapp.processEvents()
            assert both_off.wait(1)
            assert not release.is_set() and thread.is_alive()
            assert rig.dmm._measurement_owner is not None
            assert not any(name == "reset_to_single_sample" for _, name, _ in events)
            # A GUI az OFF után is szabad; a DMM-drain a runnerben vár.
            qapp.processEvents()
        finally:
            release.set()
            thread.join(3)
            panel.close()
        assert not thread.is_alive() and len(results) == 1
        assert results[0].status == ("FAULT" if emergency else "STOPPED")
        assert results[0].total_discharge_ah == 0.0
        windows = [e for e in rig.events if e["event_code"] == "OCV_SOC_PULSE_WINDOW"]
        assert windows and "OFF-küldés=" in windows[0]["event_message"]
        assert "HIL_PENDING" in windows[0]["event_message"]
        assert rig.dmm._measurement_owner is None and rig.dmm.nplc == 10.0
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        after_off = [name for _, name, _ in events]
        assert after_off.index("input_off") < after_off.index("reset_to_single_sample")


class TestR16B22IecStopSignal:
    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("emergency", [False, True])
    @pytest.mark.parametrize("tick", [0.0, 30.0])
    def test_gui_worker_runner_off_chain(self, qapp, monkeypatch, phase, emergency, tick):
        from threading import Event, Thread, get_ident
        from types import SimpleNamespace
        from unittest.mock import Mock

        from PySide6.QtCore import Qt

        from Prog.gui.main_window import MainWindow
        from Prog.gui.panels.live_panel import LivePanel
        from Prog.src.test_runner import StepKind, TestType
        from Prog.tests.test_iec_dc_resistance_controller import _B22Rig
        from Prog.tests.test_test_runner import TestR16B12

        rig = _B22Rig.make(monkeypatch, phase)
        runner, _logger, _events, _off, _order, step = TestR16B12.runner(
            rig.ctrl, StepKind.IEC_DC_RESISTANCE, monkeypatch, tick,
        )
        rig.limit_stop[0] = runner.request_stop
        entered, release = Event(), Event()
        gui_thread = get_ident()
        off_threads, samples = [], []
        def blocked_voltage(_value):
            if rig.load.input_commanded_on:
                entered.set()
                assert release.wait(3), "A GUI nem oldotta fel a meresi I/O-t"
        rig.hooks["read_voltage"] = blocked_voltage
        rig.hooks["all_outputs_off"] = lambda _value: (
            off_threads.append(get_ident()) if entered.is_set() else None
        )
        def bounded_sample(_value):
            samples.append(True)
            if len(samples) >= 8:
                runner.request_stop()
        runner.on_sample = bounded_sample
        worker = TestRunnerWorker(runner, TestPlan(TestType.IEC_DC_INTERNAL_RESISTANCE, (step,)))
        results = []
        worker.finished.connect(results.append, Qt.ConnectionType.DirectConnection)
        panel = LivePanel()
        window = SimpleNamespace(_worker=worker, _status_bar=Mock())
        panel.stop_requested.connect(lambda: MainWindow._stop_test(window))
        panel.emergency_stop_requested.connect(lambda: MainWindow._emergency_stop(window))
        panel.set_status("RUNNING")
        thread = Thread(target=worker.run)
        try:
            thread.start()
            assert entered.wait(2), "Az IEC I/O-trigger nem sult el"
            (panel._emstop_btn if emergency else panel._stop_btn).click()
            qapp.processEvents()
            assert runner.emergency_stop_requested if emergency else runner.stop_requested
        finally:
            release.set()
            thread.join(3)
            panel.close()
        assert not thread.is_alive() and entered.is_set()
        assert len(results) == 1
        assert results[0].status == ("FAULT" if emergency else "STOPPED")
        assert results[0].reason == ("USER_EMERGENCY_STOP" if emergency else "USER_STOP_REQUESTED")
        assert off_threads and all(identity != gui_thread for identity in off_threads)
        assert rig.voltage._measurement_owner is None
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        names = [name for name, _, _ in rig.trace]
        assert names.index("input_off") < names.index("release_measurement")


def test_b23b_worker_final_charge_real_runner_signal_chain(qapp, monkeypatch):
    from Prog.tests.test_test_runner import _b23b_memory_runner

    runner, ctrl, _, checkpoints, _, ticks, _, _, guard_failures = _b23b_memory_runner(monkeypatch)
    worker = TestRunnerWorker(runner, TestPlan.traditional_learning())
    finished = []
    worker.finished.connect(finished.append)
    worker.run()
    assert not guard_failures, f"B23b swallowed callback AssertionError: {guard_failures}"
    assert 1 <= len(ticks) <= 24, "Worker must execute bounded real runner"
    assert len(finished) == 1 and finished[0].status == "DONE"
    assert checkpoints and isinstance(checkpoints[-1].get("tlc_completion"), dict), "Final charge checkpoint must exist"
    record = checkpoints[-1]["tlc_completion"]
    assert record == ctrl.tlc_completion == runner.tlc_completion
    assert record["final_recharge"]["completed_at_utc"]
    assert record["learning_evidence"]["result_summary"]["result"] == "BQ_LEARNING_VERIFIED"
