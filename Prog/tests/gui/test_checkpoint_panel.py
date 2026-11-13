"""CheckpointPanel unit tesztek.

Release-audit 2026-08-23 (2.17): a BQ ellenőrzési checklist korábban
dekoratív volt — a bejelölés semmit nem kapuzott és sehova nem naplózódott.
"""
from __future__ import annotations

from Prog.gui.panels.checkpoint_panel import CheckpointPanel


class TestChecklistState:
    def test_all_unchecked_by_default(self, qapp):
        panel = CheckpointPanel()
        state = panel.checklist_state()
        assert len(state) == len(panel._CHECKLIST_ITEMS)
        assert all(done is False for done in state.values())

    def test_reflects_checked_boxes(self, qapp):
        panel = CheckpointPanel()
        panel._checkboxes[0].setChecked(True)
        panel._checkboxes[2].setChecked(True)

        state = panel.checklist_state()

        checked_items = [item for item, done in state.items() if done]
        assert len(checked_items) == 2
        assert panel._CHECKLIST_ITEMS[0] in checked_items
        assert panel._CHECKLIST_ITEMS[2] in checked_items

    def test_show_checkpoint_resets_checklist(self, qapp):
        panel = CheckpointPanel()
        panel._checkboxes[0].setChecked(True)

        panel.show_checkpoint({
            "step_name": "manual_bq_checkpoint",
            "next_step_index": 5,
            "total_charge_ah": 1.0,
            "total_discharge_ah": 0.5,
            "resume_possible": False,
        })

        state = panel.checklist_state()
        assert all(done is False for done in state.values())


class TestCloseButtonReenabledAfterContinuing:
    def test_show_checkpoint_reenables_close_button_after_set_continuing(self, qapp):
        """[regresszió, Teljes kódbázis review 17. kör (2026-09-05),
        GUI/UX/Állapot P3 lelet] set_continuing() letiltja a "Teszt
        lezárása" gombot, de show_checkpoint() sosem engedélyezte
        vissza — jelenleg nincs élő útvonal (egyetlen terminális
        MANUAL_CHECKPOINT), de a widget önmagában hibás lenne, ha egy
        jövőbeli terv több checkpointot tartalmazna."""
        panel = CheckpointPanel()
        panel.set_continuing()
        assert panel._close_btn.isEnabled() is False

        panel.show_checkpoint({
            "step_name": "manual_bq_checkpoint_2",
            "next_step_index": 9,
            "total_charge_ah": 1.0,
            "total_discharge_ah": 0.5,
            "resume_possible": False,
        })

        assert panel._close_btn.isEnabled() is True


class TestR18SafeOffUncertaintyWarning:
    """[regresszió, Kör 18 MAX-effort review, hardver/instrument-control
    reviewer P2-2, DFAM-018+DFAM-001] a statikus "A mérés biztonságos
    állapotban megállt — PSU OFF, Load OFF" szöveg korábban feltétel
    nélkül jelent meg, sosem nézte a tényleges safe-off eredményt --
    a session-vég popup/indítási dialógus testvér-mintáját (R17-03/
    R17-17) kapja meg most a checkpoint panel is, SZÁNDÉKOSAN KÜLÖN
    widgetben (nem a statikus szövegbe olvasztva)."""

    def test_uncertain_psu_shows_warning(self, qapp):
        panel = CheckpointPanel()
        panel.show_checkpoint({
            "step_name": "manual_bq_checkpoint",
            "next_step_index": 5,
            "total_charge_ah": 1.0,
            "total_discharge_ah": 0.5,
            "resume_possible": False,
            "safe_off_result": {"PSU": False, "Load": True},
        })
        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is True
        assert "PSU" in panel._safe_off_warning_lbl.text()
        assert "Load" not in panel._safe_off_warning_lbl.text().split("PSU")[0]
        # R1 B7S V-1: F-42 óta a False readback-eredetű is lehet.
        assert "safe-off utáni állapotellenőrzés" in panel._safe_off_warning_lbl.text()
        assert "nem hardveres readback" not in panel._safe_off_warning_lbl.text()

    def test_confirmed_safe_off_hides_warning(self, qapp):
        panel = CheckpointPanel()
        panel.show_checkpoint({
            "step_name": "manual_bq_checkpoint",
            "next_step_index": 5,
            "total_charge_ah": 1.0,
            "total_discharge_ah": 0.5,
            "resume_possible": False,
            "safe_off_result": {"PSU": True, "Load": True},
        })
        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is False

    def test_missing_safe_off_result_key_fails_closed(self, qapp):
        """Régi sémájú/direkt tesztelt event dict, safe_off_result kulcs
        nélkül -- nincs bizonyíték a biztonságos lekapcsolásra, tehát
        fail-closed módon figyelmeztetünk, nem hallgatunk."""
        panel = CheckpointPanel()
        panel.show_checkpoint({
            "step_name": "manual_bq_checkpoint",
            "next_step_index": 5,
            "total_charge_ah": 1.0,
            "total_discharge_ah": 0.5,
            "resume_possible": False,
        })
        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is True

    def test_warning_hidden_again_on_next_confirmed_checkpoint(self, qapp):
        """Egy korábbi bizonytalan checkpoint utáni, MOST igazolt
        safe-off-nál a figyelmeztetésnek el kell tűnnie -- ne ragadjon
        be a widget láthatósága."""
        panel = CheckpointPanel()
        panel.show_checkpoint({
            "step_name": "cp1", "next_step_index": 1,
            "total_charge_ah": 1.0, "total_discharge_ah": 0.5,
            "resume_possible": False,
            "safe_off_result": {"PSU": False, "Load": True},
        })
        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is True

        panel.show_checkpoint({
            "step_name": "cp2", "next_step_index": 2,
            "total_charge_ah": 1.0, "total_discharge_ah": 0.5,
            "resume_possible": False,
            "safe_off_result": {"PSU": True, "Load": True},
        })
        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is False


_R1B6_EVENT_UNCERTAIN = {
    "step_name": "manual_bq_checkpoint", "next_step_index": 5,
    "total_charge_ah": 1.0, "total_discharge_ah": 0.5,
    "resume_possible": False,
    "safe_off_result": {"PSU": False, "Load": True},
}


class TestR1B6F08SessionFinalizedDisablesSafeOff:
    """[regresszio, R1 B6, F-08, DFAM-001] a terminalis checkpoint utan a
    session lezarul es a muszer-kapcsolatok bontva vannak -- a piros "Safe
    Off" gomb ekkor hatastalan, ezert nem maradhat aktivnak latszo."""

    def test_set_session_finalized_disables_safe_off_with_tooltip(self, qapp):
        panel = CheckpointPanel()
        panel.show_checkpoint(dict(_R1B6_EVENT_UNCERTAIN))
        assert panel._emstop_btn.isEnabled() is True

        panel.set_session_finalized()

        assert panel._emstop_btn.isEnabled() is False
        tooltip = panel._emstop_btn.toolTip()
        assert "lezárult" in tooltip
        assert "Ellenőrizd fizikailag" in tooltip

    def test_set_session_finalized_keeps_uncertainty_label_and_close_button(self, qapp):
        """FINAL B6/3: a bizonytalansagjelzo cimke es a "Teszt lezarasa"
        gomb allapota valtozatlan."""
        panel = CheckpointPanel()
        panel.show_checkpoint(dict(_R1B6_EVENT_UNCERTAIN))
        label_text = panel._safe_off_warning_lbl.text()

        panel.set_session_finalized()

        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is True
        assert panel._safe_off_warning_lbl.text() == label_text
        assert "PSU" in label_text
        assert panel._close_btn.isEnabled() is True

    def test_show_checkpoint_reenables_safe_off_and_clears_tooltip(self, qapp):
        panel = CheckpointPanel()
        panel.set_session_finalized()
        assert panel._emstop_btn.isEnabled() is False

        panel.show_checkpoint(dict(_R1B6_EVENT_UNCERTAIN, resume_possible=True))

        assert panel._emstop_btn.isEnabled() is True
        assert panel._emstop_btn.toolTip() == ""

    def test_set_continuing_keeps_safe_off_enabled(self, qapp):
        """KEEP_AS_IS kontroll: a folytatas alatt a Safe Off megmarad."""
        panel = CheckpointPanel()
        panel.show_checkpoint(dict(_R1B6_EVENT_UNCERTAIN, resume_possible=True))

        panel.set_continuing()

        assert panel._emstop_btn.isEnabled() is True


def _r1f42_real_runner(tmp_path):
    """Valódi TestRunner mock műszerekkel (a GUI-lánc teszthez)."""
    from Prog.src.battery_profile import BatteryProfile
    from Prog.src.instrument_manager import InstrumentManager
    from Prog.src.logger import LogConfig, Logger
    from Prog.src.safety import PsuMode, SafetyManager
    from Prog.src.test_runner import TestRunner, TestRunnerConfig
    from Prog.tests.mock_drivers.mock_dmm import MockDMM
    from Prog.tests.mock_drivers.mock_load import MockLoad
    from Prog.tests.mock_drivers.mock_psu import MockPSU

    profile = BatteryProfile(
        battery_name="Test", manufacturer="FIAMM", model="FG",
        nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
    )
    instruments = InstrumentManager(
        psu=MockPSU(voltage_V=14.4, current_A=1.75),
        load=MockLoad(voltage_V=12.5, current_A=0.7),
        dmm_voltage=MockDMM(voltage_V=12.5, temperature_C=25.0),
        dmm_temperature=MockDMM(voltage_V=0.0, temperature_C=25.0),
    )
    logger = Logger(session_dir=tmp_path, config=LogConfig())
    runner = TestRunner(
        instrument_manager=instruments,
        safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
        logger=logger,
        profile=profile,
        config=TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="test"),
        charge_ctrl_factory=lambda: None,
        discharge_ctrl_factory=lambda: None,
        relax_ctrl_factory=lambda step: None,
    )
    return runner, logger


class TestR1F42ManualCheckpointDemotionReachesPanel:
    """[regresszió, R1 F-42, FINAL B7S/1 + D-B7S-1] A manuális checkpoint
    safe-offja utáni kényszerített poll energia-bizonytalansága (PSU
    commanded=OFF/actual=ON) a TELJES láncon (TestRunner._run_manual_
    checkpoint() -> worker._handle_event -> checkpoint_reached signal ->
    CheckpointPanel.show_checkpoint) a látható figyelmeztetésig jut. A régi
    kódon a safe_off_result PSU=True maradt, a figyelmeztetés rejtve."""

    @staticmethod
    def _run_chain(qapp, tmp_path, monkeypatch, inject_psu_on: bool):
        from Prog.gui.panels.checkpoint_panel import CheckpointPanel
        from Prog.gui.worker import TestRunnerWorker
        from Prog.src.test_runner import StepKind, TestPlan, TestStep, TestType

        runner, logger = _r1f42_real_runner(tmp_path)
        if inject_psu_on:
            original = runner._instruments.safe_all_off

            def _safe_all_off_then_psu_on():
                result = original()
                runner._instruments.psu._output_is_on = True
                return result

            monkeypatch.setattr(runner._instruments, "safe_all_off", _safe_all_off_then_psu_on)
        plan = TestPlan(
            test_type=TestType.BQ_LEARNING_PHYSICAL,
            steps=(TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint"),),
        )
        worker = TestRunnerWorker(runner, plan)
        panel = CheckpointPanel()
        worker.checkpoint_reached.connect(panel.show_checkpoint)
        finished: list = []
        worker.finished.connect(finished.append)

        worker.run()
        logger.close()
        return panel, finished

    def test_demoted_psu_shows_warning_via_worker_signal(self, qapp, tmp_path, monkeypatch):
        panel, finished = self._run_chain(qapp, tmp_path, monkeypatch, inject_psu_on=True)

        assert finished and finished[0].status == "CHECKPOINT_STOPPED"
        assert finished[0].safe_off_result["PSU"] is False
        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is True
        assert "PSU" in panel._safe_off_warning_lbl.text()

    def test_clean_readback_keeps_warning_hidden_via_worker_signal(
        self, qapp, tmp_path, monkeypatch
    ):
        panel, finished = self._run_chain(qapp, tmp_path, monkeypatch, inject_psu_on=False)

        assert finished and finished[0].status == "CHECKPOINT_STOPPED"
        assert finished[0].safe_off_result["PSU"] is True
        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is False
