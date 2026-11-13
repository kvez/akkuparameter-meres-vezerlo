"""
TestRunnerWorker — egyetlen PySide6 ↔ TestRunner bridge réteg.
A TestRunner GUI-független marad; a worker fordítja signalokra a callback-eket.
"""
from __future__ import annotations

import dataclasses

from PySide6.QtCore import QObject, Signal, Slot

from Prog.src.test_runner import TestPlan, TestResult


class TestRunnerWorker(QObject):
    sample_ready       = Signal(dict)
    event_ready        = Signal(dict)
    device_error_ready = Signal(dict)
    status_changed     = Signal(str)
    checkpoint_reached = Signal(dict)
    finished           = Signal(object)
    fault              = Signal(str)
    step_changed       = Signal(dict)
    # Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói döntés
    # 2026-09-10): a PSU CV/CC regulációs állapotának GUI-only, NEM-escaláló
    # jelzése -- l. test_runner.py::_poll_psu_regulation_state() docstringje
    # arról, miért nem a device_error_ready-n megy.
    psu_regulation_ready = Signal(dict)

    def __init__(self, runner, test_plan: TestPlan, start_step_index: int = 0) -> None:
        super().__init__()
        self._runner = runner
        self._test_plan = test_plan
        self._runner.on_sample = self.sample_ready.emit
        self._runner.on_event = self._handle_event
        self._runner.on_device_error = self.device_error_ready.emit
        self._runner.on_psu_regulation = self.psu_regulation_ready.emit
        self._runner.on_step_changed = self.step_changed.emit
        self._checkpoint_next_step_index: int | None = None
        # Teljes kódbázis review 22. kör (2026-09-07), Session/Workflow P1
        # lelet: az EMERGENCY_STOP_AT_CHECKPOINT ág (lent) korábban egy
        # csupasz TestResult(status="FAULT", reason=...)-ot gyártott — ez
        # minden session-összeget (Ah/Wh, hibaszámlálók) alapértékre
        # (0.0/0/False) nullázott, noha a checkpointig ténylegesen lezajlott
        # ciklusok valós, nem-nulla összegeket halmoztak fel. A CHECKPOINT_
        # STOPPED eredményt itt mentjük el, hogy a vészleállítás-ág ebből
        # tudja származtatni a záró FAULT-eredményt, ne a semmiből.
        self._last_checkpoint_result: TestResult | None = None
        # WP-AK-02: nem 0 érték = az induló run() cross-process resume-ként
        # (session_recovery.evaluate_resume() RESUME_ALLOWED-ja után) egy
        # biztonságos határtól folytatja a tervet, nem az elejétől.
        self._initial_start_step_index = start_step_index

    def _handle_event(self, event: dict) -> None:
        self.event_ready.emit(event)
        if event.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED":
            # Governance-v2 review-kör (MAX verifier O-2, DFAM-001 -- a
            # korábban javított F9 finding alakzata): a next_step_index
            # korábban FELTÉTEL NÉLKÜL tárolódott MINDEN MANUAL_BQ_
            # CHECKPOINT_REACHED eseménynél, az esemény SAJÁT
            # `resume_possible` mezőjének ellenőrzése nélkül -- egy
            # TERMINÁLIS checkpointnál (a jelenlegi tervekben ez az
            # EGYETLEN létező eset, l. test_runner.py::_run_manual_
            # checkpoint() `checkpoint_is_terminal`) is eltárolt egy
            # folytatásra alkalmatlan (a terv végén túlmutató) indexet,
            # amit a request_continue_from_checkpoint() "van érvényes
            # index" ága érvényesnek fogadott volna el. Ma a gyakorlati út
            # zárva (checkpoint_panel.py a Folytatás gombot ugyanezen
            # `resume_possible` alapján tiltja), de ez a MÁSODIK, redundáns
            # védelmi réteg hiányzott. Az explicit None-ra állítás (nem
            # csak "nem írjuk felül") azért kell, hogy egy KORÁBBI,
            # folytatható checkpoint indexe se maradhasson érvényben egy
            # KÉSŐBBI, terminális checkpoint után.
            if event.get("resume_possible", False):
                self._checkpoint_next_step_index = event.get("next_step_index")
            else:
                self._checkpoint_next_step_index = None
            self.checkpoint_reached.emit(event)

    def _do_run(self, start_step_index: int = 0) -> None:
        self.status_changed.emit("RUNNING")
        try:
            result = self._runner.run(self._test_plan, start_step_index)
        except BaseException as exc:
            # DATA-003: egy itt (a TestRunner.run() SAJÁT try/except-jén KÍVÜL)
            # elkapott kivétel is terminális FAULT — kell hozzá `finished`
            # emit is, különben a MainWindow._on_finished (riportíró) sosem fut.
            #
            # C-04/DFAM-002 (2026-09-15, RECURRING -- l. test_runner.py:833
            # azonos indoklása): `except Exception`-ből `except BaseException`-re
            # bővítve, hogy egy KeyboardInterrupt/SystemExit itt is garantáltan
            # kiváltsa a `finished` emit-et (report.json). A `raise` a `finished.
            # emit()` UTÁN kötelező -- az emit sorrendje nem változhat, mert a
            # riportíró (`MainWindow._on_finished`) ettől függ.
            self.status_changed.emit("FAULT")
            fault_result = TestResult(status="FAULT", reason=str(exc))
            self.fault.emit(fault_result.reason or "UNKNOWN_FAULT")
            self.finished.emit(fault_result)
            if not isinstance(exc, Exception):
                raise
            return

        if result.status == "FAULT":
            # DATA-003: korábban itt CSAK `fault` emittálódott — a `finished`
            # (amire MainWindow._on_finished/_write_report kötve van) sosem
            # tüzelt FAULT-ra, tehát pont a legfontosabb esetben nem készült
            # report.json. A `fault` jelzés (kritikus dialógus) megmarad,
            # PLUSZ most a `finished` is fut a teljes TestResult-tal.
            self.status_changed.emit("FAULT")
            self.fault.emit(result.reason or "UNKNOWN_FAULT")
            self.finished.emit(result)
        elif result.status == "STOPPED":
            self.status_changed.emit("STOPPED")
            self.finished.emit(result)
        elif result.status == "CHECKPOINT_STOPPED":
            self._last_checkpoint_result = result
            self.status_changed.emit("CHECKPOINT_STOPPED")
            self.finished.emit(result)
        elif result.status == "DONE":
            self.status_changed.emit("DONE")
            self.finished.emit(result)
        else:
            # Kör 20 SW20-6 (DFAM-004+DFAM-018): korábban ez a catch-all `else`
            # MINDEN ismeretlen status-t "DONE"-ra (sikeres befejezésre)
            # képezett le -- egy jövőbeli terminális status (pl. "DEGRADED")
            # csendben sikeres befejezésnek tűnt volna. SZÁNDÉKOSAN NEM Enum
            # -- az JSON-szerializációt is érintene (l. archívum kikötése).
            #
            # MAX verifier (Kör 20 E17) PONTOSÍTÁSA: ez a `raise` a `try:`/
            # `except BaseException` blokkon (ami csak a fenti
            # `self._runner.run(...)` hívást védi) KÍVÜL fut, tehát
            # védtelenül a worker-szálas `sys.excepthook`-ig jutna --
            # EMPIRIKUSAN igazolva, hogy a `main.py`-beli emergency-hook
            # SZÁNDÉKOSAN NEM próbál safe_all_off()-ot más szálról érkező
            # kivételnél (l. test_main_emergency_excepthook.py::
            # test_exception_from_other_thread_skips_safe_off_but_still_
            # chains). EZ NEM az `_is_finished()`/`_controller_faulted()`
            # (S4-07) mintája -- azok a `run()` SAJÁT `try/except
            # BaseException`-je BELÜL futnak, ami `_emergency_stop()`-ot
            # (safe-off) hív. A jelen `raise` biztonságossága EZÉRT NEM a
            # excepthook-tól, hanem attól függ, hogy `TestRunner.run()`
            # MINDEN terminális visszatérése ELŐTT már lefuttatta a
            # safe-offot (`_safe_all_off_with_uncertainty_check()`) -- ha ez
            # a dispatch egyáltalán eléri ezt az ágat, a hardver már OFF.
            #
            # Kör 21 F10 (F21-G3, DFAM-011+DFAM-018, Kör 20 E17 fix-induced
            # regresszió): a `raise` korábban SEMMIT nem emittált -- a
            # négy testvér-ág (fent) mind `status_changed`+`finished`-et
            # emittál (FAULT plusz `fault`-ot is), és a `:83-100` `except
            # BaseException` kezelő is mindhármat emittálja `raise` ELŐTT.
            # Enélkül a GUI örökre "RUNNING"-nál ragadt volna, és
            # `MainWindow._on_finished` (a riportíró) sosem futott volna --
            # nincs report.json. Az alábbi a `:94-97`-tel EGYEZŐ mintát
            # követi (önálló, `reason`-ös TestResult, NEM a `result`
            # Ah/Wh-összegeinek átvitele -- ismeretlen status esetén nincs
            # forrás, ami igazolná, hogy azok az összegek érvényesek).
            self.status_changed.emit("FAULT")
            fault_result = TestResult(
                status="FAULT",
                reason=f"Unknown TestResult.status: {result.status!r}",
            )
            self.fault.emit(fault_result.reason or "UNKNOWN_FAULT")
            self.finished.emit(fault_result)
            raise ValueError(f"Unknown TestResult.status: {result.status!r}")

    @Slot()
    def run(self) -> None:
        self._do_run(start_step_index=self._initial_start_step_index)

    @Slot()
    def request_stop(self) -> None:
        self._runner.request_stop()

    @Slot(str)
    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None:
        self._runner.request_emergency_stop(reason)

    @Slot()
    def request_continue_from_checkpoint(self) -> None:
        # Review-kör 5, Session/Workflow S5-03 (P2, NOT_REACHABLE -- l.
        # test_runner.py::_run_manual_checkpoint() azonos hivatkozású
        # kommentje): a jelenlegi TestPlan-készletben a MANUAL_CHECKPOINT
        # mindig az utolsó lépés, tehát resume_possible mindig False, és a
        # checkpoint_panel.py "Folytatás" gombja emiatt sosem engedélyezett
        # -- ez a metódus ma szerkezetileg elérhetetlen a GUI-n keresztül.
        # KEEP_AS_IS, előre felkészített egy jövőbeli, nem-terminális
        # MANUAL_CHECKPOINT tervhez.
        if self._checkpoint_next_step_index is None:
            self.event_ready.emit({
                "event_code": "CHECKPOINT_RESUME_INVALID_STATE",
                "event_message": "Folytatás kérése érkezett, de nincs érvényes next_step_index.",
                "severity": "WARNING",
            })
            return
        # Teljes kódbázis review 19. kör (2026-09-06), GUI/UX/Állapot P1
        # lelet: ha az operátor a checkpointnál parkolva a "Safe Off"
        # gombbal vészleállítást kért, a run() hurok ekkor MÁR nem fut,
        # tehát semmi nem olvasta ki a kérést — az alábbi reset_control_
        # flags() korábban csendben törölte volna, mielőtt bármi
        # reagálhatott volna rá, és a session válasz nélkül folytatódott
        # volna. CLAUDE.md §15 "Emergency stop mindig látható és
        # egyértelmű legyen" — a kérés nyomtalanul eltűnt volna.
        if self._runner.emergency_stop_requested:
            reason = self._runner.emergency_stop_reason
            self.event_ready.emit({
                "event_code": "CHECKPOINT_RESUME_BLOCKED_BY_EMERGENCY_STOP",
                "event_message": (
                    f"Folytatás elutasítva — vészleállítás kérve a "
                    f"checkpointnál: {reason}"
                ),
                "severity": "CRITICAL",
            })
            self.status_changed.emit("FAULT")
            # Teljes kódbázis review 22. kör (2026-09-07), Session/Workflow P1
            # lelet: egy csupasz TestResult(status="FAULT", reason=...) minden
            # session-összeget (Ah/Wh, communication_faults_count,
            # extreme_dt_s_count, relax_dmm_fault_count,
            # temperature_out_of_range_count, safe_off_attempted/result)
            # alapértékre nullázott volna, noha a checkpointig ténylegesen
            # lezajlott ciklusok valós, nem-nulla összegeket halmoztak fel —
            # a __init__-ben elmentett self._last_checkpoint_result-ból
            # SZÁRMAZTATVA építjük a záró FAULT-eredményt, csak a
            # státusz/indoklás/folytathatóság mezőit cserélve.
            if self._last_checkpoint_result is not None:
                # Governance-v2 review-kör 1 (MAX verifier NV-1, batch 4.1
                # follow-up): a `dataclasses.replace()` a `_last_checkpoint_
                # result` MINDEN mezőjét örökli, ha nincs explicit felülírva —
                # ez korábban a checkpoint ELÉRÉSEKOR (akár órákkal/napokkal
                # korábban) kiszámolt safe_off_attempted/safe_off_result
                # mezőket vitte tovább változatlanul erre a FAULT eredményre
                # is, holott a checkpointnál parkolva kért vészleállítás
                # PILLANATÁBAN egyetlen friss OFF parancs sem ment ki — ez
                # egy hamis POZITÍV safe-off attesztáció volt a report.json-
                # ban, pont az a hibaosztály (CF-02/DFAM-016), amiért a
                # batch 4.1 létezik. A tényleges friss safe-off ezen a GUI/
                # worker-szintű útvonalon még nyitott (batch 4.1b).
                fault_result = dataclasses.replace(
                    self._last_checkpoint_result,
                    status="FAULT",
                    reason=f"EMERGENCY_STOP_AT_CHECKPOINT: {reason}",
                    resume_possible=False,
                    next_step_index=0,
                    safe_off_attempted=False,
                    safe_off_result={},
                )
            else:
                fault_result = TestResult(
                    status="FAULT", reason=f"EMERGENCY_STOP_AT_CHECKPOINT: {reason}"
                )
            self.fault.emit(fault_result.reason)
            self.finished.emit(fault_result)
            return
        self._runner.reset_control_flags()
        self._do_run(start_step_index=self._checkpoint_next_step_index)

    @property
    def runner(self):
        return self._runner
