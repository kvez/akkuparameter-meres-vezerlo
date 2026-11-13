"""
TestRunner unit tesztek.
Stub kontrollereket használ az orchestrációs logika izolált teszteléséhez.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from Prog.src.charge_controller import ChargeState
from Prog.src.discharge_controller import DischargeState
from Prog.src.relax_controller import RelaxConfig, RelaxState
from Prog.src.traditional_learning_controller import LearningState
from Prog.src.golden_gg_controller import GoldenGgState
from Prog.src.ocv_soc_controller import OcvSocState
from Prog.src.chemid_controller import ChemIdState
from Prog.src.iec_dc_resistance_controller import IecDcResistanceState
from Prog.src.test_runner import (
    TestType, StepKind, TestStep, TestPlan,
    TestResult, TestRunnerConfig, TestRunner,
)


# ------------------------------------------------------------------ #
# Stub kontrollerek az orchestrációs logika teszteléséhez            #
# ------------------------------------------------------------------ #

class _StubChargeCtrl:
    """N advance() hívás után CHARGE_DONE állapotba kerül."""
    def __init__(self, steps_to_done: int = 3, fault_at_step: int | None = None):
        self._state = ChargeState.INIT
        self._steps = steps_to_done
        self._fault_at = fault_at_step
        self._tick = 0
        self.accumulated_charge_Ah: float = 0.0
        # Teljes kódbázis review 17. kör (2026-09-05), Metrológia/
        # Adatintegritás P2 lelet: l. TestResultTotalWh — a TestResult
        # total_charge_wh/total_discharge_wh mezőinek regressziós
        # tesztjéhez kell egy nem-nulla Wh-forrás a stub-on.
        self.accumulated_charge_Wh: float = 0.0
        self.fault_reason: str = ""
        self.last_integration_source: str = "PSU_READBACK"
        # Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra
        # P2 lelet: l. TestBuildSample.test_charge_step_populates_psu_set_
        # current_a_and_taper_timer_s.
        self.i_psu_set_A: float = 0.0
        self.taper_timer_s: float = 0.0
        self.extreme_dt_s_count: int = 0
        self.max_observed_dt_s: float = 0.0
        # Review-kör 6 (2026-09-15), C-08a: l. TestBuildSample new
        # PSU-current tesztjei -- alapból None/None, ami a getattr()
        # hívóban a LEGACY (nyers read_all() readback) ágat választja,
        # tehát a meglévő tesztek viselkedése változatlan marad.
        self.i_psu_measured_A: float | None = None
        self.psu_current_readback_valid: bool | None = None
        # Review-kör 6 (2026-09-15), C-08b: l. TestBuildSample új
        # PSU-feszültség tesztjei -- ugyanaz az elv, mint fent.
        self.u_psu_measured_V: float | None = None
        self.psu_voltage_readback_valid: bool | None = None

    @property
    def state(self) -> ChargeState:
        return self._state

    def advance(self, dt_s: float) -> ChargeState:
        self._tick += 1
        self.accumulated_charge_Ah += 0.01
        self.accumulated_charge_Wh += 0.13
        if self._fault_at is not None and self._tick >= self._fault_at:
            self.fault_reason = "STUB_CHARGE_FAULT"
            self._state = ChargeState.FAULT
        elif self._tick >= self._steps:
            self._state = ChargeState.CHARGE_DONE
        return self._state

    def reset(self) -> None:
        self._state = ChargeState.INIT
        self._tick = 0
        self.accumulated_charge_Ah = 0.0
        self.accumulated_charge_Wh = 0.0


class _StubDischargeCtrl:
    """N advance() hívás után DISCHARGE_DONE állapotba kerül."""
    def __init__(self, steps_to_done: int = 3, fault_at_step: int | None = None):
        self._state = DischargeState.INIT
        self._steps = steps_to_done
        self._fault_at = fault_at_step
        self._tick = 0
        self.accumulated_discharge_Ah: float = 0.0
        # Teljes kódbázis review 17. kör (2026-09-05), Metrológia/
        # Adatintegritás P2 lelet — l. _StubChargeCtrl azonos mezője.
        self.accumulated_discharge_Wh: float = 0.0
        self.fault_reason: str = ""
        self.last_integration_source: str = "LOAD_READBACK"
        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # l. TestBuildSample új Load-current tesztjei -- alapból None/None,
        # ami a getattr() hívóban a LEGACY (nyers read_all() readback) ágat
        # választja, tehát a meglévő tesztek viselkedése változatlan marad.
        self.i_load_measured_A: float | None = None
        self.load_current_readback_valid: bool | None = None

    @property
    def state(self) -> DischargeState:
        return self._state

    def advance(self, dt_s: float) -> DischargeState:
        self._tick += 1
        self.accumulated_discharge_Ah += 0.01
        self.accumulated_discharge_Wh += 0.13
        if self._fault_at is not None and self._tick >= self._fault_at:
            self.fault_reason = "STUB_DISCHARGE_FAULT"
            self._state = DischargeState.FAULT
        elif self._tick >= self._steps:
            self._state = DischargeState.DISCHARGE_DONE
        return self._state

    def reset(self) -> None:
        self._state = DischargeState.INIT
        self._tick = 0
        self.accumulated_discharge_Ah = 0.0


class _StubRelaxCtrl:
    """N advance() hívás után RELAX_DONE állapotba kerül."""
    def __init__(self, steps_to_done: int = 2):
        self._state = RelaxState.INIT
        self._steps = steps_to_done
        self._tick = 0

    @property
    def state(self) -> RelaxState:
        return self._state

    def advance(self, dt_s: float) -> RelaxState:
        self._tick += 1
        if self._tick >= self._steps:
            self._state = RelaxState.RELAX_DONE
        return self._state

    def reset(self) -> None:
        self._state = RelaxState.INIT
        self._tick = 0


class _StubLearningCtrl:
    """WP-AK-16: N advance() hívás után DONE állapotba kerül (TraditionalLearningController)."""
    def __init__(self, steps_to_done: int = 3, fault_at_step: int | None = None):
        self._state = LearningState.INIT
        self._steps = steps_to_done
        self._fault_at = fault_at_step
        self._tick = 0
        self.fault_reason: str = ""
        self.accumulated_charge_Ah: float = 0.0
        self.accumulated_discharge_Ah: float = 0.0

    @property
    def state(self) -> LearningState:
        return self._state

    def advance(self, dt_s: float) -> LearningState:
        self._tick += 1
        self.accumulated_charge_Ah += 0.01
        self.accumulated_discharge_Ah += 0.02
        if self._fault_at is not None and self._tick >= self._fault_at:
            self.fault_reason = "STUB_LEARNING_FAULT"
            self._state = LearningState.FAULT
        elif self._tick >= self._steps:
            self._state = LearningState.DONE
        return self._state


class _StubGoldenGgCtrl:
    """WP-AK-16: N advance() hívás után DONE állapotba kerül (GoldenGgController)."""
    def __init__(self, steps_to_done: int = 3, fault_at_step: int | None = None):
        self._state = GoldenGgState.INIT
        self._steps = steps_to_done
        self._fault_at = fault_at_step
        self._tick = 0
        self.fault_reason: str = ""
        self.accumulated_charge_Ah: float = 0.0
        self.accumulated_discharge_Ah: float = 0.0
        # Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        # + Metrológia P2 lelet: l. TestBuildSample.test_golden_gg_wh_fields_
        # use_public_property_not_private_integrator.
        self.accumulated_charge_Wh: float = 0.0
        self.accumulated_discharge_Wh: float = 0.0

    @property
    def state(self) -> GoldenGgState:
        return self._state

    def advance(self, dt_s: float) -> GoldenGgState:
        self._tick += 1
        self.accumulated_charge_Ah += 0.01
        self.accumulated_discharge_Ah += 0.02
        self.accumulated_charge_Wh += 0.13
        self.accumulated_discharge_Wh += 0.26
        if self._fault_at is not None and self._tick >= self._fault_at:
            self.fault_reason = "STUB_GOLDEN_GG_FAULT"
            self._state = GoldenGgState.FAULT
        elif self._tick >= self._steps:
            self._state = GoldenGgState.DONE
        return self._state


class _StubOcvSocCtrl:
    """OcvSocController stub — állapot közvetlenül állítható a tesztben."""
    def __init__(self):
        self._state = OcvSocState.INIT
        self.fault_reason: str = ""
        self.accumulated_charge_Ah: float = 0.0
        self.accumulated_discharge_Ah: float = 0.0

    @property
    def state(self) -> OcvSocState:
        return self._state

    def advance(self, dt_s: float) -> OcvSocState:
        return self._state


class _StubIecDcResistanceCtrl:
    """IecDcResistanceController stub — állapot közvetlenül állítható."""
    def __init__(self):
        self._state = IecDcResistanceState.INIT
        self.fault_reason: str = ""
        self.accumulated_charge_Ah: float = 0.0
        self.accumulated_discharge_Ah: float = 0.0
        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # l. _StubDischargeCtrl azonos hivatkozású mezője.
        self.i_load_measured_A: float | None = None
        self.load_current_readback_valid: bool | None = None

    @property
    def state(self) -> IecDcResistanceState:
        return self._state

    def advance(self, dt_s: float) -> IecDcResistanceState:
        return self._state


class _StubChemIdCtrl:
    """ChemIdCharacterizationController stub — állapot közvetlenül állítható."""
    def __init__(self):
        self._state = ChemIdState.INIT
        self.fault_reason: str = ""
        self.accumulated_charge_Ah: float = 0.0
        self.accumulated_discharge_Ah: float = 0.0

    @property
    def state(self) -> ChemIdState:
        return self._state

    def advance(self, dt_s: float) -> ChemIdState:
        return self._state


# ------------------------------------------------------------------ #
# Fixture helper                                                      #
# ------------------------------------------------------------------ #

def _make_runner(tmp_path, cc=None, dc=None, rc=None, tlc=None, ggc=None, iec=None, config=None):
    from Prog.src.battery_profile import BatteryProfile
    from Prog.src.safety import SafetyManager, PsuMode
    from Prog.src.logger import Logger, LogConfig
    from Prog.src.instrument_manager import InstrumentManager
    from Prog.tests.mock_drivers.mock_psu import MockPSU
    from Prog.tests.mock_drivers.mock_load import MockLoad
    from Prog.tests.mock_drivers.mock_dmm import MockDMM

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
    safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
    logger = Logger(session_dir=tmp_path, config=LogConfig())
    if config is None:
        config = TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="test")

    _cc = cc if cc is not None else _StubChargeCtrl(steps_to_done=3)
    _dc = dc if dc is not None else _StubDischargeCtrl(steps_to_done=3)
    _rc = rc if rc is not None else _StubRelaxCtrl(steps_to_done=2)

    runner = TestRunner(
        instrument_manager=instruments,
        safety=safety,
        logger=logger,
        profile=profile,
        config=config,
        charge_ctrl_factory=lambda: _cc,
        discharge_ctrl_factory=lambda: _dc,
        relax_ctrl_factory=lambda step: _rc,
        traditional_learning_ctrl_factory=(lambda: tlc) if tlc is not None else None,
        golden_gg_ctrl_factory=(lambda: ggc) if ggc is not None else None,
        iec_dc_resistance_ctrl_factory=(lambda: iec) if iec is not None else None,
    )
    return runner, logger


# ------------------------------------------------------------------ #
# Task 1: Adatstruktúrák                                             #
# ------------------------------------------------------------------ #

class TestDataStructures:
    def test_test_type_values(self):
        assert TestType.CHARACTERIZATION.value == "CHARACTERIZATION"
        assert TestType.BQ_LEARNING_PHYSICAL.value == "BQ_LEARNING_PHYSICAL"

    def test_step_kind_values(self):
        for kind in StepKind:
            assert kind.value == kind.name

    def test_test_step_frozen(self):
        step = TestStep(kind=StepKind.CHARGE, label="charge")
        with pytest.raises(Exception):
            step.kind = StepKind.DISCHARGE  # type: ignore[misc]

    def test_test_plan_frozen(self):
        plan = TestPlan(test_type=TestType.CHARACTERIZATION, steps=())
        with pytest.raises(Exception):
            plan.test_type = TestType.BQ_LEARNING_PHYSICAL  # type: ignore[misc]

    def test_test_result_defaults(self):
        result = TestResult(status="DONE")
        assert result.reason == ""
        assert result.total_charge_ah == 0.0
        assert result.total_discharge_ah == 0.0

    def test_runner_config_defaults(self):
        config = TestRunnerConfig()
        assert config.runner_tick_s == 2.0
        assert config.sleep_enabled is True
        assert config.test_name == "unnamed"


class TestPlanChargeDischargeOnly:
    def test_charge_only_plan_has_charge_and_relax_steps(self):
        plan = TestPlan.charge_only()
        assert plan.test_type == TestType.CHARGE_ONLY
        assert len(plan.steps) == 2
        assert plan.steps[0].kind == StepKind.CHARGE
        assert plan.steps[0].label == "charge"
        assert plan.steps[1].kind == StepKind.RELAX
        assert plan.steps[1].label == "relax_after_charge"

    def test_discharge_only_plan_has_single_discharge_step(self):
        plan = TestPlan.discharge_only()
        assert plan.test_type == TestType.DISCHARGE_ONLY
        assert len(plan.steps) == 1
        assert plan.steps[0].kind == StepKind.DISCHARGE
        assert plan.steps[0].label == "discharge"


class TestTestPlanFactories:
    def test_characterization_step_count(self):
        plan = TestPlan.characterization()
        assert len(plan.steps) == 4

    def test_characterization_step_order(self):
        plan = TestPlan.characterization()
        kinds = [s.kind for s in plan.steps]
        assert kinds == [
            StepKind.CHARGE,
            StepKind.RELAX,
            StepKind.DISCHARGE,
            StepKind.RELAX,
        ]

    def test_characterization_labels(self):
        plan = TestPlan.characterization()
        labels = [s.label for s in plan.steps]
        assert labels == [
            "charge",
            "relax_after_charge",
            "discharge",
            "relax_after_discharge",
        ]

    def test_characterization_type(self):
        plan = TestPlan.characterization()
        assert plan.test_type == TestType.CHARACTERIZATION

    def test_characterization_relax_purpose_is_structured_not_label_derived(self):
        """[regresszió, Governance-v2 review-kör 1 (CF-33/DFAM-025
        implicit_dispatch_on_unstructured_label, batch 4.5)] a plan-
        konstrukció explicit relax_purpose-t állít be a 2 RELAX lépésre —
        main_window.py::_make_relax_ctrl() ezt olvassa, NEM a label
        szövegét parse-olja."""
        plan = TestPlan.characterization()
        relax_steps = [s for s in plan.steps if s.kind == StepKind.RELAX]
        assert len(relax_steps) == 2
        assert relax_steps[0].relax_purpose == "post_charge"
        assert relax_steps[1].relax_purpose == "post_discharge"

    def test_bq_learning_step_count(self):
        plan = TestPlan.bq_learning_physical()
        assert len(plan.steps) == 9

    def test_bq_learning_last_step_is_manual_checkpoint(self):
        plan = TestPlan.bq_learning_physical()
        assert plan.steps[-1].kind == StepKind.MANUAL_CHECKPOINT
        assert plan.steps[-1].label == "manual_bq_checkpoint"

    def test_bq_learning_relax_purpose_is_structured_not_label_derived(self):
        """[regresszió, Governance-v2 review-kör 1 (CF-33/DFAM-025, batch
        4.5)] ugyanaz az elvárás, mint a characterization() plan-nél, a
        4 RELAX lépésre (2 ciklus × post_charge/post_discharge)."""
        plan = TestPlan.bq_learning_physical()
        relax_steps = [s for s in plan.steps if s.kind == StepKind.RELAX]
        assert len(relax_steps) == 4
        assert [s.relax_purpose for s in relax_steps] == [
            "post_charge", "post_discharge", "post_charge", "post_discharge",
        ]

    def test_bq_learning_has_two_charge_steps(self):
        plan = TestPlan.bq_learning_physical()
        charge_steps = [s for s in plan.steps if s.kind == StepKind.CHARGE]
        assert len(charge_steps) == 2
        assert charge_steps[0].label == "charge_1"
        assert charge_steps[1].label == "charge_2"

    def test_bq_learning_type(self):
        plan = TestPlan.bq_learning_physical()
        assert plan.test_type == TestType.BQ_LEARNING_PHYSICAL

    def test_chemid_characterization_pba_step_count(self):
        """SUP03-CHEMID-010: önálló teszttípus, egyetlen CHEMID lépéssel
        (a belső ChemIdCharacterizationController kezeli a saját fázisait)."""
        plan = TestPlan.chemid_characterization_pba()
        assert len(plan.steps) == 1
        assert plan.steps[0].kind == StepKind.CHEMID
        assert plan.steps[0].label == "chemid_characterization"

    def test_chemid_characterization_pba_type(self):
        plan = TestPlan.chemid_characterization_pba()
        assert plan.test_type == TestType.CHEMID_CHARACTERIZATION_PBA

    def test_chemid_type_distinct_from_other_modes(self):
        """SUP03-CHEMID-010: 'Ez nem azonos: CHARACTERIZATION, OCV_SOC_
        CHARACTERIZATION, BQ_LEARNING_PHYSICAL tesztmodokkal.'"""
        assert TestType.CHEMID_CHARACTERIZATION_PBA not in (
            TestType.CHARACTERIZATION,
            TestType.OCV_SOC_CHARACTERIZATION,
            TestType.BQ_LEARNING_PHYSICAL,
        )

    def test_traditional_learning_step_count(self):
        """WP-AK-16: önálló teszttípus, egyetlen TRADITIONAL_LEARNING lépéssel
        (a belső TraditionalLearningController kezeli a saját fázisait)."""
        plan = TestPlan.traditional_learning()
        assert len(plan.steps) == 1
        assert plan.steps[0].kind == StepKind.TRADITIONAL_LEARNING
        assert plan.steps[0].label == "traditional_learning"

    def test_traditional_learning_type(self):
        plan = TestPlan.traditional_learning()
        assert plan.test_type == TestType.TRADITIONAL_LEARNING

    def test_golden_gg_step_count(self):
        """WP-AK-16: önálló teszttípus, egyetlen GOLDEN_GG lépéssel (a belső
        GoldenGgController kezeli a saját fázisait)."""
        plan = TestPlan.golden_gg()
        assert len(plan.steps) == 1
        assert plan.steps[0].kind == StepKind.GOLDEN_GG
        assert plan.steps[0].label == "golden_gg"

    def test_golden_gg_type(self):
        plan = TestPlan.golden_gg()
        assert plan.test_type == TestType.GOLDEN_GG

    def test_traditional_learning_and_golden_gg_distinct_from_other_modes(self):
        assert TestType.TRADITIONAL_LEARNING not in (
            TestType.CHARACTERIZATION, TestType.BQ_LEARNING_PHYSICAL,
            TestType.OCV_SOC_CHARACTERIZATION, TestType.CHEMID_CHARACTERIZATION_PBA,
            TestType.GOLDEN_GG,
        )
        assert TestType.GOLDEN_GG not in (
            TestType.CHARACTERIZATION, TestType.BQ_LEARNING_PHYSICAL,
            TestType.OCV_SOC_CHARACTERIZATION, TestType.CHEMID_CHARACTERIZATION_PBA,
            TestType.TRADITIONAL_LEARNING,
        )


# ------------------------------------------------------------------ #
# Task 3: TestRunner __init__ és stop API                            #
# ------------------------------------------------------------------ #

class TestTestRunnerInit:
    def test_initial_status_is_idle(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        assert runner.status == "IDLE"
        logger.close()

    def test_initial_flags_false(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        assert runner.stop_requested is False
        assert runner.emergency_stop_requested is False
        logger.close()

    def test_request_stop_sets_flag(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.request_stop()
        assert runner.stop_requested is True
        logger.close()

    def test_request_emergency_stop_sets_flag_and_reason(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.request_emergency_stop("TEST_REASON")
        assert runner.emergency_stop_requested is True
        assert runner.emergency_stop_reason == "TEST_REASON"
        logger.close()

    def test_request_emergency_stop_default_reason(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.request_emergency_stop()
        assert runner.emergency_stop_reason == "USER_EMERGENCY_STOP"
        logger.close()


# ------------------------------------------------------------------ #
# Task 4: Segédpredikátum metódusok                                  #
# ------------------------------------------------------------------ #

class TestHelperPredicates:
    def test_is_finished_charge_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_charge_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.FAULT
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_charge_running(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        assert runner._is_finished(stub) is False
        logger.close()

    def test_is_finished_discharge_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_relax_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAX_DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_relax_relaxing(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAXING
        assert runner._is_finished(stub) is False
        logger.close()

    def test_controller_faulted_charge_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.FAULT
        assert runner._controller_faulted(stub) is True
        logger.close()

    def test_controller_faulted_charge_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_DONE
        assert runner._controller_faulted(stub) is False
        logger.close()

    def test_controller_faulted_relax(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAXING
        assert runner._controller_faulted(stub) is False
        logger.close()

    def test_is_finished_raises_on_unknown_state_type(self, tmp_path):
        """[regresszió, review-kör 4, S4-07 (P3, DFAM-004 silent_catchall_
        dispatch)] a korábbi `return True` egy ismeretlen state-típust
        csendben "a lépés kész"-ként kezelt -- egy jövőbeli StepKind
        bevezetésekor (ahogy a 2. körben az IEC-nél történt) elég egy
        kimaradt isinstance ág ahhoz, hogy ez a lépés az ELSŐ tick ELŐTT
        "DONE"-nak tűnjön, nulla mintával/méréssel."""
        runner, logger = _make_runner(tmp_path)

        class _UnknownState:
            pass

        stub = _StubChargeCtrl()
        stub._state = _UnknownState()
        with pytest.raises(ValueError, match="Unknown controller state type"):
            runner._is_finished(stub)
        logger.close()

    def test_controller_faulted_raises_on_unknown_state_type(self, tmp_path):
        """[regresszió, review-kör 4, S4-07] l. test_is_finished_raises_on_
        unknown_state_type docstringje ugyanerről a hibaosztályról -- itt a
        korábbi `return False` ("nincs hiba") volt a csendes fail-open."""
        runner, logger = _make_runner(tmp_path)

        class _UnknownState:
            pass

        stub = _StubChargeCtrl()
        stub._state = _UnknownState()
        with pytest.raises(ValueError, match="Unknown controller state type"):
            runner._controller_faulted(stub)
        logger.close()

    def test_controller_for_charge_step(self, tmp_path):
        cc = _StubChargeCtrl()
        runner, logger = _make_runner(tmp_path, cc=cc)
        step = TestStep(StepKind.CHARGE, "charge")
        assert runner._controller_for_step(step) is cc
        logger.close()

    def test_controller_for_unknown_step_raises(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "x")
        with pytest.raises(ValueError):
            runner._controller_for_step(step)
        logger.close()

    # ------------------------------------------------------------------ #
    # WP-AK-16: TRADITIONAL_LEARNING / GOLDEN_GG                          #
    # ------------------------------------------------------------------ #

    def test_is_finished_learning_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubLearningCtrl()
        stub._state = LearningState.DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_learning_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubLearningCtrl()
        stub._state = LearningState.FAULT
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_learning_running(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubLearningCtrl()
        stub._state = LearningState.PRECOND
        assert runner._is_finished(stub) is False
        logger.close()

    def test_controller_faulted_learning_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubLearningCtrl()
        stub._state = LearningState.FAULT
        assert runner._controller_faulted(stub) is True
        logger.close()

    def test_controller_faulted_learning_done_not_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubLearningCtrl()
        stub._state = LearningState.DONE
        assert runner._controller_faulted(stub) is False
        logger.close()

    def test_is_finished_golden_gg_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_golden_gg_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.FAULT
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_golden_gg_running(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.PRECOND
        assert runner._is_finished(stub) is False
        logger.close()

    def test_controller_faulted_golden_gg_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.FAULT
        assert runner._controller_faulted(stub) is True
        logger.close()

    def test_controller_faulted_golden_gg_done_not_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.DONE
        assert runner._controller_faulted(stub) is False
        logger.close()

    def test_controller_for_traditional_learning_step(self, tmp_path):
        tlc = _StubLearningCtrl()
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")
        assert runner._controller_for_step(step) is tlc
        logger.close()

    def test_controller_for_traditional_learning_missing_factory_raises(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")
        with pytest.raises(ValueError):
            runner._controller_for_step(step)
        logger.close()

    def test_controller_for_golden_gg_step(self, tmp_path):
        ggc = _StubGoldenGgCtrl()
        runner, logger = _make_runner(tmp_path, ggc=ggc)
        step = TestStep(StepKind.GOLDEN_GG, "golden_gg")
        assert runner._controller_for_step(step) is ggc
        logger.close()

    def test_controller_for_golden_gg_missing_factory_raises(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.GOLDEN_GG, "golden_gg")
        with pytest.raises(ValueError):
            runner._controller_for_step(step)
        logger.close()

    def test_controller_for_iec_dc_resistance_step(self, tmp_path):
        iec = _StubIecDcResistanceCtrl()
        runner, logger = _make_runner(tmp_path, iec=iec)
        step = TestStep(StepKind.IEC_DC_RESISTANCE, "iec_dc_resistance")
        assert runner._controller_for_step(step) is iec
        logger.close()

    def test_controller_for_iec_dc_resistance_missing_factory_raises(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        step = TestStep(StepKind.IEC_DC_RESISTANCE, "iec_dc_resistance")
        with pytest.raises(ValueError):
            runner._controller_for_step(step)
        logger.close()

    def test_is_finished_iec_dc_resistance_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubIecDcResistanceCtrl()
        stub._state = IecDcResistanceState.DONE
        assert runner._is_finished(stub) is True
        logger.close()

    def test_is_finished_iec_dc_resistance_not_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubIecDcResistanceCtrl()
        stub._state = IecDcResistanceState.LOAD_1
        assert runner._is_finished(stub) is False
        logger.close()

    def test_controller_faulted_iec_dc_resistance(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        stub = _StubIecDcResistanceCtrl()
        stub._state = IecDcResistanceState.FAULT
        assert runner._controller_faulted(stub) is True
        logger.close()


# ------------------------------------------------------------------ #
# Task 5: _build_sample                                               #
# ------------------------------------------------------------------ #

class TestBuildSample:
    def test_charge_step_fields(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["test_name"] == "test"
        assert sample["step_name"] == "charge"
        assert sample["state"] == "CHARGE_CC"
        assert sample["isolation_state"] == "PSU_OUTPUT_OFF_ONLY"
        assert sample["psu_mode"] == "INDEPENDENT"
        assert sample["discharge_current_A"] == 0.0
        assert sample["charge_current_A"] is not None
        assert sample["signed_current_A"] is not None
        assert sample["accumulated_charge_Ah"] == stub.accumulated_charge_Ah
        logger.close()

    def test_charge_step_accumulated_ah_adds_session_baseline_not_just_step_own_value(
        self, tmp_path
    ):
        """[regresszió, review-kör 5 (2026-09-10), GUI/UX F2 (P2, DFAM-021
        rokona)] bq_learning_physical() tervben KÉT KÜLÖN StepKind.CHARGE
        lépés van (charge_1/charge_2) -- charge_2 kezdetén egy FRISS
        ChargeController-példány accumulated_charge_Ah-ja 0-ról indul. A fix
        ELŐTT sample["accumulated_charge_Ah"] = getattr(controller, ...) volt
        -- ez charge_2 alatt a session TELJES (charge_1+charge_2) töltése
        helyett CSAK charge_2 saját, kis értékét mutatta volna, hamis
        "visszaesést" okozva a Live panelen/samples.csv-ben. A session-
        szintű bázist (self._total_charge_ah, itt a charge_1 lezárt
        összege) a step saját, belépéskori bázisához (charge_ah_before)
        képesti DELTÁJÁVAL kell összeadni."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_charge_ah = 5.0  # charge_1 már lezárt, session-szintű bázis
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.accumulated_charge_Ah = 0.2  # charge_2 SAJÁT, friss progressziója
        step = TestStep(StepKind.CHARGE, "charge_2")

        sample = runner._build_sample(step, stub, charge_ah_before=0.0)

        assert sample["accumulated_charge_Ah"] == pytest.approx(5.2)
        logger.close()

    def test_discharge_step_populates_method_origin_when_controller_exposes_it(self, tmp_path):
        """[regresszió, Governance-v2 review-kör 1 (CF-24/DFAM-003 7.
        előfordulás + BAT-RI-001 sértés, batch 4.6)] a DischargeController
        method_origin property-jét a samples.csv-nek is meg kell kapnia."""
        runner, logger = _make_runner(tmp_path)
        stub = _StubDischargeCtrl()
        stub.method_origin = "PROJECT_ENGINEERING_METHOD"
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert sample["method_origin"] == "PROJECT_ENGINEERING_METHOD"
        logger.close()

    def test_discharge_step_populates_synchronization_quality_when_controller_exposes_it(
        self, tmp_path
    ):
        """[regresszió, MAX verifier P2-1 lelete (CF-24 utóellenőrzés,
        batch 4.6)] a DischargeController synchronization_quality
        property-jét is meg kell kapnia a samples.csv-nek — a method_
        origin önmagában nem jelzi a mintavételi módot."""
        runner, logger = _make_runner(tmp_path)
        stub = _StubDischargeCtrl()
        stub.synchronization_quality = "SOFTWARE_TICK_TIMED"
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert sample["synchronization_quality"] == "SOFTWARE_TICK_TIMED"
        logger.close()

    def test_charge_step_method_origin_is_none_when_controller_has_no_rb_measurement(
        self, tmp_path
    ):
        """[regresszió] egy CHARGE lépés controllere (nincs rb_*_mohm
        mérése) NE kapjon method_origin-t sem — a mező kizárólag a
        rb_*_mohm mezőkhöz tartozó jelölés, nem általános session-cimke."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["method_origin"] is None
        logger.close()

    def test_charge_step_populates_psu_set_current_a_and_taper_timer_s(self, tmp_path):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] a
        logger.py CSV_COLUMNS-ban deklarált psu_set_current_A/taper_timer_s
        mezőket _build_sample() korábban sosem töltötte ki."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.i_psu_set_A = 1.75
        stub.taper_timer_s = 42.0
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["psu_set_current_A"] == pytest.approx(1.75)
        assert sample["taper_timer_s"] == pytest.approx(42.0)
        logger.close()

    def test_charge_step_populates_extreme_dt_s_fields(self, tmp_path):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] az
        Integrator.extreme_dt_s_count/max_observed_dt_s (1. kör javítás)
        frissült, de korábban sehol nem került kiolvasásra."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.extreme_dt_s_count = 2
        stub.max_observed_dt_s = 350.0
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["extreme_dt_s_count"] == 2
        assert sample["max_observed_dt_s"] == pytest.approx(350.0)
        logger.close()

    def test_charge_current_uses_controller_value_not_read_all(self, tmp_path):
        """[regresszió, teljes kódbázis review kör 6 (2026-09-15), C-08a,
        DFAM-018 rokona] a CHARGE/GOLDEN_GG(FULL_CHARGE)/TRADITIONAL_
        LEARNING(PRECHARGE/RECHARGE)/OCV_SOC(PRECHARGE)/CHEMID(FULL_
        CHARGE_MANUFACTURER_METHOD)/IEC_DC_RESISTANCE(PRECHARGE) ágak
        korábban MINDIG a nyers read_all() psu_readback_current_A-t
        naplózták charge_current_A/signed_current_A-ként -- a beágyazott
        ChargeController SAJÁT, már hiba-kezelt olvasása (i_psu_
        measured_A/psu_current_readback_valid) figyelmen kívül maradt.
        `_make_runner()` a MockPSU-t 1.75 A-re állítja (l. `readings.
        get("psu_readback_current_A")`), ez a teszt ettől ELTÉRŐ
        controller-oldali értékkel igazolja, hogy a controller property
        élvez elsőbbséget."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.i_psu_measured_A = 1.5  # a MockPSU raw readbackje 1.75 -- SZÁNDÉKOSAN eltérő
        stub.psu_current_readback_valid = True
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == pytest.approx(1.5)
        assert sample["signed_current_A"] == pytest.approx(1.5)
        logger.close()

    def test_charge_current_is_none_not_zero_when_controller_readback_invalid(self, tmp_path):
        """CLAUDE.md §6 "aktív energiaátadás közben kritikus readback hibát
        tilos 0-val csendben helyettesíteni" -- a controller SAJÁT
        readback-hibája (psu_current_readback_valid=False) esetén a
        naplózott értéknek None-nak KELL maradnia, nem 0.0-nak és nem a
        (szintén hiba-érzékeny) nyers read_all() readbacknek."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.i_psu_measured_A = 0.0
        stub.psu_current_readback_valid = False
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] is None
        assert sample["signed_current_A"] is None
        logger.close()

    def test_legacy_controller_without_property_keeps_read_all_value(self, tmp_path):
        """Egy controller, amelyiken nincs i_psu_measured_A/psu_current_
        readback_valid property (getattr() default None-t ad mindkettőre)
        -- a hívónak a NYERS read_all() readbackre kell visszaesnie,
        bit-pontosan a C-08a előtti viselkedéssel megegyezően."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        assert stub.i_psu_measured_A is None
        assert stub.psu_current_readback_valid is None
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == pytest.approx(1.75)
        assert sample["signed_current_A"] == pytest.approx(1.75)
        logger.close()

    def test_psu_voltage_and_current_use_controller_value_and_skip_physical_read(
        self, tmp_path
    ):
        """[regresszió, teljes kódbázis review kör 6 (2026-09-15), C-08b]
        ha a controller EBBEN a tickben már ténylegesen olvasta a PSU
        feszültséget/áramot (mindkét validity flag nem None), a sample-nek
        EZT az értéket kell használnia -- ÉS a read_all()-nek NEM szabad
        egy MÁSODIK, fizikai PSU-olvasást indítania (skip_psu_voltage/
        skip_psu_current=True). A MockPSU raw readbackje (14.4V/1.75A,
        l. _make_runner()) SZÁNDÉKOSAN eltér a controller-értéktől, hogy
        a teszt megkülönböztesse a két forrást."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.u_psu_measured_V = 14.0
        stub.psu_voltage_readback_valid = True
        stub.i_psu_measured_A = 1.5
        stub.psu_current_readback_valid = True
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["psu_readback_voltage_V"] == pytest.approx(14.0)
        assert sample["psu_readback_current_A"] == pytest.approx(1.5)
        assert sample["charge_current_A"] == pytest.approx(1.5)
        assert not runner._instruments.psu.called("measure_output_voltage()")
        assert not runner._instruments.psu.called("measure_output_current()")
        logger.close()

    def test_psu_voltage_is_none_not_raw_when_controller_readback_invalid(self, tmp_path):
        """CLAUDE.md §6 -- ha a controller EBBEN a tickben megpróbálta
        olvasni a PSU-feszültséget, de a readback hibázott
        (psu_voltage_readback_valid=False, u_psu_measured_V=None), a
        naplózott értéknek None-nak KELL maradnia -- sem a controller
        belső (hiba miatti) helykitöltőjének, sem a NYERS read_all()
        readbacknek nem szabad csendben becsúsznia. A fizikai olvasás
        ekkor is skip-elve marad (a controller MÁR próbálkozott EBBEN a
        tickben, egy harmadik olvasás redundáns lenne)."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.u_psu_measured_V = None
        stub.psu_voltage_readback_valid = False
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["psu_readback_voltage_V"] is None
        assert not runner._instruments.psu.called("measure_output_voltage()")
        logger.close()

    def test_stub_without_psu_voltage_property_falls_back_to_instrument_read(self, tmp_path):
        """MAX verifier P3-2 (2026-09-15) pontosítás: a _StubChargeCtrl
        MOST MÁR expose-olja a u_psu_measured_V-t/psu_voltage_readback_
        valid-ot (alapból None-ra), tehát ez a teszt a NONE-ÉRTÉKŰ ágat
        fedi le (a getattr() ekkor is a LEGACY ágat választja) -- a
        teljesen HIÁNYZÓ property esetét (getattr default) a discharge/
        relax stub-ok fedik. A viselkedés mindkét esetben azonos:
        read_all() ténylegesen olvassa a PSU-t, bit-pontosan a C-08b
        előtti viselkedéssel megegyezően."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        assert stub.u_psu_measured_V is None
        assert stub.psu_voltage_readback_valid is None
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["psu_readback_voltage_V"] == pytest.approx(14.4)  # MockPSU default
        assert runner._instruments.psu.called("measure_output_voltage()")
        logger.close()

    def test_u_drop_v_and_diode_power_use_controller_psu_pair(self, tmp_path):
        """[regresszió, C-08b] u_drop_V/diode_power_W (dióda-esés) a C-08a
        előtt is, majd C-08a után is a nyers read_all() u_psu/i_psu párral
        számolt -- C-08b óta ugyanaz az u_psu/i_psu (fent) a controller-
        elsőbbségű, tick-konzisztens merge, tehát ez a számítás is
        automatikusan a controller párjával dolgozik, ha az elérhető."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._instruments.psu.output_commanded_on = True
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.u_psu_measured_V = 14.0
        stub.psu_voltage_readback_valid = True
        stub.i_psu_measured_A = 1.5
        stub.psu_current_readback_valid = True
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        # u_batt a stub-on nincs kitéve -> a raw MockDMM 12.5V-ját használja.
        assert sample["u_drop_V"] == pytest.approx(14.0 - 12.5)
        assert sample["diode_power_W"] == pytest.approx((14.0 - 12.5) * 1.5)
        logger.close()

    def test_no_physical_psu_voltage_read_when_controller_faulted_after_current_read(
        self, tmp_path
    ):
        """Teljes kódbázis review 2. kör (2026-09-15, R2-07, 12. writer-kör):
        ha a controller EBBEN a tickben MÁR olvasta a PSU áramát (a
        feszültséget viszont még nem -- pl. egy tick-közbeni FAULT az
        áram-olvasás UTÁN, a feszültség-olvasás ELŐTT), a read_all()-nek
        SEM a feszültség-, SEM az áram-csatornán nem szabad egy MÁSODIK,
        fizikai PSU-olvasást indítania -- a PSU két csatornája egy fizikai
        eszköz, egy safe-off utáni második lekérdezés akár ~30s blokkoló
        I/O-t jelentene egy bizonyítottan halott/lekapcsolt PSU-n."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.i_psu_measured_A = 3.0
        stub.psu_current_readback_valid = True
        stub.u_psu_measured_V = None
        stub.psu_voltage_readback_valid = None
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert not runner._instruments.psu.called("measure_output_voltage()")
        assert sample["psu_readback_voltage_V"] is None
        assert sample["psu_readback_current_A"] == pytest.approx(3.0)
        logger.close()

    def test_no_physical_psu_read_after_psu_comm_lost(self, tmp_path):
        """R2-07: a controller áram-olvasása HIBÁZOTT ebben a tickben
        (psu_current_readback_valid=False -- PSU_COMM_LOST, a controller
        MÁR emergency_stop()-olt) -- a feszültség-csatornán SEM szabad
        egy fizikai olvasást indítani egy bizonyítottan halott PSU-n."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.FAULT
        stub.i_psu_measured_A = 0.0
        stub.psu_current_readback_valid = False
        stub.u_psu_measured_V = None
        stub.psu_voltage_readback_valid = None
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert not runner._instruments.psu.called("measure_output_voltage()")
        assert not runner._instruments.psu.called("measure_output_current()")
        assert sample["psu_readback_voltage_V"] is None
        assert sample["psu_readback_current_A"] is None
        assert sample["psu_readback_valid"] is False
        logger.close()

    def test_top_of_tick_gate_fault_tick_still_uses_raw_psu_read_all(self, tmp_path):
        """R2-07 anti-regresszió: ha a controller EBBEN a tickben
        EGYIK PSU-csatornát sem próbálta olvasni (mindkét flag None --
        top-of-tick safety-kapu FAULT, l. C-08a F-1), a read_all()-nek
        TOVÁBBRA IS el kell végeznie a nyers fizikai olvasást mindkét
        csatornán -- ez a C-08a F-1 szándékolt nyers-fallback viselkedés,
        nem szabad, hogy egy túl széles skip-kapu véletlenül elnyelje."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        assert stub.psu_current_readback_valid is None
        assert stub.psu_voltage_readback_valid is None
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert runner._instruments.psu.called("measure_output_voltage()")
        assert runner._instruments.psu.called("measure_output_current()")
        assert sample["psu_readback_voltage_V"] == pytest.approx(14.4)
        assert sample["psu_readback_current_A"] == pytest.approx(1.75)
        logger.close()

    def test_discharge_current_uses_controller_value_not_read_all(self, tmp_path):
        """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        pontos Load-oldali testvér a fenti test_charge_current_uses_
        controller_value_not_read_all-nak -- a DischargeController SAJÁT,
        már hiba-kezelt olvasása (i_load_measured_A/load_current_
        readback_valid) élvez elsőbbséget a nyers read_all() readback
        felett, és a read_all() NEM indít egy MÁSODIK, fizikai Load-áram-
        olvasást."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._instruments.load._input_is_on = True  # a MockLoad raw readbackje ekkor 0.7 A
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_CC_RUN
        stub.i_load_measured_A = 5.0  # SZÁNDÉKOSAN eltérő a MockLoad raw 0.7 A-tól
        stub.load_current_readback_valid = True
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert sample["discharge_current_A"] == pytest.approx(5.0)
        assert sample["signed_current_A"] == pytest.approx(-5.0)
        assert sample["load_readback_current_A"] == pytest.approx(5.0)
        assert not runner._instruments.load.called("measure_current()")
        logger.close()

    def test_discharge_current_is_none_not_zero_when_load_readback_invalid(self, tmp_path):
        """CLAUDE.md §6 -- a controller SAJÁT readback-hibája
        (load_current_readback_valid=False) esetén a naplózott értéknek
        None-nak KELL maradnia, nem 0.0-nak és nem a nyers read_all()
        readbacknek."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.FAULT
        stub.i_load_measured_A = 0.0
        stub.load_current_readback_valid = False
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert sample["discharge_current_A"] is None
        assert sample["signed_current_A"] is None
        assert sample["load_readback_current_A"] is None
        assert sample["load_readback_valid"] is False
        assert not runner._instruments.load.called("measure_current()")
        logger.close()

    def test_dead_load_tick_does_not_query_load_voltage(self, tmp_path):
        """Kör 20 Batch E4 (SW20-1, P2, DFAM-001+DFAM-027) -- DISZKRIMINÁLÓ:
        ha a controller EBBEN a tickben megállapította, hogy a Load halott
        (load_current_readback_valid=False -- ez a 6 controller mind a 7
        hívóhelyén azt jelenti, hogy az emergency_stop("LOAD_COMM_LOST")
        és a safe-off MÁR lefutott), a _build_sample() NEM küldhet egy
        MEAS:VOLT?-ot a bizonyítottan halott Loadra (akár ~15s blokkoló
        I/O, rules/10-safety.md #3). Pre-fix ez a teszt bukik."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.FAULT
        stub.i_load_measured_A = 0.0
        stub.load_current_readback_valid = False
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert not runner._instruments.load.called("measure_voltage()")
        assert sample["load_readback_voltage_V"] is None
        logger.close()

    def test_live_load_tick_still_queries_load_voltage(self, tmp_path):
        """ELLENPRÓBA (a SZŰK szerződés őre): ha a controller EBBEN a
        tickben SIKERESEN olvasta a Load-áramot (valid=True, az eszköz
        bizonyítottan él), a Load-feszültség csatorna VÁLTOZATLANUL
        olvasódik -- ezt egy téves, széles (`is not None`) predikátum
        elrontaná, mert egyik controller sem olvassa önállóan a Load-
        feszültséget."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_CC_RUN
        stub.i_load_measured_A = 5.0
        stub.load_current_readback_valid = True
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert runner._instruments.load.called("measure_voltage()")
        assert not runner._instruments.load.called("measure_current()")
        assert sample["load_readback_voltage_V"] is not None
        logger.close()

    def test_controller_without_load_property_still_queries_load_voltage(self, tmp_path):
        """ELLENPRÓBA (`None` ág): egy controller, amelyiken nincs
        load_current_readback_valid property (getattr() default None-t ad)
        -- a Load-feszültség csatorna VÁLTOZATLANUL olvasódik, mert a
        skip_load_voltage kapu KIZÁRÓLAG az explicit `is False` ágon áll
        be, sosem `is not None`-nal."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._instruments.load._input_is_on = True

        class _LegacyDischargeCtrlWithoutLoadProperty:
            def __init__(self):
                self._state = DischargeState.DISCHARGE_CC_RUN
                self.fault_reason = ""
                self.accumulated_discharge_Ah = 0.0

            @property
            def state(self):
                return self._state

        stub = _LegacyDischargeCtrlWithoutLoadProperty()
        assert getattr(stub, "load_current_readback_valid", None) is None
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert runner._instruments.load.called("measure_voltage()")
        assert sample["load_readback_voltage_V"] is not None
        logger.close()

    def test_legacy_discharge_controller_without_load_property_keeps_read_all_value(
        self, tmp_path
    ):
        """Egy controller, amelyiken nincs i_load_measured_A/load_current_
        readback_valid property (getattr() default None-t ad mindkettőre)
        -- a hívónak a NYERS read_all() readbackre kell visszaesnie,
        bit-pontosan a R2-06 előtti viselkedéssel megegyezően."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._instruments.load._input_is_on = True

        class _LegacyDischargeCtrlWithoutLoadProperty:
            def __init__(self):
                self._state = DischargeState.DISCHARGE_CC_RUN
                self.fault_reason = ""
                self.accumulated_discharge_Ah = 0.0

            @property
            def state(self):
                return self._state

        stub = _LegacyDischargeCtrlWithoutLoadProperty()
        assert getattr(stub, "i_load_measured_A", None) is None
        assert getattr(stub, "load_current_readback_valid", None) is None
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert sample["discharge_current_A"] == pytest.approx(0.7)  # MockLoad raw readback
        assert runner._instruments.load.called("measure_current()")
        logger.close()

    def test_iec_dc_resistance_load_1_uses_controller_current_not_read_all(self, tmp_path):
        """R2-06: legalább egy composite (nem sima StepKind.DISCHARGE) ágra
        is igazolva a controller-elsőbbségű Load-áram merge, hogy ne csak
        a StepKind.DISCHARGE lefedettsége bizonyítsa a mechanizmust (a
        C-08a F-5 tesztlefedettségi rés megismétlésének elkerülése)."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._instruments.load._input_is_on = True
        stub = _StubIecDcResistanceCtrl()
        stub._state = IecDcResistanceState.LOAD_1
        stub.i_load_measured_A = 4.0
        stub.load_current_readback_valid = True
        step = TestStep(StepKind.IEC_DC_RESISTANCE, "iec_load_1")

        sample = runner._build_sample(step, stub)

        assert sample["discharge_current_A"] == pytest.approx(4.0)
        assert sample["signed_current_A"] == pytest.approx(-4.0)
        assert not runner._instruments.load.called("measure_current()")
        logger.close()

    def test_discharge_step_fields(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_CC_RUN
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert sample["step_name"] == "discharge"
        assert sample["state"] == "DISCHARGE_CC_RUN"
        assert sample["charge_current_A"] == 0.0
        assert sample["isolation_state"] == "PSU_OUTPUT_OFF_ONLY"
        logger.close()

    def test_discharge_step_accumulated_ah_adds_session_baseline_not_just_step_own_value(
        self, tmp_path
    ):
        """[regresszió, review-kör 5 (2026-09-10), GUI/UX F2 (P2, DFAM-021
        rokona)] pontos testvér a fenti CHARGE-tesztnek --
        bq_learning_physical() discharge_1/discharge_2 lépéspárja ugyanazt
        a hibaosztályt hordozza a kisütés oldalán."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_discharge_ah = 3.0  # discharge_1 már lezárt, session-szintű bázis
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_CC_RUN
        stub.accumulated_discharge_Ah = 0.1  # discharge_2 SAJÁT, friss progressziója
        step = TestStep(StepKind.DISCHARGE, "discharge_2")

        sample = runner._build_sample(step, stub, discharge_ah_before=0.0)

        assert sample["accumulated_discharge_Ah"] == pytest.approx(3.1)
        logger.close()

    def test_relax_step_fields(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAXING
        step = TestStep(StepKind.RELAX, "relax_after_charge")

        sample = runner._build_sample(step, stub)

        assert sample["step_name"] == "relax_after_charge"
        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == 0.0
        logger.close()

    def test_relax_step_wh_fields_reflect_session_totals_not_none(self, tmp_path):
        """[regresszió, Teljes kódbázis review 17. kör (2026-09-05),
        Metrológia/Adatintegritás P2 lelet] a StepKind.RELAX ág az Ah-
        testvérpárt helyesen a session-szintű futó összegből (self.
        _total_charge_ah/_total_discharge_ah) tölti, de a Wh-testvérpár
        korábban NEM állt be itt — a lenti (CHARGE/DISCHARGE-et kizáró)
        getattr(RelaxController, "accumulated_charge_Wh", None) ág
        (RelaxController nem exponálja ezt a property-t) RÁÍRTA None-nal,
        holott az Ah-oszlop ugyanabban a sorban valós, nem-null értéket
        mutatott."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_charge_ah = 1.23
        runner._total_discharge_ah = 4.56
        runner._total_charge_wh = 15.9
        runner._total_discharge_wh = 58.9
        stub = _StubRelaxCtrl()
        stub._state = RelaxState.RELAXING
        step = TestStep(StepKind.RELAX, "relax_after_charge")

        sample = runner._build_sample(step, stub)

        assert sample["accumulated_charge_Ah"] == pytest.approx(1.23)
        assert sample["accumulated_discharge_Ah"] == pytest.approx(4.56)
        assert sample["accumulated_charge_Wh"] == pytest.approx(15.9)
        assert sample["accumulated_discharge_Wh"] == pytest.approx(58.9)
        logger.close()

    def test_golden_gg_full_charge_shows_real_psu_current(self, tmp_path):
        """[regresszió] GOLDEN_GG FULL_CHARGE alatt a PSU ténylegesen aktív —
        signed_current_A NEM lehet csendben 0 (CLAUDE.md 10.4)."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.FULL_CHARGE
        step = TestStep(StepKind.GOLDEN_GG, "golden_gg")

        sample = runner._build_sample(step, stub)

        # MockPSU: current_A=1.75
        assert sample["charge_current_A"] == pytest.approx(1.75)
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == pytest.approx(1.75)
        logger.close()

    def test_golden_gg_system_rate_discharge_shows_real_load_current(self, tmp_path):
        """[regresszió] GOLDEN_GG SYSTEM_RATE_DISCHARGE alatt a Load aktív —
        signed_current_A negatív, a load leolvasásból."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.SYSTEM_RATE_DISCHARGE
        step = TestStep(StepKind.GOLDEN_GG, "golden_gg")
        # MockLoad.measure_current() csak input_on() után ad vissza nem-nulla
        # értéket — ezt hívja a GoldenGgController._begin_system_rate_discharge().
        runner._instruments.load.input_on()

        sample = runner._build_sample(step, stub)

        # MockLoad: current_A=0.7
        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == pytest.approx(0.7)
        assert sample["signed_current_A"] == pytest.approx(-0.7)
        logger.close()

    def test_golden_gg_wh_fields_use_public_property_not_private_integrator(self, tmp_path):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P2] a
        Wh-mezők korábban getattr(controller, "_integrator", None)-t
        kerestek — ez az attribútumnév a GoldenGgController-en (és a másik
        3 összetett controlleren) SOSEM létezett (`_energy_integrator` a
        neve), ezért a Wh-mezők a TELJES session alatt None maradtak a
        samples.csv-ben. Mostantól a publikus accumulated_charge_Wh/
        accumulated_discharge_Wh property-t olvassuk."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.FULL_CHARGE
        stub.accumulated_charge_Wh = 12.5
        stub.accumulated_discharge_Wh = 3.75
        step = TestStep(StepKind.GOLDEN_GG, "golden_gg")

        sample = runner._build_sample(step, stub)

        assert sample["accumulated_charge_Wh"] == pytest.approx(12.5)
        assert sample["accumulated_discharge_Wh"] == pytest.approx(3.75)
        logger.close()

    def test_golden_gg_relax_state_zero_current(self, tmp_path):
        """GOLDEN_GG RELAX_2H/RELAX_5H alatt sem PSU, sem Load nem aktív —
        itt a 0 A helyes, nem maszkolt readback."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.RELAX_2H
        step = TestStep(StepKind.GOLDEN_GG, "golden_gg")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == 0.0
        logger.close()

    def test_ocv_soc_precharge_shows_real_psu_current(self, tmp_path):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] OCV_SOC
        PRECHARGE alatt a PSU ténylegesen aktív — signed_current_A NEM lehet
        csendben 0 (CLAUDE.md 10.4), a GOLDEN_GG-nél már javított mintát követve."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubOcvSocCtrl()
        stub._state = OcvSocState.PRECHARGE
        step = TestStep(StepKind.OCV_SOC, "ocv_soc")

        sample = runner._build_sample(step, stub)

        # MockPSU: current_A=1.75
        assert sample["charge_current_A"] == pytest.approx(1.75)
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == pytest.approx(1.75)
        logger.close()

    def test_ocv_soc_step_discharge_shows_real_load_current(self, tmp_path):
        """OCV_SOC STEP_DISCHARGE alatt a Load aktív — signed_current_A
        negatív, a load leolvasásból."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubOcvSocCtrl()
        stub._state = OcvSocState.STEP_DISCHARGE
        step = TestStep(StepKind.OCV_SOC, "ocv_soc")
        # MockLoad.measure_current() csak input_on() után ad vissza nem-nulla
        # értéket (l. golden_gg-nél is ugyanez a minta).
        runner._instruments.load.input_on()

        sample = runner._build_sample(step, stub)

        # MockLoad: current_A=0.7
        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == pytest.approx(0.7)
        assert sample["signed_current_A"] == pytest.approx(-0.7)
        logger.close()

    def test_ocv_soc_impulse_on_shows_real_load_current(self, tmp_path):
        """Az impulzus-alfázisok (IMPULSE_ON/ACQUIRE/FAST_*) is Load-aktívak."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubOcvSocCtrl()
        stub._state = OcvSocState.IMPULSE_ACQUIRE
        step = TestStep(StepKind.OCV_SOC, "ocv_soc")
        runner._instruments.load.input_on()

        sample = runner._build_sample(step, stub)

        assert sample["discharge_current_A"] == pytest.approx(0.7)
        assert sample["signed_current_A"] == pytest.approx(-0.7)
        logger.close()

    def test_ocv_soc_relax_state_zero_current(self, tmp_path):
        """STEP_RELAX/PRECHARGE_RELAX/LOG_SOC_POINT alatt sem PSU, sem Load
        nem aktív — itt a 0 A helyes, nem maszkolt readback."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubOcvSocCtrl()
        stub._state = OcvSocState.STEP_RELAX
        step = TestStep(StepKind.OCV_SOC, "ocv_soc")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == 0.0
        logger.close()

    def test_iec_dc_resistance_precharge_shows_real_psu_current(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubIecDcResistanceCtrl()
        stub._state = IecDcResistanceState.PRECHARGE
        step = TestStep(StepKind.IEC_DC_RESISTANCE, "iec_dc_resistance")

        sample = runner._build_sample(step, stub)

        # MockPSU: current_A=1.75 (l. _make_runner())
        assert sample["charge_current_A"] == pytest.approx(1.75)
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == pytest.approx(1.75)
        logger.close()

    def test_iec_dc_resistance_load_1_shows_real_load_current(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubIecDcResistanceCtrl()
        stub._state = IecDcResistanceState.LOAD_1
        step = TestStep(StepKind.IEC_DC_RESISTANCE, "iec_dc_resistance")
        runner._instruments.load.input_on()

        sample = runner._build_sample(step, stub)

        # MockLoad: current_A=0.7 (l. _make_runner())
        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == pytest.approx(0.7)
        assert sample["signed_current_A"] == pytest.approx(-0.7)
        logger.close()

    def test_iec_dc_resistance_open_circuit_rest_zero_current(self, tmp_path):
        """OPEN_CIRCUIT_REST/PRECHARGE_RELAX/COMPUTE_RESULT alatt sem PSU,
        sem Load nem aktív -- itt a 0 A helyes, nem maszkolt readback."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubIecDcResistanceCtrl()
        stub._state = IecDcResistanceState.OPEN_CIRCUIT_REST
        step = TestStep(StepKind.IEC_DC_RESISTANCE, "iec_dc_resistance")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == 0.0
        logger.close()

    def test_phase_elapsed_s_passed_through_when_controller_exposes_it(self, tmp_path):
        """[CF-19, governance-v2 kör 2, DFAM-023] _build_sample()-nek
        közvetlenül a controller phase_elapsed_s property-jéből kell
        töltenie a mezőt, ha az létezik (összetett controllerek)."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubOcvSocCtrl()
        stub._state = OcvSocState.STEP_RELAX
        stub.phase_elapsed_s = 123.5  # type: ignore[attr-defined]
        step = TestStep(StepKind.OCV_SOC, "ocv_soc")

        sample = runner._build_sample(step, stub)

        assert sample["phase_elapsed_s"] == pytest.approx(123.5)
        logger.close()

    def test_phase_elapsed_s_none_when_controller_lacks_it(self, tmp_path):
        """[CF-19] egyszerű StepKindek (CHARGE/DISCHARGE/RELAX/stb.)
        controllerein nincs phase_elapsed_s property -- a mező None marad,
        nem 0/hamis érték."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["phase_elapsed_s"] is None
        logger.close()

    def test_chemid_full_charge_shows_real_psu_current(self, tmp_path):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] CHEMID
        FULL_CHARGE_MANUFACTURER_METHOD alatt a PSU ténylegesen aktív —
        signed_current_A NEM lehet csendben 0 (CLAUDE.md 10.4)."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChemIdCtrl()
        stub._state = ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD
        step = TestStep(StepKind.CHEMID, "chemid_characterization")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == pytest.approx(1.75)
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == pytest.approx(1.75)
        logger.close()

    def test_chemid_discharge_shows_real_load_current(self, tmp_path):
        """CHEMID C20_DISCHARGE_TO_MANUFACTURER_MINIMUM alatt a Load aktív —
        signed_current_A negatív, a load leolvasásból."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChemIdCtrl()
        stub._state = ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        step = TestStep(StepKind.CHEMID, "chemid_characterization")
        runner._instruments.load.input_on()

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == pytest.approx(0.7)
        assert sample["signed_current_A"] == pytest.approx(-0.7)
        logger.close()

    def test_chemid_relax_state_zero_current(self, tmp_path):
        """RELAX_5H_FULL/RELAX_5H_EMPTY alatt sem PSU, sem Load nem aktív."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChemIdCtrl()
        stub._state = ChemIdState.RELAX_5H_FULL
        step = TestStep(StepKind.CHEMID, "chemid_characterization")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == 0.0
        logger.close()

    def test_traditional_learning_precharge_shows_real_psu_current(self, tmp_path):
        """[regresszió, release-audit 2026-08-23 2.4/B] TRADITIONAL_LEARNING
        PRECHARGE alatt a PSU ténylegesen aktív — signed_current_A NEM lehet
        csendben 0 (CLAUDE.md 10.4), a GOLDEN_GG-nél már javított mintát követve."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubLearningCtrl()
        stub._state = LearningState.PRECHARGE
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")

        sample = runner._build_sample(step, stub)

        # MockPSU: current_A=1.75
        assert sample["charge_current_A"] == pytest.approx(1.75)
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == pytest.approx(1.75)
        logger.close()

    def test_traditional_learning_recharge_shows_real_psu_current(self, tmp_path):
        """RECHARGE ugyanúgy PSU-aktív fázis, mint PRECHARGE."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubLearningCtrl()
        stub._state = LearningState.RECHARGE
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == pytest.approx(1.75)
        assert sample["signed_current_A"] == pytest.approx(1.75)
        logger.close()

    def test_traditional_learning_discharge_1_shows_real_load_current(self, tmp_path):
        """[regresszió, release-audit 2026-08-23 2.4/B] DISCHARGE_1 alatt a
        Load aktív — signed_current_A negatív, a load leolvasásból."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubLearningCtrl()
        stub._state = LearningState.DISCHARGE_1
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")
        runner._instruments.load.input_on()

        sample = runner._build_sample(step, stub)

        # MockLoad: current_A=0.7
        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == pytest.approx(0.7)
        assert sample["signed_current_A"] == pytest.approx(-0.7)
        logger.close()

    def test_traditional_learning_discharge_2_shows_real_load_current(self, tmp_path):
        """DISCHARGE_2 ugyanúgy Load-aktív fázis, mint DISCHARGE_1."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubLearningCtrl()
        stub._state = LearningState.DISCHARGE_2
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")
        runner._instruments.load.input_on()

        sample = runner._build_sample(step, stub)

        assert sample["discharge_current_A"] == pytest.approx(0.7)
        assert sample["signed_current_A"] == pytest.approx(-0.7)
        logger.close()

    def test_traditional_learning_relax_state_zero_current(self, tmp_path):
        """TRADITIONAL_LEARNING RELAX_1/RELAX_2 stb. alatt sem PSU, sem Load
        nem aktív — itt a 0 A helyes, nem maszkolt readback."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubLearningCtrl()
        stub._state = LearningState.RELAX_1
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] == 0.0
        assert sample["discharge_current_A"] == 0.0
        assert sample["signed_current_A"] == 0.0
        logger.close()

    def test_traditional_learning_accumulated_ah_reflects_live_controller_not_frozen_total(
        self, tmp_path,
    ):
        """[regresszió, session_fg1_20260823_200337 nyomán] Korábban a
        TRADITIONAL_LEARNING ág az accumulated_charge_Ah/discharge_Ah
        mezőkbe a runner szintű self._total_charge_ah/self._total_
        discharge_ah befagyott futó összeget írta (ami erre a StepKind-ra
        SOSEM frissült, l. TestRunStep) — a "Kapacitás integrátor" grafikon
        és a Töltve/Kisütve mezők ezért a TELJES traditional_learning
        session alatt 0-n álltak, holott a controller saját élő
        accumulated_charge_Ah/discharge_Ah property-je a valódi, aktuálisan
        felhalmozott értéket mutatta."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_charge_ah = 0.0
        runner._total_discharge_ah = 0.0
        stub = _StubLearningCtrl()
        stub._state = LearningState.DISCHARGE_1
        stub.accumulated_charge_Ah = 1.23
        stub.accumulated_discharge_Ah = 4.56
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")

        sample = runner._build_sample(step, stub)

        assert sample["accumulated_charge_Ah"] == pytest.approx(1.23)
        assert sample["accumulated_discharge_Ah"] == pytest.approx(4.56)
        logger.close()

    def test_golden_gg_accumulated_ah_reflects_live_controller_not_frozen_total(
        self, tmp_path,
    ):
        """[regresszió] ugyanaz a hiba, mint a TRADITIONAL_LEARNING ágban —
        l. fenti teszt docstringje."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_charge_ah = 0.0
        runner._total_discharge_ah = 0.0
        stub = _StubGoldenGgCtrl()
        stub._state = GoldenGgState.SYSTEM_RATE_DISCHARGE
        stub.accumulated_charge_Ah = 2.34
        stub.accumulated_discharge_Ah = 5.67
        step = TestStep(StepKind.GOLDEN_GG, "golden_gg")

        sample = runner._build_sample(step, stub)

        assert sample["accumulated_charge_Ah"] == pytest.approx(2.34)
        assert sample["accumulated_discharge_Ah"] == pytest.approx(5.67)
        logger.close()

    def test_charge_step_psu_readback_failure_stays_none(self, tmp_path):
        """[regresszió, release-audit 2026-08-23 2.4/A] PSU áram-readback
        hiba (SCPI/VISA kivétel -> None) esetén charge_current_A/
        signed_current_A NEM eshet csendben 0.0-ra aktív töltés alatt
        (CLAUDE.md 10.4 "TILOS") — None marad, psu_readback_valid jelzi."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        def _raise() -> float:
            raise RuntimeError("SCPI timeout")

        runner._instruments.psu.measure_output_current = _raise  # type: ignore[method-assign]

        sample = runner._build_sample(step, stub)

        assert sample["charge_current_A"] is None
        assert sample["signed_current_A"] is None
        assert sample["psu_readback_valid"] is False
        logger.close()

    def test_discharge_step_load_readback_failure_stays_none(self, tmp_path):
        """[regresszió, release-audit 2026-08-23 2.4/A] Load áram-readback
        hiba esetén discharge_current_A/signed_current_A NEM eshet csendben
        0.0-ra/-0.0-ra aktív kisütés alatt — None marad."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_CC_RUN
        step = TestStep(StepKind.DISCHARGE, "discharge")

        def _raise() -> float:
            raise RuntimeError("SCPI timeout")

        runner._instruments.load.measure_current = _raise  # type: ignore[method-assign]

        sample = runner._build_sample(step, stub)

        assert sample["discharge_current_A"] is None
        assert sample["signed_current_A"] is None
        assert sample["load_readback_valid"] is False
        logger.close()

    def test_readback_valid_flags_true_on_healthy_reading(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["psu_readback_valid"] is True
        assert sample["load_readback_valid"] is True
        logger.close()

    def test_u_drop_computed_when_psu_output_on(self, tmp_path):
        """u_drop_V/diode_power_W (dióda-esés) csak PSU OUTPUT ON mellett
        értelmezhető -- ez a teszt a PSU-t explicit bekapcsolt állapotba
        hozza (a valós output_on() hívás mintáját követve), mielőtt a
        mintát épít."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        # MockPSU: voltage_V=14.4, MockDMM: voltage_V=12.5 → u_drop = 14.4 - 12.5 = 1.9
        runner._instruments.psu.output_on()
        stub = _StubChargeCtrl()
        step = TestStep(StepKind.CHARGE, "charge")
        sample = runner._build_sample(step, stub)
        assert sample["u_drop_V"] == pytest.approx(14.4 - 12.5, abs=0.01)
        assert sample["diode_power_W"] is not None
        logger.close()

    def test_u_drop_none_when_psu_output_off(self, tmp_path):
        """[regresszió] A felhasználó kérésére: a dióda-esés (u_drop_V/
        diode_power_W) fizikailag értelmetlen, amíg a PSU output NINCS
        bekapcsolva (pl. relax/kisütés alatt) -- korábban a mező feltétel
        nélkül számolódott a psu_readback_voltage_V/battery_voltage_V
        különbségéből, függetlenül attól, hogy a PSU ténylegesen hajt-e
        áramot. A GUI/log ilyenkor "-"-t (None) mutasson, ne egy
        félrevezető számot."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        assert runner._instruments.psu.output_commanded_on is False
        stub = _StubChargeCtrl()
        step = TestStep(StepKind.CHARGE, "charge")
        sample = runner._build_sample(step, stub)
        assert sample["u_drop_V"] is None
        assert sample["diode_power_W"] is None
        logger.close()

    def test_timestamp_present(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        step = TestStep(StepKind.CHARGE, "charge")
        sample = runner._build_sample(step, stub)
        assert sample["timestamp_iso"] is not None
        assert sample["elapsed_s"] is not None
        logger.close()

    def test_controller_u_batt_v_preferred_over_independent_dmm_read(self, tmp_path):
        """2026-08-23 traditional_learning OV incidens elemzés: ha a
        controller expose-olja a saját, tick-konzisztens u_batt_V-jét, a
        sample-nek EZT kell használnia — nem egy második, deszinkron
        InstrumentManager.read_all() DMM-lekérdezést (CLAUDE.md §9)."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)

        class _StubWithOwnVoltage(_StubChargeCtrl):
            u_batt_V = 29.775  # eltér a MockDMM 12.5V-os beállított értékétől

        stub = _StubWithOwnVoltage()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["battery_voltage_V"] == pytest.approx(29.775)
        assert not runner._instruments.dmm_voltage.called("read_voltage()")
        logger.close()

    def test_stub_without_u_batt_v_falls_back_to_instrument_read(self, tmp_path):
        """Ha a controller NEM expose-olja a u_batt_V-t (pl. a régi
        stub-ok/kontrollerek), a viselkedés változatlan marad: a
        read_all() olvassa ki a DMM-et."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["battery_voltage_V"] == pytest.approx(12.5)  # MockDMM default
        assert runner._instruments.dmm_voltage.called("read_voltage()")
        logger.close()

    def test_controller_battery_temperature_c_preferred_over_independent_dmm_read(self, tmp_path):
        """Ugyanaz az elv, mint test_controller_u_batt_v_preferred_over_
        independent_dmm_read-nél, de a hőmérséklet-DMM-re (l.
        TraditionalLearningController.battery_temperature_C docstringje)."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)

        class _StubWithOwnTemperature(_StubChargeCtrl):
            battery_temperature_C = 31.4  # eltér a MockDMM 22.0°C-os defaultjától

        stub = _StubWithOwnTemperature()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["battery_temperature_C"] == pytest.approx(31.4)
        assert sample["dmm_temperature_valid"] is True
        assert not runner._instruments.dmm_temperature.called("read_temperature()")
        logger.close()

    def test_stub_without_battery_temperature_c_falls_back_to_instrument_read(self, tmp_path):
        """Ha a controller NEM expose-olja a battery_temperature_C-t, a
        viselkedés változatlan marad: a read_all() olvassa ki a DMM_T-t."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["battery_temperature_C"] == pytest.approx(25.0)  # _make_runner fixture default
        assert runner._instruments.dmm_temperature.called("read_temperature()")
        logger.close()

    def test_stub_temp_dmm_valid_false_overrides_stale_battery_temperature(self, tmp_path):
        """Review-kör 3 (2026-09-09), Metrológia M3-01 (P1, DFAM-001+
        DFAM-018): korábban sample["dmm_temperature_valid"] =
        battery_temp is not None volt -- mivel minden controller a
        SIKERTELEN hőmérséklet-DMM-olvasás esetén is megtartja az utolsó
        (stale) battery_temperature_C értéket, ez a mező GYAKORLATILAG
        MINDIG True-t adott, akkor is, ha az adott tickben ténylegesen
        kiesett a hőmérséklet-DMM. Most a controller saját temp_dmm_valid
        property-jét kell előnyben részesítenie, a dmm_valid (feszültség)
        testvér-mintájával azonosan."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)

        class _StubWithStaleTemperature(_StubChargeCtrl):
            battery_temperature_C = 31.4  # stale, korábbi sikeres olvasásból
            temp_dmm_valid = False  # ebben a tickben a DMM-olvasás elbukott

        stub = _StubWithStaleTemperature()
        stub._state = ChargeState.CHARGE_CC
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["battery_temperature_C"] == pytest.approx(31.4)
        assert sample["dmm_temperature_valid"] is False
        logger.close()

    def test_charge_step_wh_cross_direction_uses_session_fallback_not_none(self, tmp_path):
        """[regresszió, Teljes kódbázis review 3. kör (2026-08-29),
        Metrológia/Adatintegritás P2 lelet] A Wh-testvérpár (2. kör) nem
        kapott keresztirányú session-szintű fallback-et CHARGE/DISCHARGE
        StepKind-nál, ellentétben az Ah-val: egy CHARGE lépés
        accumulated_discharge_Wh mezője a ChargeController-en SOSEM
        létező attribútum miatt mindig None maradt, holott a runner
        self._total_discharge_wh futó összege (l. _flush_totals())
        ténylegesen tartalmazza a korábbi DISCHARGE lépés(ek) Wh-ját."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_discharge_wh = 12.34
        stub = _StubChargeCtrl()
        stub._state = ChargeState.CHARGE_CC
        stub.accumulated_charge_Wh = 5.67
        step = TestStep(StepKind.CHARGE, "charge")

        sample = runner._build_sample(step, stub)

        assert sample["accumulated_charge_Wh"] == pytest.approx(5.67)
        assert sample["accumulated_discharge_Wh"] == pytest.approx(12.34)
        logger.close()

    def test_discharge_step_wh_cross_direction_uses_session_fallback_not_none(self, tmp_path):
        """[regresszió] ugyanaz a hiba tükörképe DISCHARGE StepKind-nál —
        l. fenti teszt docstringje."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_charge_wh = 9.87
        stub = _StubDischargeCtrl()
        stub._state = DischargeState.DISCHARGE_CC_RUN
        stub.accumulated_discharge_Wh = 3.21
        step = TestStep(StepKind.DISCHARGE, "discharge")

        sample = runner._build_sample(step, stub)

        assert sample["accumulated_discharge_Wh"] == pytest.approx(3.21)
        assert sample["accumulated_charge_Wh"] == pytest.approx(9.87)
        logger.close()

    def test_charge_step_flush_totals_accumulates_wh_alongside_ah(self, tmp_path):
        """[regresszió] a _run_step()-beli _flush_totals() a Wh-testvérpárt
        (self._total_charge_wh/_total_discharge_wh) is frissítse, ugyanúgy
        mint az Ah-t — enélkül a fenti fallback örökre 0.0-n állna."""
        cc = _StubChargeCtrl(steps_to_done=3)
        cc.accumulated_charge_Wh = 0.0

        def _advance(dt_s: float):
            cc._tick += 1
            cc.accumulated_charge_Ah += 0.01
            cc.accumulated_charge_Wh += 0.15
            if cc._tick >= cc._steps:
                cc._state = ChargeState.CHARGE_DONE
            return cc._state

        cc.advance = _advance  # type: ignore[method-assign]
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")

        runner._run_step(0, step)

        assert runner._total_charge_wh == pytest.approx(0.45, abs=0.001)
        logger.close()


# ------------------------------------------------------------------ #
# Task 6: Stop metódusok (_emergency_stop, _graceful_stop, _run_manual_checkpoint)  #
# ------------------------------------------------------------------ #

class TestStopMethods:
    def test_emergency_stop_returns_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        result = runner._emergency_stop("TEST_REASON")
        assert result.status == "FAULT"
        assert result.reason == "TEST_REASON"
        assert runner.status == "FAULT"
        logger.close()

    def test_emergency_stop_accumulates_ah(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._total_charge_ah = 1.5
        runner._total_discharge_ah = 1.2
        result = runner._emergency_stop("TEST")
        assert result.total_charge_ah == pytest.approx(1.5)
        assert result.total_discharge_ah == pytest.approx(1.2)
        logger.close()

    def test_graceful_stop_returns_stopped(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        result = runner._graceful_stop("USER_STOP")
        assert result.status == "STOPPED"
        assert result.reason == "USER_STOP"
        assert runner.status == "STOPPED"
        logger.close()

    def test_emergency_stop_logging_failure_preserves_safe_off_result(self, tmp_path):
        """[regresszió, Teljes kódbázis review 19. kör (2026-09-06), Session/
        Workflow P2 lelet] a safe-off MINDIG elsőként, védetten fut — de az
        utána következő naplózás korábban védtelen volt. Egy I/O-hiba itt
        kiszállt volna egészen worker.py külső catch-all-jáig, ami egy ÚJ,
        szintetikus TestResult-ot gyártott volna safe_off_attempted=False/
        safe_off_result={} alapértékkel, csendesen elveszítve a MÁR
        sikeresen kiszámolt safe-off eredményt."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)

        def _raise_on_checkpoint(_state):
            raise OSError("simulated disk full")
        logger.write_checkpoint = _raise_on_checkpoint  # type: ignore[method-assign]

        events = []
        runner.on_event = lambda ev: events.append(ev)

        result = runner._emergency_stop("TEST_REASON")

        assert result.status == "FAULT"
        assert result.safe_off_attempted is True
        assert result.safe_off_result == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        assert any(e.get("event_code") == "EMERGENCY_STOP_LOGGING_FAILED" for e in events)
        assert any(e.get("event_code") == "EMERGENCY_STOP" for e in events)

    def test_graceful_stop_logging_failure_preserves_safe_off_result(self, tmp_path):
        """L. test_emergency_stop_logging_failure_preserves_safe_off_result
        ugyanerről a mintáról, itt a GRACEFUL_STOP útvonalon."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)

        def _raise_on_checkpoint(_state):
            raise OSError("simulated disk full")
        logger.write_checkpoint = _raise_on_checkpoint  # type: ignore[method-assign]

        events = []
        runner.on_event = lambda ev: events.append(ev)

        result = runner._graceful_stop("USER_STOP")

        assert result.status == "STOPPED"
        assert result.safe_off_attempted is True
        assert result.safe_off_result == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        assert any(e.get("event_code") == "GRACEFUL_STOP_LOGGING_FAILED" for e in events)
        assert any(e.get("event_code") == "GRACEFUL_STOP" for e in events)

    def test_manual_checkpoint_returns_stopped(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        runner._total_charge_ah = 1.5
        runner._total_discharge_ah = 1.4
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_manual_checkpoint(8, step)
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        assert result.total_charge_ah == pytest.approx(1.5)
        assert result.total_discharge_ah == pytest.approx(1.4)
        # [regresszió, review-kör 5, Session/Workflow S5-07 (P3)] a másik
        # 3 terminális/félig-terminális ág (DONE/FAULT/STOPPED) mindegyike
        # frissíti a runner SAJÁT self.status mezőjét -- ez az ág korábban
        # NEM, "RUNNING"-on ragadt volna.
        assert runner.status == "CHECKPOINT_STOPPED"
        logger.close()

    def test_manual_checkpoint_calls_on_event(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        events = []
        runner.on_event = events.append
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        runner._run_manual_checkpoint(8, step)
        assert len(events) == 1
        assert events[0]["event_code"] == "MANUAL_BQ_CHECKPOINT_REACHED"
        assert events[0]["status"] == "CHECKPOINT_STOPPED"
        assert events[0]["resume_possible"] is False          # ← volt: True
        assert events[0]["checkpoint_is_terminal"] is True   # ← új assertion
        assert "next_step_index" in events[0]
        # Kör 18 (2026-09-16), hardver/instrument-control reviewer P2-2:
        # a checkpoint_panel.py-nak szüksége van a tényleges safe-off
        # eredményre, hogy ne állítson feltétel nélkül "PSU OFF, Load
        # OFF"-ot -- ez az esemény az EGYETLEN csatorna, ami eljut hozzá.
        assert "safe_off_result" in events[0]
        assert events[0]["safe_off_result"]["PSU"] is True
        assert events[0]["safe_off_result"]["Load"] is True
        logger.close()

    def test_manual_checkpoint_event_carries_uncertain_safe_off_result(self, tmp_path):
        """[regresszió, Kör 18 MAX-effort review, hardver/instrument-
        control reviewer P2-2] ha a PSU/Load OFF parancs bizonytalan, az
        eseménynek ezt is hordoznia kell -- korábban a
        MANUAL_BQ_CHECKPOINT_REACHED esemény egyáltalán nem tartalmazta
        a safe_off_result kulcsot, a checkpoint_panel.py ezért sosem
        tudott figyelmeztetni a kézi BQ-munka teljes ideje alatt."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        events = []
        runner.on_event = events.append
        runner._instruments.psu.raise_on_safe_off = True
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        runner._run_manual_checkpoint(8, step)
        checkpoint_event = next(
            e for e in events if e["event_code"] == "MANUAL_BQ_CHECKPOINT_REACHED"
        )
        assert checkpoint_event["safe_off_result"]["PSU"] is False
        logger.close()

    def test_manual_checkpoint_performs_guaranteed_safe_off(self, tmp_path):
        """[regresszió, MAX verifier V-1 (batch 4.1) + NV-2 (batch 4.1
        follow-up)] a `_run_manual_checkpoint()` (CHECKPOINT_STOPPED
        útvonal) a DONE ághoz hasonlóan garantált safe-offot ad — a
        `TestResult`-nak `safe_off_attempted=True`-t és a tényleges
        `safe_off_result`-ot kell tartalmaznia, ÉS a mock PSU/Load
        `safe_off()`-jának ténylegesen le kell futnia. Korábban (a MAX
        verifier V-1 leletéig) ez az ág egyáltalán nem hívott safe-offot —
        ennek a tesztnek NULLA regressziós lefedettsége volt, amíg a
        verifier ezt (NV-2) ki nem mutatta."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_manual_checkpoint(8, step)
        logger.close()
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.safe_off_attempted is True
        assert result.safe_off_result == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        assert "safe_off()" in runner._instruments.psu.calls
        assert "safe_off()" in runner._instruments.load.calls

    def test_manual_checkpoint_nulls_active_controller_so_resume_time_stop_does_not_reabort(
        self, tmp_path
    ):
        """[regresszió, MAX verifier V-5 (batch 4.1) + NV-2 (batch 4.1
        follow-up) + F3 pontosítás (batch 4.1 follow-up, 3. kör)] a
        checkpoint elérésekor a MEGELŐZŐ (már véglegesen befejezett) lépés
        controllere még `self._active_controller`-ben állhat (ezt a
        `_run_step()` állítja be minden CHARGE/DISCHARGE lépés elején). A
        `_run_manual_checkpoint()` V-5 fixje ezt itt nullázza — enélkül egy
        resume-kor (`start_step_index=next`) `run()` ELŐTT érkező
        vészleállítás újra meghívná a MÁR lezárt controller saját
        `emergency_stop()`-ját, téves eredmény-klasszifikációt okozva (pl.
        Traditional Learning VERIFIED/AWAITING → FAILED egy már lezárt
        lépésen).

        F3 (MAX verifier, 3. kör): az eredeti verzió a `bq_learning_
        physical()` tervet használta, ahol a checkpoint az UTOLSÓ lépés
        (`next_step_index == len(steps)`) — a `run(plan, next_step_index)`
        emiatt a `test_runner.py:412` érvénytelen-index kapun rövidre zárt
        volna, a for-ciklusba be sem lépve, tehát a `calls == []` asszerció
        VAKON teljesült volna akkor is, ha a nullázás nem működne. Egy
        SAJÁT, 3 lépéses terv (CHARGE → MANUAL_CHECKPOINT → RELAX) ahol a
        checkpoint NEM az utolsó lépés, ténylegesen végigfuttatja a resume
        útvonalat a RELAX lépésig, ahol a vészleállítás a `run()` for-
        ciklusának tetején, a RELAX `_run_step()`-je ELŐTT sül el."""
        calls: list[str] = []

        class _StubChargeCtrlWithAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                calls.append(reason)

        cc = _StubChargeCtrlWithAbort(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(
                TestStep(StepKind.CHARGE, "charge"),
                TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint"),
                TestStep(StepKind.RELAX, "relax_after_checkpoint"),
            ),
        )
        runner._active_plan = plan
        # A checkpoint ELÉRÉSEKOR self._active_controller MÉG a megelőző,
        # már befejezett charge lépés controllerére mutat — ezt szimuláljuk
        # itt közvetlenül, ugyanúgy, ahogy a valós run()-ban a _run_step()
        # állítaná be a checkpoint ELŐTTI lépésnél.
        runner._active_controller = cc
        step = plan.steps[1]
        checkpoint_result = runner._run_manual_checkpoint(1, step)
        assert checkpoint_result.next_step_index == 2
        assert checkpoint_result.resume_possible is True
        assert runner._active_controller is None

        runner.reset_control_flags()
        runner.request_emergency_stop("TEST_RESUME_TIME_STOP")
        result = runner.run(plan, checkpoint_result.next_step_index)
        logger.close()

        assert result.status == "FAULT"
        assert calls == []  # a checkpoint ELŐTTI, már lezárt controller NEM abortálódik újra

    def test_manual_checkpoint_logging_failure_does_not_break_checkpoint_event(self, tmp_path):
        """[regresszió, MAX verifier NV-4 (batch 4.1 follow-up)] a másik 3
        terminális ág (DONE/_emergency_stop()/_graceful_stop()) mindegyike
        védve van egy log_event()/write_checkpoint() I/O hibától — ez az ág
        korábban nem volt. L. test_graceful_stop_logging_failure_preserves_
        safe_off_result ugyanerről a mintáról."""
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()

        def _raise_on_checkpoint(_state):
            raise OSError("simulated disk full")
        logger.write_checkpoint = _raise_on_checkpoint  # type: ignore[method-assign]

        events = []
        runner.on_event = lambda ev: events.append(ev)
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")

        result = runner._run_manual_checkpoint(8, step)

        assert result.status == "CHECKPOINT_STOPPED"
        assert result.safe_off_attempted is True
        assert result.safe_off_result == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        assert any(e.get("event_code") == "MANUAL_CHECKPOINT_LOGGING_FAILED" for e in events)
        assert any(e.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED" for e in events)


# ------------------------------------------------------------------ #
# Task 1 (6C): resume_possible + checkpoint_is_terminal              #
# ------------------------------------------------------------------ #

class TestCheckpointTerminal:
    def test_testresult_resume_possible_default_false(self):
        r = TestResult(status="DONE")
        assert r.resume_possible is False

    def test_testresult_next_step_index_default_zero(self):
        r = TestResult(status="DONE")
        assert r.next_step_index == 0

    def test_checkpoint_is_terminal_for_bq_learning(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_manual_checkpoint(8, step)
        assert result.resume_possible is False
        assert result.next_step_index == 9
        logger.close()

    def test_checkpoint_resume_possible_in_event_dict(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        events = []
        runner.on_event = events.append
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        runner._run_manual_checkpoint(8, step)
        assert events[0]["resume_possible"] is False
        assert events[0]["checkpoint_is_terminal"] is True
        logger.close()

    def test_terminal_checkpoint_is_terminal_persisted_to_checkpoint_json(self, tmp_path):
        """[regresszió, Teljes kódbázis review 20. kör (2026-09-06), Session/
        Workflow P1 lelet] checkpoint_is_terminal korábban csak az in-memory
        event-dictbe került, a lemezre írt checkpoint.json-ba NEM —
        session_recovery.evaluate_resume() emiatt egy sikeresen, a terv
        VÉGÉig eljutott checkpointot is folytathatónak minősíthetett."""
        import json
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        runner._run_manual_checkpoint(8, step)
        checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
        assert checkpoint["checkpoint_is_terminal"] is True
        logger.close()

    def test_terminal_checkpoint_auto_closes_logger(self, tmp_path):
        """Terminális checkpoint után run() automatikusan zárja a logger-t."""
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.bq_learning_physical())
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.resume_possible is False
        assert logger._closed is True
        logger.close()  # idempotens — nem dob hibát

    def test_terminal_checkpoint_close_failure_does_not_misclassify_as_fault(self, tmp_path):
        """[regresszió, MAX verifier F1 (batch 4.1 follow-up, 3. kör)] a
        `run()`-ban a terminális CHECKPOINT_STOPPED ág `self._logger.
        close()` hívása korábban védtelen volt — ez volt az EGYETLEN
        védtelen terminális-úti logger-I/O a fájlban (a másik 4 —
        DONE/_emergency_stop/_graceful_stop/_run_manual_checkpoint — mind
        try/except-ben van). Egy itt keletkező I/O hiba enélkül a run()
        külső try/except-jéig futott volna fel, ami egy MÁR sikeresen elért
        terminális BQ checkpointot UNHANDLED_EXCEPTION FAULT-tá minősített
        volna át, eldobva a legitim CHECKPOINT_STOPPED eredményt (egy
        MÁSODIK, redundáns safe-off kísérlet mellett)."""
        runner, logger = _make_runner(tmp_path)

        def _raise_on_close():
            raise OSError("simulated disk full")
        logger.close = _raise_on_close  # type: ignore[method-assign]

        events = []
        runner.on_event = lambda ev: events.append(ev)
        result = runner.run(TestPlan.bq_learning_physical())

        assert result.status == "CHECKPOINT_STOPPED"
        assert result.resume_possible is False
        assert any(e.get("event_code") == "CHECKPOINT_CLOSE_FAILED" for e in events)

    def test_invalid_start_step_index_still_performs_safe_off_and_closes_logger(self, tmp_path):
        """[regresszió, governance-v2 review-kör 1, CF-44/F9, DFAM-001
        sibling] a run() elején lévő start_step_index tartomány-ellenőrzés
        korábban CSUPASZ TestResult(status='FAULT', ...)-ot adott vissza --
        safe_all_off()/logger.close() NÉLKÜL, ellentétben a run() MINDEN MÁS
        terminális ágával (DONE/_emergency_stop/_graceful_stop/
        _run_manual_checkpoint). Elérhető: session_recovery.evaluate_resume()
        nem validálja a checkpoint next_safe_step_index-ét a TÉNYLEGES
        test_plan.steps hosszához képest."""
        runner, logger = _make_runner(tmp_path)
        events = []
        runner.on_event = lambda ev: events.append(ev)

        result = runner.run(TestPlan.characterization(), start_step_index=99)

        assert result.status == "FAULT"
        assert "start_step_index=99" in result.reason
        assert result.safe_off_attempted is True
        assert result.safe_off_result.get("PSU") is True
        assert result.safe_off_result.get("Load") is True
        assert "safe_off()" in runner._instruments._psu.calls
        assert "safe_off()" in runner._instruments._load.calls
        assert logger._closed is True

    def test_invalid_start_step_index_negative_also_performs_safe_off(self, tmp_path):
        runner, _logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.characterization(), start_step_index=-1)
        assert result.status == "FAULT"
        assert result.safe_off_attempted is True
        assert "safe_off()" in runner._instruments._psu.calls

    def test_invalid_start_step_index_sets_runner_status_to_fault(self, tmp_path):
        """[regresszió, Kör 19 D8d, F19-S4, DFAM-016 terminal_cleanup_
        restore_omission 2. előfordulása S5-07/_run_manual_checkpoint()
        után] ez az ág korábban a TELJES terminális szekvenciát lefuttatta
        (safe-off, esemény, logger.close()), DE self.status-t SOSEM
        állította -- a visszaadott TestResult helyesen FAULT-ot jelzett,
        de a runner-példány SAJÁT self.status attribútuma a MEGELŐZŐ
        (itt: induló "IDLE") értéken ragadt volna."""
        runner, _logger = _make_runner(tmp_path)
        assert runner.status == "IDLE"

        result = runner.run(TestPlan.characterization(), start_step_index=99)

        assert result.status == "FAULT"
        assert runner.status == "FAULT"

    def test_invalid_start_step_index_preserves_checkpoint_session_totals(self, tmp_path):
        """[regresszió, review-kör 4, S4-06 (P3, DFAM-021/022)] a checkpoint-
        ból már beolvasott self._config.initial_* session-összegek korábban
        elvesztek ezen a korai FAULT-ágon -- pont egy sikertelen resume
        esetén (pl. megváltozott/rövidebb terv miatt a régi checkpoint
        next_safe_step_index-e már tartományon kívülre esik), ahol a
        megszakadás előtti mérési adat a legértékesebb lenne. A
        checkpoint.json maga megőrzi az adatot, de a report.json-ból eddig
        nem volt rekonstruálható."""
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="test",
            initial_charge_ah=1.5, initial_discharge_ah=2.5,
            initial_charge_wh=18.0, initial_discharge_wh=30.0,
            initial_communication_faults_count=3,
            initial_extreme_dt_s_count=1, initial_max_observed_dt_s=5.5,
            initial_relax_dmm_fault_count=2,
            initial_temperature_out_of_range_count=4,
            initial_temp_dmm_fault_ticks=6,
            initial_warning_code_counts={"TEMPERATURE_OUT_OF_RANGE": 4},
        )
        runner, _logger = _make_runner(tmp_path, config=config)

        result = runner.run(TestPlan.characterization(), start_step_index=99)

        assert result.status == "FAULT"
        assert result.total_charge_ah == pytest.approx(1.5)
        assert result.total_discharge_ah == pytest.approx(2.5)
        assert result.total_charge_wh == pytest.approx(18.0)
        assert result.total_discharge_wh == pytest.approx(30.0)
        assert result.communication_faults_count == 3
        assert result.extreme_dt_s_count == 1
        assert result.max_observed_dt_s == pytest.approx(5.5)
        assert result.relax_dmm_fault_count == 2
        assert result.temperature_out_of_range_count == 4
        assert result.temp_dmm_fault_ticks == 6
        assert result.warning_code_counts == {"TEMPERATURE_OUT_OF_RANGE": 4}

    def test_run_with_start_step_index_skips_steps(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        calls = []
        runner.on_step_changed = calls.append
        runner.run(TestPlan.characterization(), start_step_index=2)
        # characterization: charge(0), relax(1), discharge(2), relax(3)
        # start_step_index=2 → csak discharge és relax_after_discharge fut
        assert len(calls) == 2
        assert calls[0]["step_label"] == "discharge"
        assert calls[1]["step_label"] == "relax_after_discharge"
        logger.close()

    def test_reset_control_flags_clears_stop_requested(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.stop_requested = True
        runner.reset_control_flags()
        assert runner.stop_requested is False
        logger.close()

    def test_reset_control_flags_clears_emergency_stop(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.emergency_stop_requested = True
        runner.emergency_stop_reason = "TEST_REASON"
        runner.reset_control_flags()
        assert runner.emergency_stop_requested is False
        assert runner.emergency_stop_reason == ""
        logger.close()


# ------------------------------------------------------------------ #
# Task 7: _run_step                                                   #
# ------------------------------------------------------------------ #

class TestRunStep:
    def test_charge_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=3))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "DONE"
        logger.close()

    def test_discharge_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, dc=_StubDischargeCtrl(steps_to_done=3))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.DISCHARGE, "discharge")
        result = runner._run_step(0, step)
        assert result.status == "DONE"
        logger.close()

    def test_relax_step_completes_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path, rc=_StubRelaxCtrl(steps_to_done=2))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        result = runner._run_step(0, step)
        assert result.status == "DONE"
        logger.close()

    def test_charge_step_fault_returns_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(fault_at_step=2))
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "FAULT"
        assert "FAULT" in result.reason
        logger.close()

    def test_emergency_stop_interrupts_charge(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.emergency_stop_requested = True
        runner.emergency_stop_reason = "USER_EMSTOP"
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "FAULT"
        assert "USER_EMSTOP" in result.reason
        logger.close()

    def test_stop_request_interrupts_charge(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.stop_requested = True
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "STOPPED"
        logger.close()

    def test_stop_request_interrupts_discharge(self, tmp_path):
        runner, logger = _make_runner(tmp_path, dc=_StubDischargeCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.stop_requested = True
        step = TestStep(StepKind.DISCHARGE, "discharge")
        result = runner._run_step(0, step)
        assert result.status == "STOPPED"
        logger.close()

    def test_stop_request_interrupts_relax(self, tmp_path):
        runner, logger = _make_runner(tmp_path, rc=_StubRelaxCtrl(steps_to_done=100))
        runner._start_time = datetime.now(timezone.utc)
        runner.stop_requested = True
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        result = runner._run_step(0, step)
        assert result.status == "STOPPED"
        logger.close()

    def test_manual_checkpoint_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
        result = runner._run_step(0, step)
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        logger.close()

    def test_charge_accumulates_ah(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=5)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        runner._run_step(0, step)
        # 5 advance() × 0.01 Ah = 0.05 Ah
        assert runner._total_charge_ah == pytest.approx(0.05, abs=0.001)
        logger.close()

    def test_discharge_accumulates_ah(self, tmp_path):
        dc = _StubDischargeCtrl(steps_to_done=4)
        runner, logger = _make_runner(tmp_path, dc=dc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.DISCHARGE, "discharge")
        runner._run_step(0, step)
        # 4 advance() × 0.01 Ah = 0.04 Ah
        assert runner._total_discharge_ah == pytest.approx(0.04, abs=0.001)
        logger.close()

    def test_charge_accumulates_ah_even_on_fault(self, tmp_path):
        """[regresszió, session_fg1_20260823_200337 nyomán] Korábban a
        self._total_charge_ah összegzés KIZÁRÓLAG a while-ciklus SIKERES
        lezárása után futott — egy FAULT-tal megszakadó lépés (pl.
        MAX_CHARGE_AH_REACHED) esetén a report.json total_charge_ah 0.0
        maradt, holott a controller ténylegesen töltött (l. CLAUDE.md
        §13: minden terminális állapot, nem csak DONE, kapjon pontos
        riportot)."""
        cc = _StubChargeCtrl(steps_to_done=100, fault_at_step=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "FAULT"
        # 3 advance() × 0.01 Ah = 0.03 Ah — NEM 0.0
        assert runner._total_charge_ah == pytest.approx(0.03, abs=0.001)
        assert result.total_charge_ah == pytest.approx(0.03, abs=0.001)
        logger.close()

    def test_emergency_stop_flushes_partial_charge_ah(self, tmp_path):
        """[regresszió, ugyanaz a hiba, mint fent] EMERGENCY_STOP korai
        return-je is a befagyott self._total_charge_ah-t adta vissza,
        elveszítve a vészleállítás ELŐTT, a ciklus belsejében már
        felhalmozott Ah-t. A vészleállítást egy on_sample callback-kel
        kérjük 2 tick lefutása UTÁN, hogy legyen ténylegesen felhalmozott,
        flush-olandó részösszeg."""
        cc = _StubChargeCtrl(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        tick_count = {"n": 0}

        def _on_sample(sample: dict) -> None:
            tick_count["n"] += 1
            if tick_count["n"] >= 2:
                runner.emergency_stop_requested = True
                runner.emergency_stop_reason = "USER_EMSTOP"

        runner.on_sample = _on_sample
        step = TestStep(StepKind.CHARGE, "charge")
        result = runner._run_step(0, step)
        assert result.status == "FAULT"
        # 2 advance() × 0.01 Ah = 0.02 Ah — NEM 0.0
        assert runner._total_charge_ah == pytest.approx(0.02, abs=0.001)
        assert result.total_charge_ah == pytest.approx(0.02, abs=0.001)
        logger.close()

    def test_traditional_learning_step_accumulates_ah_via_generic_getattr(self, tmp_path):
        """[regresszió, session_fg1_20260823_200337 nyomán] Korábban a
        StepKind.CHARGE/DISCHARGE-re szűkített if/elif miatt a runner
        self._total_charge_ah/_total_discharge_ah SOSEM frissült
        TRADITIONAL_LEARNING lépésnél — sem sikeres, sem hibás
        lezáráskor — így a report.json total_charge_ah/total_discharge_ah
        a TELJES session alatt 0.0 maradt."""
        tlc = _StubLearningCtrl(steps_to_done=4)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning")
        result = runner._run_step(0, step)
        assert result.status == "DONE"
        # 4 advance() × 0.01 Ah / × 0.02 Ah (l. _StubLearningCtrl.advance())
        assert runner._total_charge_ah == pytest.approx(0.04, abs=0.001)
        assert runner._total_discharge_ah == pytest.approx(0.08, abs=0.001)
        logger.close()

    def test_golden_gg_step_accumulates_ah_via_generic_getattr(self, tmp_path):
        """[regresszió] ugyanaz a hiba, mint a TRADITIONAL_LEARNING
        esetben — l. fenti teszt docstringje."""
        ggc = _StubGoldenGgCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, ggc=ggc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.GOLDEN_GG, "golden_gg")
        result = runner._run_step(0, step)
        assert result.status == "DONE"
        assert runner._total_charge_ah == pytest.approx(0.03, abs=0.001)
        assert runner._total_discharge_ah == pytest.approx(0.06, abs=0.001)
        logger.close()

    def test_controller_reset_called_if_available(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        # Első ciklus
        runner._run_step(0, TestStep(StepKind.CHARGE, "charge_1"))
        assert cc.state == ChargeState.CHARGE_DONE
        # Második ciklus — reset() visszaállítja INIT-re
        runner._run_step(0, TestStep(StepKind.CHARGE, "charge_2"))
        assert cc.state == ChargeState.CHARGE_DONE
        logger.close()


class TestControllerFaultReentry:
    """[regresszió, 2026-09-15, C-13/DFAM-016 RECURRING] amikor a controller
    SAJÁT MAGA lép FAULT-ba (_controller_faulted() ág `run()`), a run()-beli
    "safe_off_attempted" dispatch (l. run() kommentje) MÉGEGYSZER hívta volna
    _abort_active_controller()-en át a controller emergency_stop()-ját --
    TRADITIONAL_LEARNING/GOLDEN_GG-nél ez valós, ismételt BQ I/O-t (Cell BL
    restore) jelentett volna. A self._active_controller = None a
    _controller_faulted() ágon ezt megszünteti (ugyanaz a minta, mint a
    DONE-ági nullázás)."""

    def test_faulted_controller_emergency_stop_called_only_by_itself(self, tmp_path):
        calls = []

        class _StubChargeCtrlWithAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                calls.append(reason)

        cc = _StubChargeCtrlWithAbort(fault_at_step=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHARGE, "charge")

        result = runner._run_step(0, step)

        assert result.status == "FAULT"
        # A stub SAJÁT emergency_stop()-ja (amit MI hívnánk a tesztben, ha a
        # controller valódi FAULT-logikát futtatna) itt nem hívódik -- a
        # _controller_faulted() ág csak ÉSZLELI az already-FAULT állapotot,
        # nem ő idézi elő. A lényeg: run()-on át _abort_active_controller()
        # NEM hívja meg MÉGEGYSZER, mert self._active_controller már None.
        assert calls == []
        logger.close()

    def test_run_emergency_stop_after_controller_faulted_does_not_reabort(self, tmp_path):
        """A teljes run()-úton (nem csak _run_step()-en): egy controller-
        eredetű FAULT után a run() safe_off_attempted=False-t lát, tehát
        MÉGEGYSZER hívja _emergency_stop()-ot -> _abort_active_controller()-t
        -- ez a második hívás NEM érheti el újra a MÁR FAULT-ban lévő
        controllert."""
        calls = []

        class _StubChargeCtrlWithAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                calls.append(reason)

        cc = _StubChargeCtrlWithAbort(fault_at_step=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))

        assert result.status == "FAULT"
        # Mutáció-diszkrimináció: a fix ELŐTT ez a hívás 1 lett volna
        # (a run()-beli MÁSODIK _emergency_stop() a stub-ot MÉGEGYSZER
        # elérte volna _abort_active_controller()-en át).
        assert calls == []
        logger.close()


class TestRunnerAbortOriginTagging:
    """[regresszió, 2026-09-15, MAX verifier 1. kör P1-A (REJECTED) + P2-C
    lelet] a C-09 fix runner-oldali fele (_abort_active_controller()
    beállítja a controller.runner_abort_origin-t) korábban NULLA
    diszkrimináló teszttel rendelkezett -- ez a két teszt lezárja mind a
    hiányzó lefedettséget (P2-C), mind a talált regressziót (P1-A: egy
    VALÓS, controller-eredetű safety fault hamisan runner-eredetűnek
    minősülhetett, ha a runner egy MÁSIK, független ágon [SCPI_CRITICAL_
    ERROR] keresztül hívta _abort_active_controller()-t UGYANABBAN a
    tickben, amikor a controller MÁR saját magától FAULT-ba lépett)."""

    def test_abort_active_controller_sets_runner_abort_origin_before_calling_it(self, tmp_path):
        """P2-C: a runner-oldali fél tényleges bizonyítéka -- korábban
        EZT semmi nem tesztelte, csak a TLC _classify_result()-jét
        elszigetelten, kézzel beállított runner_abort_origin-nal."""
        class _StubLearningCtrlWithAbort(_StubLearningCtrl):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.runner_abort_origin: str | None = None
                self.seen_origin_at_call: str | None = None

            def emergency_stop(self, reason: str) -> None:
                # A tagging-nek a hívás ELŐTT kell megtörténnie.
                self.seen_origin_at_call = self.runner_abort_origin

        tlc = _StubLearningCtrlWithAbort(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        runner._active_controller = tlc

        runner._abort_active_controller("USER_EMERGENCY_STOP")

        assert tlc.runner_abort_origin == "USER_EMERGENCY_STOP"
        assert tlc.seen_origin_at_call == "USER_EMERGENCY_STOP"
        logger.close()

    def test_self_faulted_controller_not_tagged_as_runner_abort_even_via_independent_scpi_branch(
        self, tmp_path
    ):
        """P1-A: a MAX verifier által empirikusan reprodukált regresszió --
        a controller a SAJÁT advance()-ében FAULT-ba lép (pl. BATTERY_
        OVERVOLTAGE-analóg), ÉS ugyanabban a tickben egy FÜGGETLEN SCPI-
        kritikus-hiba poll-ág is visszatér, MIELŐTT a _run_step() elérné a
        _controller_faulted() ágat. A régi kódon ez a controllert HAMISAN
        "runner-eredetű"-ként jelölte volna meg -> _classify_result()
        AWAITING-re fokozta volna le a valódi biztonsági FAULT-ot."""
        class _StubLearningCtrlSelfFaultsImmediately(_StubLearningCtrl):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.runner_abort_origin: str | None = None
                self.emergency_stop_calls: list[str] = []

            def advance(self, dt_s: float) -> LearningState:
                # A controller a LEGELSŐ tickben, SAJÁT magától FAULT-ba lép
                # -- ez modellezi a controller saját safety-/mérési logikáját
                # (pl. BATTERY_OVERVOLTAGE), NEM a runner által kiváltott
                # megszakítást.
                self._tick += 1
                self.fault_reason = "SELF_TRIGGERED_SAFETY_FAULT"
                self._state = LearningState.FAULT
                return self._state

            def emergency_stop(self, reason: str) -> None:
                self.emergency_stop_calls.append(reason)

        tlc = _StubLearningCtrlSelfFaultsImmediately()
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        # Minden ticken pollozzon, és a kritikus SCPI-hiba az 1. advance()
        # self-FAULT-ja UTÁN kerüljön a sorba -- így a SCPI_CRITICAL_ERROR
        # ág UGYANABBAN a tickben tér vissza, amikor a controller (fent) már
        # FAULT-ba lépett a saját advance()-ében. R1 B7S V-2: a run() előtti
        # injekciót az R-1 lépéshatár-poll a controller létrehozása előtt
        # elfogyasztaná, és a teszt vákuumban (tick=0, INIT) zöld maradna.
        runner._config.device_error_poll_interval_s = 0.0
        _real_advance = tlc.advance

        def _advance_then_scpi_error(dt_s):
            state = _real_advance(dt_s)
            runner._instruments.psu.pending_errors.append('-222,"Data out of range"')
            return state

        tlc.advance = _advance_then_scpi_error

        result = runner.run(TestPlan(
            test_type=TestType.TRADITIONAL_LEARNING,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "learning"),),
        ))
        logger.close()

        assert result.status == "FAULT"
        # Nem-vákuum előfeltételek (R1 B7S V-2): a controller ténylegesen
        # lefutott, saját magától FAULT-olt, és a SCPI-ág döntött.
        assert tlc._tick == 1
        assert tlc.state is LearningState.FAULT
        assert result.reason.startswith("SCPI_CRITICAL_ERROR[PSU]")
        # A KRITIKUS assert: a controller SAJÁT safety-hibája SOHA nem
        # kaphat runner_abort_origin-t, függetlenül attól, hogy a runner
        # MELYIK ágon (SCPI_CRITICAL_ERROR vagy _controller_faulted())
        # keresztül hívta az abortot.
        assert tlc.runner_abort_origin is None
        # MAX verifier F-1 lelete (2026-09-15, R2-02): a stub MÁR gyűjtötte
        # az emergency_stop_calls-t (fent), de korábban SOHA nem assertáltunk
        # rá -- ez engedte át 8 committon keresztül azt a hibát, hogy a
        # SCPI_CRITICAL_ERROR ág egy MÁR terminális (itt: FAULT) controllert
        # újra megabortált. A tagging-vizsgálat (fenti assert) önmagában NEM
        # fedte ezt: egy None-runner_abort_origin akkor is igaz maradt volna,
        # ha az emergency_stop() ténylegesen másodszor is lefutott.
        assert tlc.emergency_stop_calls == []

    def test_base_exception_during_controller_abort_does_not_block_safe_off(self, tmp_path):
        """[regresszió, 2026-09-15, MAX verifier 1. kör P2-B lelet + 2. kör
        F-2 lelet] `_abort_active_controller()` saját docstringje ígéri,
        hogy egy itt keletkező hiba NEM akadályozhatja a hívó
        (`_emergency_stop()`) garantált safe-off lépését -- korábban erre
        NEM volt diszkrimináló teszt (a `except Exception` -> `except
        BaseException` mutáció visszavonása a teljes suite-on átment).
        Itt egy `KeyboardInterrupt`-ot dobunk a controller SAJÁT
        emergency_stop()-jából, és igazoljuk, hogy a hívó garantált,
        alacsonyabb szintű safe-offja (PSU ALL OFF + Load OFF) ETTŐL
        FÜGGETLENÜL lefut."""
        class _StubLearningCtrlRaisesOnAbort(_StubLearningCtrl):
            def emergency_stop(self, reason: str) -> None:
                raise KeyboardInterrupt()

        tlc = _StubLearningCtrlRaisesOnAbort(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        runner._active_controller = tlc
        runner._instruments.psu.output_commanded_on = True
        runner._instruments.load.input_commanded_on = True

        result = runner._emergency_stop("USER_EMERGENCY_STOP")

        assert result.status == "FAULT"
        assert result.safe_off_attempted is True
        assert runner._instruments.psu.output_commanded_on is False
        assert runner._instruments.load.input_commanded_on is False
        events_csv = (tmp_path / "events.csv").read_text(encoding="utf-8")
        assert "CONTROLLER_ABORT_FAILED" in events_csv
        assert "EMERGENCY_STOP" in events_csv
        logger.close()

    def test_controller_faulted_raising_inside_tagging_guard_does_not_block_safe_off(
        self, tmp_path
    ):
        """[regresszió, 2026-09-15, MAX verifier 2. kör F-1 lelet + 3. kör
        G-1 lelet] Egy ISMERETLEN state-típusra a `try:`-on BELÜLI
        ellenőrzések (jelenleg `_is_finished()` fut ELŐSZÖR, R2-02 óta --
        korábban `_controller_faulted(controller)` volt az első ilyen hívás,
        l. test_runner.py::_is_finished() és ::_controller_faulted() -- mindkettő
        azonos szerkezetű, egymást követő metódus, mindkettő a saját
        ismeretlen-típus ágán dob) explicit `raise ValueError`-t dobnak. Ha ez a
        hívás a `try:` blokk ELŐTT állna (az 1. körben EZ volt a hiba), a
        `ValueError` kikerülné a `except BaseException`-t, és a hívó
        (`_emergency_stop()`) garantált safe-off-ja (PSU/Load OFF) ELŐTT
        szállna ki. A jelenlegi kód mindkét ellenőrzést a `try:`-on BELÜLRE
        tette -- ez a teszt ezt a konkrét utat fedi (a fenti teszt csak a
        közvetlen `emergency_stop()`-ból dobott kivételt fedte, nem a
        `try:`-on belüli, `abort(reason)` ELŐTTI ellenőrzések SAJÁT
        kivételét)."""
        class _StubLearningCtrlWithUnknownStateType(_StubLearningCtrl):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.runner_abort_origin: str | None = None

            @property
            def state(self):
                # Szándékosan NEM LearningState (vagy bármely ismert
                # állapot-enum) -- R2-02 óta ez ELSŐKÉNT az _is_finished()
                # ismeretlen-típus ágát kényszeríti ki (raise ValueError),
                # NEM a _controller_faulted()-ét (az sosem fut le, mert a
                # _is_finished() korábban dob) -- a teszt által fedett
                # viselkedés (safe-off a kivétel ELLENÉRE lefut) ettől
                # függetlenül azonos.
                return "UNKNOWN_STATE_SENTINEL"

            def emergency_stop(self, reason: str) -> None:
                pass

        tlc = _StubLearningCtrlWithUnknownStateType(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        runner._active_controller = tlc
        runner._instruments.psu.output_commanded_on = True
        runner._instruments.load.input_commanded_on = True

        result = runner._emergency_stop("USER_EMERGENCY_STOP")

        assert result.status == "FAULT"
        assert result.safe_off_attempted is True
        assert runner._instruments.psu.output_commanded_on is False
        assert runner._instruments.load.input_commanded_on is False
        events_csv = (tmp_path / "events.csv").read_text(encoding="utf-8")
        assert "CONTROLLER_ABORT_FAILED" in events_csv
        assert "EMERGENCY_STOP" in events_csv
        logger.close()

    def test_done_controller_is_not_re_aborted_via_scpi_critical_branch(self, tmp_path):
        """[regresszió, teljes kódbázis review kör 8 (2026-09-15), R2-02, P1]
        a legdrágább projekt-deliverable védelme: egy controller, ami a
        SAJÁT advance()-ében DONE-ba lép (pl. bizonyítottan sikeres BQ
        learning eredménnyel), NEM abortálható újra egy MÁSODIK, független
        ok (itt: SCPI_CRITICAL_ERROR poll-ág, ami NEM nullázza
        self._active_controller-t) miatt UGYANABBAN a tickben. Enélkül a
        _abort_active_controller() feltétel nélkül hívta volna a controller
        emergency_stop()-ját, ami a TraditionalLearningControllernél a DONE
        state-et FAULT-ra írta volna felül és a _classify_result()-öt
        újrafuttatta volna -- egy VALÓDI, sikeres eredményt LEARNING_
        RESULT_AWAITING+ABORTED_BY_RUNNER-re fokozva le a report.json-ban."""
        class _StubLearningCtrlReachesDone(_StubLearningCtrl):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.runner_abort_origin: str | None = None
                self.emergency_stop_calls: list[str] = []

            def emergency_stop(self, reason: str) -> None:
                self.emergency_stop_calls.append(reason)

        tlc = _StubLearningCtrlReachesDone(steps_to_done=1)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        # Minden ticken pollozzon, és az ELSŐ tick advance()-e alatt legyen
        # kritikus SCPI-hiba várakozásban -- így a SCPI_CRITICAL_ERROR ág
        # UGYANABBAN a tickben tér vissza, amikor a controller (fent) már
        # DONE-ba lépett a saját advance()-ében. R1 R-1 (B7S): a hibát a
        # lépés KÖZBEN (az 1. advance()-ben) injektáljuk, nem a run() előtt
        # -- egy indulás előtti hibát most már a lépéshatár-poll a controller
        # létrehozása ELŐTT FAULT-ra visz (l. TestR1R1StepBoundaryEnergy-
        # StatePoll), ami ennek a tesztnek a szándékát (DONE-ba lépett
        # controller + SCPI-FAULT ugyanabban a tickben) nem érné el.
        runner._config.device_error_poll_interval_s = 0.0
        _real_advance = tlc.advance

        def _advance_then_scpi_error(dt_s):
            state = _real_advance(dt_s)
            runner._instruments.psu.pending_errors.append('-222,"Data out of range"')
            return state

        tlc.advance = _advance_then_scpi_error

        result = runner.run(TestPlan(
            test_type=TestType.TRADITIONAL_LEARNING,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "learning"),),
        ))
        logger.close()

        assert result.status == "FAULT"  # a session-szintű eredmény helyesen FAULT (SCPI hiba)
        assert tlc.emergency_stop_calls == []  # de a controllert NEM abortáltuk újra
        assert tlc.state is LearningState.DONE  # a DONE-t NEM írtuk felül
        assert tlc.runner_abort_origin is None

    def test_terminal_state_guard_does_not_skip_runner_safe_off(self, tmp_path):
        """[regresszió, R2-02] az új terminális-állapot őr NEM akadályozza
        a hívó (_emergency_stop()) garantált, alacsonyabb szintű safe-off
        lépését -- egy DONE controllernél is Load OFF + PSU ALL OFF fut,
        csak a controller SAJÁT emergency_stop()-ja marad ki."""
        class _StubLearningCtrlDone(_StubLearningCtrl):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.emergency_stop_calls: list[str] = []

            def emergency_stop(self, reason: str) -> None:
                self.emergency_stop_calls.append(reason)

        tlc = _StubLearningCtrlDone(steps_to_done=1)
        tlc._state = LearningState.DONE
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        runner._active_controller = tlc
        runner._instruments.psu.output_commanded_on = True
        runner._instruments.load.input_commanded_on = True

        result = runner._emergency_stop("USER_EMERGENCY_STOP")

        assert result.safe_off_attempted is True
        assert runner._instruments.psu.output_commanded_on is False
        assert runner._instruments.load.input_commanded_on is False
        assert tlc.emergency_stop_calls == []
        events_csv = (tmp_path / "events.csv").read_text(encoding="utf-8")
        assert "EMERGENCY_STOP" in events_csv
        logger.close()

    def test_nonterminal_learning_ctrl_psu_scpi_critical_abort_dispatches_cleanup(
        self, tmp_path
    ):
        """[regresszió, R1 F-38 (DFAM-016), B10] a tick-hurok
        SCPI_CRITICAL_ERROR ágának NEM-terminális párja: eddig csak a
        terminális (DONE/FAULT) controllert fedte teszt (fent), így egy
        `self._active_controller = None` a SCPI-ágban a teljes
        test_test_runner.py-t zölden hagyta volna, miközben a CF-01
        controller-cleanup (TLC: Cell BL restore + _classify_result())
        kimarad. A SCPI-ág SZÁNDÉKOSAN NEM nullázza az
        `_active_controller`-t (CF-01): a run() -> _emergency_stop() ->
        _abort_active_controller() lánc éri el a még futó controllert.

        Hatókör: a stub emergency_stop() csak rögzít, semmit nem kapcsol
        ki -- a teszt kizárólag a cleanup-DISPATCH helyességét bizonyítja
        (pontosan egyszeri controller-abort a hívás előtt beállított
        runner_abort_origin-nal + a független runner safe-off), NEM a
        tényleges TLC BQ Data Flash restore-t. A controller-abort és a
        runner safe-off egymáshoz viszonyított sorrendjét szándékosan NEM
        pinneljük (D-B10-4)."""
        class _StubLearningCtrlNonTerminal(_StubLearningCtrl):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.runner_abort_origin: str | None = None
                self.emergency_stop_calls: list[str] = []
                self.origin_seen_at_abort: list[str | None] = []

            def emergency_stop(self, reason: str) -> None:
                # Semmit nem kapcsol ki: a runner safe-offját így önállóan
                # bizonyítjuk.
                self.emergency_stop_calls.append(reason)
                self.origin_seen_at_abort.append(self.runner_abort_origin)

        tlc = _StubLearningCtrlNonTerminal(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        psu = runner._instruments.psu
        load = runner._instruments.load
        # Minden ticken pollozzon; a hibát a lépés KÖZBEN (a 2. advance()
        # után) injektáljuk -- egy run() előtti injekciót a lépéshatár-poll
        # a controller létrehozása előtt elfogyasztaná (R1 B7S V-2).
        runner._config.device_error_poll_interval_s = 0.0
        injected_at: dict[str, int] = {}
        _real_advance = tlc.advance

        def _advance_then_scpi_error(dt_s):
            state = _real_advance(dt_s)
            if tlc._tick == 2:
                psu.output_on()  # aktív energiaátadás modellezése
                injected_at["psu"] = len(psu.calls)
                injected_at["load"] = len(load.calls)
                psu.pending_errors.append('-222,"Data out of range"')
            return state

        tlc.advance = _advance_then_scpi_error

        result = runner.run(TestPlan(
            test_type=TestType.TRADITIONAL_LEARNING,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "learning"),),
        ))
        logger.close()

        # Nem-vákuum előfeltételek: az injektálás megtörtént, a SCPI-ág
        # ugyanabban a tickben döntött, és a controller NEM terminális.
        assert set(injected_at) == {"psu", "load"}
        assert tlc._tick == 2
        assert tlc.state not in (LearningState.DONE, LearningState.FAULT)
        assert result.status == "FAULT"
        assert result.reason.startswith("SCPI_CRITICAL_ERROR[PSU]")
        # Pontosan egyszeri controller-abort; a runner_abort_origin a hívás
        # ELŐTT beállítva.
        assert tlc.emergency_stop_calls == [result.reason]
        assert tlc.origin_seen_at_abort == [result.reason]
        assert tlc.runner_abort_origin == result.reason
        # Független runner safe-off: az injektáláskor mentett hívásindex
        # UTÁN mindkét eszközön van OFF-parancs, és nincs további ON.
        assert result.safe_off_attempted is True
        assert psu.output_commanded_on is False
        assert load.input_commanded_on is False
        psu_after = psu.calls[injected_at["psu"]:]
        load_after = load.calls[injected_at["load"]:]
        assert any(
            c in ("safe_off()", "all_outputs_off()", "output_off()") for c in psu_after
        )
        assert any(c in ("safe_off()", "input_off()") for c in load_after)
        assert "output_on()" not in psu_after
        assert "input_on()" not in load_after
        events_csv = (tmp_path / "events.csv").read_text(encoding="utf-8")
        assert "EMERGENCY_STOP" in events_csv
        assert "SCPI_CRITICAL_ERROR[PSU]" in events_csv

    def test_nonterminal_charge_ctrl_load_scpi_critical_abort_dispatches_cleanup(
        self, tmp_path
    ):
        """[regresszió, R1 F-38 (DFAM-016), B10] ugyanaz a nem-terminális
        SCPI_CRITICAL_ERROR-ág ChargeController-stubbal (StepKind.CHARGE,
        TestPlan.characterization() 1. lépése) és LOAD-oldali kritikus
        hibával -- a dispatch eszköz- és controller-típusfüggetlen. A
        SCPI-ág szándékosan NEM nullázza az `_active_controller`-t (CF-01).
        Hatókör: csak a cleanup-dispatch helyessége (a stub emergency_stop()
        semmit nem kapcsol ki), nem valós controller-cleanup; a controller-
        abort és a runner safe-off sorrendje nincs pinnelve (D-B10-4)."""
        class _StubChargeCtrlNonTerminal(_StubChargeCtrl):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.emergency_stop_calls: list[str] = []

            def emergency_stop(self, reason: str) -> None:
                self.emergency_stop_calls.append(reason)

        cc = _StubChargeCtrlNonTerminal(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        psu = runner._instruments.psu
        load = runner._instruments.load
        runner._config.device_error_poll_interval_s = 0.0
        injected_at: dict[str, int] = {}
        _real_advance = cc.advance

        def _advance_then_scpi_error(dt_s):
            state = _real_advance(dt_s)
            if cc._tick == 2:
                psu.output_on()  # aktív töltés modellezése
                injected_at["psu"] = len(psu.calls)
                injected_at["load"] = len(load.calls)
                load.pending_errors.append('-222,"Data out of range"')
            return state

        cc.advance = _advance_then_scpi_error

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert set(injected_at) == {"psu", "load"}
        assert cc._tick == 2
        assert cc.state not in (ChargeState.CHARGE_DONE, ChargeState.FAULT)
        assert result.status == "FAULT"
        assert result.reason.startswith("SCPI_CRITICAL_ERROR[Load]")
        assert result.active_step_label == "charge"
        assert cc.emergency_stop_calls == [result.reason]
        assert result.safe_off_attempted is True
        assert psu.output_commanded_on is False
        assert load.input_commanded_on is False
        psu_after = psu.calls[injected_at["psu"]:]
        load_after = load.calls[injected_at["load"]:]
        assert any(
            c in ("safe_off()", "all_outputs_off()", "output_off()") for c in psu_after
        )
        assert any(c in ("safe_off()", "input_off()") for c in load_after)
        assert "output_on()" not in psu_after
        assert "input_on()" not in load_after
        events_csv = (tmp_path / "events.csv").read_text(encoding="utf-8")
        assert "EMERGENCY_STOP" in events_csv
        assert "SCPI_CRITICAL_ERROR[Load]" in events_csv


class TestBaseExceptionTerminalPath:
    """[regresszió, 2026-09-15, C-04/DFAM-002 RECURRING -- 2. előfordulás,
    precedens: pulse_measurement_service.py] a run() fő tick-hurka valós
    time.sleep()-et hív -- egy KeyboardInterrupt (Ctrl+C a felhasználó
    terminálablakában) `BaseException`, NEM `Exception`, ezért korábban
    kikerülte a garantált safe-off/FAULT-log/logger.close() láncot."""

    def test_keyboard_interrupt_triggers_safe_off_and_propagates(self, tmp_path):
        class _StubChargeCtrlRaisesInterrupt(_StubChargeCtrl):
            def advance(self, dt_s: float) -> ChargeState:
                raise KeyboardInterrupt()

        cc = _StubChargeCtrlRaisesInterrupt()
        runner, logger = _make_runner(tmp_path, cc=cc)

        with pytest.raises(KeyboardInterrupt):
            runner.run(TestPlan(
                test_type=TestType.CHARACTERIZATION,
                steps=(TestStep(StepKind.CHARGE, "charge"),),
            ))

        # A garantált safe-off ÉS a terminális naplózás a raise ELŐTT
        # lefutott -- mutáció (BaseException -> Exception) esetén a
        # KeyboardInterrupt megállítaná a hurkot, de a lenti assertek
        # buknának (nincs safe_all_off, nincs EMERGENCY_STOP esemény).
        assert runner._instruments.psu.output_commanded_on is False
        assert runner._instruments.load.input_commanded_on is False
        events_csv = (tmp_path / "events.csv").read_text(encoding="utf-8")
        assert "EMERGENCY_STOP" in events_csv
        assert "UNHANDLED_EXCEPTION" in events_csv
        # A logger már lezárva a _emergency_stop() finally-jében (l.
        # test_runner.py:1571-1574) -- a close() itt csak idempotens
        # takarítás, nem újabb bizonyíték.
        logger.close()

    def test_regular_exception_still_returns_fault_without_propagating(self, tmp_path):
        """Karakterizációs teszt: egy normál (nem-BaseException) kivétel
        viselkedése VÁLTOZATLAN -- a fix csak a BaseException-ágat bővíti,
        nem a meglévő Exception-utat."""
        class _StubChargeCtrlRaisesValueError(_StubChargeCtrl):
            def advance(self, dt_s: float) -> ChargeState:
                raise ValueError("boom")

        cc = _StubChargeCtrlRaisesValueError()
        runner, logger = _make_runner(tmp_path, cc=cc)

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))

        assert result.status == "FAULT"
        assert "UNHANDLED_EXCEPTION" in result.reason
        logger.close()

    def test_keyboard_interrupt_during_done_tail_safe_off_still_attempts_psu_off(
        self, tmp_path
    ):
        """[regresszió, Kör 17 MAX-effort review, R17-19, DFAM-002 3.
        elofordulas -- "scope" varians] a run() DONE-tailje (a garantalt
        safe-off + zaro checkpoint + logger.close()) a fenti C-04 fix
        try/except BaseException hatokoren KIVUL allt -- egy Ctrl+C a
        garantalt safe-off KOZBEN (Load OFF utan, PSU OFF ELOTT) korabban
        PSU OFF megkiserlese, esemeny es logger.close() nelkul szakitotta
        meg a sessiont."""
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        psu = runner._instruments.psu

        original_safe_off = psu.safe_off
        state = {"calls": 0}

        def _flaky_psu_safe_off():
            state["calls"] += 1
            if state["calls"] == 1:
                raise KeyboardInterrupt()
            return original_safe_off()

        psu.safe_off = _flaky_psu_safe_off

        with pytest.raises(KeyboardInterrupt):
            runner.run(TestPlan(
                test_type=TestType.CHARACTERIZATION,
                steps=(TestStep(StepKind.CHARGE, "charge"),),
            ))

        assert state["calls"] >= 2, "a PSU OFF-ot masodszor is meg kellett kiserelni"
        events_csv = (tmp_path / "events.csv").read_text(encoding="utf-8")
        assert "DONE_TAIL_INTERRUPTED" in events_csv
        assert logger._closed is True

    def test_keyboard_interrupt_during_done_checkpoint_write_still_logs_and_closes(
        self, tmp_path
    ):
        """A guard nem csak a safe-off hivast fedi, hanem a TELJES DONE-
        tailt -- egy KeyboardInterrupt a zaro checkpoint-irasban (a
        safe-off UTAN, MAR sikeresen) is a teljes guardot valtja ki, nem
        csak egy szukebb, csak a :950 sor koruli try-t.

        MEGJEGYZES: ez a pontos hely (a checkpoint-iras korul) a MEGLEVO
        belso `finally: self._logger.close()` (R17-01 elotti, valtozatlan
        kod) UTAN eri el az uj guardot -- a `close()` MAR lefutott, mielott
        az uj `except BaseException` agunk `log_event()`-et probalna hivni,
        ezert az `events.csv`-be iras itt legitim modon nem lehetseges
        (zart fajlra irna). Ez egy R17-07 hatokorebe eso, ismert es
        dokumentalt maradvany (Logger.close() nem per-resource izolalt,
        korai _closed latch) -- ITT az `on_event` GUI-csatornat kell
        ellenorizni, ami a logger allapotatol fuggetlenul mukodik."""
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        gui_events = []
        runner.on_event = gui_events.append

        # CSAK a terminalis (status=="DONE") irast szakitjuk meg -- a
        # periodikus (status=="RUNNING") checkpoint-irasok a tick-hurokban,
        # a KORABBI (R17-01) guard alatt futnak, es AZOK megszakitasa a
        # run() kulso except BaseException-jehez (:882) tartozik, nem
        # ehhez a tesztezhez.
        def _raises_keyboard_interrupt_on_done(payload):
            if payload.get("status") == "DONE":
                raise KeyboardInterrupt()

        logger.write_checkpoint = _raises_keyboard_interrupt_on_done

        with pytest.raises(KeyboardInterrupt):
            runner.run(TestPlan(
                test_type=TestType.CHARACTERIZATION,
                steps=(TestStep(StepKind.CHARGE, "charge"),),
            ))

        assert any(e.get("event_code") == "DONE_TAIL_INTERRUPTED" for e in gui_events)
        assert logger._closed is True

    def test_done_tail_interrupt_is_not_reported_as_runner_fault(self, tmp_path):
        """A 'rossz fix' elleni diszkriminator: ha a writer a korabbi, :811
        korul nyilo try szkopjat szelesitette volna ki a DONE-tailre (a
        finding elutasitott elso opcioja), ez a teszt buknia -- a DONE-
        tail megszakitasa NEM futtathatja a FAULT-klasszifikaciot."""
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        psu = runner._instruments.psu

        original_safe_off = psu.safe_off
        state = {"calls": 0}

        def _flaky_psu_safe_off():
            state["calls"] += 1
            if state["calls"] == 1:
                raise KeyboardInterrupt()
            return original_safe_off()

        psu.safe_off = _flaky_psu_safe_off

        with pytest.raises(KeyboardInterrupt):
            runner.run(TestPlan(
                test_type=TestType.CHARACTERIZATION,
                steps=(TestStep(StepKind.CHARGE, "charge"),),
            ))

        events_csv = (tmp_path / "events.csv").read_text(encoding="utf-8")
        assert "EMERGENCY_STOP" not in events_csv
        assert "UNHANDLED_EXCEPTION" not in events_csv
        assert runner.status == "DONE"
        assert logger._closed is True

    def test_done_tail_live_event_is_critical_and_tail_sequence_unchanged(
        self, tmp_path
    ):
        """[regresszió, R3-B3 (R3-GUI-1, P3), DFAM-018 RECURRING] a DONE-tail
        `on_event` DONE_TAIL_INTERRUPTED eseménye explicit severity nélkül
        ment ki -- a `_infer_severity()` INFO-nak minősítette (színtelen élő
        sor), holott a perzisztens `log_event()` ugyanerre is_critical=True.
        A fix CSAK a severity-mezőt adja hozzá. A DONE-tail sorrendjét és
        pontos hívásszámait (retry safe-off Load -> PSU, egy kritikus
        log_event + flush, egy close + flush, egy on_event, majd az EREDETI
        kivétel újradobása) a HEAD-en (ebbbd98) azonos injekcióval mért
        baseline rögzíti (Codex R3-fixplan kötelező kiegészítés)."""
        import csv

        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
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
        gui_events: list[dict] = []

        def _on_event(event):
            trace.append(("on_event", event.get("event_code")))
            gui_events.append(event)

        runner.on_event = _on_event

        with pytest.raises(KeyboardInterrupt) as ei:
            runner.run(TestPlan(
                test_type=TestType.CHARACTERIZATION,
                steps=(TestStep(StepKind.CHARGE, "charge"),),
            ))

        # Az EREDETI kivételpéldány terjed tovább (nem egy új példány).
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
            ("on_event", "DONE_TAIL_INTERRUPTED"),
        ]
        assert psu_calls["n"] == 2
        tail_events = [
            e for e in gui_events if e.get("event_code") == "DONE_TAIL_INTERRUPTED"
        ]
        assert len(tail_events) == 1
        assert tail_events[0]["severity"] == "CRITICAL"
        assert tail_events[0]["event_message"] == "KeyboardInterrupt: R3-DONE-tail"
        with open(tmp_path / "events.csv", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        tail_rows = [r for r in rows if r["event_code"] == "DONE_TAIL_INTERRUPTED"]
        assert len(tail_rows) == 1
        assert tail_rows[0]["is_critical"] == "True"
        assert logger._closed is True
        assert runner.status == "DONE"


# ------------------------------------------------------------------ #
# Task 8: run() integrációs tesztek                                  #
# ------------------------------------------------------------------ #

class TestRunIntegration:
    def test_characterization_done(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.characterization())
        assert result.status == "DONE"
        assert runner.status == "DONE"
        logger.close()

    def test_done_writes_terminal_checkpoint(self, tmp_path):
        """[regresszió, Teljes kódbázis review 20. kör (2026-09-06), Session/
        Workflow P2 lelet] a teljes terv sikeres DONE-lezárása korábban nem
        írt záró checkpointot — az utolsó lemezre írt checkpoint.json a
        legutóbbi periodikus ("RUNNING", jellemzően safe_boundary=False)
        állapot maradt, ami egy TÉNYLEGESEN sikeresen befejezett sessiont
        tévesen "megszakadt"-ként mutatott volna a session_recovery
        rétegnek egy, a logger.close() és a report.json-írás közötti
        crash esetén."""
        import json
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.characterization())
        assert result.status == "DONE"
        checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
        assert checkpoint["status"] == "DONE"
        assert checkpoint["checkpoint_is_terminal"] is True
        assert checkpoint["safe_boundary"] is True
        logger.close()

    def test_emergency_stop_requested_on_final_tick_of_last_step_not_reported_as_done(
        self, tmp_path
    ):
        """[regresszió, Review-kör 2 (2026-09-08), Session/Workflow/
        Állapotgép S-3 lelet] a `while not self._is_finished(controller)`
        tick-hurok (_run_step()) az `emergency_stop_requested`-et a hurok
        TETEJÉN, a `controller.advance()` hívás ELŐTT ellenőrzi -- ha a
        jelző PONT az UTOLSÓ lépés UTOLSÓ tickje (maga a `controller.
        advance()`, ami a lépést lezárja) KÖZBEN válik igazzá, a hurok
        `emergency_stop_requested` újraellenőrzése NÉLKÜL lép ki, és a
        run() for-ciklusa is simán a záró DONE ágra fut, mert nincs
        következő lépés, ahol a ciklus tetején lévő ellenőrzés elkaphatná.
        Itt egy EGYETLEN lépéses tervvel és egy `on_sample` callback-kel
        szimuláljuk, ami PONT az utolsó (2.) ticken állítja be a jelzőt."""
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        tick_count = {"n": 0}

        def _on_sample(sample: dict) -> None:
            tick_count["n"] += 1
            if tick_count["n"] >= 2:
                runner.emergency_stop_requested = True
                runner.emergency_stop_reason = "USER_EMSTOP"

        runner.on_sample = _on_sample
        plan = TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        result = runner.run(plan)
        assert result.status == "FAULT"
        assert result.reason == "USER_EMSTOP"
        assert runner.status == "FAULT"
        logger.close()

    def test_characterization_accumulates_ah(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=5)   # 5 × 0.01 = 0.05 Ah
        dc = _StubDischargeCtrl(steps_to_done=4) # 4 × 0.01 = 0.04 Ah
        runner, logger = _make_runner(tmp_path, cc=cc, dc=dc)
        result = runner.run(TestPlan.characterization())
        assert result.status == "DONE"
        assert result.total_charge_ah == pytest.approx(0.05, abs=0.001)
        assert result.total_discharge_ah == pytest.approx(0.04, abs=0.001)
        logger.close()

    def test_bq_learning_stops_at_manual_checkpoint(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.bq_learning_physical())
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.reason == "MANUAL_BQ_CHECKPOINT_REACHED"
        logger.close()

    def test_bq_learning_accumulates_two_cycles(self, tmp_path):
        # Factory-mintával friss stub-ot ad minden lépésnél — így 2 teljes ciklus összeadódik.
        # 3 tick × 0.01 Ah × 2 töltés = 0.06 Ah; 2 tick × 0.01 Ah × 2 kisütés = 0.04 Ah
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode
        from Prog.src.logger import Logger, LogConfig
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

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
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        config = TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="test")

        runner = TestRunner(
            instrument_manager=instruments,
            safety=safety,
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=3),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=2),
            relax_ctrl_factory=lambda step: _StubRelaxCtrl(steps_to_done=2),
        )
        result = runner.run(TestPlan.bq_learning_physical())
        assert result.total_charge_ah == pytest.approx(0.06, abs=0.001)
        assert result.total_discharge_ah == pytest.approx(0.04, abs=0.001)
        logger.close()

    def test_emergency_stop_before_first_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.emergency_stop_requested = True
        runner.emergency_stop_reason = "PRE_TEST_FAULT"
        result = runner.run(TestPlan.characterization())
        assert result.status == "FAULT"
        assert "PRE_TEST_FAULT" in result.reason
        logger.close()

    def test_graceful_stop_before_first_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.stop_requested = True
        result = runner.run(TestPlan.characterization())
        assert result.status == "STOPPED"
        logger.close()

    def test_fault_in_charge_step_triggers_emergency(self, tmp_path):
        cc = _StubChargeCtrl(fault_at_step=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        result = runner.run(TestPlan.characterization())
        assert result.status == "FAULT"
        logger.close()

    def test_unhandled_exception_returns_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        def _bad_run_step(step_index, step):
            raise RuntimeError("simulated crash")
        runner._run_step = _bad_run_step
        result = runner.run(TestPlan.characterization())
        assert result.status == "FAULT"
        assert "UNHANDLED_EXCEPTION" in result.reason
        logger.close()

    def test_samples_written_to_csv(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.run(TestPlan.characterization())
        logger.close()
        csv_path = tmp_path / "samples.csv"
        assert csv_path.exists()
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) > 2  # header + legalább 1 adat sor

    def test_events_written_to_events_csv(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.run(TestPlan.characterization())
        logger.close()
        events_path = tmp_path / "events.csv"
        assert events_path.exists()

    def test_checkpoint_json_written(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.run(TestPlan.characterization())
        logger.close()
        import json
        cp = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
        assert "status" in cp


# ------------------------------------------------------------------ #
# Task 1: Callback tesztek                                           #
# ------------------------------------------------------------------ #

class TestCallbacks:
    def test_on_sample_called_for_each_tick(self, tmp_path):
        """on_sample callback minden tickben meghívódik."""
        runner, _ = _make_runner(tmp_path)
        samples = []
        runner.on_sample = samples.append

        runner.run(TestPlan.characterization())

        assert len(samples) > 0
        assert all("battery_voltage_V" in s for s in samples)
        assert all("elapsed_s" in s for s in samples)

    def test_on_sample_none_does_not_crash(self, tmp_path):
        """on_sample=None esetén nincs hiba."""
        runner, _ = _make_runner(tmp_path)
        runner.on_sample = None
        result = runner.run(TestPlan.characterization())
        assert result.status == "DONE"

    def test_on_event_called_on_emergency_stop(self, tmp_path):
        """on_event callback meghívódik emergency stop esetén."""
        runner, _ = _make_runner(tmp_path)
        events = []
        runner.on_event = events.append
        runner.request_emergency_stop("TEST_REASON")
        runner.run(TestPlan.characterization())
        codes = [e["event_code"] for e in events]
        assert "EMERGENCY_STOP" in codes

    def test_on_event_called_on_graceful_stop(self, tmp_path):
        """on_event callback meghívódik graceful stop esetén."""
        runner, _ = _make_runner(
            tmp_path,
            rc=_StubRelaxCtrl(steps_to_done=100),
        )
        events = []
        runner.on_event = events.append
        runner.request_stop()
        runner.run(TestPlan.characterization())
        codes = [e["event_code"] for e in events]
        assert "GRACEFUL_STOP" in codes

    def test_on_event_none_does_not_crash(self, tmp_path):
        """on_event=None esetén nincs hiba."""
        runner, _ = _make_runner(tmp_path)
        runner.on_event = None
        runner.request_emergency_stop("TEST")
        result = runner.run(TestPlan.characterization())
        assert result.status == "FAULT"

    def test_on_sample_receives_step_name(self, tmp_path):
        """on_sample sample dict tartalmazza a step_name mezőt."""
        runner, _ = _make_runner(tmp_path)
        samples = []
        runner.on_sample = samples.append
        runner.run(TestPlan.characterization())
        step_names = {s["step_name"] for s in samples}
        assert "charge" in step_names


# ------------------------------------------------------------------ #
# Task 1 (6B): on_step_changed callback                              #
# ------------------------------------------------------------------ #

class TestStepChanged:
    def test_on_step_changed_called_for_each_step(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        calls = []
        runner.on_step_changed = calls.append
        runner.run(TestPlan.characterization())
        # characterization: 4 lépés
        assert len(calls) == 4
        assert calls[0]["step_label"] == "charge"
        assert calls[0]["runner_status"] == "RUNNING"
        assert calls[0]["step_index"] == 0
        assert calls[0]["step_count"] == 4
        logger.close()

    def test_on_step_changed_payload_has_step_kind(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        calls = []
        runner.on_step_changed = calls.append
        runner.run(TestPlan.characterization())
        assert calls[0]["step_kind"] == "CHARGE"
        assert calls[1]["step_kind"] == "RELAX"
        assert calls[2]["step_kind"] == "DISCHARGE"
        logger.close()


class TestRealDtIntegration:
    """K1: _run_step() valós dt_s-t ad az advance()-nek, nem nominálisát."""

    def test_advance_receives_actual_elapsed_not_nominal(self, tmp_path):
        from unittest.mock import patch, MagicMock

        recorded_dt: list[float] = []

        class DtRecordingRelax:
            def __init__(self):
                self._tick = 0

            @property
            def state(self) -> RelaxState:
                return RelaxState.RELAX_DONE if self._tick >= 2 else RelaxState.RELAXING

            def advance(self, dt_s: float) -> RelaxState:
                recorded_dt.append(dt_s)
                self._tick += 1
                return self.state

            def reset(self) -> None:
                pass

        runner, logger = _make_runner(tmp_path, rc=DtRecordingRelax())

        # perf_counter: 0.0 → inicializálás, majd 2.4s-os I/O-val emulált tick
        counter_values = [0.0, 2.4, 4.8, 7.2]
        counter_idx = [0]

        def mock_perf() -> float:
            val = counter_values[min(counter_idx[0], len(counter_values) - 1)]
            counter_idx[0] += 1
            return val

        step = TestStep(StepKind.RELAX, "relax_dt_test")
        plan = TestPlan(test_type=None, steps=(step,))

        with patch("Prog.src.test_runner.time") as mock_time:
            mock_time.perf_counter.side_effect = mock_perf
            mock_time.sleep = MagicMock()
            runner.run(plan)

        assert len(recorded_dt) >= 1
        assert abs(recorded_dt[0] - 2.4) < 0.05, (
            f"Elvárt ~2.4s valós dt_s, kapott {recorded_dt[0]}s (nominális tick=1.0s)"
        )
        logger.close()

    def test_first_tick_dt_is_non_negative(self, tmp_path):
        """P1-F: Az első tick dt_s >= 0.0 (perf_counter garantálja, nincs negatív drift)."""
        recorded_dt: list[float] = []

        class DtRecordingRelax:
            def __init__(self):
                self._tick = 0

            @property
            def state(self) -> RelaxState:
                return RelaxState.RELAX_DONE if self._tick >= 1 else RelaxState.RELAXING

            def advance(self, dt_s: float) -> RelaxState:
                recorded_dt.append(dt_s)
                self._tick += 1
                return self.state

            def reset(self) -> None:
                pass

        runner, logger = _make_runner(tmp_path, rc=DtRecordingRelax())
        step = TestStep(StepKind.RELAX, "relax_first_tick")
        plan = TestPlan(test_type=None, steps=(step,))
        runner.run(plan)

        assert len(recorded_dt) >= 1
        assert all(dt >= 0.0 for dt in recorded_dt), (
            f"Negatív dt_s érték: {[dt for dt in recorded_dt if dt < 0.0]}"
        )
        logger.close()


class TestSession11RelaxDmmFaultCountAggregation:
    """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
    Adatintegritás P2 lelet] a RelaxController.dmm_fault_count (önálló
    StepKind.RELAX úton, CHARGE_ONLY/CHARACTERIZATION/BQ_LEARNING_PHYSICAL
    relax-lépései) korábban KIZÁRÓLAG a beágyazott GoldenGg/
    TraditionalLearning RelaxController-eknél aggregálódott — az önálló
    úton sosem jutott el sem a TestResult-ig, sem a report.json-ig."""

    def test_relax_dmm_fault_count_reaches_test_result(self, tmp_path):
        class FaultyRelax:
            def __init__(self):
                self._tick = 0
                self.dmm_fault_count = 0

            @property
            def state(self) -> RelaxState:
                return RelaxState.RELAX_DONE if self._tick >= 2 else RelaxState.RELAXING

            def advance(self, dt_s: float) -> RelaxState:
                self._tick += 1
                self.dmm_fault_count += 1  # minden tick DMM-hibát szimulál
                return self.state

            def reset(self) -> None:
                pass

        runner, logger = _make_runner(tmp_path, rc=FaultyRelax())
        step = TestStep(StepKind.RELAX, "relax_with_dmm_faults")
        plan = TestPlan(test_type=None, steps=(step,))

        result = runner.run(plan)
        logger.close()

        assert result.relax_dmm_fault_count == 2

    def test_zero_by_default_when_no_dmm_faults(self, tmp_path):
        runner, logger = _make_runner(tmp_path, rc=_StubRelaxCtrl(steps_to_done=2))
        step = TestStep(StepKind.RELAX, "relax_clean")
        plan = TestPlan(test_type=None, steps=(step,))

        result = runner.run(plan)
        logger.close()

        assert result.relax_dmm_fault_count == 0


class TestSession21TemperatureOutOfRangeCountAggregation:
    """[regresszió, Teljes kódbázis review 21. kör (2026-09-07), Session/
    Workflow/Állapotgép P2 lelet] a safety.check_test_temperature_range()
    (20. kör) által generált figyelmeztetés-számláló
    (temperature_out_of_range_count) korábban egyik controlleren sem
    aggregálódott session-szinten a TestResult-ig — az extreme_dt_s_count
    mintáját követi (generikus getattr-alapú delta-összegzés
    _flush_totals()-ban, azonos tulajdonságnéven minden controlleren)."""

    def test_temperature_out_of_range_count_reaches_test_result(self, tmp_path):
        class OutOfRangeChargeCtrl(_StubChargeCtrl):
            def __init__(self):
                super().__init__(steps_to_done=2)
                self.temperature_out_of_range_count = 0

            def advance(self, dt_s: float):
                self.temperature_out_of_range_count += 1
                return super().advance(dt_s)

        runner, logger = _make_runner(tmp_path, cc=OutOfRangeChargeCtrl())
        step = TestStep(StepKind.CHARGE, "charge_out_of_range")
        plan = TestPlan(test_type=None, steps=(step,))

        result = runner.run(plan)
        logger.close()

        assert result.temperature_out_of_range_count == 2

    def test_zero_by_default_when_temperature_in_range(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=2))
        step = TestStep(StepKind.CHARGE, "charge_clean")
        plan = TestPlan(test_type=None, steps=(step,))

        result = runner.run(plan)
        logger.close()

        assert result.temperature_out_of_range_count == 0

    def test_temp_dmm_fault_ticks_reaches_test_result(self, tmp_path):
        """[regresszió, MAX verifier (review-kör 3, a4d9e82debe0bdfcb) P2,
        DFAM-001 4. előfordulása] a temp_dmm_fault_ticks (l. charge_
        controller.py/discharge_controller.py property) korábban KIZÁRÓLAG
        az 5 összetett controlleren, a GUI-rétegben (main_window.py
        temp_dmm_fault_ticks_total) került összegzésre -- a
        TestResult/test_runner.py generikus, MINDEN StepKind-et lefedő
        delta-alapú aggregációja (l. a fenti temperature_out_of_range_count
        azonos tesztje) hiányzott. Egy önálló StepKind.CHARGE lépés
        hőmérséklet-DMM-kiesését a capacity_result_quality DEGRADED-gate
        emiatt sosem látta."""
        class TempDmmFaultChargeCtrl(_StubChargeCtrl):
            def __init__(self):
                super().__init__(steps_to_done=2)
                self.temp_dmm_fault_ticks = 0

            def advance(self, dt_s: float):
                self.temp_dmm_fault_ticks += 1
                return super().advance(dt_s)

        runner, logger = _make_runner(tmp_path, cc=TempDmmFaultChargeCtrl())
        step = TestStep(StepKind.CHARGE, "charge_temp_dmm_fault")
        plan = TestPlan(test_type=None, steps=(step,))

        result = runner.run(plan)
        logger.close()

        assert result.temp_dmm_fault_ticks == 2

    def test_temp_dmm_fault_ticks_zero_by_default(self, tmp_path):
        runner, logger = _make_runner(tmp_path, cc=_StubChargeCtrl(steps_to_done=2))
        step = TestStep(StepKind.CHARGE, "charge_clean")
        plan = TestPlan(test_type=None, steps=(step,))

        result = runner.run(plan)
        logger.close()

        assert result.temp_dmm_fault_ticks == 0


class TestLoggerCloseOnTermination:
    """E5: Logger.close() terminális állapotokban hívódik."""

    def test_logger_close_called_on_done(self, tmp_path):
        from unittest.mock import MagicMock
        from Prog.src.logger import Logger, LogConfig

        real_logger = Logger(tmp_path, LogConfig())
        logger_mock = MagicMock(wraps=real_logger)

        runner, _ = _make_runner(tmp_path / "unused", rc=_StubRelaxCtrl(steps_to_done=1))
        runner._logger = logger_mock

        step = TestStep(StepKind.RELAX, "relax")
        plan = TestPlan(test_type=None, steps=(step,))
        runner.run(plan)

        logger_mock.close.assert_called()
        real_logger.close()

    def test_logger_close_called_on_fault(self, tmp_path):
        from unittest.mock import MagicMock
        from Prog.src.logger import Logger, LogConfig

        real_logger = Logger(tmp_path, LogConfig())
        logger_mock = MagicMock(wraps=real_logger)

        fault_cc = _StubChargeCtrl(steps_to_done=5, fault_at_step=2)
        runner, _ = _make_runner(tmp_path / "unused2", cc=fault_cc)
        runner._logger = logger_mock

        plan = TestPlan(test_type=None, steps=(TestStep(StepKind.CHARGE, "charge"),))
        runner.run(plan)

        logger_mock.close.assert_called()
        real_logger.close()


# ------------------------------------------------------------------ #
# [M-02] Relax gyár step argumentumot kap                           #
# ------------------------------------------------------------------ #

class TestRelaxFactoryReceivesStep:
    """[M-02] relax_ctrl_factory(step) — a gyár megkapja a lépést,
    hogy megkülönbözthesse post-charge (2h) vs post-discharge (5h) relaxot."""

    def test_relax_factory_called_with_step(self, tmp_path):
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode
        from Prog.src.logger import Logger, LogConfig
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        received: list[object] = []

        def _relax_factory(step):
            received.append(step)
            return _StubRelaxCtrl(steps_to_done=1)

        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=MockDMM(voltage_V=12.5),
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        config = TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="t")
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=1),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=_relax_factory,
        )

        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(
                TestStep(StepKind.RELAX, "relax_after_charge"),
                TestStep(StepKind.RELAX, "relax_after_discharge"),
            ),
        )
        runner.run(plan)
        logger.close()

        assert len(received) == 2
        labels = [s.label for s in received]
        assert "relax_after_charge" in labels
        assert "relax_after_discharge" in labels


# ------------------------------------------------------------------ #
# [H-03] NPLC konfiguráció worker threadben                          #
# ------------------------------------------------------------------ #

class TestNplcInWorkerThread:
    """[H-03] DMM NPLC-t a TestRunner konfigurálja (worker thread),
    nem a GUI thread — elkerüli a race conditiont."""

    def test_runner_config_has_nplc_fields(self):
        cfg = TestRunnerConfig(charge_discharge_nplc=5.0, relax_ocv_nplc=20.0)
        assert cfg.charge_discharge_nplc == 5.0
        assert cfg.relax_ocv_nplc == 20.0

    def test_nplc_set_before_charge_step(self, tmp_path):
        """CHARGE lépés előtt set_nplc(charge_discharge_nplc) hívódik a DMM-en."""
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode
        from Prog.src.logger import Logger, LogConfig
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        dmm_v = MockDMM(voltage_V=12.5)
        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=7.0,
        )
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=1),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=lambda step: _StubRelaxCtrl(steps_to_done=1),
        )
        plan = TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        runner.run(plan)
        logger.close()

        assert dmm_v.called("set_nplc(7.0)")

    def test_nplc_set_failure_is_logged_not_silently_swallowed(self):
        """Governance-v2 review-kör 1 (Hardver reviewer, CF-37/HW-R1-07,
        DFAM-012 rokona): a korábbi `except Exception: pass` feltevése
        ("DMM fault a mérésnél derül ki") HAMIS — rossz NPLC-vel a READ?
        érvényes értéket ad, csak kisebb integrációs idővel/zajelnyomással,
        tehát sem fault, sem warning, sem quality flag nem keletkezett. Egy
        OCV/relax lépés futhat végig a charge-hoz szánt gyors NPLC-vel,
        észrevétlenül. Fix: NPLC_SET_FAILED esemény + `_last_configured_
        nplc` None-ra állítása (a lépés nem áll meg — ez a viselkedés-
        döntés megmarad)."""
        from unittest.mock import MagicMock
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        dmm_v = MockDMM(voltage_V=12.5)
        dmm_v.set_nplc = MagicMock(side_effect=Exception("SCPI timeout"))
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        logger = MagicMock()
        config = TestRunnerConfig(charge_discharge_nplc=7.0, relax_ocv_nplc=20.0)
        runner = TestRunner(
            instrument_manager=instruments,
            safety=MagicMock(),
            logger=logger,
            profile=MagicMock(),
            config=config,
            charge_ctrl_factory=lambda: None,
            discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
        )
        step = TestStep(StepKind.CHARGE, "charge")

        runner._configure_dmm_for_step(step)  # nem szabad kivételt dobnia

        assert runner._last_configured_nplc is None
        assert any(
            call.args and call.args[0] == "NPLC_SET_FAILED"
            for call in logger.log_event.call_args_list
        )


def _make_flaky_set_nplc(dmm_v, fail_first_n: int) -> list:
    """Kör 20 Batch E2 (F20-S1): az első `fail_first_n` set_nplc() hívás
    dob, a többi a valódi mockot futtatja. A bukott kísérlet is bekerül a
    dmm_v.calls-ba (ahogy a valódi MockDMM.set_nplc() teszi), hogy a
    hívási SORREND is assertálható legyen."""
    real = dmm_v.set_nplc
    seen: list = []

    def _wrapped(nplc: float) -> None:
        seen.append(nplc)
        if len(seen) <= fail_first_n:
            dmm_v.calls.append(f"set_nplc({nplc})")
            raise RuntimeError("Kör 20 Batch E2 teszt: szimulált set_nplc hiba")
        real(nplc)

    dmm_v.set_nplc = _wrapped
    return seen


class TestNplcRetryForBaseStepKinds:
    """Kör 20 Batch E2 (F20-S1, P2, DFAM-001 + DFAM-043): korábban a
    set_nplc() retry STRUKTURÁLISAN nem létezett StepKind.CHARGE/
    DISCHARGE/RELAX-en, mert egyik sem szerepel sem a
    _RELAX_SUBSTATE_NAMES-ban, sem a _FAST_SUBSTATE_NAMES-ban -- a
    _reconfigure_dmm_nplc_for_relax_substate() a `fast_substates is None`
    ágon feltétel nélkül visszatért, MÉG a `_last_configured_nplc is None`
    állapot ellenőrzése ELŐTT. L. _reconfigure_dmm_nplc_for_relax_
    substate() Kör 20-ban kiegészített docstringje."""

    def test_charge_step_retries_base_nplc_after_first_failure(self, tmp_path):
        """DISZKRIMINÁLÓ: a fix ELŐTTI kódon a második `set_nplc()` hívás
        SOHA nem történik meg (a `_last_configured_nplc` örökre None
        marad), tehát a DMM a teljes CHARGE lépésre az ELŐZŐ lépés
        integrációs idején ragad."""
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        _make_flaky_set_nplc(dmm_v, fail_first_n=1)

        plan = TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == ["set_nplc(1.0)", "set_nplc(1.0)"]
        assert runner._last_configured_nplc == 1.0

    def test_discharge_step_retries_base_nplc_after_first_failure(self, tmp_path):
        """Sibling-lefedettség: StepKind.DISCHARGE ugyanabba a "nincs
        substate-térkép" ágba esik, mint CHARGE."""
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        dc = _StubDischargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, dc=dc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        _make_flaky_set_nplc(dmm_v, fail_first_n=1)

        plan = TestPlan(
            test_type=TestType.DISCHARGE_ONLY,
            steps=(TestStep(StepKind.DISCHARGE, "discharge"),),
        )
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == ["set_nplc(1.0)", "set_nplc(1.0)"]
        assert runner._last_configured_nplc == 1.0

    def test_relax_step_retries_with_relax_ocv_nplc_not_charge_discharge_nplc(self, tmp_path):
        """A RELAX StepKind a `_SLOW_BASE_NPLC_STEP_KINDS` halmazban van --
        ez a teszt azt bizonyítja, hogy a retry a HELYES (lassú) alap-NPLC-t
        választja, nem a gyorsat. Egy hibás `_base_nplc_for_step()`
        implementáció (pl. felcserélt ág) itt bukna, a CHARGE/DISCHARGE
        teszteken viszont nem, mert azok mindkét NPLC-értéket ugyanarra
        a StepKind-ra várják."""
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        _make_flaky_set_nplc(dmm_v, fail_first_n=1)

        plan = TestPlan(test_type=None, steps=(TestStep(StepKind.RELAX, "relax"),))
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == ["set_nplc(10.0)", "set_nplc(10.0)"]
        assert runner._last_configured_nplc == 10.0

    def test_traditional_learning_retry_behaviour_unchanged(self, tmp_path):
        """KONTROLL (I2): a TRADITIONAL_LEARNING StepKind (a
        _RELAX_SUBSTATE_NAMES ágon megy, amit E2 NEM módosított) egy
        lépés-eleji set_nplc()-hibája után ugyanúgy retry-zik, mint a fix
        előtt -- a `target_nplc == _last_configured_nplc` egyenlőség-őr
        önmagában is helyesen kezeli a None esetet, ezt a fix nem
        érintette."""
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        tlc = _ScriptedGoldenGgCtrl([
            LearningState.DISCHARGE_1, LearningState.DISCHARGE_1, LearningState.DONE,
        ])
        runner, logger = _make_runner(tmp_path, tlc=tlc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        _make_flaky_set_nplc(dmm_v, fail_first_n=1)

        plan = TestPlan(
            test_type=TestType.TRADITIONAL_LEARNING,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning"),),
        )
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == ["set_nplc(1.0)", "set_nplc(1.0)"]
        assert runner._last_configured_nplc == 1.0


class TestNplcSetFailedEventChannel:
    """Kör 20 Batch E2 (F20-S2, P3, DFAM-011 + DFAM-016): a NPLC_SET_FAILED
    volt a runner egyetlen hibája, ami sosem jutott el az élő GUI-ra
    (on_event), és mindkét kibocsátója védtelen log_event()-et hívott."""

    def test_log_event_io_error_does_not_escalate_to_unhandled_exception(self, tmp_path):
        """DISZKRIMINÁLÓ: a fix ELŐTTI kódon a `_reconfigure_dmm_nplc_for_
        relax_substate()`-beli védtelen log_event() egy lemez-I/O hibája a
        tick-hurkot megszakítva UNHANDLED_EXCEPTION FAULT-tá eszkalálná az
        ártalmatlan NPLC-hibát."""
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        from unittest.mock import MagicMock

        rc = _StubRelaxCtrl(steps_to_done=3)
        runner, _logger = _make_runner(tmp_path, rc=rc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        dmm_v.simulate_nplc_set_failure = True  # TARTÓS hiba minden ticken
        runner._logger.log_event = MagicMock(side_effect=OSError("disk full"))

        plan = TestPlan(test_type=None, steps=(TestStep(StepKind.RELAX, "relax"),))
        result = runner.run(plan)

        assert "UNHANDLED_EXCEPTION" not in (result.reason or "")
        assert result.status != "FAULT" or "NPLC" not in (result.reason or "").upper()

    def test_nplc_set_failed_is_forwarded_to_on_event(self, tmp_path):
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        dmm_v.simulate_nplc_set_failure = True
        events: list = []
        runner.on_event = events.append

        plan = TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        runner.run(plan)
        logger.close()

        assert any(ev.get("event_code") == "NPLC_SET_FAILED" for ev in events)

    def test_recovered_then_failing_again_emits_a_second_live_event(self, tmp_path):
        """MAX verifier lelet (V-1, a7934a12879524dde): a siker-ágakon
        `self._nplc_set_failing = False` (:2319, :2440) korábban NEM volt
        diszkribáló teszttel lefedve -- e nélkül a flag a szegmens
        VÉGÉIG True maradna egy átmeneti hiba után, és egy KÉSŐBBI,
        FÜGGETLEN NPLC-hiba SOHA nem jutna el az élő GUI-ra, pontosan az
        elvesztett diagnosztika, amiért F20-S2 létezik. Két lépéses terv
        (CHARGE majd RELAX), mindkettő saját lépés-eleji set_nplc()-je
        bukik -- a kettő KÖZÖTT a CHARGE retry-je sikerrel zár, ami
        visszaállítja a kaput."""
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        cc = _StubChargeCtrl(steps_to_done=2)
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc, rc=rc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        real_set_nplc = dmm_v.set_nplc
        fail_at_indices = {1, 3}  # 1. hívás (CHARGE setup), 3. hívás (RELAX setup)
        seen: list = []

        def _wrapped(nplc: float) -> None:
            seen.append(nplc)
            if len(seen) in fail_at_indices:
                dmm_v.calls.append(f"set_nplc({nplc})")
                raise RuntimeError("Kör 20 Batch E2 teszt: szimulált set_nplc hiba")
            real_set_nplc(nplc)

        dmm_v.set_nplc = _wrapped
        events: list = []
        runner.on_event = events.append

        plan = TestPlan(
            test_type=None,
            steps=(TestStep(StepKind.CHARGE, "charge"), TestStep(StepKind.RELAX, "relax")),
        )
        runner.run(plan)
        logger.close()

        live_events = [ev for ev in events if ev.get("event_code") == "NPLC_SET_FAILED"]
        assert len(live_events) == 2

    def test_sustained_failure_emits_only_one_live_event(self, tmp_path):
        """I7/N1: egy TARTÓS set_nplc()-hiba (minden ticken újra megkísérelve
        az F20-S1 retry óta) NEM áraszthatja el a GUI eseménynaplóját --
        a `_nplc_set_failing` tranzíció-kapu csak EGY élő eseményt enged
        át, amíg a hiba folyamatos. Az events.csv írási KÍSÉRLETE (l.
        log_event hívásszáma) NEM ritkított."""
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        cc = _StubChargeCtrl(steps_to_done=5)
        runner, logger = _make_runner(tmp_path, cc=cc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        dmm_v.simulate_nplc_set_failure = True
        events: list = []
        runner.on_event = events.append
        log_event_calls: list = []
        real_log_event = runner._logger.log_event

        def _counting_log_event(*args, **kwargs):
            log_event_calls.append(args)
            return real_log_event(*args, **kwargs)

        runner._logger.log_event = _counting_log_event

        plan = TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        runner.run(plan)
        logger.close()

        live_events = [ev for ev in events if ev.get("event_code") == "NPLC_SET_FAILED"]
        assert len(live_events) == 1
        nplc_log_attempts = [c for c in log_event_calls if c and c[0] == "NPLC_SET_FAILED"]
        assert len(nplc_log_attempts) > 1


class TestRunScopedFlagReset:
    """Kör 20 Batch E2 (F20-S6, P3, DFAM-032) + WORK_STATE.json
    open_findings[63] (O2) egyesített lezárása: mindkét flag SZEGMENS-
    hatókörű, de eddig kizárólag __init__-ben nullázódott -- UGYANAZON
    runner-példány MÁSODIK run()-hívásánál (manuális BQ checkpoint utáni
    folytatás) az előző szegmens értéke öröklődött volna."""

    def test_second_run_segment_clears_segment_scoped_flags(self, tmp_path):
        """A flag-reset a `run()` ELEJÉN (a `self.status = "RUNNING"`
        közelében) történik, MIELŐTT bármilyen controller-advance() vagy
        terminális safe-off lefutna -- ezért a 2. szegmens ELSŐ tickjén,
        a stub controller `advance()`-ja MÁR a nullázott értéket kell
        lássa, még a lépés/session befejezése (ami legitim módon ÚJRA
        beírja `_last_safe_off_result`-ot a saját safe-offjával) ELŐTT."""
        captured: dict = {}

        class _CapturingChargeCtrl(_StubChargeCtrl):
            def advance(self, dt_s: float):
                if not captured:
                    captured["last_safe_off_result"] = runner._last_safe_off_result
                    captured["periodic_checkpoint_write_failing"] = (
                        runner._periodic_checkpoint_write_failing
                    )
                    captured["nplc_set_failing"] = runner._nplc_set_failing
                return super().advance(dt_s)

        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
        )
        cc = _StubChargeCtrl(steps_to_done=1)
        runner, logger = _make_runner(tmp_path, cc=cc, config=config)

        plan = TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        runner.run(plan)

        # Az 1. szegmens VÉGÉN szimulált, öröklődésre hajlamos állapot.
        runner._last_safe_off_result = {"PSU": True, "Load": True}
        runner._periodic_checkpoint_write_failing = True
        runner._nplc_set_failing = True

        # Kör 20 Batch E2 (F20-S6/O2): UGYANAZON runner-példány 2. run()-
        # hívása -- a status resethez reset_control_flags() a valódi
        # worker.py::request_continue_from_checkpoint() mintáját követi.
        runner.reset_control_flags()
        runner._charge_ctrl_factory = lambda: _CapturingChargeCtrl(steps_to_done=1)
        runner.run(plan)
        logger.close()

        assert captured["last_safe_off_result"] is None
        assert captured["periodic_checkpoint_write_failing"] is False
        assert captured["nplc_set_failing"] is False


class _ScriptedGoldenGgCtrl:
    """WP-AK-16 GoldenGgController stub — a state egy előre megadott
    listán halad végig, egy elem per advance() hívás (a valódi TICK-
    sorrend szimulálásához, NPLC-váltás regressziós teszthez)."""
    def __init__(self, state_script: list) -> None:
        self._states = list(state_script)
        self._i = 0
        self._state = self._states[0]
        self.fault_reason: str = ""
        self.accumulated_charge_Ah: float = 0.0
        self.accumulated_discharge_Ah: float = 0.0
        self.accumulated_charge_Wh: float = 0.0
        self.accumulated_discharge_Wh: float = 0.0

    @property
    def state(self):
        return self._state

    def advance(self, dt_s: float):
        if self._i < len(self._states) - 1:
            self._i += 1
        self._state = self._states[self._i]
        return self._state


class TestSession3RelaxSubstateNplc:
    """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
    Workflow P2 lelet #3] a TRADITIONAL_LEARNING/GOLDEN_GG StepKind
    egyetlen, opak lépésen belül CC/CV töltési ÉS relax-fázisokat is
    átfog — a NPLC-t korábban _configure_dmm_for_step() a teljes lépésre
    EGYSZER, a lépés elején állította be (charge_discharge_nplc, gyors/
    durva), a belső relax-fázisok (pl. RELAX_2H/RELAX_5H) sosem kapták
    meg a szűk dV/dt-küszöb megbízható kiértékeléséhez szükséges alacsony
    DMM-zajú (magas NPLC) mérést. _reconfigure_dmm_nplc_for_relax_substate()
    dinamikusan, tickenként vált a controller élő állapotától függően."""

    def test_golden_gg_relax_2h_substate_gets_relax_nplc(self, tmp_path):
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        dmm_v = MockDMM(voltage_V=12.5)
        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        # PRECOND (gyors) -> RELAX_2H (lassú) -> RELAX_2H (nincs ismételt hívás)
        # -> SYSTEM_RATE_DISCHARGE (gyors) -> DONE.
        ggc = _ScriptedGoldenGgCtrl([
            GoldenGgState.PRECOND, GoldenGgState.RELAX_2H, GoldenGgState.RELAX_2H,
            GoldenGgState.SYSTEM_RATE_DISCHARGE, GoldenGgState.DONE,
        ])
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=1),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=lambda step: _StubRelaxCtrl(steps_to_done=1),
            golden_gg_ctrl_factory=lambda: ggc,
        )
        plan = TestPlan(
            test_type=TestType.GOLDEN_GG,
            steps=(TestStep(StepKind.GOLDEN_GG, "golden_gg"),),
        )
        runner.run(plan)
        logger.close()

        # A lépés elején a step-szintű konfiguráció beállítja a gyors NPLC-t,
        # RELAX_2H belépéskor magasra vált, SYSTEM_RATE_DISCHARGE-nél vissza
        # a gyorsra — de a második RELAX_2H ticknél NEM ismétli a hívást
        # (self._last_configured_nplc alapú dedup).
        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == [
            "set_nplc(1.0)",   # _configure_dmm_for_step() a lépés elején
            "set_nplc(10.0)",  # PRECOND -> RELAX_2H váltáskor
            "set_nplc(1.0)",   # RELAX_2H -> SYSTEM_RATE_DISCHARGE váltáskor
        ]

    def test_charge_only_step_unaffected_stays_on_fast_nplc(self, tmp_path):
        """A CHARGE/DISCHARGE StepKind (nincs a _RELAX_SUBSTATE_NAMES
        térképben) továbbra is VÁLTOZATLANUL a step-szintű NPLC-t használja
        — a fix nem érinti a CC/CV fast_loop szabályozás DMM-olvasási
        ciklusidejét (HIL-megfontolás, l. az osztály docstringje)."""
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        dmm_v = MockDMM(voltage_V=12.5)
        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=3),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=lambda step: _StubRelaxCtrl(steps_to_done=1),
        )
        plan = TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == ["set_nplc(1.0)"]  # csak a step-eleji hívás, tickenként nincs újra

    def test_traditional_learning_relax_1_substate_gets_relax_nplc(self, tmp_path):
        """L. test_golden_gg_relax_2h_substate_gets_relax_nplc docstringje
        ugyanerről a mintáról — a TRADITIONAL_LEARNING StepKind
        _RELAX_SUBSTATE_NAMES bejegyzésére (RELAX_1)."""
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        dmm_v = MockDMM(voltage_V=12.5)
        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        tlc = _ScriptedGoldenGgCtrl([
            LearningState.DISCHARGE_1, LearningState.RELAX_1, LearningState.RECHARGE,
            LearningState.DONE,
        ])
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=1),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=lambda step: _StubRelaxCtrl(steps_to_done=1),
            traditional_learning_ctrl_factory=lambda: tlc,
        )
        plan = TestPlan(
            test_type=TestType.TRADITIONAL_LEARNING,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning"),),
        )
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == [
            "set_nplc(1.0)",   # _configure_dmm_for_step() a lépés elején
            "set_nplc(10.0)",  # DISCHARGE_1 -> RELAX_1 váltáskor
            "set_nplc(1.0)",   # RELAX_1 -> RECHARGE váltáskor
        ]

    def test_traditional_learning_rebound_check_1_substate_gets_relax_nplc(self, tmp_path):
        """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), Session/
        Workflow P2 lelet] REBOUND_CHECK_1/2 (2026-08-24, `7f714f2`, MIUTÁN
        ez a térkép megépült — sosem lett utólag felvéve) fizikailag/
        mérésmódszertanilag megegyezik RELAX_1/RELAX_2-vel (Load OFF,
        settle-várakozás, feszültség-összehasonlítás), de korábban NEM
        volt a _RELAX_SUBSTATE_NAMES-ben — a DMM NPLC a rebound-ellenőrzés
        alatt a gyors (charge_discharge_nplc) értéken maradt, holott a
        projekt saját konvenciója szerint a magas NPLC (relax_ocv_nplc)
        indokolt akkufeszültség-méréshez."""
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        dmm_v = MockDMM(voltage_V=12.5)
        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        # DISCHARGE_1 (gyors) -> REBOUND_CHECK_1 (lassú, ez az új eset) ->
        # RECHARGE (gyors) -> DONE. Szándékosan NEM megy RELAX_1-en keresztül
        # — az is a relax-substate térképben van, ezért a REBOUND_CHECK_1
        # saját, önálló hozzájárulása a set_nplc(10.0) híváshoz csak így
        # különíthető el (különben a teszt akkor is zölden futna, ha a
        # fix hiányozna, mert a RELAX_1 önmagában is kiváltaná a váltást).
        tlc = _ScriptedGoldenGgCtrl([
            LearningState.DISCHARGE_1, LearningState.REBOUND_CHECK_1,
            LearningState.RECHARGE, LearningState.DONE,
        ])
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=1),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=lambda step: _StubRelaxCtrl(steps_to_done=1),
            traditional_learning_ctrl_factory=lambda: tlc,
        )
        plan = TestPlan(
            test_type=TestType.TRADITIONAL_LEARNING,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning"),),
        )
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == [
            "set_nplc(1.0)",   # _configure_dmm_for_step() a lépés elején
            "set_nplc(10.0)",  # DISCHARGE_1 -> REBOUND_CHECK_1 váltáskor
            "set_nplc(1.0)",   # REBOUND_CHECK_1 -> RECHARGE váltáskor
        ]


class TestSession7OcvSocChemidFastLoopNplc:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Session/
    Workflow P2 lelet #2] Az OCV_SOC/CHEMID StepKind PRECHARGE/FULL_CHARGE_
    MANUFACTURER_METHOD alállapotában (fast_loop_enabled=True beágyazott
    ChargeController, a 2026-08-23 OV-incidens mitigációja) a NPLC korábban
    a teljes lépésre relax_ocv_nplc (lassú) maradt — a 6. kör dinamikus
    NPLC-váltó mechanizmusa csak TRADITIONAL_LEARNING/GOLDEN_GG-re volt
    regisztrálva. _FAST_SUBSTATE_NAMES az ELLENKEZŐ irányban dinamikus: a
    lassú az ALAP (_configure_dmm_for_step()), PRECHARGE/FULL_CHARGE_
    MANUFACTURER_METHOD DEMOTE-ol a gyorsra."""

    def test_ocv_soc_precharge_substate_gets_fast_nplc(self, tmp_path):
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        dmm_v = MockDMM(voltage_V=12.5)
        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        # PRECHARGE (fast_loop, gyors kell) -> PRECHARGE_RELAX (lassú,
        # pontos OCV-méréshez kell) -> PRECHARGE_RELAX (dedup) -> DONE.
        ocv_ctrl = _ScriptedGoldenGgCtrl([
            OcvSocState.PRECHARGE, OcvSocState.PRECHARGE,
            OcvSocState.PRECHARGE_RELAX, OcvSocState.PRECHARGE_RELAX,
            OcvSocState.DONE,
        ])
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=1),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=lambda step: _StubRelaxCtrl(steps_to_done=1),
            ocv_soc_ctrl_factory=lambda: ocv_ctrl,
        )
        plan = TestPlan(
            test_type=TestType.OCV_SOC_CHARACTERIZATION,
            steps=(TestStep(StepKind.OCV_SOC, "ocv_soc"),),
        )
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == [
            "set_nplc(10.0)",  # _configure_dmm_for_step() a lépés elején (lassú alap)
            "set_nplc(1.0)",   # PRECHARGE-re demote (fast_loop)
            "set_nplc(10.0)",  # PRECHARGE -> PRECHARGE_RELAX promote vissza
        ]

    def test_chemid_full_charge_substate_gets_fast_nplc(self, tmp_path):
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        dmm_v = MockDMM(voltage_V=12.5)
        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        chemid_ctrl = _ScriptedGoldenGgCtrl([
            ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD,
            ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD,
            ChemIdState.RELAX_5H_FULL, ChemIdState.RELAX_5H_FULL,
            ChemIdState.RECHARGE_REQUIRED,
        ])
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger,
            profile=profile,
            config=config,
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=1),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=lambda step: _StubRelaxCtrl(steps_to_done=1),
            chemid_ctrl_factory=lambda: chemid_ctrl,
        )
        plan = TestPlan(
            test_type=TestType.CHEMID_CHARACTERIZATION_PBA,
            steps=(TestStep(StepKind.CHEMID, "chemid"),),
        )
        runner.run(plan)
        logger.close()

        calls = [c for c in dmm_v.calls if c.startswith("set_nplc(")]
        assert calls == [
            "set_nplc(10.0)",  # _configure_dmm_for_step() a lépés elején (lassú alap)
            "set_nplc(1.0)",   # FULL_CHARGE_MANUFACTURER_METHOD-ra demote (fast_loop)
            "set_nplc(10.0)",  # -> RELAX_5H_FULL promote vissza
        ]


class TestSession7RelaxControllerUBattProxy:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Szoftver/
    Architektúra P2 lelet #1] RelaxController nem exponált u_batt_V
    property-t (csak last_voltage_V-t) — test_runner.py::_build_sample()
    getattr(controller, "u_batt_V", None)-je StepKind.RELAX önálló
    lépéseknél (CHARACTERIZATION/BQ_LEARNING_PHYSICAL/CHARGE_ONLY relax-
    lépései) emiatt mindig None-t adott vissza, ezért InstrumentManager.
    read_all() MÁSODSZOR is lekérdezte ugyanazt a fizikai feszültség-DMM-et
    minden ticken (CLAUDE.md §9 "egy DMM-et egy tickben egy komponens
    olvasson")."""

    def test_relax_step_reads_voltage_dmm_only_once_per_tick(self, monkeypatch):
        """A worker az egyetlen feszültségforrás; a runner csak a cache-t naplózza."""
        from itertools import count
        from threading import Event, get_ident
        from unittest.mock import Mock, patch

        import Prog.src.test_runner as module
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger
        from Prog.src.relax_controller import RelaxController, RelaxSamplingService
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        dmm_v = MockDMM(voltage_V=12.5)
        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        instruments = InstrumentManager(
            psu=MockPSU(voltage_V=14.4, current_A=1.75),
            load=MockLoad(voltage_V=12.5, current_A=0.7),
            dmm_voltage=dmm_v,
            dmm_temperature=MockDMM(temperature_C=25.0),
        )
        rc = RelaxController(dmm_v, RelaxConfig(min_relax_s=2.0))
        logger = Mock(spec=Logger)
        runner = TestRunner(
            instrument_manager=instruments,
            safety=SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT),
            logger=logger, profile=profile,
            config=TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False, test_name="t"),
            charge_ctrl_factory=lambda: _StubChargeCtrl(steps_to_done=1),
            discharge_ctrl_factory=lambda: _StubDischargeCtrl(steps_to_done=1),
            relax_ctrl_factory=lambda step: rc,
        )
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        worker_waiting = Event()
        read_threads = []

        def wait_for_stop(stop, delay):
            # A worker órája áll: egy kezdőminta, majd stopig várakozás.
            assert delay == pytest.approx(1.5)
            worker_waiting.set()
            assert stop.wait(2.0)  # Csak deadlock-watchdog, nem mérési időalap.
            return True

        service = RelaxSamplingService(dmm_v, clock=lambda: 0.0, wait=wait_for_stop)

        def service_factory(dmm):
            assert dmm is dmm_v
            return service

        original_advance = rc.advance

        def advance(dt_s):
            assert worker_waiting.wait(2.0)
            assert dmm_v.measurement_owned
            return original_advance(dt_s)

        original_read = dmm_v.read_voltage

        def read_voltage(*, owner=None):
            read_threads.append(get_ident())
            return original_read(owner=owner)

        monkeypatch.setattr(module, "RelaxSamplingService", service_factory)
        monkeypatch.setattr(rc, "advance", Mock(side_effect=advance))
        monkeypatch.setattr(dmm_v, "read_voltage", Mock(side_effect=read_voltage))
        monkeypatch.setattr(instruments, "read_all", Mock(wraps=instruments.read_all))
        try:
            with patch("Prog.src.test_runner.time") as mock_time:
                mock_time.perf_counter.side_effect = count(0.0, 1.0)
                mock_time.monotonic.return_value = 0.0
                # A stop utáni szálzárás itt is csak deadlock-watchdog.
                mock_time.sleep.side_effect = lambda delay: service._thread.join(2.0)
                result = runner.run(plan)
        finally:
            service.stop()
            if service._thread is not None:
                service._thread.join(2.0)
                assert not service._thread.is_alive()

        assert result.status == "DONE"
        assert rc.state == RelaxState.RELAX_DONE
        assert rc.sampling_mode == "SERVICE"
        assert rc.advance.call_count == 2
        assert instruments.read_all.call_count == rc.advance.call_count
        assert all(
            call.kwargs["skip_battery_voltage"]
            for call in instruments.read_all.call_args_list
        )
        # Egy worker-minta két runner-ticken: nincs második fizikai olvasás.
        assert dmm_v.read_voltage.call_count == 1
        assert dmm_v.read_voltage.call_args.kwargs["owner"] is not None
        assert read_threads == [service._thread.ident]
        assert read_threads[0] != get_ident()
        assert [call.args[0]["battery_voltage_V"] for call in logger.log_sample.call_args_list] == [
            12.5, 12.5,
        ]
        assert rc.sampling_shutdown_complete
        assert not dmm_v.measurement_owned

    def test_relax_controller_u_batt_v_property_matches_last_voltage(self):
        from Prog.src.relax_controller import RelaxConfig, RelaxController
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        dmm_v = MockDMM(voltage_V=12.34)
        rc = RelaxController(dmm_v, RelaxConfig(min_relax_s=100.0))
        rc.advance(1.0)  # INIT -> RELAXING, olvas
        assert rc.u_batt_V == pytest.approx(12.34)
        assert rc.u_batt_V == rc.last_voltage_V


# ------------------------------------------------------------------ #
# Review-kör 5, Hardver/Műszervezérlés HW-R5-B: PSU CV/CC GUI-only    #
# ------------------------------------------------------------------ #

class TestPsuRegulationStatePolling:
    """HW-R5-B (P2, felhasználói döntés 2026-09-10): a PSU CV/CC
    regulációs állapota egy KÜLÖN, NEM-escaláló csatornán (on_psu_
    regulation) jut el a hívóhoz -- SOHA nem device-error, SOHA nem
    FAULT-ot okoz, SOHA nem kerül a session CSV/riportba."""

    def test_on_psu_regulation_called_with_decoded_state(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.psu.regulation_cv = True
        runner._instruments.psu.regulation_on = True
        received = []
        runner.on_psu_regulation = received.append
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "DONE"
        assert len(received) >= 1
        assert received[0] == {"CV": True, "CC": False, "ON": True}
        # A HW-R5-A tanulsága: egy normál üzemben szinte MINDIG igaz
        # állapotbit (itt: CV töltés abszorpciós fázisában) SOHA nem
        # eszkalálhat FAULT-ra -- a fenti result.status == "DONE" MAGA a
        # regressziós bizonyíték, hogy ez a csatorna szerkezetileg el van
        # választva a poll_device_errors()-tól.

    def test_readback_failure_does_not_raise_or_crash_tick(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.psu.simulate_regulation_state_readback_timeout = True
        received = []
        runner.on_psu_regulation = received.append
        result = runner.run(TestPlan.characterization())  # nem szabad kivételt dobnia
        logger.close()
        assert result.status == "DONE"
        assert received == []

    def test_no_callback_registered_is_a_noop(self, tmp_path):
        """Alapértelmezésben (GUI nélküli/headless futtatás) on_psu_
        regulation None -- a poll ekkor egyáltalán nem hívja a drivert,
        nincs felesleges SCPI-forgalom."""
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "DONE"
        assert not runner._instruments.psu.called("read_regulation_state")

    def test_psu_regulation_never_reaches_logger_or_csv(self, tmp_path):
        """Felhasználói döntés: GUI-only -- SEMMILYEN CSV_COLUMNS mező
        nem hordozza ezt az adatot, és a Logger sosem kap ilyen hívást."""
        from Prog.src.logger import CSV_COLUMNS
        assert not any("regulation" in c and "cv" in c.lower() for c in CSV_COLUMNS)
        assert not any(c in ("psu_cv", "psu_cc", "psu_regulation_mode") for c in CSV_COLUMNS)


# ------------------------------------------------------------------ #
# WP-AK-01 / GAP-OLD-002 (INS-003): SCPI kritikus hiba → FAULT        #
# ------------------------------------------------------------------ #

class TestScpiCriticalErrorEscalation:
    """INS-003: aktív energiaátadás (CHARGE/DISCHARGE) alatt egy nem
    whitelistelt SCPI hiba alapból FAULT-ot vált ki."""

    def test_critical_scpi_error_during_charge_faults_immediately(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._instruments.psu.pending_errors.append(
            '-222,"Data out of range"'
        )
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "FAULT"
        assert "SCPI_CRITICAL_ERROR" in result.reason
        assert "PSU" in result.reason
        assert result.active_step_label == "charge"
        assert result.safe_off_attempted is True
        assert result.communication_faults_count >= 1

    def test_scpi_error_during_relax_does_not_fault(self, tmp_path):
        """RELAX nem 'aktív energiaátadás' — a hiba logolódik, de nem faultol."""
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0  # minden tick pollozzon
        runner._instruments.psu.pending_errors.append(
            '-222,"Data out of range"'
        )
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        result = runner.run(plan)
        logger.close()
        assert result.status == "DONE"
        assert result.communication_faults_count >= 1

    def test_critical_scpi_error_during_golden_gg_faults(self, tmp_path):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] A GOLDEN_GG
        StepKind korábban KIMARADT a kritikus-SCPI-hiba → FAULT gate-ből —
        egy több órás fizikai ciklus alatt egy csak error-queue-ban
        jelentkező hiba észrevétlen maradt volna."""
        runner, logger = _make_runner(
            tmp_path, ggc=_StubGoldenGgCtrl(steps_to_done=5)
        )
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.psu.pending_errors.append('-222,"Data out of range"')
        result = runner.run(TestPlan.golden_gg())
        logger.close()
        assert result.status == "FAULT"
        assert "SCPI_CRITICAL_ERROR" in result.reason

    def test_critical_scpi_error_during_ocv_soc_faults(self, tmp_path):
        """Ugyanaz az OCV_SOC StepKindre — az OcvSocController PRECHARGE/
        STEP_DISCHARGE alatt aktívan vezérli a PSU-t/Loadot."""
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.load.pending_errors.append('-222,"Data out of range"')
        stub = _StubOcvSocCtrl()
        stub._state = OcvSocState.STEP_DISCHARGE

        def factory():
            return stub

        runner._ocv_soc_ctrl_factory = factory
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.OCV_SOC, "ocv_soc"),),
        )
        result = runner.run(plan)
        logger.close()
        assert result.status == "FAULT"
        assert "SCPI_CRITICAL_ERROR" in result.reason

    def test_whitelisted_scpi_error_during_charge_does_not_fault(self, tmp_path, monkeypatch):
        import Prog.src.scpi_error_policy as policy
        monkeypatch.setattr(
            policy, "NON_CRITICAL_SCPI_ERROR_SUBSTRINGS", frozenset({"BENIGN_TEST"})
        )
        runner, logger = _make_runner(tmp_path)
        runner._instruments.psu.pending_errors.append(
            '-100,"BENIGN_TEST harmless"'
        )
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "DONE"

    def test_load_informational_questionable_bits_do_not_fault_charge(self, tmp_path):
        """[regresszió, HW-R5-A (P1), a HW4-01 (78100ab) közvetlen
        következménye] Valós hardveren a Load STAT:QUES:COND? RS/VON/TBF
        bitjei NORMÁL üzemben (négyvezetékes sense, rákötött akku, sikeres
        trace-capture) szinte MINDIG aktívak -- a HW4-01 fix eredeti
        verziója ezeket is a kritikus device-error csatornára küldte,
        ami MINDEN CHARGE/DISCHARGE sessiont hamis FAULT-tal állított
        volna le az első poll-tickre. A javítás után ezek a bitek NEM
        eszkalálnak (l. instrument_manager.py::
        _poll_load_questionable_status())."""
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0  # minden tick pollozzon
        runner._instruments.load.questionable_status_active_bits = frozenset(
            {"RS", "VON", "TBF"}
        )
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "DONE"

    def test_load_protection_questionable_bit_still_faults_charge(self, tmp_path):
        """Ellenpróba: a HW-R5-A fix nem gyengítheti a tényleges védelmi/
        trip-bit detekciót -- PS (Protection Shutdown) továbbra is
        FAULT-ot ad."""
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.load.questionable_status_active_bits = frozenset({"PS"})
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "FAULT"
        assert "SCPI_CRITICAL_ERROR" in result.reason
        assert "QUESTIONABLE_STATUS" in result.reason
        assert "PS" in result.reason


class TestR1F01RelaxUnexpectedEnergizedStateFaults:
    """[regresszió, R1 F-01 + F-05, B1B2_fixplan_FINAL.md B1/1-3, B1/5] A
    producer (instrument_manager) által típusosan jelölt energia-állapot-
    bizonytalanság (commanded=OFF/actual=ON, *_STATE_POLL_FAILED, PSU/Load
    ERROR_QUEUE_POLL_FAILED timeout) StepKind-től FÜGGETLENÜL a meglévő
    FAULT-ágon -> _emergency_stop()-on fut át, önálló RELAX-ban is. A régi
    kódon RELAX-ban ezek csak naplózódtak (DONE), és a következő energizáló
    lépés elindult. Sima SCPI-hiba, QUESTIONABLE_STATUS, DMM-poll-hiba és
    commanded=ON/actual=OFF RELAX-ban változatlanul nem FAULT."""

    @staticmethod
    def _relax_plan(*extra_steps):
        return TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),) + tuple(extra_steps),
        )

    @staticmethod
    def _inject_actual_on(runner, device):
        if device == "PSU":
            runner._instruments.psu._output_is_on = True
            return "OUTPUT_STATE_MISMATCH"
        runner._instruments.load._input_is_on = True
        return "INPUT_STATE_MISMATCH"

    @staticmethod
    def _inject_check_error_timeout(runner, device, monkeypatch):
        from Prog.src.exceptions import InstrumentTimeoutError

        if device == "PSU":
            runner._instruments.psu.simulate_check_error_failure = True
            return
        load = runner._instruments.load

        def _timeout_check_error(clear_first=True):
            load.calls.append("check_error()")
            raise InstrumentTimeoutError("simulated Load SYST:ERR? timeout")

        monkeypatch.setattr(load, "check_error", _timeout_check_error)

    @staticmethod
    def _real_relax_ctrl_with_done_hook(monkeypatch, on_done):
        """Valódi RelaxController, min_relax_s=0: az 1. advance() csak
        RELAXING-ra lép (relax_controller.py INIT-ág), a 2. adja a
        RELAX_DONE-t. Az `on_done` PONT a RELAX_DONE-t eredményező tickben
        fut (a runner pollja ugyanebben a tickben, az advance() UTÁN jön)."""
        from Prog.src.relax_controller import RelaxConfig, RelaxController
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        rc = RelaxController(MockDMM(voltage_V=12.5), RelaxConfig(min_relax_s=0.0))
        states: list = []
        real_advance = rc.advance

        def _advance(dt_s):
            state = real_advance(dt_s)
            states.append(state)
            if state == RelaxState.RELAX_DONE:
                on_done()
            return state

        monkeypatch.setattr(rc, "advance", _advance)
        return rc, states

    @staticmethod
    def _assert_emergency_stop_safe_off(
        result, runner, tmp_path, events, readback_unverified_device=None
    ):
        """`readback_unverified_device`: R1 F-42 (B7S) -- ha a hibainjekció
        TARTÓS (a safe-off utáni kényszerített pollban is fennáll: az eszköz
        állapota nem olvasható / a lekérdezés timeoutol), az eszköz safe_off_
        result-ja a parancs-szintű True helyett False, és SAFE_OFF_RESULT_
        UNCERTAIN esemény nevezi meg. A többi eszköz True marad; a Load OFF /
        PSU ALL OFF / nincs ON / EMERGENCY_STOP elvárások változatlanok."""
        psu = runner._instruments.psu
        load = runner._instruments.load
        assert result.status == "FAULT"
        assert result.safe_off_attempted is True
        for name in ("Load", "PSU"):
            if name == readback_unverified_device:
                assert result.safe_off_result.get(name) is False
            else:
                assert result.safe_off_result.get(name) is True
        if readback_unverified_device is not None:
            assert any(
                e.get("event_code") == "SAFE_OFF_RESULT_UNCERTAIN"
                and readback_unverified_device in e.get("event_message", "")
                for e in events
            )
        assert load.called("safe_off()")
        assert psu.called("all_outputs_off()")
        assert psu._output_is_on is False
        assert load._input_is_on is False
        assert not psu.called("output_on()")
        assert not load.called("input_on()")
        assert any(e.get("event_code") == "EMERGENCY_STOP" for e in events)
        assert "EMERGENCY_STOP" in (tmp_path / "events.csv").read_text(encoding="utf-8")

    @pytest.mark.parametrize("device", ["PSU", "Load"])
    def test_relax_actual_on_while_commanded_off_faults_via_emergency_stop(
        self, tmp_path, device
    ):
        rc = _StubRelaxCtrl(steps_to_done=50)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.device_error_poll_interval_s = 0.0
        marker = self._inject_actual_on(runner, device)
        events: list = []
        runner.on_event = events.append

        result = runner.run(self._relax_plan())
        logger.close()

        assert result.status == "FAULT"
        assert f"SCPI_CRITICAL_ERROR[{device}]" in result.reason
        assert marker in result.reason
        assert "commanded=OFF" in result.reason
        assert "actual=ON" in result.reason
        assert rc._tick == 1
        assert result.active_step_label == "relax_only"
        self._assert_emergency_stop_safe_off(result, runner, tmp_path, events)

    @pytest.mark.parametrize("device", ["PSU", "Load"])
    def test_relax_actual_on_fault_stops_plan_before_next_energizing_step(
        self, tmp_path, device
    ):
        cc = _StubChargeCtrl(steps_to_done=3)
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc, rc=rc)
        runner._config.device_error_poll_interval_s = 0.0
        self._inject_actual_on(runner, device)
        events: list = []
        runner.on_event = events.append

        result = runner.run(self._relax_plan(TestStep(StepKind.CHARGE, "charge")))
        logger.close()

        assert result.status == "FAULT"
        assert result.active_step_label == "relax_only"
        assert cc._tick == 0
        self._assert_emergency_stop_safe_off(result, runner, tmp_path, events)

    @pytest.mark.parametrize("device", ["PSU", "Load"])
    def test_relax_actual_on_faults_with_real_relax_controller_on_relax_done_tick(
        self, tmp_path, device, monkeypatch
    ):
        """FINAL B1/2: a RELAX_DONE-t eredményező tickre injektált actual=ON
        is FAULT (a régi kódon DONE)."""
        holder: dict = {}
        rc, states = self._real_relax_ctrl_with_done_hook(
            monkeypatch, lambda: holder.setdefault("marker", self._inject_actual_on(runner, device))
        )
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.device_error_poll_interval_s = 0.0
        events: list = []
        runner.on_event = events.append

        result = runner.run(self._relax_plan())
        logger.close()

        assert states == [RelaxState.RELAXING, RelaxState.RELAX_DONE]
        assert result.status == "FAULT"
        assert f"SCPI_CRITICAL_ERROR[{device}]" in result.reason
        assert holder["marker"] in result.reason
        assert "actual=ON" in result.reason
        assert result.active_step_label == "relax_only"
        self._assert_emergency_stop_safe_off(result, runner, tmp_path, events)

    @pytest.mark.parametrize("device", ["PSU", "Load"])
    def test_relax_actual_on_with_check_error_timeout_faults_before_next_step(
        self, tmp_path, device, monkeypatch
    ):
        """FINAL B1/3 (F-01 x F-05): commanded=OFF/actual=ON + check_error()
        timeout a RELAX_DONE-tickben. Az F-05 rövidzár miatt az állapot-
        olvasás ugyanabban a pollban kimarad -- a timeout-bejegyzés
        bizonytalan-jelölése miatt a runner mégis FAULT-ra fut, a
        következő CHARGE lépés egyetlen tickje sem fut le (régi kód: RELAX
        DONE, CHARGE-tick == 1)."""
        cc = _StubChargeCtrl(steps_to_done=3)

        def _on_done():
            self._inject_actual_on(runner, device)
            self._inject_check_error_timeout(runner, device, monkeypatch)

        rc, states = self._real_relax_ctrl_with_done_hook(monkeypatch, _on_done)
        runner, logger = _make_runner(tmp_path, cc=cc, rc=rc)
        runner._config.device_error_poll_interval_s = 0.0
        events: list = []
        runner.on_event = events.append

        result = runner.run(self._relax_plan(TestStep(StepKind.CHARGE, "charge")))
        logger.close()

        assert states == [RelaxState.RELAXING, RelaxState.RELAX_DONE]
        assert result.status == "FAULT"
        assert f"SCPI_CRITICAL_ERROR[{device}]" in result.reason
        assert "ERROR_QUEUE_POLL_FAILED" in result.reason
        assert result.active_step_label == "relax_only"
        assert cc._tick == 0
        # az állapotolvasás csak az 1. (RELAXING) tick pollján futott le
        if device == "PSU":
            assert runner._instruments.psu.calls.count("read_output_state()") == 1
        else:
            assert runner._instruments.load.calls.count("read_input_state()") == 1
        # R1 F-42 (B7S): a check_error() timeout tartós -> a safe-off utáni
        # kényszerített pollban is fennáll -> az eszköz safe_off_result-ja
        # False (az energia-állapot nem igazolt).
        self._assert_emergency_stop_safe_off(
            result, runner, tmp_path, events, readback_unverified_device=device
        )

    @pytest.mark.parametrize("device", ["PSU", "Load"])
    def test_relax_state_poll_failure_faults(self, tmp_path, device):
        """FINAL B1/1: OUTPUT/INPUT_STATE_POLL_FAILED (az állapot nem
        olvasható) RELAX-ban is FAULT."""
        rc = _StubRelaxCtrl(steps_to_done=50)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.device_error_poll_interval_s = 0.0
        if device == "PSU":
            runner._instruments.psu.simulate_output_state_readback_timeout = True
            marker = "OUTPUT_STATE_POLL_FAILED"
        else:
            runner._instruments.load.simulate_input_state_readback_timeout = True
            marker = "INPUT_STATE_POLL_FAILED"
        events: list = []
        runner.on_event = events.append

        result = runner.run(self._relax_plan())
        logger.close()

        assert result.status == "FAULT"
        assert f"SCPI_CRITICAL_ERROR[{device}]" in result.reason
        assert marker in result.reason
        assert rc._tick == 1
        # R1 F-42 (B7S): a readback-timeout tartós -> a safe-off utáni
        # kényszerített pollban is fennáll -> False.
        self._assert_emergency_stop_safe_off(
            result, runner, tmp_path, events, readback_unverified_device=device
        )

    def test_regulation_poll_not_called_before_fault_decision_in_active_step(self, tmp_path):
        """FINAL B1/5: nem-None regulation-callback mellett egy már
        eldönthető FAULT elé nem kerül diagnosztikai PSU-I/O (régi kód: a
        read_regulation_state() a FAULT-döntés ELŐTT futott)."""
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.psu.regulation_cv = True
        runner._instruments.psu.regulation_on = True
        # R1 B7S N-2: az injekció az 1. CHARGE-advance()-ben történik, hogy
        # az aktív lépés cadence-ága döntsön, ne az R-1 lépéshatár-poll.
        _real_advance = cc.advance

        def _advance_then_scpi_error(dt_s):
            state = _real_advance(dt_s)
            if cc._tick == 1:
                runner._instruments.psu.pending_errors.append('-222,"Data out of range"')
            return state

        cc.advance = _advance_then_scpi_error
        received: list = []
        runner.on_psu_regulation = received.append

        result = runner.run(TestPlan.characterization())
        logger.close()

        psu_calls = runner._instruments.psu.calls
        assert result.status == "FAULT"
        assert cc._tick == 1  # a cadence-ág döntött, nem a lépéshatár
        assert "SCPI_CRITICAL_ERROR" in result.reason
        assert "all_outputs_off()" in psu_calls
        first_off = psu_calls.index("all_outputs_off()")
        assert "read_regulation_state()" not in psu_calls[:first_off]
        assert received == []

    def test_regulation_poll_not_called_before_relax_energy_state_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path, rc=_StubRelaxCtrl(steps_to_done=50))
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.psu._output_is_on = True
        received: list = []
        runner.on_psu_regulation = received.append

        result = runner.run(self._relax_plan())
        logger.close()

        psu_calls = runner._instruments.psu.calls
        assert result.status == "FAULT"
        assert "all_outputs_off()" in psu_calls
        first_off = psu_calls.index("all_outputs_off()")
        assert "read_regulation_state()" not in psu_calls[:first_off]
        assert received == []

    # --- scope-pin / negatív kontroll: a régi kódon is zöld ---------------

    def test_relax_commanded_on_actual_off_does_not_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.psu.output_commanded_on = True  # actual=OFF marad

        result = runner.run(self._relax_plan())
        logger.close()

        assert result.status == "DONE"
        assert result.communication_faults_count >= 1

    def test_relax_load_questionable_protection_bit_does_not_fault(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        runner._instruments.load.questionable_status_active_bits = frozenset({"PS"})

        result = runner.run(self._relax_plan())
        logger.close()

        assert result.status == "DONE"
        assert result.communication_faults_count >= 1

    def test_relax_dmm_check_error_timeout_does_not_fault(self, tmp_path, monkeypatch):
        from Prog.src.exceptions import InstrumentTimeoutError

        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        dmm_v = runner._instruments.dmm_voltage

        def _timeout_check_error():
            dmm_v.calls.append("check_error()")
            raise InstrumentTimeoutError("simulated DMM SYST:ERR? timeout")

        monkeypatch.setattr(dmm_v, "check_error", _timeout_check_error)

        result = runner.run(self._relax_plan())
        logger.close()

        assert result.status == "DONE"
        assert result.communication_faults_count >= 1

    def test_relax_non_timeout_error_queue_poll_failure_does_not_fault(
        self, tmp_path, monkeypatch
    ):
        from Prog.src.exceptions import InstrumentCommandError

        runner, logger = _make_runner(tmp_path)
        runner._config.device_error_poll_interval_s = 0.0
        load = runner._instruments.load

        def _command_error_check_error(clear_first=True):
            load.calls.append("check_error()")
            raise InstrumentCommandError("simulated non-timeout SYST:ERR? failure")

        monkeypatch.setattr(load, "check_error", _command_error_check_error)

        result = runner.run(self._relax_plan())
        logger.close()

        assert result.status == "DONE"
        assert result.communication_faults_count >= 1


# ------------------------------------------------------------------ #
# WP-AK-01 / GAP-OLD-001 (DATA-003): TestResult traceability-mezők   #
# ------------------------------------------------------------------ #

class TestTerminalReportFields:
    """DATA-003: minden végállapothoz elérhető legyen legalább status/reason/
    active_phase/safe-off/communication-faults a riporthoz."""

    def test_done_result_has_active_step_label_and_attempts_safe_off(self, tmp_path):
        """Governance-v2 review-kör 1 (CF-02/DFAM-016+018, MASTER_
        CONSOLIDATED.md batch 4.1): a DONE ág korábban volt az EGYETLEN
        terminális út, ami NEM hívta a safe-off-ot — a végső fizikai
        állapot kizárólag az utolsó controller saját DONE-ágának sikerén
        múlt, bizonyíték nélkül a riportban. Mostantól minden terminális
        út (DONE is) garantáltan megkísérli a safe-off-ot, ugyanúgy, mint
        FAULT/STOPPED."""
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "DONE"
        assert result.active_step_label == "relax_after_discharge"
        assert result.safe_off_attempted is True
        assert result.communication_faults_count == 0

    def test_fault_result_has_safe_off_attempted_true(self, tmp_path):
        cc = _StubChargeCtrl(fault_at_step=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "FAULT"
        assert result.active_step_label == "charge"
        assert result.safe_off_attempted is True

    def test_graceful_stop_result_has_safe_off_attempted_true(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.stop_requested = True
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "STOPPED"
        assert result.safe_off_attempted is True

    def test_done_result_safe_off_result_reports_per_device_success(self, tmp_path):
        """CF-02 fix: l. test_done_result_has_active_step_label_and_
        attempts_safe_off() indoklása — DONE-nál is a tényleges, per-eszköz
        safe_off() eredmény kerül a riportba, nem üres dict."""
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "DONE"
        assert result.safe_off_result == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }

    def test_graceful_stop_safe_off_result_reports_per_device_success(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.stop_requested = True
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.safe_off_result == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }

    def test_uncertain_psu_off_during_emergency_stop_logs_critical_event_and_is_visible_in_result(
        self, tmp_path
    ):
        """CLAUDE.md §10.4: bizonytalan OFF-állapot (safe_off() sikertelen)
        sosem maradhat csendben — WarningCode.PSU_OUTPUT_STATE_UNCERTAIN
        látszódjon a safe_off_result-ban ÉS egy kritikus eseményben."""
        import csv

        cc = _StubChargeCtrl(fault_at_step=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._instruments.psu.raise_on_safe_off = True
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.status == "FAULT"
        assert result.safe_off_result["PSU"] is False
        assert result.safe_off_result["Load"] is True
        rows = list(csv.DictReader((tmp_path / "events.csv").open()))
        uncertain_rows = [r for r in rows if r["event_code"] == "SAFE_OFF_RESULT_UNCERTAIN"]
        assert len(uncertain_rows) == 1
        assert uncertain_rows[0]["is_critical"] == "True"
        assert "PSU_OUTPUT_STATE_UNCERTAIN" in uncertain_rows[0]["event_message"]

    def test_uncertain_load_off_during_graceful_stop_logs_critical_event(self, tmp_path):
        import csv

        runner, logger = _make_runner(tmp_path)
        runner._instruments.load.raise_on_safe_off = True
        runner.stop_requested = True
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.safe_off_result["Load"] is False
        rows = list(csv.DictReader((tmp_path / "events.csv").open()))
        uncertain_rows = [r for r in rows if r["event_code"] == "SAFE_OFF_RESULT_UNCERTAIN"]
        assert len(uncertain_rows) == 1
        assert "LOAD_INPUT_STATE_UNCERTAIN" in uncertain_rows[0]["event_message"]

    def test_both_psu_and_load_off_confirmed_logs_no_uncertain_event(self, tmp_path):
        import csv

        runner, logger = _make_runner(tmp_path)
        runner.stop_requested = True
        runner.run(TestPlan.characterization())
        logger.close()
        rows = list(csv.DictReader((tmp_path / "events.csv").open()))
        assert not [r for r in rows if r["event_code"] == "SAFE_OFF_RESULT_UNCERTAIN"]

    def test_checkpoint_result_has_active_step_label(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.bq_learning_physical())
        logger.close()
        assert result.status == "CHECKPOINT_STOPPED"
        assert result.active_step_label == "manual_bq_checkpoint"


# ------------------------------------------------------------------ #
# Governance-v2 review-kör 1 (CF-01/DFAM-016, MASTER_CONSOLIDATED.md   #
# batch 4.1): a runner-vezérelt megszakítás elérje a controller SAJÁT #
# emergency_stop()-ját, ne csak a runner alacsonyabb szintű safe-offát.#
# ------------------------------------------------------------------ #

class TestControllerAbortHookOnRunnerInterrupt:
    """Korábban a `_run_step()`-ben létrehozott controller LOKÁLIS volt —
    a runner-vezérelt megszakítás (`_emergency_stop()`/`_graceful_stop()`,
    akár lépések között, akár egy lépés kellős közepén) sosem hívta meg a
    controller saját `emergency_stop()`-ját. Ennek a hiánynak a valós
    következménye a Traditional Learning Cell BL ideiglenes learning-
    értékének tartós, néma benn maradása a BQ Data Flash-ben — itt a
    generikus stub-kontrolleren keresztül, a runner orchestrációs
    szintjén bizonyítjuk."""

    def test_emergency_stop_mid_step_calls_active_controllers_emergency_stop(self, tmp_path):
        calls: list[str] = []

        class _StubChargeCtrlWithAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                calls.append(reason)

        cc = _StubChargeCtrlWithAbort(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        original_advance = cc.advance

        def _advance_then_request_emergency_stop(dt_s: float):
            result = original_advance(dt_s)
            if cc._tick == 2:
                runner.request_emergency_stop("TEST_MID_STEP_INTERRUPT")
            return result

        cc.advance = _advance_then_request_emergency_stop  # type: ignore[method-assign]

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "FAULT"
        assert calls == ["TEST_MID_STEP_INTERRUPT"]

    def test_graceful_stop_mid_step_calls_active_controllers_emergency_stop(self, tmp_path):
        calls: list[str] = []

        class _StubChargeCtrlWithAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                calls.append(reason)

        cc = _StubChargeCtrlWithAbort(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        original_advance = cc.advance

        def _advance_then_request_stop(dt_s: float):
            result = original_advance(dt_s)
            if cc._tick == 2:
                runner.request_stop()
            return result

        cc.advance = _advance_then_request_stop  # type: ignore[method-assign]

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "STOPPED"
        assert calls == ["USER_STOP_REQUESTED"]

    def test_emergency_stop_mid_advance_skips_bookkeeping_for_that_tick(self, tmp_path):
        """[regresszió, 2026-09-15, C-01/DFAM-027 másodlagos fél] A `_run_
        step()` tick-hurka korábban az `emergency_stop_requested`-et CSAK a
        hurok TETEJÉN (a KÖVETKEZŐ `advance()`-hívás ELŐTT) ellenőrizte — ha
        a jelzés PONT egy `advance()` KÖZBEN érkezett (pl. egy hosszú,
        throttle-ölt BQ-poll alatt, l. C-01 fő fix), a hurok MÉG lefuttatta
        az adott tick TELJES mintaépítés/naplózás/error-poll/checkpoint-írás
        körét, mielőtt a tetejére visszatérve észrevette volna — extra,
        szükségtelen latenciát adva a vészleállítás válaszidejéhez PONT
        aktív energiaátadás alatt. A fix a jelzést KÖZVETLENÜL az
        `advance()` UTÁN, a bookkeeping ELŐTT is ellenőrzi (csak akkor, ha a
        controller EZEN a ticken NEM lett finished/FAULT -- a terminális
        tick mintája megmarad, második controller-abort nélkül). A közös
        lezárás új diagnosztikai poll és pacing előtt fut. Ez a teszt azt
        bizonyítja, hogy a még aktív controller megszakított tickjének mintája
        MÁR NEM kerül be a samples.csv-be (a régi kódon 2 adat-sor lenne,
        az 1. ÉS a megszakított 2. tické is)."""
        class _StubChargeCtrlWithAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                pass

        cc = _StubChargeCtrlWithAbort(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        original_advance = cc.advance

        def _advance_then_request_emergency_stop(dt_s: float):
            result = original_advance(dt_s)
            if cc._tick == 2:
                runner.request_emergency_stop("TEST_MID_ADVANCE_SKIP_SAMPLE")
            return result

        cc.advance = _advance_then_request_emergency_stop  # type: ignore[method-assign]

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "FAULT"
        samples_path = tmp_path / "samples.csv"
        lines = samples_path.read_text(encoding="utf-8").splitlines()
        data_rows = len(lines) - 1  # fejléc nélkül
        assert data_rows == 1  # csak az 1. (befejezett) tick mintája; a
        # megszakított 2. tické NEM kerül be.

    def test_graceful_stop_mid_advance_does_not_skip_bookkeeping_for_that_tick(self, tmp_path):
        """[regresszió, 2026-09-15, C-01/DFAM-027, MAX verifier V-6 lelete]
        A fenti korai-kilépés KIZÁRÓLAG `emergency_stop_requested`-re
        vonatkozik -- a `stop_requested` (operátori GRACEFUL stop) esetén
        NINCS safety-haszna a korai kilépésnek (nincs aktív-energiaátadási
        veszély, amit gyorsabban kellene elhárítani), csak a MEGSZAKÍTOTT
        tick mintasorának indokolatlan elvesztését okozná -- ezért a
        graceful ág a RÉGI sorrenden marad: a megszakított tick mintája
        IS bekerül a samples.csv-be (2 sor, NEM 1), mielőtt a hurok a
        tetején észreveszi a `stop_requested`-et."""
        class _StubChargeCtrlWithAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                pass

        cc = _StubChargeCtrlWithAbort(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        original_advance = cc.advance

        def _advance_then_request_stop(dt_s: float):
            result = original_advance(dt_s)
            if cc._tick == 2:
                runner.request_stop()
            return result

        cc.advance = _advance_then_request_stop  # type: ignore[method-assign]

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "STOPPED"
        samples_path = tmp_path / "samples.csv"
        lines = samples_path.read_text(encoding="utf-8").splitlines()
        data_rows = len(lines) - 1  # fejléc nélkül
        assert data_rows == 2  # 1. ÉS a 2. (graceful-stop-pillanatú) tické is.

    def test_controller_without_emergency_stop_method_does_not_break_runner(self, tmp_path):
        """A meglévő stub-kontrollerek (pl. sima _StubChargeCtrl) nem
        exponálnak emergency_stop()-ot — az abort-hook ilyenkor csendben
        no-op, nem akaszthatja meg a garantált safe-offot.

        A megszakítást a lépés KÖZEPÉN, az `advance()`-en belülről váltjuk ki
        (nem `run()` előtt), hogy `self._active_controller` ténylegesen be
        legyen állítva, amikor az abort-hook lefut — enélkül a teszt azt is
        "PASS"-olná, ha az abort-hook egyáltalán nem futna le (lásd MAX
        verifier V-2 finding, batch 4.1)."""
        cc = _StubChargeCtrl(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        original_advance = cc.advance

        def _advance_then_request_emergency_stop(dt_s: float):
            result = original_advance(dt_s)
            if cc._tick == 2:
                runner.request_emergency_stop("TEST_NO_ABORT_METHOD")
            return result

        cc.advance = _advance_then_request_emergency_stop  # type: ignore[method-assign]
        assert not hasattr(cc, "emergency_stop")

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "FAULT"
        assert result.safe_off_attempted is True
        # Nincs kritikus CONTROLLER_ABORT_FAILED esemény — a hiányzó
        # metódus a `getattr(..., None)` ágon csendben, kivétel nélkül
        # no-op, nem egy elnyelt kivétel miatt "véletlenül" zöld a teszt.
        import csv

        rows = list(csv.DictReader((tmp_path / "events.csv").open()))
        assert not [r for r in rows if r["event_code"] == "CONTROLLER_ABORT_FAILED"]

    def test_controller_emergency_stop_raising_does_not_prevent_safe_off(self, tmp_path):
        """Ha a controller SAJÁT emergency_stop()-ja hibázik (pl. BQ
        transport hiba a Cell BL restore közben), ez NEM akadályozhatja a
        runner garantált, alacsonyabb szintű safe-offát (Load/PSU OFF).

        A megszakítást a lépés KÖZEPÉN, az `advance()`-en belülről váltjuk ki
        (nem `run()` előtt), hogy `self._active_controller` ténylegesen be
        legyen állítva, amikor a hibázó `emergency_stop()` lefut (MAX
        verifier V-2 finding, batch 4.1)."""
        class _StubChargeCtrlRaisingOnAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                raise RuntimeError("simulated BQ transport failure during abort")

        cc = _StubChargeCtrlRaisingOnAbort(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        original_advance = cc.advance

        def _advance_then_request_emergency_stop(dt_s: float):
            result = original_advance(dt_s)
            if cc._tick == 2:
                runner.request_emergency_stop("TEST_ABORT_RAISES")
            return result

        cc.advance = _advance_then_request_emergency_stop  # type: ignore[method-assign]

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "FAULT"
        assert result.safe_off_attempted is True
        assert result.safe_off_result == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        # MAX verifier V-7: a controller-abort hiba a session logjába
        # (events.csv) is bekerül, nem csak on_event-re megy — enélkül ez a
        # teszt akkor is zöld maradna, ha a raising emergency_stop() sosem
        # futott volna le ténylegesen.
        import csv

        rows = list(csv.DictReader((tmp_path / "events.csv").open()))
        abort_failed_rows = [r for r in rows if r["event_code"] == "CONTROLLER_ABORT_FAILED"]
        assert len(abort_failed_rows) == 1
        assert "TEST_ABORT_RAISES" in abort_failed_rows[0]["event_message"]
        assert "simulated BQ transport failure during abort" in abort_failed_rows[0]["event_message"]
        assert abort_failed_rows[0]["is_critical"] == "True"

    def test_emergency_stop_between_steps_does_not_reabort_already_completed_controller(
        self, tmp_path
    ):
        """[regresszió, MAX verifier NV-3 (batch 4.1 follow-up)] a MAX
        verifier empirikusan igazolta, hogy egy köztes lépés sikeres DONE
        befejezése UTÁN, a KÖVETKEZŐ lépés indulása ELŐTT (a run()
        for-ciklusának tetején) érkező runner-vezérelt megszakítás a
        nullázás nélkül újra meghívta volna a MÁR lezárt lépés
        controllerének saját `emergency_stop()`-ját. A `characterization()`
        terv első lépése (CHARGE) fejeződik be, MIELŐTT a második (RELAX)
        elindulna — a vészleállítást a CHARGE utolsó tickjén belülről
        váltjuk ki, hogy pontosan a két lépés KÖZÖTTI ablakban süljön el."""
        calls: list[str] = []

        class _StubChargeCtrlWithAbort(_StubChargeCtrl):
            def emergency_stop(self, reason: str) -> None:
                calls.append(reason)

        cc = _StubChargeCtrlWithAbort(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        original_advance = cc.advance

        def _advance_then_request_emergency_stop(dt_s: float):
            result = original_advance(dt_s)
            if cc._tick == 2:  # a CHARGE lépés UTOLSÓ tickje (CHARGE_DONE)
                runner.request_emergency_stop("TEST_BETWEEN_STEPS_INTERRUPT")
            return result

        cc.advance = _advance_then_request_emergency_stop  # type: ignore[method-assign]

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "FAULT"
        assert calls == []  # a MÁR lezárt CHARGE controller NEM abortálódik újra


# ------------------------------------------------------------------ #
# Governance-v2 review-kör 1 (CF-14/DFAM-016, MASTER_CONSOLIDATED.md   #
# batch 4.1, MAX verifier V-3): a _run_step() tick-hurkot körülvevő    #
# try/finally _flush_totals()-a két önálló tulajdonságot garantál —   #
# 1) egy VÁRATLAN kivétel (pl. controller.advance() belsejéből) sem   #
# veszíti el a részleges Ah/Wh-t; 2) a korai-return ágak explicit     #
# hívása + az azt követő finally-beli hívás UGYANARRA a visszatérésre #
# nem duplázza a deltát (idempotencia). Ezekre a b979bf1 commit nem   #
# tartalmazott közvetlen regressziós tesztet.                         #
# ------------------------------------------------------------------ #

class TestFlushTotalsOnUnexpectedExceptionAndIdempotency:
    def test_exception_mid_tick_still_flushes_partial_ah_wh(self, tmp_path):
        """Ha a controller.advance() a hurok KÖZEPÉN, váratlanul dob (nem a
        runner-vezérelt emergency/graceful stop útján), a `_run_step()`-et
        körülvevő `finally: _flush_totals()` biztonsági hálója akkor is
        beírja a MÁR felhalmozott részleges Ah/Wh-t self._total_charge_ah-ba,
        mielőtt a kivétel a run() külső try/except-jéig felfut és
        UNHANDLED_EXCEPTION FAULT-tá alakul. Enélkül ez az érték csendben
        0.0 maradt volna a végső riportban egy több órás session váratlan
        crash-e után is."""
        cc = _StubChargeCtrl(steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)
        original_advance = cc.advance

        def _advance_then_raise(dt_s: float):
            result = original_advance(dt_s)
            if cc._tick == 2:
                raise RuntimeError("simulated unexpected mid-tick failure")
            return result

        cc.advance = _advance_then_raise  # type: ignore[method-assign]

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "FAULT"
        assert "UNHANDLED_EXCEPTION" in result.reason
        assert "simulated unexpected mid-tick failure" in result.reason
        # 2 sikeres advance() hívás (0.01 Ah/hívás) — a MÁSODIK hívás a saját
        # `self._tick`-jét 2-re növeli, MIELŐTT a wrapper ugyanabban a
        # hívásban dob — ez a delta a finally-ban futó _flush_totals()-nak
        # köszönhetően nem veszhet el.
        assert result.total_charge_ah == pytest.approx(0.02)
        assert result.total_charge_wh == pytest.approx(0.26)

    def test_flush_totals_idempotent_across_explicit_and_finally_call(self, tmp_path):
        """A `_controller_faulted()` ág EXPLICIT módon hívja a
        _flush_totals()-t, majd egy közvetlen `TestResult(status="FAULT",
        ...)`-tal (NEM `_emergency_stop()`-on keresztül) tér vissza —
        ilyenkor `safe_off_attempted` alapértelmezett False marad. A
        `_run_step()`-et körülvevő `finally` UGYANERRE a return-re
        MÉGEGYSZER lefuttatja a _flush_totals()-t, MIELŐTT a hívás tényleg
        visszatérne — ha ez nem idempotens, self._total_charge_ah duplázva
        marad. Mivel `safe_off_attempted=False`, a `run()` a step_result
        felfogása után ÚJRA feldolgozza a hibát a saját
        `self._emergency_stop(...)`-ján keresztül (l. run() CF-01
        melléklelet-kommentje), ami egy MÁSODIK TestResult-ot épít
        `self._total_charge_ah`-ból — ide a duplázott érték már látszódna,
        ha a _flush_totals() nem lenne idempotens."""
        cc = _StubChargeCtrl(fault_at_step=3, steps_to_done=100)
        runner, logger = _make_runner(tmp_path, cc=cc)

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "FAULT"
        # 3 advance() hívás * 0.01 Ah = 0.03. Ha az explicit
        # _controller_faulted()-ágbeli hívás + a finally-beli második hívás
        # duplázna, ez 0.06 lenne a run()-ig felérő végső TestResult-ban.
        assert result.total_charge_ah == pytest.approx(0.03)
        assert result.total_charge_wh == pytest.approx(0.39)


# ------------------------------------------------------------------ #
# WP-AK-02 (REC-001..004, GAP-OLD-003): checkpoint traceability-mezők #
# ------------------------------------------------------------------ #

def _configure_traceability(runner) -> None:
    """Teszt-segéd: a _make_runner() alap TestRunnerConfig-ját tölti fel
    WP-AK-02 traceability-mezőkkel (session_recovery.evaluate_resume()-hoz)."""
    runner._config.session_id = "session_TEST_20260821"
    runner._config.process_run_id = "run-abc-123"
    runner._config.config_hash = "cfg-hash-xyz"
    runner._config.profile_hash = "profile-hash-xyz"
    runner._config.sample_id = "S001"
    runner._config.instrument_identities = {"PSU": "MOCK_PSU"}


class TestCheckpointTraceabilityFields:
    """A checkpoint.json-nak minden íráskor tartalmaznia kell a
    session_recovery.evaluate_resume() által igényelt mezőket, helyes
    safe_boundary értékkel a lépéstípus szerint."""

    def _read_checkpoint(self, tmp_path) -> dict:
        import json
        return json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))

    def test_periodic_checkpoint_during_relax_is_safe_boundary(self, tmp_path):
        rc = _StubRelaxCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, rc=rc)
        _configure_traceability(runner)
        runner._checkpoint_interval_s = 0.0  # minden tick checkpointoljon
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()
        cp = self._read_checkpoint(tmp_path)
        assert cp["safe_boundary"] is True
        assert cp["session_id"] == "session_TEST_20260821"
        assert cp["process_run_id"] == "run-abc-123"
        assert cp["config_hash"] == "cfg-hash-xyz"
        assert cp["profile_hash"] == "profile-hash-xyz"
        assert cp["sample_id"] == "S001"
        assert cp["instrument_identities"] == {"PSU": "MOCK_PSU"}

    def test_checkpoint_reflects_pending_bq_df_restore_within_one_tick(self, tmp_path):
        """[regresszió, teljes kódbázis review kör 7 (2026-09-15), C-12]
        a TraditionalLearningController.pending_bq_df_restore mező
        VÁLTOZÁSÁNAK legfeljebb 1 tick késleltetéssel meg kell jelennie
        a lemezre írt checkpoint.json-ban, NE csak a 10s-es (itt
        szándékosan nagyra állított) checkpoint_interval_s leteltekor.

        MAX verifier F2 lelete (2026-09-15): a runner a mezőt kizárólag
        a `"pending_bq_df_restore"` string-névvel, `getattr(..., None)`
        default-dal olvassa a controllerről -- ha a property egyszer
        átnevezésre kerülne a valódi TraditionalLearningController-en,
        a runner CSENDBEN mindig None-t írna, és a lenti, saját nevű
        stub-bal futó teszt ezt NEM venné észre. Ez az explicit
        hasattr-ellenőrzés zárja rövidre azt a csendes szétcsúszást."""
        from Prog.src.traditional_learning_controller import (
            TraditionalLearningController,
        )
        assert hasattr(TraditionalLearningController, "pending_bq_df_restore"), (
            "a valodi TraditionalLearningController property-je atnevezodott "
            "vagy eltunt -- a runner getattr()-je csendben None-t irna"
        )

        class _StubLearningCtrlWithPendingRestore(_StubLearningCtrl):
            pending_bq_df_restore: dict | None = None

            def advance(self, dt_s):
                state = super().advance(dt_s)
                if self._tick >= 2:
                    self.pending_bq_df_restore = {
                        "field": "cell_bl_set_volt_mV",
                        "original_mV": 1950,
                        "restore_confirmed": None,
                        "bq_unsealed": True,
                    }
                return state

        tlc = _StubLearningCtrlWithPendingRestore(steps_to_done=5)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        _configure_traceability(runner)
        runner._checkpoint_interval_s = 1000.0  # NE a periodikus időzítő írjon
        recorded_checkpoints = []
        original_write = logger.write_checkpoint
        logger.write_checkpoint = lambda state: (
            recorded_checkpoints.append(dict(state)), original_write(state)
        )[1]
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "learning"),),
        )
        runner.run(plan)
        logger.close()

        found = [cp for cp in recorded_checkpoints if cp.get("pending_bq_df_restore")]
        assert found, "a valtozas-alapu azonnali checkpoint-iras nem tortent meg"
        assert found[0]["pending_bq_df_restore"]["original_mV"] == 1950

    def test_terminal_checkpoint_preserves_pending_bq_df_restore_after_controller_cleared(
        self, tmp_path
    ):
        """[regresszió, MAX verifier F1 lelete (2026-09-15), C-12] mind a
        4 terminális write_checkpoint()-hívás (DONE/FAULT/STOPPED/manual)
        ELŐTT a hívó MÁR `self._active_controller = None`-t állít (korábbi
        review-körök bevált mintája) -- egy közvetlen `getattr(self.
        _active_controller, ...)` ekkor MINDIG None-t adott volna, akkor
        is, ha az UTOLSÓ ÉLŐ érték egy meg-nem-oldott BQ DF maradványt
        jelzett. A TERMINÁLIS (DONE) checkpointnak is meg kell őriznie a
        maradványt, nem csak a köztes periodikus írásoknak."""
        class _StubLearningCtrlWithPersistentPendingRestore(_StubLearningCtrl):
            pending_bq_df_restore: dict | None = None

            def advance(self, dt_s):
                state = super().advance(dt_s)
                self.pending_bq_df_restore = {
                    "field": "cell_bl_set_volt_mV",
                    "original_mV": 1950,
                    "restore_confirmed": False,
                    "bq_unsealed": True,
                }
                return state

        tlc = _StubLearningCtrlWithPersistentPendingRestore(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        _configure_traceability(runner)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "learning"),),
        )
        runner.run(plan)
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["status"] == "DONE"
        assert cp.get("pending_bq_df_restore") is not None
        assert cp["pending_bq_df_restore"]["original_mV"] == 1950

    def test_checkpoint_reflects_pending_bq_unsealed_within_one_tick(self, tmp_path):
        """R2-11b (2026-09-15, 14. writer-kör, DFAM-016 rokona): ugyanaz a
        változás-alapú, legfeljebb 1 tick késleltetésű checkpoint-írás,
        mint a testvér pending_bq_df_restore-nál, a GoldenGgController.
        pending_bq_unsealed mezőjére. Ugyanaz a hasattr-őrteszt, mint a
        MAX verifier F2 lelete a testvér tesztnél -- ha a valódi property
        átnevezésre kerülne, a runner getattr()-je csendben None-t írna."""
        from Prog.src.golden_gg_controller import GoldenGgController
        assert hasattr(GoldenGgController, "pending_bq_unsealed"), (
            "a valodi GoldenGgController property-je atnevezodott vagy "
            "eltunt -- a runner getattr()-je csendben None-t irna"
        )

        class _StubGoldenGgCtrlWithPendingUnsealed(_StubGoldenGgCtrl):
            pending_bq_unsealed: dict | None = None

            def advance(self, dt_s):
                state = super().advance(dt_s)
                if self._tick >= 2:
                    self.pending_bq_unsealed = {
                        "controller": "GOLDEN_GG",
                        "reason": "SOURCE_SNAPSHOT_READ_REQUIRES_UNSEALED",
                        "bq_unsealed": True,
                    }
                return state

        ggc = _StubGoldenGgCtrlWithPendingUnsealed(steps_to_done=5)
        runner, logger = _make_runner(tmp_path, ggc=ggc)
        _configure_traceability(runner)
        runner._checkpoint_interval_s = 1000.0  # NE a periodikus időzítő írjon
        recorded_checkpoints = []
        original_write = logger.write_checkpoint
        logger.write_checkpoint = lambda state: (
            recorded_checkpoints.append(dict(state)), original_write(state)
        )[1]
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.GOLDEN_GG, "golden_gg"),),
        )
        runner.run(plan)
        logger.close()

        found = [cp for cp in recorded_checkpoints if cp.get("pending_bq_unsealed")]
        assert found, "a valtozas-alapu azonnali checkpoint-iras nem tortent meg"
        assert found[0]["pending_bq_unsealed"]["controller"] == "GOLDEN_GG"

    def test_checkpoint_reflects_tlc_pending_bq_unsealed_within_one_tick(self, tmp_path):
        """[regresszió, Kör 17 MAX-effort review, R17-08] a generikus
        _checkpoint_traceability_fields() helper getattr()-alapú, tehát
        NEM igényelt módosítást ahhoz, hogy a TraditionalLearningController
        ÚJ pending_bq_unsealed property-jét is felvegye -- ez a teszt ezt
        bizonyítja: a runner-oldali plumbing valóban generikus, nem
        GoldenGgController-specifikus."""
        from Prog.src.traditional_learning_controller import (
            TraditionalLearningController,
        )
        assert hasattr(TraditionalLearningController, "pending_bq_unsealed"), (
            "a valodi TraditionalLearningController property-je "
            "atnevezodott vagy eltunt -- a runner getattr()-je csendben "
            "None-t irna"
        )

        class _StubLearningCtrlWithPendingUnsealed(_StubLearningCtrl):
            pending_bq_unsealed: dict | None = None

            def advance(self, dt_s):
                state = super().advance(dt_s)
                if self._tick >= 2:
                    self.pending_bq_unsealed = {
                        "controller": "TRADITIONAL_LEARNING",
                        "reason": "PRECOND_DF_READ_REQUIRES_UNSEALED",
                        "bq_unsealed": True,
                    }
                return state

        tlc = _StubLearningCtrlWithPendingUnsealed(steps_to_done=5)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        _configure_traceability(runner)
        runner._checkpoint_interval_s = 1000.0  # NE a periodikus időzítő írjon
        recorded_checkpoints = []
        original_write = logger.write_checkpoint
        logger.write_checkpoint = lambda state: (
            recorded_checkpoints.append(dict(state)), original_write(state)
        )[1]
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "learning"),),
        )
        runner.run(plan)
        logger.close()

        found = [cp for cp in recorded_checkpoints if cp.get("pending_bq_unsealed")]
        assert found, "a valtozas-alapu azonnali checkpoint-iras nem tortent meg"
        assert found[0]["pending_bq_unsealed"]["controller"] == "TRADITIONAL_LEARNING"

    def test_terminal_checkpoint_preserves_pending_bq_unsealed_after_controller_cleared(
        self, tmp_path
    ):
        """R2-11b: a TERMINÁLIS (DONE) checkpointnak is meg kell őriznie a
        pending_bq_unsealed maradványt, nem csak a köztes periodikus
        írásoknak -- ugyanaz a fallback-to-last-known-value minta, mint a
        testvér pending_bq_df_restore-nál."""
        class _StubGoldenGgCtrlWithPersistentPendingUnsealed(_StubGoldenGgCtrl):
            pending_bq_unsealed: dict | None = None

            def advance(self, dt_s):
                state = super().advance(dt_s)
                self.pending_bq_unsealed = {
                    "controller": "GOLDEN_GG",
                    "reason": "SOURCE_SNAPSHOT_READ_REQUIRES_UNSEALED",
                    "bq_unsealed": True,
                }
                return state

        ggc = _StubGoldenGgCtrlWithPersistentPendingUnsealed(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, ggc=ggc)
        _configure_traceability(runner)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.GOLDEN_GG, "golden_gg"),),
        )
        runner.run(plan)
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["status"] == "DONE"
        assert cp.get("pending_bq_unsealed") is not None
        assert cp["pending_bq_unsealed"]["controller"] == "GOLDEN_GG"

    def test_pending_bq_unsealed_and_pending_bq_df_restore_are_independent_fields(
        self, tmp_path
    ):
        """R2-11b (DFAM-018 semantic-collapse-avoidance guard): a Cell BL
        Data Flash-maradvány és a BQ seal-állapot-maradvány KÉT KÜLÖN
        kockázat -- egy controller, ami mindkettőt jelzi (elvben nem
        fordulhat elő egyszerre TLC/GG-n, de a runner-oldali mezőknek
        akkor is FÜGGETLENNEK kell maradniuk egymástól), nem eshet össze
        egyetlen mezőbe. Mutáció-bizonyíték: ha valaki a runner két
        `getattr(...)` hívását egy közös változóra vonná össze, ez a
        teszt buktatná."""
        class _StubGoldenGgCtrlWithBothResiduals(_StubGoldenGgCtrl):
            pending_bq_unsealed: dict | None = None
            pending_bq_df_restore: dict | None = None

            def advance(self, dt_s):
                state = super().advance(dt_s)
                self.pending_bq_unsealed = {
                    "controller": "GOLDEN_GG",
                    "reason": "SOURCE_SNAPSHOT_READ_REQUIRES_UNSEALED",
                    "bq_unsealed": True,
                }
                self.pending_bq_df_restore = {
                    "field": "unrelated_df_field",
                    "original_mV": 1234,
                }
                return state

        ggc = _StubGoldenGgCtrlWithBothResiduals(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, ggc=ggc)
        _configure_traceability(runner)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.GOLDEN_GG, "golden_gg"),),
        )
        runner.run(plan)
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp.get("pending_bq_unsealed") is not None
        assert cp.get("pending_bq_df_restore") is not None
        assert cp["pending_bq_unsealed"] != cp["pending_bq_df_restore"]
        assert cp["pending_bq_unsealed"]["controller"] == "GOLDEN_GG"
        assert cp["pending_bq_df_restore"]["field"] == "unrelated_df_field"

    def test_periodic_checkpoint_during_charge_is_not_safe_boundary(self, tmp_path):
        """[frissítve, Teljes kódbázis review 20. kör (2026-09-06), Session/
        Workflow P2 lelet] a run() DONE-lezárása mostantól egy záró,
        safe_boundary=True checkpointot ír (l. TestRunIntegration::
        test_done_writes_terminal_checkpoint) — ez a lemezen felülírja a
        periodikus (mid-run) checkpointot. A periodikus írás saját
        safe_boundary-jét ezért minden write_checkpoint()-hívás
        elfogásával, nem a végső lemez-állapotból ellenőrizzük."""
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        _configure_traceability(runner)
        runner._checkpoint_interval_s = 0.0
        recorded_checkpoints = []
        original_write = logger.write_checkpoint
        logger.write_checkpoint = lambda state: (
            recorded_checkpoints.append(dict(state)), original_write(state)
        )[1]
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge_only"),),
        )
        runner.run(plan)
        logger.close()
        periodic = [cp for cp in recorded_checkpoints if cp.get("status") == "RUNNING"]
        assert periodic, "nem történt periodikus checkpoint-írás"
        assert all(cp["safe_boundary"] is False for cp in periodic)

    def test_periodic_checkpoint_during_discharge_is_not_safe_boundary(self, tmp_path):
        """L. test_periodic_checkpoint_during_charge_is_not_safe_boundary
        ugyanerről a mintáról."""
        dc = _StubDischargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, dc=dc)
        _configure_traceability(runner)
        runner._checkpoint_interval_s = 0.0
        recorded_checkpoints = []
        original_write = logger.write_checkpoint
        logger.write_checkpoint = lambda state: (
            recorded_checkpoints.append(dict(state)), original_write(state)
        )[1]
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.DISCHARGE, "discharge_only"),),
        )
        runner.run(plan)
        logger.close()
        periodic = [cp for cp in recorded_checkpoints if cp.get("status") == "RUNNING"]
        assert periodic, "nem történt periodikus checkpoint-írás"
        assert all(cp["safe_boundary"] is False for cp in periodic)

    def test_manual_checkpoint_is_safe_boundary_with_next_step_index(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        _configure_traceability(runner)
        runner.run(TestPlan.bq_learning_physical())
        logger.close()
        cp = self._read_checkpoint(tmp_path)
        assert cp["safe_boundary"] is True
        # manual_bq_checkpoint a 9. (index 8) lépés a bq_learning_physical planben;
        # next_step_index = step_index + 1 = 9 (a terv végén túl, checkpoint_is_terminal).
        assert cp["next_safe_step_index"] == 9

    def test_fault_checkpoint_is_not_safe_boundary(self, tmp_path):
        cc = _StubChargeCtrl(fault_at_step=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        _configure_traceability(runner)
        runner.run(TestPlan.characterization())
        logger.close()
        cp = self._read_checkpoint(tmp_path)
        assert cp["safe_boundary"] is False

    def test_graceful_stop_checkpoint_is_not_safe_boundary(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        _configure_traceability(runner)
        runner.stop_requested = True
        runner.run(TestPlan.characterization())
        logger.close()
        cp = self._read_checkpoint(tmp_path)
        assert cp["safe_boundary"] is False

    def test_unconfigured_traceability_fields_default_to_empty(self, tmp_path):
        """Ha a hívó (régi teszt/híváshely) nem tölti ki a traceability
        mezőket, a checkpoint üres stringeket/dict-et kapjon, ne dobjon
        kivételt — a session_recovery ezt fail-safe BLOCKED-ként kezeli."""
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._checkpoint_interval_s = 0.0
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()
        cp = self._read_checkpoint(tmp_path)
        assert cp["session_id"] == ""
        assert cp["instrument_identities"] == {}


class TestR1703LastSafeOffResultCheckpointAndLiveFeed:
    """[regresszió, Kör 17 MAX-effort review, R17-03+R17-17+V4-1,
    DFAM-016+018+026] a `_safe_all_off_with_uncertainty_check()`
    eredménye korábban SEMMILYEN élő-GUI vagy perzisztens (checkpoint.
    json-beli) csatornán nem jelent meg -- egy bizonytalan safe-off
    csendben "DONE"-ként jelent meg, és egy crash után a KÖVETKEZŐ
    indításnál sem volt semmi jele annak, hogy a kimenet ENERGIZÁLT
    maradhatott."""

    def _read_checkpoint(self, tmp_path) -> dict:
        import json
        return json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))

    def test_uncertain_safe_off_reaches_on_event_live_feed(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._instruments.psu.raise_on_safe_off = True
        events = []
        runner.on_event = events.append

        runner.run(TestPlan.characterization())
        logger.close()

        uncertain_events = [
            e for e in events if e.get("event_code") == "SAFE_OFF_RESULT_UNCERTAIN"
        ]
        assert uncertain_events, (
            "a bizonytalan safe-off korabban csak az events.csv-be jutott, "
            "az elo GUI-feedbe (on_event) nem"
        )
        assert "PSU" in uncertain_events[0]["event_message"]

    def test_confirmed_safe_off_emits_no_on_event(self, tmp_path):
        """A sikeres (nem bizonytalan) agon NEM szabad uj on_event-nek
        megjelennie -- ez tartja meg a mar meglevo, len(events)==1-et
        ellenorzo tesztek (pl. test_manual_checkpoint_calls_on_event)
        ervenyesseget, es kerul el egy uj esemeny-aradas kockazatot."""
        runner, logger = _make_runner(tmp_path)
        events = []
        runner.on_event = events.append

        runner.run(TestPlan.characterization())
        logger.close()

        assert not [
            e for e in events if e.get("event_code") == "SAFE_OFF_RESULT_UNCERTAIN"
        ]

    def test_done_checkpoint_records_confirmed_last_safe_off_result(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.run(TestPlan.characterization())
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["last_safe_off_result"] == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }

    def test_emergency_stop_checkpoint_records_uncertain_last_safe_off_result(
        self, tmp_path
    ):
        cc = _StubChargeCtrl(fault_at_step=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._instruments.psu.raise_on_safe_off = True

        runner.run(TestPlan.characterization())
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["last_safe_off_result"]["PSU"] is False
        assert cp["last_safe_off_result"]["Load"] is True

    def test_graceful_stop_checkpoint_records_last_safe_off_result(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner.stop_requested = True

        runner.run(TestPlan.characterization())
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["last_safe_off_result"] == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }

    def test_manual_checkpoint_records_last_safe_off_result(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.bq_learning_physical())
        logger.close()

        assert result.status == "CHECKPOINT_STOPPED"
        cp = self._read_checkpoint(tmp_path)
        assert cp["last_safe_off_result"] == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }

    def test_periodic_running_checkpoint_has_no_safe_off_record(self, tmp_path):
        """A HÁROM-ÁLLAPOTÚ diszkriminátor: amíg a session MÉG FUT, a
        mezőnek None-nak KELL lennie -- nem szabad optimista módon
        seedelve lennie, csak azért, mert a session eddig hiba nélkül
        futott. A DONE-agi zaro iras felulirna a periodikus allapotot a
        lemezen, ezert kozvetlenul a write_checkpoint() hivasokat
        fogjuk el."""
        rc = _StubRelaxCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._checkpoint_interval_s = 0.0
        recorded = []
        original_write = logger.write_checkpoint
        logger.write_checkpoint = lambda state: (
            recorded.append(dict(state)), original_write(state)
        )[1]
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()

        running_checkpoints = [cp for cp in recorded if cp.get("status") == "RUNNING"]
        assert running_checkpoints, "nem sikerult periodikus checkpointot elfogni"
        assert all(cp.get("last_safe_off_result") is None for cp in running_checkpoints)

    def test_resumed_runner_does_not_inherit_parent_safe_off_evidence(self, tmp_path):
        """Egy resume-olt process NEM orokolheti a SZULO safe-off
        allitasat egy hardverrol, amit MAGA sosem parancsolt -- nincs
        `initial_last_safe_off_result` a TestRunnerConfig-ban."""
        runner, logger = _make_runner(tmp_path)
        assert not hasattr(runner._config, "initial_last_safe_off_result")
        assert runner._last_safe_off_result is None
        logger.close()


class TestPeriodicCheckpointIoGuard:
    """[regresszió, Kör 17 MAX-effort review, R17-01, DFAM-001+DFAM-032]
    a periodikus write_checkpoint()-hívás volt az 5 hívóhely EGYETLEN
    védtelenje -- egy tranziens OSError (Windows sharing violation,
    antivírus, OneDrive) korábban a TELJES sessiont FAULT-tá minősítette,
    TRADITIONAL_LEARNING-nél nem-resume-olható módon (nincs RELAX lépés
    a tervben, is_safe_boundary strukturálisan mindig False)."""

    def _read_checkpoint(self, tmp_path) -> dict:
        import json
        return json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))

    def test_periodic_checkpoint_permission_error_does_not_fault_session(
        self, tmp_path
    ):
        cc = _StubChargeCtrl(steps_to_done=4)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._checkpoint_interval_s = 0.0  # minden tick irjon
        events = []
        runner.on_event = events.append

        original_write = logger.write_checkpoint
        state = {"calls": 0}

        def _flaky_write(payload):
            state["calls"] += 1
            if state["calls"] == 1:
                raise PermissionError("simulated sharing violation")
            return original_write(payload)

        logger.write_checkpoint = _flaky_write

        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        result = runner.run(plan)
        logger.close()

        assert result.status == "DONE"
        assert "UNHANDLED_EXCEPTION" not in (result.reason or "")
        assert any(
            e.get("event_code") == "PERIODIC_CHECKPOINT_WRITE_FAILED"
            for e in events
        ), "a bukott periodikus irasnak esemenyt kell adnia, nem csendben elnyelodnie"
        cp = self._read_checkpoint(tmp_path)
        assert cp["status"] == "DONE"

    def test_failed_periodic_checkpoint_retries_pending_bq_residual_on_next_tick(
        self, tmp_path
    ):
        """A DFAM-032-diszkriminator: egy bukott, maradvanyos periodikus
        iras utan a maradvanynak a KOVETKEZO tickben sikeresen lemezre kell
        kerulnie (a valtozas-alapu ag _pending_changed-je a bukott iras
        utan False, mert a _last_pending_bq_df_restore mar a maradvanyt
        tartja). R1 F-16 ota a retry-t NEM a pending-rollback, hanem a
        _periodic_checkpoint_write_failing OR-triggere garantalja -- ez a
        teszt a retry tenyet ellenorzi, mechanizmustol fuggetlenul."""
        class _StubLearningCtrlWithPendingRestore(_StubLearningCtrl):
            pending_bq_df_restore: dict | None = None

            def advance(self, dt_s):
                stt = super().advance(dt_s)
                if self._tick >= 2:
                    self.pending_bq_df_restore = {
                        "field": "cell_bl_set_volt_mV",
                        "original_mV": 1950,
                        "restore_confirmed": None,
                        "bq_unsealed": True,
                    }
                return stt

        tlc = _StubLearningCtrlWithPendingRestore(steps_to_done=6)
        runner, logger = _make_runner(tmp_path, tlc=tlc)
        _configure_traceability(runner)
        runner._checkpoint_interval_s = 1000.0  # NE az ido-alapu ag irjon

        original_write = logger.write_checkpoint
        recorded = []
        state = {"already_failed_once": False}

        def _fails_once_on_residual(payload):
            has_residual = payload.get("pending_bq_df_restore") is not None
            if has_residual and not state["already_failed_once"]:
                state["already_failed_once"] = True
                recorded.append((dict(payload), "FAIL"))
                raise OSError("simulated disk full")
            recorded.append((dict(payload), "OK"))
            return original_write(payload)

        logger.write_checkpoint = _fails_once_on_residual

        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.TRADITIONAL_LEARNING, "learning"),),
        )
        runner.run(plan)
        logger.close()

        # CSAK a periodikus (status=="RUNNING") irasokat nezzuk -- a
        # terminalis (DONE) checkpoint a _checkpoint_traceability_fields()
        # sajat "utolso ismert ertek" fallbackje miatt a maradvanyt AKKOR IS
        # tartalmazza, ha a periodikus retry maga hibas (retry-trigger
        # nelkul) -- az assertnek ezert a bug-ot NEM szabad elrejtenie a
        # terminalis iras mogott.
        ok_with_residual = [
            payload for payload, outcome in recorded
            if outcome == "OK"
            and payload.get("status") == "RUNNING"
            and payload.get("pending_bq_df_restore")
        ]
        assert ok_with_residual, (
            "a maradvany sosem kerult sikeresen lemezre a periodikus agon a "
            "bukott iras utan -- a bookkeeping rollback nelkul csendben elveszett"
        )
        assert ok_with_residual[0]["pending_bq_df_restore"]["original_mV"] == 1950
        assert any(outcome == "FAIL" for _, outcome in recorded), (
            "a teszt harness nem valtotta ki a szimulalt irasi hibat"
        )

    def test_periodic_checkpoint_type_error_still_faults_session(self, tmp_path):
        """Karakterizacio: egy LOGIKAI hiba (nem OSError) a periodikus
        iras kozben TOVABBRA IS FAULT-oljon -- a fix nem szelesedhet
        `except Exception`-re, mert az egy programhibat is csendben
        elnyelne egy tobb-oras session kozepen."""
        cc = _StubChargeCtrl(steps_to_done=4)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._checkpoint_interval_s = 0.0
        events = []
        runner.on_event = events.append

        def _raises_type_error(payload):
            raise TypeError("not JSON serializable")

        logger.write_checkpoint = _raises_type_error

        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        result = runner.run(plan)
        logger.close()

        assert result.status == "FAULT"
        assert "UNHANDLED_EXCEPTION" in (result.reason or "")
        assert not any(
            e.get("event_code") == "PERIODIC_CHECKPOINT_WRITE_FAILED"
            for e in events
        )

    def test_persistently_failing_periodic_checkpoint_emits_event_only_on_transition(
        self, tmp_path
    ):
        """[regresszió, Kör 17 MAX-effort review, MAX verifier F-3 lelete]
        egy TARTÓSAN sikertelen lemezírás (pl. tartós sharing violation)
        NEM adhat egy új PERIODIC_CHECKPOINT_WRITE_FAILED eseményt MINDEN
        tickben -- ez a GUI event-log 500-soros gyűrűspufferét percek
        alatt kitörölné, elfedve a korábbi diagnosztikát. Az írási
        KÍSÉRLET magának minden tickben meg kell történnie (a maradvány-
        láthatóság elsőbbséget kap), csak az esemény-kiadás ritkított a
        hiba-állapot ÁTMENETÉRE. A teszt egy MÁSODIK hiba-epizódot is
        lefed egy közbeeső sikeres írás után -- annak is új eseményt kell
        adnia (a flag helyesen resetelődik siker után)."""
        cc = _StubChargeCtrl(steps_to_done=8)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._checkpoint_interval_s = 0.0
        events = []
        runner.on_event = events.append

        original_write = logger.write_checkpoint
        state = {"tick": 0}

        def _fails_ticks_1_to_3_and_5(payload):
            state["tick"] += 1
            if state["tick"] in (1, 2, 3, 5):
                raise OSError(f"simulated failure on tick {state['tick']}")
            return original_write(payload)

        logger.write_checkpoint = _fails_ticks_1_to_3_and_5

        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )
        result = runner.run(plan)
        logger.close()

        assert result.status == "DONE"
        failure_events = [
            e for e in events if e.get("event_code") == "PERIODIC_CHECKPOINT_WRITE_FAILED"
        ]
        # 2 epizod: tick 1-3 (1 esemeny a False->True atmeneten) + tick 5
        # (ujra 1 esemeny, mert a tick 4 sikere resetelte a flaget).
        assert len(failure_events) == 2, (
            f"pontosan 2 atmenet-esemenyt vartunk (2 hiba-epizod), {len(failure_events)} jott"
        )


class TestResumeInitialAhTotals:
    """WP-AK-02 review-lelet (Important/P1): cross-process resume esetén a
    megszakadt session Ah-összegei NEM veszhetnek el — run() a
    TestRunnerConfig.initial_charge_ah/initial_discharge_ah értékről induljon,
    ne mindig 0.0-ról."""

    def test_default_config_starts_from_zero(self, tmp_path):
        """Nem-resume eset (alapértelmezett config): változatlan viselkedés."""
        runner, logger = _make_runner(tmp_path)  # alap cc/dc: steps_to_done=3
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.total_charge_ah == pytest.approx(0.03, abs=0.001)

    def test_result_includes_total_charge_and_discharge_wh(self, tmp_path):
        """[regresszió, Teljes kódbázis review 17. kör (2026-09-05),
        Metrológia/Adatintegritás P2 lelet] a session-szintű futó Wh-összeg
        (self._total_charge_wh/_total_discharge_wh, a 3. kör óta létezik és
        a _flush_totals() pontosan úgy frissíti, mint az Ah-t) korábban
        SOHA nem jutott el a TestResult-ig — egy teljes session energia-
        mérlege (Wh) rekonstruálhatatlan volt a report.json-ból."""
        runner, logger = _make_runner(tmp_path)  # alap cc/dc: steps_to_done=3
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.total_charge_wh == pytest.approx(0.39, abs=0.001)
        assert result.total_discharge_wh == pytest.approx(0.39, abs=0.001)

    def test_resume_config_starts_from_checkpoint_totals(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=3)
        dc = _StubDischargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc, dc=dc)
        runner._config.initial_charge_ah = 2.5
        runner._config.initial_discharge_ah = 1.1
        result = runner.run(TestPlan.characterization())
        logger.close()
        # 3 tick × 0.01 Ah töltés/kisütés a stub-okból, RÁADÁSUL a resume
        # kiindulási 2.5/1.1 Ah — az összegnek erre kell épülnie, nem 0-ra.
        assert result.total_charge_ah == pytest.approx(2.5 + 0.03, abs=0.001)
        assert result.total_discharge_ah == pytest.approx(1.1 + 0.03, abs=0.001)

    def test_resume_config_totals_reflected_in_checkpoint(self, tmp_path):
        rc = _StubRelaxCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.initial_charge_ah = 4.0
        runner._config.initial_discharge_ah = 0.0
        runner._checkpoint_interval_s = 0.0
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()
        import json
        cp = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
        assert cp["charge_ah"] == pytest.approx(4.0, abs=0.001)

    def test_resume_config_starts_from_checkpoint_wh_totals(self, tmp_path):
        """[regresszió, CF-17/MET-R1-08 (DFAM-001, batch 4.3)] a Wh-testvérpár
        resume-seedelése — a testvér test_resume_config_starts_from_
        checkpoint_totals() Ah-mintája. Enélkül a total_charge_wh/
        total_discharge_wh a cross-process resume UTÁNI run()-szegmensre
        korlátozódna, miközben a total_charge_ah/total_discharge_ah a
        TELJES (megszakadás előtti + utáni) sessiont fedné le — ugyanabban
        a riportban két különböző időintervallum, jelölés nélkül."""
        cc = _StubChargeCtrl(steps_to_done=3)
        dc = _StubDischargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc, dc=dc)
        runner._config.initial_charge_wh = 32.5
        runner._config.initial_discharge_wh = 14.3
        result = runner.run(TestPlan.characterization())
        logger.close()
        # 3 tick × 0.13 Wh töltés/kisütés a stub-okból, RÁADÁSUL a resume
        # kiindulási 32.5/14.3 Wh — az összegnek erre kell épülnie, nem 0-ra.
        assert result.total_charge_wh == pytest.approx(32.5 + 0.39, abs=0.001)
        assert result.total_discharge_wh == pytest.approx(14.3 + 0.39, abs=0.001)

    def test_resume_config_wh_totals_reflected_in_checkpoint(self, tmp_path):
        """[regresszió, CF-17/MET-R1-08 (batch 4.3)] a checkpoint.json-nak
        (nem csak a végső TestResult-nak) is tartalmaznia kell a Wh-
        testvérpárt — enélkül egy MÁSODIK cross-process resume-nál a Wh
        ismét elveszne, mert nincs honnan visszaolvasni."""
        rc = _StubRelaxCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.initial_charge_wh = 55.0
        runner._config.initial_discharge_wh = 0.0
        runner._checkpoint_interval_s = 0.0
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()
        import json
        cp = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
        assert cp["charge_wh"] == pytest.approx(55.0, abs=0.001)

    def test_periodic_running_checkpoint_during_relax_includes_wh(self, tmp_path):
        """[regresszió, MAX verifier V-2 (CF-17 utóellenőrzés, batch 4.3)]
        a fenti test_resume_config_wh_totals_reflected_in_checkpoint a
        lemezen VÉGÜL megmaradó checkpoint.json-t nézi — ez a terv utolsó
        lépése után a DONE-ági write_checkpoint() írása, ami MINDIG
        `checkpoint_is_terminal=True`-t kap, tehát `evaluate_resume()`
        SOSEM engedélyezné a folytatást (BLOCKED_ALREADY_TERMINAL). A
        VALÓS cross-process resume egyetlen forrása a RELAX lépés
        KÖZBENI, periodikus "RUNNING" checkpoint-írás (`_run_step()`
        tick-hurka, `_checkpoint_interval_s` szerint) — ezt a verifier
        mutációval bizonyította teszteletlennek (a periodikus ágból a
        charge_wh/discharge_wh törlése egyetlen tesztet sem buktatott
        meg). Ez a teszt MINDEN write_checkpoint()-hívást elfog (nem csak
        a lemezen végül megmaradót), és megköveteli, hogy legalább egy
        "RUNNING" (nem terminális) hívás is tartalmazza a helyes
        charge_wh/discharge_wh értéket."""
        rc = _StubRelaxCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.initial_charge_wh = 55.0
        runner._config.initial_discharge_wh = 12.0
        runner._checkpoint_interval_s = 0.0

        captured: list[dict] = []
        original_write_checkpoint = logger.write_checkpoint

        def _capturing_write_checkpoint(state):
            captured.append(dict(state))
            return original_write_checkpoint(state)
        logger.write_checkpoint = _capturing_write_checkpoint  # type: ignore[method-assign]

        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()

        running_checkpoints = [c for c in captured if c.get("status") == "RUNNING"]
        assert running_checkpoints, "nem történt periodikus RUNNING checkpoint-írás"
        for cp in running_checkpoints:
            assert cp["charge_wh"] == pytest.approx(55.0, abs=0.001)
            assert cp["discharge_wh"] == pytest.approx(12.0, abs=0.001)

    def test_full_cross_process_resume_cycle_preserves_ah_and_wh(self, tmp_path):
        """[regresszió, MAX verifier V-3 (CF-17 utóellenőrzés, batch 4.3)]
        vége-vége integrációs teszt: 1. run() megszakad egy periodikus
        RUNNING checkpoint után -> a checkpointból ÚJ TestRunnerConfig
        épül (pontosan úgy, ahogy main_window.py::_build_runner() a
        resume-detektáló ágban teszi) -> 2. run() a checkpointból
        folytatva -> a VÉGSŐ TestResult Ah ÉS Wh összege egyaránt a
        TELJES (1. + 2. run()) sessiont fedi, nem csak a 2. szegmenst."""
        # 1. run() — 2 CHARGE tick, majd "megszakítjuk" (nem futtatjuk
        # végig a teljes tervet, csak amennyi egy periodikus checkpointhoz
        # kell).
        cc1 = _StubChargeCtrl(steps_to_done=2)
        runner1, logger1 = _make_runner(tmp_path, cc=cc1)
        runner1._checkpoint_interval_s = 0.0
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(
                TestStep(StepKind.CHARGE, "charge"),
                TestStep(StepKind.RELAX, "relax_after_charge"),
            ),
        )
        captured: list[dict] = []
        original_write_checkpoint = logger1.write_checkpoint

        def _capturing_write_checkpoint(state):
            captured.append(dict(state))
            return original_write_checkpoint(state)
        logger1.write_checkpoint = _capturing_write_checkpoint  # type: ignore[method-assign]

        runner1.run(plan)
        logger1.close()

        running_checkpoints = [c for c in captured if c.get("status") == "RUNNING"]
        assert running_checkpoints
        last_running = running_checkpoints[-1]
        # A "megszakadás" pillanatában elért Ah/Wh — pontosan azt vesszük
        # át, amit main_window.py a checkpoint.json-ból olvasna ki.
        seed_charge_ah = last_running["charge_ah"]
        seed_charge_wh = last_running["charge_wh"]
        assert seed_charge_ah > 0.0
        assert seed_charge_wh > 0.0

        # 2. run() — ÚJ TestRunner/config, a checkpointból seedelve.
        cc2 = _StubChargeCtrl(steps_to_done=100)  # már "kész", a RELAX-ra lépünk
        cc2._state = ChargeState.CHARGE_DONE
        runner2, logger2 = _make_runner(tmp_path / "resume", cc=cc2)
        runner2._config.initial_charge_ah = seed_charge_ah
        runner2._config.initial_charge_wh = seed_charge_wh
        result = runner2.run(plan, start_step_index=1)  # a RELAX lépéstől folytat
        logger2.close()

        assert result.status == "DONE"
        assert result.total_charge_ah == pytest.approx(seed_charge_ah, abs=1e-6)
        assert result.total_charge_wh == pytest.approx(seed_charge_wh, abs=1e-6)


class TestResumeSessionStartTimeAndDiagnosticCounters:
    """Governance-v2 review-kör 1 (CF-18 + V-4, DFAM-022, batch 4.3, SPR-04
    hátralévő fele): a fenti TestResumeInitialAhTotals Ah/Wh-mintáját
    bővíti az idő-origóra (CF-18) és 5 további session-szintű
    diagnosztikai számlálóra (V-4, a CF-17 MAX verifier önálló, ide vont
    lelete). A V-2 tanulságát (a checkpoint-perzisztenciát a PERIODIKUS
    RUNNING írás, nem a végleges lemezen maradó DONE-ági fájl bizonyítja)
    itt már elsőre alkalmazzuk."""

    def test_resume_config_session_start_time_extends_total_session_duration(self, tmp_path):
        """A total_session_duration_s a SEEDELT (megszakadás előtti)
        idő-origótól számoljon, ne a friss self._start_time-tól — enélkül
        ez az érték ~0-hoz közeli maradna resume után is, holott a valódi
        session órákig tartott a megszakadás előtt."""
        from datetime import timedelta
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc)
        seed_origin = datetime.now(timezone.utc) - timedelta(seconds=5000)
        runner._config.initial_session_start_time_iso = seed_origin.isoformat()
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        result = runner.run(plan)
        logger.close()
        assert result.total_session_duration_s == pytest.approx(5000, abs=5)

    def test_sample_session_elapsed_s_spans_seeded_resume_origin(self, tmp_path):
        """R2-14 (2026-09-15, DFAM-022 rokona): a "Session idő" GUI-kijelzés
        korábban a sample["elapsed_s"]-t (run()-SZEGMENS ideje) használta --
        egy resume után ez 0-ról indult újra, holott a session órák óta
        tartott. Az új sample["session_elapsed_s"] a SEEDELT (megszakadás
        előtti) origótól számol, ugyanabból a forrásból, mint a fent már
        igazolt total_session_duration_s. A diszkriminatív pár: session_
        elapsed_s a szegmensidőnél SOKKAL nagyobb, elapsed_s magától a friss
        self._start_time-tól számol."""
        from datetime import timedelta
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc)
        seed_origin = datetime.now(timezone.utc) - timedelta(seconds=5000)
        runner._config.initial_session_start_time_iso = seed_origin.isoformat()
        samples: list[dict] = []
        runner.on_sample = samples.append
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()

        assert samples, "nem keszult minta a futas alatt"
        assert samples[0]["session_elapsed_s"] == pytest.approx(5000, abs=5)
        assert samples[0]["elapsed_s"] < 5.0

    def test_build_sample_session_elapsed_s_is_none_without_run(self, tmp_path):
        """_build_sample() run() NÉLKÜL (pl. közvetlen teszthívás) is
        hívható -- ilyenkor self._session_start_time None, a mezőnek NEM
        szabad kivételt dobnia, hanem None-t kell adnia (ugyanaz a guard-
        minta, mint elapsed_s-nél)."""
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc)
        step = TestStep(StepKind.RELAX, "relax_only")

        sample = runner._build_sample(step, rc)
        logger.close()

        assert sample["session_elapsed_s"] is None

    def test_invalid_session_start_time_iso_falls_back_and_emits_event(self, tmp_path):
        """Sérült/csonka checkpoint-mező esetén NEM szabad elcrashelni egy
        egyébként érvényes resume-ot egy metaadat-mező miatt — de a
        degradáció NEM lehet hallgatólagos (CLAUDE.md §7)."""
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.initial_session_start_time_iso = "NOT-AN-ISO-TIMESTAMP"
        events: list[dict] = []
        runner.on_event = lambda ev: events.append(ev)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        result = runner.run(plan)
        logger.close()
        assert result.status == "DONE"
        assert result.total_session_duration_s < 5.0
        assert any(e.get("event_code") == "SESSION_START_TIME_PARSE_FAILED" for e in events)

    def test_resume_config_seeds_and_accumulates_diagnostic_counters(self, tmp_path):
        """[V-4] a session-szintű diagnosztikai számlálók (communication_
        faults_count, extreme_dt_s_count, max_observed_dt_s, relax_dmm_
        fault_count, temperature_out_of_range_count) resume-seedelése ÉS
        (max_observed_dt_s kivételével, ami futó MAXIMUM) a szegmens
        közben mért új delta hozzáadása egyaránt helyes legyen — ugyanaz a
        minta, mint az Ah-nál (test_resume_config_starts_from_checkpoint_
        totals)."""
        class _RelaxCtrlWithDiagnostics(_StubRelaxCtrl):
            def __init__(self, steps_to_done=3):
                super().__init__(steps_to_done)
                self.dmm_fault_count = 0
                self.extreme_dt_s_count = 0
                self.max_observed_dt_s = 0.001
                self.temperature_out_of_range_count = 0

            def advance(self, dt_s):
                state = super().advance(dt_s)
                self.dmm_fault_count += 1
                self.extreme_dt_s_count += 1
                self.temperature_out_of_range_count += 1
                return state

        rc = _RelaxCtrlWithDiagnostics(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.initial_communication_faults_count = 7
        runner._config.initial_extreme_dt_s_count = 10
        # A seedelt max_observed_dt_s (500.0) JÓVAL nagyobb, mint amit a
        # stub controller saját maga jelent (0.001) — ez a diszkrimináló
        # eset: ha a seedelés hiányozna/hibás lenne, a futó max() a kicsi
        # stub-értéket adná vissza, nem a nagy, megszakadás előtti értéket.
        runner._config.initial_max_observed_dt_s = 500.0
        runner._config.initial_relax_dmm_fault_count = 20
        runner._config.initial_temperature_out_of_range_count = 30
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        result = runner.run(plan)
        logger.close()

        assert result.communication_faults_count == 7
        assert result.extreme_dt_s_count == 10 + 3
        assert result.max_observed_dt_s == pytest.approx(500.0)
        assert result.relax_dmm_fault_count == 20 + 3
        assert result.temperature_out_of_range_count == 30 + 3

    def test_periodic_running_checkpoint_includes_session_start_time_and_diagnostic_counters(
        self, tmp_path
    ):
        """[V-2 tanulság előre alkalmazva] a checkpoint.json lemezen VÉGÜL
        megmaradó állapota mindig a terminális (resume-nál blokkolt)
        DONE-ági írás — a VALÓS resume-forrás a periodikus RUNNING írás.
        Ez a teszt MINDEN write_checkpoint()-hívást elfog, és megköveteli,
        hogy legalább egy RUNNING hívás is tartalmazza a helyes idő-origót
        és mind az 5 diagnosztikai számlálót."""
        from datetime import timedelta
        rc = _StubRelaxCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, rc=rc)
        seed_origin = datetime.now(timezone.utc) - timedelta(seconds=1234)
        runner._config.initial_session_start_time_iso = seed_origin.isoformat()
        runner._config.initial_communication_faults_count = 1
        runner._config.initial_extreme_dt_s_count = 2
        runner._config.initial_max_observed_dt_s = 3.5
        runner._config.initial_relax_dmm_fault_count = 4
        runner._config.initial_temperature_out_of_range_count = 5
        runner._checkpoint_interval_s = 0.0

        captured: list[dict] = []
        original_write_checkpoint = logger.write_checkpoint

        def _capturing_write_checkpoint(state):
            captured.append(dict(state))
            return original_write_checkpoint(state)
        logger.write_checkpoint = _capturing_write_checkpoint  # type: ignore[method-assign]

        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()

        running_checkpoints = [c for c in captured if c.get("status") == "RUNNING"]
        assert running_checkpoints, "nem történt periodikus RUNNING checkpoint-írás"
        for cp in running_checkpoints:
            assert cp["session_start_time_iso"] == seed_origin.isoformat()
            assert cp["communication_faults_count"] == 1
            assert cp["extreme_dt_s_count"] == 2
            assert cp["max_observed_dt_s"] == pytest.approx(3.5)
            assert cp["relax_dmm_fault_count"] == 4
            assert cp["temperature_out_of_range_count"] == 5

    def test_full_cross_process_resume_cycle_preserves_diagnostic_counters(self, tmp_path):
        """[V-4, a CF-17 V-3 e2e-mintáját követve] 1. run() megszakad egy
        periodikus RUNNING checkpoint után -> a checkpointból ÚJ
        TestRunnerConfig épül (pontosan úgy, ahogy main_window.py::
        _build_runner() a resume-detektáló ágban teszi) -> 2. run() a
        checkpointból folytatva -> a VÉGSŐ TestResult a diagnosztikai
        számlálókban a TELJES (1. + 2. run()) sessiont fedi, nem csak a
        2. szegmenst."""
        class _ChargeCtrlWithDiagnostics(_StubChargeCtrl):
            def __init__(self, steps_to_done=2):
                super().__init__(steps_to_done)
                self.extreme_dt_s_count = 0
                self.temperature_out_of_range_count = 0

            def advance(self, dt_s):
                state = super().advance(dt_s)
                self.extreme_dt_s_count += 1
                self.temperature_out_of_range_count += 1
                return state

        cc1 = _ChargeCtrlWithDiagnostics(steps_to_done=2)
        runner1, logger1 = _make_runner(tmp_path, cc=cc1)
        runner1._checkpoint_interval_s = 0.0
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(
                TestStep(StepKind.CHARGE, "charge"),
                TestStep(StepKind.RELAX, "relax_after_charge"),
            ),
        )
        captured: list[dict] = []
        original_write_checkpoint = logger1.write_checkpoint

        def _capturing_write_checkpoint(state):
            captured.append(dict(state))
            return original_write_checkpoint(state)
        logger1.write_checkpoint = _capturing_write_checkpoint  # type: ignore[method-assign]

        runner1.run(plan)
        logger1.close()

        running_checkpoints = [c for c in captured if c.get("status") == "RUNNING"]
        assert running_checkpoints
        last_running = running_checkpoints[-1]
        seed_extreme = last_running["extreme_dt_s_count"]
        seed_temp = last_running["temperature_out_of_range_count"]
        assert seed_extreme > 0
        assert seed_temp > 0

        cc2 = _StubChargeCtrl(steps_to_done=100)  # már "kész", a RELAX-ra lépünk
        cc2._state = ChargeState.CHARGE_DONE
        runner2, logger2 = _make_runner(tmp_path / "resume", cc=cc2)
        runner2._config.initial_extreme_dt_s_count = seed_extreme
        runner2._config.initial_temperature_out_of_range_count = seed_temp
        result = runner2.run(plan, start_step_index=1)  # a RELAX lépéstől folytat
        logger2.close()

        assert result.status == "DONE"
        assert result.extreme_dt_s_count == seed_extreme
        assert result.temperature_out_of_range_count == seed_temp

    def test_same_process_continue_from_checkpoint_preserves_accumulated_totals(
        self, tmp_path
    ):
        """[regresszió, MAX verifier P2-1 lelete (CF-18+V-4 utóellenőrzés,
        batch 4.3 follow-up)] worker.py::request_continue_from_checkpoint()
        UGYANAZT a TestRunner-példányt hívja meg ÚJRA egy második run()-
        hívással (self._runner.run(...), l. worker.py::_do_run()) — a
        session-szintű seedelés (Ah/Wh/idő-origó/5 diagnosztikai számláló)
        ezért csak a runner-példány ELSŐ run()-hívásánál futhat le, különben
        a 2. hívás nullázná/felülírná a MÁR felhalmozott összegeket és az
        eredeti idő-origót a config akkor már elavult initial_* értékeire."""
        class _ChargeCtrlWithDiagnostics(_StubChargeCtrl):
            def __init__(self, steps_to_done=2):
                super().__init__(steps_to_done)
                self.extreme_dt_s_count = 0
                self.temperature_out_of_range_count = 0

            def advance(self, dt_s):
                state = super().advance(dt_s)
                self.extreme_dt_s_count += 1
                self.temperature_out_of_range_count += 1
                return state

        cc = _ChargeCtrlWithDiagnostics(steps_to_done=2)
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc, rc=rc)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(
                TestStep(StepKind.CHARGE, "charge"),
                TestStep(StepKind.MANUAL_CHECKPOINT, "manual_checkpoint"),
                TestStep(StepKind.RELAX, "relax_after_checkpoint"),
            ),
        )
        result1 = runner.run(plan)
        assert result1.status == "CHECKPOINT_STOPPED"
        assert result1.resume_possible is True
        seed_extreme = result1.extreme_dt_s_count
        seed_temp = result1.temperature_out_of_range_count
        seed_charge_ah = result1.total_charge_ah
        seed_session_start = runner._session_start_time
        assert seed_extreme > 0
        assert seed_charge_ah > 0.0

        # UGYANAZON runner-példány MÁSODIK run()-hívása — pontosan úgy, ahogy
        # worker.py::request_continue_from_checkpoint() -> _do_run() teszi.
        result2 = runner.run(plan, start_step_index=result1.next_step_index)
        logger.close()

        assert result2.status == "DONE"
        assert result2.extreme_dt_s_count == seed_extreme
        assert result2.temperature_out_of_range_count == seed_temp
        assert result2.total_charge_ah == pytest.approx(seed_charge_ah, abs=1e-9)
        assert runner._session_start_time == seed_session_start

    def test_tz_naive_session_start_time_iso_falls_back_and_emits_event(self, tmp_path):
        """[regresszió, MAX verifier P2-2 lelete] egy szintaktikailag
        érvényes, de időzóna nélküli ("tz-naiv") ISO-string a
        datetime.fromisoformat()-ot NEM buktatja el (sikeresen parse-ol) —
        de a tz-naiv/tz-aware datetime KIVONÁSA (total_session_duration_s
        számításához, a terminális TestResult-konstrukció pillanatában)
        TypeError-t dobna, jóval a run() eleji try/except-en túl, egy
        egyébként érvényesnek tűnő resume-ot elcrashelve a session VÉGÉN."""
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.initial_session_start_time_iso = "2026-01-01T00:00:00"  # tz-naiv
        events: list[dict] = []
        runner.on_event = lambda ev: events.append(ev)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        result = runner.run(plan)
        logger.close()
        assert result.status == "DONE"
        assert result.total_session_duration_s < 5.0
        assert any(e.get("event_code") == "SESSION_START_TIME_PARSE_FAILED" for e in events)

    def test_non_string_session_start_time_iso_falls_back_and_emits_event(self, tmp_path):
        """[regresszió, MAX verifier P2-2 lelete] egy sérült checkpoint.json
        NEM-string értéket is tartalmazhat ennél a mezőnél (pl. szám) — a
        datetime.fromisoformat() ilyenkor TypeError-t dob, amit az EREDETI
        kód (csak ValueError-t elkapó except) nem fogott el, így a run()-ból
        kiszökött volna."""
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.initial_session_start_time_iso = 12345  # type: ignore[assignment]
        events: list[dict] = []
        runner.on_event = lambda ev: events.append(ev)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        result = runner.run(plan)
        logger.close()
        assert result.status == "DONE"
        assert result.total_session_duration_s < 5.0
        assert any(e.get("event_code") == "SESSION_START_TIME_PARSE_FAILED" for e in events)

    def test_session_start_time_parse_failure_written_to_events_csv(self, tmp_path):
        """[regresszió, MAX verifier P3-A lelete (f9027e3 utóellenőrzés)] a
        SESSION_START_TIME_PARSE_FAILED esemény korábban CSAK on_event-re
        ment, a session logjába (events.csv) nem — ugyanaz a hiányzó
        audit-nyom, mint a V-7 lelet (CONTROLLER_ABORT_FAILED) esetén, l.
        _abort_active_controller() mintája. Egy GUI nélküli/lezárt session
        utólagos ellenőrzésénél enélkül nyomtalan maradt volna, hogy a
        riportban lévő total_session_duration_s degradált (fresh-start)
        idő-origóból számolódott."""
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._config.initial_session_start_time_iso = "2026-01-01T00:00:00"  # tz-naiv
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        )
        runner.run(plan)
        logger.close()

        import csv
        rows = list(csv.DictReader((tmp_path / "events.csv").open()))
        parse_failed_rows = [
            r for r in rows if r["event_code"] == "SESSION_START_TIME_PARSE_FAILED"
        ]
        assert len(parse_failed_rows) == 1
        assert "2026-01-01T00:00:00" in parse_failed_rows[0]["event_message"]


class TestWarningCodeCountsSessionAggregation:
    """Governance-v2 review-kör 1 (CF-20/DFAM-021 computed_but_never_
    persisted, batch 4.6): a safety.py::WarningCode 13 lehetséges kódja
    közül a _build_sample() minden tickben csak az AKTUÁLIS (átmeneti)
    figyelmeztetést exponálja a samples.csv `warning_flags` oszlopában —
    session-szintű összesítés (melyik kód hányszor fordult elő) korábban
    SEHOL nem létezett."""

    def test_default_config_has_empty_warning_code_counts(self, tmp_path):
        """Nincs figyelmeztetés a session alatt -> üres dict, nem None/
        hiányzó mező."""
        runner, logger = _make_runner(tmp_path)
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.warning_code_counts == {}

    def test_repeated_warning_across_ticks_accumulates_count(self, tmp_path):
        """[regresszió] a stub controller MINDEN tickben ugyanazt a
        last_warning_code-ot jelenti — a session-szintű számlálónak a
        TICKEK SZÁMÁVAL kell egyeznie, nem csak azt kell mutatnia, hogy
        a figyelmeztetés 'valamikor' előfordult (bool), és nem szabad,
        hogy az utolsó tick UTÁN nullázódjon/felülíródjon."""
        cc = _StubChargeCtrl(steps_to_done=3)
        cc.last_warning_code = "TEMPERATURE_OUT_OF_RANGE"
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc, rc=rc)
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.warning_code_counts.get("TEMPERATURE_OUT_OF_RANGE") == 3

    def test_different_warnings_across_steps_counted_separately(self, tmp_path):
        """[regresszió] a CHARGE lépés egy kódot, a DISCHARGE lépés egy
        MÁSIK kódot jelent -- a session-szintű dict-nek MINDKETTŐT külön
        kell számolnia, nem csak az utolsót megőriznie."""
        cc = _StubChargeCtrl(steps_to_done=2)
        cc.last_warning_code = "TEMPERATURE_OUT_OF_RANGE"
        dc = _StubDischargeCtrl(steps_to_done=2)
        dc.last_warning_code = "HEADROOM_APPROACHING"
        runner, logger = _make_runner(tmp_path, cc=cc, dc=dc)
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.warning_code_counts.get("TEMPERATURE_OUT_OF_RANGE") == 2
        assert result.warning_code_counts.get("HEADROOM_APPROACHING") == 2

    def test_initial_warning_code_counts_seed_is_carried_over_and_incremented(self, tmp_path):
        """[regresszió, review-kör 4 (2026-09-09), S4-05/M4-05 (P2/P3,
        DFAM-022)] a warning_code_counts korábban az EGYETLEN session-
        szintű TestResult-összeg volt, ami nem élte túl a cross-process
        resume-ot, holott (c79b8f5 óta) közvetlen bemenete a capacity_
        result_quality DEGRADED-gate-nek -- ugyanaz az átminősülés, ami a
        temp_dmm_fault_ticks resume-korforgását (fe8289c) is indokolta.
        A seedelt kód a resume UTÁNI szegmensben tovább nő, nem
        felülíródik/nullázódik."""
        cc = _StubChargeCtrl(steps_to_done=3)
        cc.last_warning_code = "TEMPERATURE_OUT_OF_RANGE"
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._config.initial_warning_code_counts = {"TEMPERATURE_OUT_OF_RANGE": 5}
        result = runner.run(TestPlan.characterization())
        logger.close()
        assert result.warning_code_counts.get("TEMPERATURE_OUT_OF_RANGE") == 5 + 3


# ------------------------------------------------------------------ #
# Teljes kódbázis review 14. kör (2026-09-04), Metrológia/Adatintegritás    #
# P1 lelet: a TestRunner tick-hurka duplán integrálhatta egy fast-loop     #
# (pl. ChargeController fast_loop_enabled ága) által belül, valós          #
# time.sleep()-alapú résztick-ekkel MÁR elfogyasztott/beintegrált          #
# időszakaszt, mert _last_tick_t-t az advance() hívás ELŐTTI időponthoz    #
# rögzítette — a KÖVETKEZŐ tick actual_dt_s-e emiatt ugyanazt az           #
# intervallumot ismét tartalmazta.                                        #
# ------------------------------------------------------------------ #

class _DtRecordingChargeCtrl:
    """Nem szimulál valós időt (sleepet) — kizárólag a hozzá beérkező
    dt_s-eket rögzíti. A tesztben a `Prog.src.test_runner.time` modult
    egy determinisztikus (nem valós faliórán alapuló) perf_counter()-
    szekvenciára mockoljuk, hogy a fast-loop wall-time-fogyasztását
    reprodukáljuk anélkül, hogy a teszt valódi time.sleep()-től (és az
    azzal járó falióra-jittertől, l. korábbi flaky futás) függne."""

    def __init__(self, steps_to_done: int = 2):
        self._state = ChargeState.CHARGE_CV_DMM_CONTROL
        self._tick = 0
        self._steps = steps_to_done
        self.dt_s_calls: list[float] = []
        self.accumulated_charge_Ah: float = 0.0
        self.fault_reason: str = ""
        self.last_integration_source: str = "PSU_READBACK"
        self.i_psu_set_A: float = 0.0
        self.taper_timer_s: float = 0.0
        self.extreme_dt_s_count: int = 0
        self.max_observed_dt_s: float = 0.0
        # Review-kör 5, Metrológia M5-01 -- l. charge_controller.py
        # last_fast_loop_extra_s property docstringje. A tesztben ezt az
        # ELSŐ advance()-hívás UTÁN a fast-loop által ténylegesen
        # elintegrált (szimulált) extra időre állítjuk.
        self.last_fast_loop_extra_s: float = 0.0

    @property
    def state(self) -> ChargeState:
        return self._state

    def advance(self, dt_s: float) -> ChargeState:
        self._tick += 1
        self.dt_s_calls.append(dt_s)
        self.accumulated_charge_Ah += 0.01
        if self._tick >= self._steps:
            self._state = ChargeState.CHARGE_DONE
        return self._state


class TestFastLoopTickTimingNotDoubleCounted:
    def test_next_tick_dt_s_excludes_reported_fast_loop_extra_time(self, tmp_path):
        """Review-kör 5, Metrológia M5-01: a 14. körben bevezetett fix
        (l. test_runner.py `_last_tick_t` kommentje) óta a hívó a
        controller SAJÁT `last_fast_loop_extra_s` jelentése alapján zárja
        ki a fast-loop által MÁR elintegrált időt a KÖVETKEZŐ tick
        dt_s-éből — NEM egy, az advance() hívás UTÁN vett, második
        `time.perf_counter()` hívással (az M5-01 fix előtt ez utóbbi a
        controller SAJÁT, nem-fast-loop I/O-idejét is tévesen "már
        elintegráltnak" tekintette volna, l. ugyanott)."""
        from unittest.mock import MagicMock, patch

        cc = _DtRecordingChargeCtrl(steps_to_done=2)
        # Az 1. tick advance()-hívása alatt a (szimulált) fast-loop 1.8s
        # valós időt már elintegrált.
        cc.last_fast_loop_extra_s = 1.8
        runner, logger = _make_runner(tmp_path, cc=cc)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )

        # Determinisztikus perf_counter()-szekvencia (0-tól indexelve) — az
        # M5-01 fix óta TICKENKÉNT EGY perf_counter()-hívás történik
        # (_tick_start_t, a hívás ELŐTT; a hívás UTÁNI _last_tick_t mostantól
        # _tick_start_t + controller.last_fast_loop_extra_s, NEM egy újabb
        # perf_counter()-hívás):
        #   [0]=0.0    -> init _last_tick_t (a hurok előtt)
        #   [1]=0.0    -> 1. tick _tick_start_t (dt_s=0.0, "első tick")
        #     -> advance() után: _last_tick_t = 0.0 + cc.last_fast_loop_extra_s(1.8) = 1.8
        #   [2]=1.81   -> 2. tick _tick_start_t -> dt_s = 1.81-1.8 = 0.01
        perf_values = [0.0, 0.0, 1.81, 1.82, 1.83]
        perf_idx = [0]

        def mock_perf() -> float:
            val = perf_values[min(perf_idx[0], len(perf_values) - 1)]
            perf_idx[0] += 1
            return val

        monotonic_idx = [0]

        def mock_monotonic() -> float:
            monotonic_idx[0] += 1
            return float(monotonic_idx[0])

        with patch("Prog.src.test_runner.time") as mock_time:
            mock_time.perf_counter.side_effect = mock_perf
            mock_time.monotonic.side_effect = mock_monotonic
            mock_time.sleep = MagicMock()
            result = runner.run(plan)
        logger.close()

        assert result.status == "DONE"
        assert cc.dt_s_calls == [pytest.approx(0.0), pytest.approx(0.01, abs=1e-9)]

    def test_next_tick_dt_s_includes_own_io_time_when_no_fast_loop(self, tmp_path):
        """Review-kör 5, Metrológia M5-01 (P1, a fő regresszió): ha a
        controller EGYÁLTALÁN NEM jelent fast-loop extra időt (a `getattr`
        alapértéke 0.0 -- ez a NEM-fast-loop állapotok/összes többi
        controller esete), a KÖVETKEZŐ tick dt_s-ének a TELJES, valós
        wall-clock időt kell tartalmaznia az előző advance()-hívás
        VÉGÉTŐL -- BELEÉRTVE az adott advance()-hívás saját (DMM/PSU/Load
        SCPI I/O) végrehajtási idejét is. A fix ELŐTTI kód ezt az I/O-időt
        egy, az advance() UTÁN vett friss `time.perf_counter()`-rel
        tévesen "már elintegráltnak" tekintette, és véglegesen kihagyta
        minden dt_s-ből -- ez volt a szisztematikus alul-integrálás."""
        from unittest.mock import MagicMock, patch

        cc = _DtRecordingChargeCtrl(steps_to_done=2)
        # cc.last_fast_loop_extra_s marad a konstruktor alapértéke (0.0) --
        # ez a NEM-fast-loop eset.
        runner, logger = _make_runner(tmp_path, cc=cc)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        )

        # Az 1. tick advance()-hívása 0.3s VALÓS I/O-időt vesz igénybe (pl.
        # DMM/PSU SCPI olvasás). A fix ELŐTTI kód tickenként KÉT
        # time.perf_counter()-hívást tett (egyet _tick_start_t-hez, egyet
        # _last_tick_t frissítéséhez KÖZVETLENÜL az advance() UTÁN) -- ez a
        # MÁSODIK hívás "elnyelte volna" a 0.3-at [2]-ként, és a 2. tick
        # dt_s-e csak [3]-[2]=0.01 lett volna (az I/O-idő véglegesen
        # elveszett). A fix UTÁNI kód tickenként EGY hívást tesz
        # (_tick_start_t), a _last_tick_t-et pedig SZÁMOLJA (_tick_start_t +
        # last_fast_loop_extra_s, itt 0.0) -- így a 0.3 valódi I/O-idő a
        # KÖVETKEZŐ tick dt_s-ébe kerül:
        #   [0]=0.0   -> init _last_tick_t
        #   [1]=0.0   -> 1. tick _tick_start_t (dt_s=0.0)
        #     -> advance() UTÁN (nincs uj perf_counter hivas): _last_tick_t = 0.0+0.0 = 0.0
        #   [2]=0.3   -> 2. tick _tick_start_t -> dt_s = 0.3-0.0 = 0.3
        perf_values = [0.0, 0.0, 0.3, 0.31, 0.32]
        perf_idx = [0]

        def mock_perf() -> float:
            val = perf_values[min(perf_idx[0], len(perf_values) - 1)]
            perf_idx[0] += 1
            return val

        monotonic_idx = [0]

        def mock_monotonic() -> float:
            monotonic_idx[0] += 1
            return float(monotonic_idx[0])

        with patch("Prog.src.test_runner.time") as mock_time:
            mock_time.perf_counter.side_effect = mock_perf
            mock_time.monotonic.side_effect = mock_monotonic
            mock_time.sleep = MagicMock()
            result = runner.run(plan)
        logger.close()

        assert result.status == "DONE"
        assert cc.dt_s_calls == [pytest.approx(0.0), pytest.approx(0.3, abs=1e-9)]


# ------------------------------------------------------------------ #
# Governance-v2 review-kör 1 (MAX verifier V-6, CF-21/MET-R1-19        #
# utóellenőrzés, batch 4.6): strukturális őr a DFAM-012 rokon          #
# mintája ellen -- egy dict-alapú log-forrás mezői csendben kieshetnek #
# egy szigorú oszloplistából (l. logger.py::log_sample()-nél ugyanaz   #
# a `{col: sample.get(col, "") for col in CSV_COLUMNS}` szűrő, mint    #
# ami az OCV_SOC_COLUMNS-nál 16 mezőt csendben elvesztett, CF-21).     #
# Ma NINCS élő eltérés (a verifier ezt önállóan, statikusan igazolta), #
# ez a teszt a JÖVŐBELI recidívát akadályozza meg.                    #
# ------------------------------------------------------------------ #

class TestBuildSampleKeysCoveredByCsvColumns:
    def test_build_sample_keys_all_present_in_csv_columns(self, tmp_path):
        from Prog.src.logger import CSV_COLUMNS

        all_keys: set[str] = set()

        # CHARGE
        runner, logger = _make_runner(tmp_path / "charge")
        cc = runner._charge_ctrl_factory()
        all_keys |= set(runner._build_sample(TestStep(StepKind.CHARGE, "charge"), cc).keys())
        logger.close()

        # DISCHARGE
        runner, logger = _make_runner(tmp_path / "discharge")
        dc = runner._discharge_ctrl_factory()
        all_keys |= set(
            runner._build_sample(TestStep(StepKind.DISCHARGE, "discharge"), dc).keys()
        )
        logger.close()

        # RELAX
        runner, logger = _make_runner(tmp_path / "relax")
        step = TestStep(StepKind.RELAX, "relax")
        rc = runner._relax_ctrl_factory(step)
        all_keys |= set(runner._build_sample(step, rc).keys())
        logger.close()

        # GOLDEN_GG
        ggc = _StubGoldenGgCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path / "golden_gg", ggc=ggc)
        all_keys |= set(
            runner._build_sample(TestStep(StepKind.GOLDEN_GG, "golden_gg"), ggc).keys()
        )
        logger.close()

        # TRADITIONAL_LEARNING
        tlc = _StubLearningCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path / "traditional_learning", tlc=tlc)
        all_keys |= set(
            runner._build_sample(
                TestStep(StepKind.TRADITIONAL_LEARNING, "traditional_learning"), tlc
            ).keys()
        )
        logger.close()

        # OCV_SOC / CHEMID -- nincs factory-támogatás a _make_runner()-ben,
        # de a _build_sample(step, controller) közvetlenül hívható a saját
        # stub controllerükkel.
        runner, logger = _make_runner(tmp_path / "ocv_soc")
        occ = _StubOcvSocCtrl()
        all_keys |= set(runner._build_sample(TestStep(StepKind.OCV_SOC, "ocv_soc"), occ).keys())
        logger.close()

        runner, logger = _make_runner(tmp_path / "chemid")
        chc = _StubChemIdCtrl()
        all_keys |= set(runner._build_sample(TestStep(StepKind.CHEMID, "chemid"), chc).keys())
        logger.close()

        missing = all_keys - set(CSV_COLUMNS)
        assert not missing, (
            f"_build_sample() kulcsai hiányoznak a CSV_COLUMNS-ból, csendben "
            f"elveszve a samples.csv-ből: {sorted(missing)}"
        )


class TestBuildSampleStepKindCoverage:
    """[regresszió, review-kör 4, S4-07 (P3, DFAM-004 silent_catchall_
    dispatch)] a _build_sample() StepKind-dispatch végén álló általános
    else ág (0.0 A, "nincs aktív energiaátadás") a RELAX StepKind-hoz
    FIZIKAILAG HELYES (relax alatt tényleg nincs töltés/kisütés), de egy
    jövőbeli StepKind bevezetésekor (ahogy a 2. körben az IEC-nél történt)
    ugyanerre az ágra esne csendben -- aktív energiaátadás alatt hamis
    nulla áramot naplózva (CLAUDE.md §6). Ez a meta-teszt statikusan
    ellenőrzi, hogy minden ténylegesen _build_sample()-ig eljutó StepKind
    (a MANUAL_CHECKPOINT-ot _run_step() ELŐBB, külön ágon kezeli, l.
    test_runner.py:949) explicit `StepKind.<NÉV>` hivatkozást kap a
    dispatch forrásában -- a RELAX az EGYETLEN dokumentált kivétel."""

    def test_every_dispatchable_step_kind_has_explicit_build_sample_branch(self):
        import inspect
        source = inspect.getsource(TestRunner._build_sample)
        explicit = {kind for kind in StepKind if f"StepKind.{kind.name}" in source}
        accepted_generic_fallthrough = {StepKind.RELAX}
        never_reaches_build_sample = {StepKind.MANUAL_CHECKPOINT}
        missing = (
            set(StepKind) - explicit - accepted_generic_fallthrough - never_reaches_build_sample
        )
        assert not missing, (
            f"Új StepKind {sorted(k.name for k in missing)} nincs explicit "
            f"kezelve a _build_sample()-ben -- ha ez szándékos (mint a "
            f"RELAX-nál), vegyél fel a fenti accepted_generic_fallthrough "
            f"halmazba; ha nem, adj hozzá egy explicit StepKind.{{...}} ágat "
            f"a dispatch-hez."
        )


class TestLoadCurrentReadbackSiblingSurface:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör,
    DFAM-018 STRUCTURAL_PREVENTION_REVIEW): minden Prog/src/*.py modul,
    ami `self._load.measure_current()`-et hív, VAGY exponálja az
    `i_load_measured_A`/`load_current_readback_valid` tick-scoped
    validity-flag párt, VAGY szerepel az explicit, indoklással ellátott
    allowlisten. Megelőzi, hogy egy jövőbeli, új hívóhely csendben
    kimaradjon ebből a mintából (DFAM-018 RECURRING család) -- teszt-őr,
    NEM refaktor (a 6 hívóhely különböző fault-kódot/utófeldolgozást
    használ, egy közös helper indokolatlanul növelné a viselkedési
    teret, l. CLAUDE.md §8)."""

    # pulse_measurement_service.py: sub-tick (tickenként N minta) mintavétel
    # -- nincs "a tick mintája", amit egyetlen tick-scoped property-ként
    # exponálni lehetne; a reprezentatív minta kiválasztása (baseline/
    # utolsó/átlag) önálló metrológiai döntés, SOURCE_REQUIRED, halasztva
    # (l. CLAIM_LEDGER.yaml LOAD-CURRENT-SOURCE-001 open_followups).
    _ALLOWLISTED_FILES = frozenset({"pulse_measurement_service.py"})

    def test_every_load_current_caller_exposes_validity_pair_or_is_allowlisted(self):
        from pathlib import Path

        src_dir = Path(__file__).resolve().parent.parent / "src"
        offenders = []
        for path in sorted(src_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "self._load.measure_current()" not in text:
                continue
            if path.name in self._ALLOWLISTED_FILES:
                continue
            if "i_load_measured_A" not in text or "load_current_readback_valid" not in text:
                offenders.append(path.name)
        assert not offenders, (
            f"Load-aram olvaso hivohely(ek) validity-flag par nelkul: {offenders} -- "
            f"l. discharge_controller.py::i_load_measured_A mintaja, vagy vedd fel "
            f"indoklassal a TestLoadCurrentReadbackSiblingSurface._ALLOWLISTED_FILES-ba."
        )

    def test_allowlist_entries_still_actually_call_measure_current(self):
        """Anti-bitrot: ha egy allowlistelt fájl mar nem is hivja a
        measure_current()-et (pl. refaktor miatt), az allowlist-bejegyzes
        felesleges es torlendo -- ez a teszt jelzi, ha ez bekovetkezik."""
        from pathlib import Path

        src_dir = Path(__file__).resolve().parent.parent / "src"
        for filename in self._ALLOWLISTED_FILES:
            text = (src_dir / filename).read_text(encoding="utf-8")
            assert "self._load.measure_current()" in text, (
                f"{filename} mar nem hivja a measure_current()-et -- "
                f"az allowlist-bejegyzes torlendo."
            )


class TestEnergizePathSafeOffSiblingSurface:
    """Kör 17 MAX-effort review (R17-22, DFAM-016, test-coverage tengely),
    a TestLoadCurrentReadbackSiblingSurface (R2-06) mintáját 1:1 követve.

    Az eredeti finding ("7 of 8 Load-energize branches untested") a
    HEAD-en NEM reprodukálódott -- a fix-planner újra-levezetése szerint
    mind a 7, Load-t energizáló modul rendelkezik saját emergency_stop()
    + input_off() teszttel (l. az egyes controller test-fájlok
    test_load_off_failure_surfaces_in_fault_reason* tesztjeit), és a
    PSU-oldali seprés (a finding saját, korábban elvégzetlen fele) is
    hiánytalannak bizonyult (egyetlen PSU-energizáló hely,
    charge_controller.py, szintén lefedve). A valódi, megmaradó
    kockázat nem a HIÁNYZÓ teszt, hanem a JÖVŐBELI DRIFT: egy új
    energizáló hívóhely, ami a párja (emergency_stop()+off-hívás)
    nélkül landol. Ezt ez az őr-teszt fedi, nem 7 új, redundáns
    tesztként."""

    # pulse_measurement_service.py: nem controller, nincs saját
    # emergency_stop()-ja -- a hívó controllerek emergency_stop()-ja
    # fedi (az energizálást ő maga is csak a hívó controller kontextu-
    # sában, annak safe-off-jától védve teszi), saját input_off()
    # hívásai külön tesztelve (test_pulse_measurement_service.py::
    # test_load_input_off_after_fetch / ..._not_silently_swallowed).
    _LOAD_ALLOWLISTED_FILES = frozenset({"pulse_measurement_service.py"})

    def _src_dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "src"

    def test_every_load_energize_caller_has_emergency_stop_pairing_or_is_allowlisted(self):
        offenders = []
        for path in sorted(self._src_dir().glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "self._load.input_on(" not in text:
                continue
            if path.name in self._LOAD_ALLOWLISTED_FILES:
                continue
            if "def emergency_stop" not in text or "self._load.input_off()" not in text:
                offenders.append(path.name)
        assert not offenders, (
            f"Load-energizalo hivohely(ek) emergency_stop()+input_off() paros "
            f"nelkul: {offenders} -- l. discharge_controller.py mintaja, vagy "
            f"vedd fel indoklassal a "
            f"TestEnergizePathSafeOffSiblingSurface._LOAD_ALLOWLISTED_FILES-ba."
        )

    def test_every_psu_energize_caller_has_emergency_stop_pairing(self):
        """Nincs allowlist -- a fix-planner altal ujra-elvegzett PSU-seprés
        szerint pontosan EGY PSU-energizalo hely letezik a teljes kodbazis-
        ban (charge_controller.py), es az is le van fedve."""
        offenders = []
        for path in sorted(self._src_dir().glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "self._psu.output_on()" not in text:
                continue
            if "def emergency_stop" not in text or "self._psu.all_outputs_off()" not in text:
                offenders.append(path.name)
        assert not offenders, (
            f"PSU-energizalo hivohely(ek) emergency_stop()+all_outputs_off() "
            f"paros nelkul: {offenders}."
        )

    def test_allowlist_entries_still_actually_call_load_input_on(self):
        """Anti-bitrot: ha egy allowlistelt fajl mar nem is hivja az
        input_on()-t (pl. refaktor miatt), az allowlist-bejegyzes
        felesleges es torlendo."""
        for filename in self._LOAD_ALLOWLISTED_FILES:
            text = (self._src_dir() / filename).read_text(encoding="utf-8")
            assert "self._load.input_on(" in text, (
                f"{filename} mar nem hivja az input_on()-t -- "
                f"az allowlist-bejegyzes torlendo."
            )


class TestLoadEnergizeDmmGateSiblingSurface:
    """Claude-Codex loop R1 B3-4 (DFAM-001 structural prevention, az F-02
    negyedik elofordulasa ugyanazon a negy kisutesindito metoduson):
    minden Prog/src fuggveny, amely `self._load.input_on(`-t hiv, a SAJAT
    torzseben (beagyazott def/lambda/osztaly csomopontjai NEM szamitanak),
    az elso input_on sora ELOTT tartalmazzon egy DMM-kaput:

        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

    A puszta `_dmm_valid`-ot olvaso `If` nem eleg (ures torzs, forditott
    feltetel, nem hivott belso fuggveny -- l. a negativ helper-teszteket).
    Altalanos vezerlesifolyamat-elemzest szandekosan NEM vegez (sorrend a
    sorszam alapjan)."""

    # Fuggvenyszintu kivetelek (fajl, fuggveny) -- indoklassal:
    # - discharge_controller.py::_run_cc_setup: a DMM-kapu (`if not
    #   self._dmm_valid: emergency_stop("DMM_FEEDBACK_LOST")`) a hivo
    #   advance() DISCHARGE_CC_SETUP/CC_RUN agaban, a _run_cc_setup()
    #   hivasa ELOTT fut, ugyanabban a tickben.
    # - pulse_measurement_service.py::begin_pulse / ::_step_hardware_pulse:
    #   nem controller (nincs sajat _dmm_valid/emergency_stop). A Load ON
    #   elott a begin_pulse() sajat DMM-olvasast vegez (SUP01-PULSE-010);
    #   olvasasi hibanal kivetelt dob a Load bekapcsolasa elott, a
    #   HARDWARE_PULSE ag (_step_hardware_pulse) erre a begin_pulse()-beli
    #   elozetes olvasasra epul.
    _ALLOWLISTED_FUNCTIONS = frozenset({
        ("discharge_controller.py", "_run_cc_setup"),
        ("pulse_measurement_service.py", "begin_pulse"),
        ("pulse_measurement_service.py", "_step_hardware_pulse"),
    })

    _CORRECT_GATE_SRC = (
        "class C:\n"
        "    def start(self):\n"
        "        if not self._dmm_valid:\n"
        "            self.emergency_stop(\"DMM_FEEDBACK_LOST\")\n"
        "            return\n"
        "        self._load.input_on()\n"
    )
    _NESTED_UNDER_OUTER_IF_SRC = (
        "class C:\n"
        "    def start(self):\n"
        "        if not self._started:\n"
        "            if not self._dmm_valid:\n"
        "                self.emergency_stop(\"DMM_FEEDBACK_LOST\")\n"
        "                return\n"
        "            self._load.input_on()\n"
    )
    _NEGATIVE_SOURCES = (
        ("empty_body", (
            "class C:\n"
            "    def start(self):\n"
            "        if not self._dmm_valid:\n"
            "            pass\n"
            "        self._load.input_on()\n"
        )),
        ("inverted_condition", (
            "class C:\n"
            "    def start(self):\n"
            "        if self._dmm_valid:\n"
            "            self.emergency_stop(\"DMM_FEEDBACK_LOST\")\n"
            "            return\n"
            "        self._load.input_on()\n"
        )),
        ("gate_only_in_nested_uncalled_def", (
            "class C:\n"
            "    def start(self):\n"
            "        def _gate():\n"
            "            if not self._dmm_valid:\n"
            "                self.emergency_stop(\"DMM_FEEDBACK_LOST\")\n"
            "                return\n"
            "        self._load.input_on()\n"
        )),
        ("missing_return", (
            "class C:\n"
            "    def start(self):\n"
            "        if not self._dmm_valid:\n"
            "            self.emergency_stop(\"DMM_FEEDBACK_LOST\")\n"
            "        self._load.input_on()\n"
        )),
        ("wrong_fault_reason", (
            "class C:\n"
            "    def start(self):\n"
            "        if not self._dmm_valid:\n"
            "            self.emergency_stop(\"SOMETHING_ELSE\")\n"
            "            return\n"
            "        self._load.input_on()\n"
        )),
        ("gate_after_input_on", (
            "class C:\n"
            "    def start(self):\n"
            "        self._load.input_on()\n"
            "        if not self._dmm_valid:\n"
            "            self.emergency_stop(\"DMM_FEEDBACK_LOST\")\n"
            "            return\n"
        )),
    )

    def _src_dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "src"

    @staticmethod
    def _own_nodes(fn):
        """A fuggveny sajat torzsenek csomopontjai -- beagyazott def/lambda/
        osztaly (es azok teljes reszfaja) kihagyva."""
        import ast
        skip = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        stack = [n for n in fn.body if not isinstance(n, skip)]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, skip))

    @staticmethod
    def _is_self_attr(node, attr: str) -> bool:
        import ast
        return (
            isinstance(node, ast.Attribute) and node.attr == attr
            and isinstance(node.value, ast.Name) and node.value.id == "self"
        )

    @classmethod
    def _is_load_input_on_call(cls, node) -> bool:
        import ast
        return (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "input_on" and cls._is_self_attr(node.func.value, "_load")
        )

    @classmethod
    def _is_dmm_gate(cls, node) -> bool:
        import ast
        if not isinstance(node, ast.If):
            return False
        test = node.test
        if not (
            isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
            and cls._is_self_attr(test.operand, "_dmm_valid")
        ):
            return False
        stop_idx = None
        for idx, stmt in enumerate(node.body):
            if (
                isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and cls._is_self_attr(stmt.value.func, "emergency_stop")
                and len(stmt.value.args) == 1
                and isinstance(stmt.value.args[0], ast.Constant)
                and stmt.value.args[0].value == "DMM_FEEDBACK_LOST"
            ):
                stop_idx = idx
                break
        if stop_idx is None:
            return False
        return any(isinstance(s, ast.Return) for s in node.body[stop_idx + 1:])

    @classmethod
    def _offenders_in_source(cls, source: str, filename: str) -> list:
        import ast
        offenders = []
        for fn in ast.walk(ast.parse(source)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            own = list(cls._own_nodes(fn))
            on_lines = [n.lineno for n in own if cls._is_load_input_on_call(n)]
            if not on_lines:
                continue
            first_on = min(on_lines)
            if not any(cls._is_dmm_gate(n) and n.lineno < first_on for n in own):
                offenders.append((filename, fn.name))
        return offenders

    def test_every_load_energize_function_has_dmm_gate_or_is_allowlisted(self):
        offenders = []
        for path in sorted(self._src_dir().glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "self._load.input_on(" not in text:
                continue
            for entry in self._offenders_in_source(text, path.name):
                if entry not in self._ALLOWLISTED_FUNCTIONS:
                    offenders.append(f"{entry[0]}::{entry[1]}")
        assert not offenders, (
            f"Load-energizalo fuggveny(ek) DMM-kapu nelkul az elso input_on() "
            f"elott: {offenders} -- `if not self._dmm_valid: self.emergency_stop("
            f"\"DMM_FEEDBACK_LOST\"); return` kell, vagy vedd fel indoklassal a "
            f"TestLoadEnergizeDmmGateSiblingSurface._ALLOWLISTED_FUNCTIONS-ba."
        )

    def test_allowlist_entries_still_exist_and_call_load_input_on(self):
        """Anti-bitrot: ha egy allowlistelt fuggveny mar nem letezik vagy
        nem hiv input_on()-t, a bejegyzes torlendo."""
        import ast
        for filename, func_name in sorted(self._ALLOWLISTED_FUNCTIONS):
            tree = ast.parse((self._src_dir() / filename).read_text(encoding="utf-8"))
            fns = [
                n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
            ]
            assert fns, f"{filename}::{func_name} mar nem letezik -- az allowlist-bejegyzes torlendo."
            assert any(
                self._is_load_input_on_call(n) for fn in fns for n in self._own_nodes(fn)
            ), f"{filename}::{func_name} mar nem hiv input_on()-t -- az allowlist-bejegyzes torlendo."

    def test_gate_detector_accepts_correct_gate(self):
        assert self._offenders_in_source(self._CORRECT_GATE_SRC, "synthetic.py") == []
        assert self._offenders_in_source(self._NESTED_UNDER_OUTER_IF_SRC, "synthetic.py") == []

    @pytest.mark.parametrize(
        "source", [src for _name, src in _NEGATIVE_SOURCES],
        ids=[name for name, _src in _NEGATIVE_SOURCES],
    )
    def test_gate_detector_rejects_non_gate(self, source):
        assert self._offenders_in_source(source, "synthetic.py") == [("synthetic.py", "start")]


class TestTerminalCheckpointRetainsSummary:
    """Kor 18, Batch C7 R17-20 (DFAM-001+DFAM-021): _emergency_stop()/
    _graceful_stop() korabban egy IMPOVERISHED checkpointot irt (csak
    status/reason/traceability), FELULIRVA a periodikus/DONE/manualis
    agak gazdagabb (Ah/Wh/ido-origo/6 szamlalo) rekordjat -- egy megsza-
    kadt FIZIKAI futas post-mortem allapota pontosan akkor semmisult
    meg, amikor a legtobbet erte volna."""

    def _read_checkpoint(self, tmp_path) -> dict:
        import json
        return json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))

    def test_emergency_stop_checkpoint_retains_cumulative_ah(self, tmp_path):
        """A `run()` fresh-session-agai a sessionstart pillanataban
        `self._config.initial_*`-bol seedelik a totalokat -- ezert
        KOZVETLENUL a _emergency_stop()-ot hivjuk (a valos hivo, run()
        _emergency_stop()-ja, ugyanezt a mar futas kozben felhalmozott
        allapotot latja), nem run()-t."""
        runner, logger = _make_runner(tmp_path)
        runner._total_charge_ah = 2.5
        runner._total_discharge_ah = 1.1
        runner._total_charge_wh = 30.0
        runner._total_discharge_wh = 13.0

        runner._emergency_stop("SIMULATED_FAULT")
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["status"] == "FAULT"
        assert cp["charge_ah"] == pytest.approx(2.5)
        assert cp["discharge_ah"] == pytest.approx(1.1)
        assert cp["charge_wh"] == pytest.approx(30.0)
        assert cp["discharge_wh"] == pytest.approx(13.0)

    def test_graceful_stop_checkpoint_retains_cumulative_ah(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._total_charge_ah = 4.2
        runner._total_discharge_ah = 0.7

        runner._graceful_stop("SIMULATED_STOP")
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["status"] == "STOPPED"
        assert cp["charge_ah"] == pytest.approx(4.2)
        assert cp["discharge_ah"] == pytest.approx(0.7)

    def test_terminal_checkpoint_retains_diagnostic_counters(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._session_start_time = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        runner._device_error_total_count = 3
        runner._total_extreme_dt_s_count = 2
        runner._max_observed_dt_s = 9.5
        runner._total_relax_dmm_fault_count = 4
        runner._total_temperature_out_of_range_count = 1
        runner._total_temp_dmm_fault_ticks = 6
        runner._warning_code_counts = {"TEMPERATURE_OUT_OF_RANGE": 2}

        runner._emergency_stop("SIMULATED_FAULT")
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["session_start_time_iso"] == "2026-09-16T12:00:00+00:00"
        assert cp["communication_faults_count"] == 3
        assert cp["extreme_dt_s_count"] == 2
        assert cp["max_observed_dt_s"] == pytest.approx(9.5)
        assert cp["relax_dmm_fault_count"] == 4
        assert cp["temperature_out_of_range_count"] == 1
        assert cp["temp_dmm_fault_ticks"] == 6
        assert cp["warning_code_counts"] == {"TEMPERATURE_OUT_OF_RANGE": 2}

    def test_fault_checkpoint_is_not_marked_terminal(self, tmp_path):
        """Tudatos dontes rogzitese (l. _checkpoint_summary_fields()
        docstringje): a `checkpoint_is_terminal` NEM kerul a FAULT/
        STOPPED checkpointra -- viselkedesileg hatastalan lenne
        (safe_boundary=False mar korabban blokkol az evaluate_resume()
        elso kapujaban), es a jelentese ("terv vegen keszult") hamis
        lenne egy kozbeni fault-ra. Ez a teszt zarolja a dontest, hogy
        egy jovobeli kor csendben ne adja hozza."""
        runner, logger = _make_runner(tmp_path)

        runner._emergency_stop("SIMULATED_FAULT")
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert "checkpoint_is_terminal" not in cp

    def test_resume_decision_for_stopped_checkpoint_is_unchanged(self, tmp_path):
        """A R17-20 eredeti finding altal felvetett kockazat (checkpoint_
        is_terminal hozzaadasa STOPPED-hez megvaltoztatna a resume-
        dontest BLOCKED_ALREADY_TERMINAL-ra) EMPIRIKUSAN cafolva: a
        `safe_boundary` ELSO kapuja mar korabban blokkol, a `checkpoint_
        is_terminal` kapu sosem erhetne el ezt a checkpointot -- ez a
        teszt a TENYLEGES evaluate_resume() hivassal igazolja, nem csak
        a kod olvasasaval."""
        from Prog.src.session_recovery import ResumeDecision, evaluate_resume

        runner, logger = _make_runner(tmp_path)
        _configure_traceability(runner)
        runner._graceful_stop("SIMULATED_STOP")
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        evaluation = evaluate_resume(
            cp, cp["config_hash"], cp["profile_hash"],
            cp["instrument_identities"], cp["sample_id"],
            total_step_count=len(TestPlan.characterization().steps),
        )
        assert evaluation.decision == ResumeDecision.BLOCKED_UNSAFE_BOUNDARY

    def test_early_fault_before_first_tick_writes_summary(self, tmp_path):
        """Egy legkorabbi fault (meg mielott barmelyik controller-tick
        lefutott volna) sem dobhat kivetelt az uj mezok olvasasakor -- a
        self._total_* mezok mind __init__-ben inicializaltak."""
        runner, logger = _make_runner(tmp_path)
        runner._emergency_stop("EARLY_FAULT_BEFORE_FIRST_TICK")
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert cp["status"] == "FAULT"
        assert cp["charge_ah"] == pytest.approx(0.0)
        assert cp["discharge_ah"] == pytest.approx(0.0)

    def test_stopped_checkpoint_is_not_marked_terminal(self, tmp_path):
        """Fuggetlen MAX verifier F2 lelete (Kor 18, Batch C7 SB5): a
        test_fault_checkpoint_is_not_marked_terminal FAULT-ra mar
        zarolta ezt a dontest, de a STOPPED agnak nem volt analog
        teszte -- egy mutacio (checkpoint_is_terminal=True hozzaadasa
        CSAK a STOPPED agra) az osszes tobbi teszt zoldsege mellett is
        eszrevetlen maradt volna."""
        runner, logger = _make_runner(tmp_path)

        runner._graceful_stop("SIMULATED_STOP")
        logger.close()

        cp = self._read_checkpoint(tmp_path)
        assert "checkpoint_is_terminal" not in cp

    def test_terminal_summary_fields_match_done_write_key_set(self, tmp_path):
        """Fuggetlen MAX verifier F3 lelete (Kor 18, Batch C7 SB5,
        strukturalis prevencio, DFAM-001+DFAM-021 2. elofordulas):
        mind az öt checkpoint-ág közös összegző helpert használ. Ez
        a paritásteszt a tényleges DONE-checkpointot olvassa vissza, és
        a traceability-kulcsok levonása után ellenőrzi a teljes összegző
        mezőkészletet. A mezők jelenlétét és értékét mind az öt ágon
        külön, nem üres halasztási időkkel is ellenőrizzük."""
        runner, logger = _make_runner(tmp_path)

        runner.run(TestPlan.characterization())
        logger.close()

        done_cp = self._read_checkpoint(tmp_path)
        assert done_cp["status"] == "DONE"
        traceability_keys = set(runner._checkpoint_traceability_fields(
            safe_boundary=True, next_safe_step_index=0,
        ).keys())
        # R16-B5bF5: a központi terminális-okfeloldó óta a "reason" a "status"-hoz
        # hasonló terminális azonosító mező, minden terminális ágon (DONE, FAULT/
        # STOPPED, manuális) kiíródik -- nem összegző mező.
        done_only_keys = set(done_cp.keys()) - traceability_keys - {
            "status", "checkpoint_is_terminal", "reason",
        }

        summary_keys = set(runner._checkpoint_summary_fields().keys())
        assert summary_keys == done_only_keys

    @pytest.mark.parametrize("status", [
        "DONE", "CHECKPOINT_STOPPED", "RUNNING", "FAULT", "STOPPED",
    ])
    def test_all_checkpoint_paths_retain_deferred_error_queue_seconds(self, monkeypatch, status):
        """A mező minden írásban jelen van, nem üres értékkel is."""
        from unittest.mock import Mock

        import Prog.src.logger as logger_module

        logger = Mock(spec=logger_module.Logger)
        monkeypatch.setattr(logger_module, "Logger", Mock(return_value=logger))
        runner, _ = _make_runner("unused")
        runner._deferred_totals = {"DMM_V": 2.5, "DMM_T": 1.25}
        runner._total_relax_missed_deadline_count = 7
        runner._checkpoint_interval_s = 0.0
        step = TestStep(StepKind.RELAX, "relax")
        plan = TestPlan(test_type=TestType.CHARACTERIZATION, steps=(step,))

        if status in ("DONE", "RUNNING"):
            assert runner.run(plan).status == "DONE"
        elif status == "CHECKPOINT_STOPPED":
            runner._start_time = datetime.now(timezone.utc)
            runner._active_plan = plan
            runner._run_manual_checkpoint(0, TestStep(StepKind.MANUAL_CHECKPOINT, "manual"))
        elif status == "FAULT":
            runner._emergency_stop("SIMULATED_FAULT")
        else:
            runner._graceful_stop("SIMULATED_STOP")

        checkpoints = [
            call.args[0] for call in logger.write_checkpoint.call_args_list
            if call.args[0]["status"] == status
        ]
        assert checkpoints
        for checkpoint in checkpoints:
            assert checkpoint["error_queue_deferred_s"] == {"DMM_V": 2.5, "DMM_T": 1.25}
            assert checkpoint["relax_missed_deadline_count"] == 7
            for key, value in runner._checkpoint_summary_fields().items():
                assert checkpoint[key] == value


class TestKor21BatchF6ForcedErrorPollOnAllTerminalPaths:
    """Kör 21 Batch F6 (METRO-R21-1, DFAM-016+DFAM-001+DFAM-021): a
    `_poll_device_errors()` korábban KIZÁRÓLAG a cadence-gated tick-ágon
    (`_should_poll_device_errors()`) és a controller-fault kényszerített
    ágon futott -- a másik 4 terminális/félig-terminális út (DONE-tail,
    `_emergency_stop()`, `_graceful_stop()`, `_run_manual_checkpoint()`)
    NEM kényszerített pollt, csak `Logger.close()`-t (vagy semmit, a
    checkpoint esetén) -- akár 60s-nyi SCPI-hiba, BELEÉRTVE a safe-off
    SAJÁT parancsai (Load OFF/PSU ALL OFF) által generált hibákat is,
    sosem került a device_errors.csv-be/communication_faults_count-ba.
    Mind a 4 útra pótolva egy kényszerített, best-effort poll a safe-off
    UTÁN. Nyitott, MÁR MEGLÉVŐ open_findings[70] tétel (a Load-nak NEM
    szabad SCPI-t küldeni, ha a controller MÁR bizonyítottan halottnak
    jelezte) UGYANEBBEN a diffben lezárva: `InstrumentManager.
    poll_device_errors(skip_load=...)`."""

    def test_emergency_stop_polls_and_records_device_error(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._instruments._psu.pending_errors.append("-113,Undefined header")

        result = runner._emergency_stop("TEST_REASON")
        logger.close()

        assert result.communication_faults_count == 1

    def test_graceful_stop_polls_and_records_device_error(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._instruments._psu.pending_errors.append("-113,Undefined header")

        result = runner._graceful_stop("USER_STOP")
        logger.close()

        assert result.communication_faults_count == 1

    def test_manual_checkpoint_polls_and_records_device_error(self, tmp_path):
        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        runner._active_plan = TestPlan.bq_learning_physical()
        runner._instruments._psu.pending_errors.append("-113,Undefined header")
        step = TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")

        result = runner._run_manual_checkpoint(8, step)
        logger.close()

        assert result.communication_faults_count == 1

    def test_done_tail_polls_and_records_device_error(self, tmp_path, monkeypatch):
        """A hibát a DONE-tail SAJÁT safe-offjának melléhatásaként
        szimuláljuk (1:1 a valós tünettel -- "a safe-off SAJÁT parancsai
        által generált hibák"), nem a plan elején beszúrva -- egy elöl
        beszúrt hiba a MÁR MEGLÉVŐ, a runner első tickjén azonnal induló
        cadence-pollon (`_last_device_error_poll_t = -1e9`) menne el,
        mielőtt a DONE-tail egyáltalán futna, és nem a kényszerített
        pollt tesztelné."""
        runner, logger = _make_runner(tmp_path)
        original_safe_off = runner._safe_all_off_with_uncertainty_check

        def _safe_off_with_error(*args, **kwargs):
            runner._instruments._psu.pending_errors.append("-113,Undefined header")
            return original_safe_off(*args, **kwargs)

        monkeypatch.setattr(runner, "_safe_all_off_with_uncertainty_check", _safe_off_with_error)

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "DONE"
        assert result.communication_faults_count == 1

    def test_dead_load_is_not_polled_after_controller_marks_it_invalid(
        self, tmp_path, monkeypatch
    ):
        """open_findings[70] lezárása: ha a legutóbb futott controller
        `load_current_readback_valid=False`-t jelzett (a Load MÁR
        bizonyítottan halott az adott tickre), a kényszerített poll NEM
        küld SEMMILYEN SCPI-t a Loadnak -- a Load teljes `calls`-listája
        VÁLTOZATLAN marad. [MAX verifier B1 lelete, 2026-09-19: a korábbi
        verzió csak a `check_error()`-t nézte, tehát a
        `_poll_output_state_mismatches()`/`_poll_load_questionable_
        status()` gate-jét NEM fedte -- egy saját mutáció (mindkettő
        `skip_load`-ját hardkódolt False-ra/eltávolítva) 407/407 zölden
        átment volna. Most a TELJES hívás-lista üres, ez mindhárom
        Load-hívást lefedi."""
        runner, logger = _make_runner(tmp_path)

        class _DeadLoadController:
            load_current_readback_valid = False

        runner._active_controller = _DeadLoadController()
        load_calls_before = len(runner._instruments._load.calls)

        runner._poll_device_errors()
        logger.close()

        assert runner._instruments._load.calls[load_calls_before:] == []

    def test_live_load_is_still_polled_when_controller_marks_it_valid(self, tmp_path):
        """Counter-teszt: a `skip_load` kapu NEM szabad tulzottan tágra
        nyílnia -- ha a controller a Loadot ÉRVÉNYESNEK (True) minősíti,
        a poll TOVÁBBRA IS lefut rá. [MAX verifier B2 lelete, 2026-09-19:
        a korábbi verzió csak a None (nincs jelzés) ágat tesztelte, a
        docstring-ben ígért True ágat nem -- egy saját mutáció (`is False`
        -> `is not None`, ami a True esetet is skip-elte volna) 407/407
        zölden átment volna]."""
        runner, logger = _make_runner(tmp_path)

        class _LiveLoadController:
            load_current_readback_valid = True

        runner._active_controller = _LiveLoadController()

        runner._poll_device_errors()
        logger.close()

        assert "check_error()" in runner._instruments._load.calls

    def test_load_is_still_polled_when_controller_gives_no_signal_at_all(self, tmp_path):
        """Counter-teszt: `None` (nincs aktív controller/nincs jelzés)
        esetén is a poll TOVÁBBRA IS lefut a Loadra -- hiányzó jelzés NEM
        egyenlő "bizonyítottan halott"-tal."""
        runner, logger = _make_runner(tmp_path)
        runner._active_controller = None

        runner._poll_device_errors()
        logger.close()

        assert "check_error()" in runner._instruments._load.calls


class TestKor21BatchF4UnknownStepStatusFaultsInsteadOfContinuing:
    """Kör 21 Batch F4 (ARC21-2, DFAM-004, a Kör 20 E17/worker.py fix
    upstream testvere): a `run()` for-ciklusa korábban FELTÉTEL NÉLKÜL
    feltételezte, hogy ha egy `_run_step()` eredménye nem FAULT/STOPPED/
    CHECKPOINT_STOPPED, akkor DONE -- egy jövőbeli, nem-terminális vagy
    ismeretlen status (pl. "DEGRADED") csendben a KÖVETKEZŐ lépésre
    léptetett volna át, TOVÁBBI energiaátadással egy be nem fejezett
    lépés után. Ellentétben a worker.py-beli testvérrel (ahol a hiányzó
    guard csak a GUI-címkét hamisította), itt a hibás irány "folytasd az
    energiaátadást", nem "hamisan jelezd a sikert"."""

    def test_unknown_step_status_faults_and_does_not_proceed_to_next_step(
        self, tmp_path, monkeypatch
    ):
        runner, logger = _make_runner(tmp_path)
        original_run_step = runner._run_step
        calls: list[int] = []

        def _fake_run_step(step_index, step):
            calls.append(step_index)
            if step_index == 0:
                return TestResult(status="DEGRADED")
            return original_run_step(step_index, step)

        monkeypatch.setattr(runner, "_run_step", _fake_run_step)

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "FAULT"
        assert calls == [0], (
            "egy ismeretlen/nem-terminalis status UTAN a terv NEM lephet "
            "at a kovetkezo lepesre -- tovabbi energiaatadas egy be nem "
            "fejezett lepes utan"
        )
        assert result.safe_off_attempted is True
        assert "safe_off()" in runner._instruments._psu.calls
        assert "safe_off()" in runner._instruments._load.calls
        assert runner.status == "FAULT"

    def test_known_terminal_statuses_still_proceed_without_raising(self, tmp_path):
        """Counter-teszt: a fix NEM szűkíthette le a MEGLÉVŐ, dokumentált
        DONE-utat -- egy teljes, csak DONE-status-ú terv továbbra is
        hiba nélkül fut végig."""
        runner, logger = _make_runner(tmp_path)

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "DONE"


# ------------------------------------------------------------------ #
# R1 B7S (B7_B7S_fixplan.md B7S + B7_B7S_fixplan_FINAL.md)            #
# ------------------------------------------------------------------ #

_F42_PATHS = ("done", "emergency", "graceful", "manual")
_F42_EXPECTED_STATUS = {
    "done": "DONE",
    "emergency": "FAULT",
    "graceful": "STOPPED",
    "manual": "CHECKPOINT_STOPPED",
}


def _f42_drive(runner, path):
    """A 4 safe-off utáni kényszerített pollos terminális út egyike."""
    if path == "done":
        # Csak RELAX: nincs lépéshatár-poll (R-1), a DONE-tail fut.
        return runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax_only"),),
        ))
    if path == "emergency":
        return runner._emergency_stop("F42_TEST_REASON")
    if path == "graceful":
        return runner._graceful_stop("USER_STOP_REQUESTED")
    runner._start_time = datetime.now(timezone.utc)
    runner._active_plan = TestPlan.bq_learning_physical()
    return runner._run_manual_checkpoint(
        8, TestStep(StepKind.MANUAL_CHECKPOINT, "manual_bq_checkpoint")
    )


def _f42_inject_after_safe_off(runner, monkeypatch, inject):
    """A sikeres (parancs-szintű True) safe_all_off() UTÁN, a kényszerített
    poll ELŐTT állítja be a hibás/bizonytalan readback-állapotot."""
    original = runner._instruments.safe_all_off

    def _wrapped():
        result = original()
        inject()
        return result

    monkeypatch.setattr(runner._instruments, "safe_all_off", _wrapped)


def _f42_read_checkpoint(tmp_path) -> dict:
    import json
    return json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))


def _f42_read_events_csv(tmp_path) -> list[dict]:
    import csv
    with open(tmp_path / "events.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class TestR1F42ForcedPollDemotesSafeOffResult:
    """[regresszió, R1 F-42 / NS-06, B7_B7S_fixplan.md B7S + FINAL B7S/1,
    D-B7S-1] A safe-off UTÁNI kényszerített poll (`_poll_device_errors()`)
    energia-állapot-bizonytalanságot jelző bejegyzése (`is_energy_state_
    uncertain()`: commanded=OFF/actual=ON, *_STATE_POLL_FAILED, F-05
    timeout-kihagyás) korábban csak naplózódott -- a safe_off_result az
    adott eszközre parancs-szintű True maradt a checkpointban, a
    TestResultban és a manuális eseményben. Most HELYBEN False-ra
    minősül vissza (előléptetés nincs), a státusz nem változik."""

    @staticmethod
    def _psu_on(runner):
        runner._instruments.psu._output_is_on = True

    @staticmethod
    def _load_on(runner):
        runner._instruments.load._input_is_on = True

    @pytest.mark.parametrize("path", _F42_PATHS)
    @pytest.mark.parametrize("device", ["PSU", "Load"])
    def test_actual_on_after_safe_off_demotes_result_everywhere(
        self, tmp_path, monkeypatch, path, device
    ):
        """1. ON-readback a safe-off után -> False a TestResultban, a
        checkpointban és (manuális út) az eseményben; SAFE_OFF_RESULT_
        UNCERTAIN on_event + events.csv kritikus sor; státusz változatlan
        (régi kód: True, nincs esemény)."""
        runner, logger = _make_runner(tmp_path)
        events: list = []
        runner.on_event = events.append
        inject = self._psu_on if device == "PSU" else self._load_on
        marker = "OUTPUT_STATE_MISMATCH" if device == "PSU" else "INPUT_STATE_MISMATCH"
        _f42_inject_after_safe_off(runner, monkeypatch, lambda: inject(runner))

        result = _f42_drive(runner, path)
        logger.close()

        assert result.status == _F42_EXPECTED_STATUS[path]
        assert runner.status == _F42_EXPECTED_STATUS[path]
        assert result.safe_off_attempted is True
        assert result.safe_off_result[device] is False
        other = "Load" if device == "PSU" else "PSU"
        assert result.safe_off_result[other] is True
        cp = _f42_read_checkpoint(tmp_path)
        assert cp["status"] == _F42_EXPECTED_STATUS[path]
        assert cp["last_safe_off_result"][device] is False
        assert cp["last_safe_off_result"][other] is True
        uncertain = [
            e for e in events if e.get("event_code") == "SAFE_OFF_RESULT_UNCERTAIN"
        ]
        assert len(uncertain) == 1
        assert device in uncertain[0]["event_message"]
        assert marker in uncertain[0]["event_message"]
        rows = [
            r for r in _f42_read_events_csv(tmp_path)
            if r["event_code"] == "SAFE_OFF_RESULT_UNCERTAIN"
        ]
        assert len(rows) == 1
        assert rows[0]["is_critical"] == "True"
        if path == "manual":
            reached = [
                e for e in events
                if e.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED"
            ]
            assert reached[0]["safe_off_result"][device] is False

    @pytest.mark.parametrize("path", _F42_PATHS)
    @pytest.mark.parametrize("device", ["PSU", "Load"])
    def test_state_read_failure_after_safe_off_demotes(
        self, tmp_path, monkeypatch, path, device
    ):
        """2. Állapotolvasási kivétel (*_STATE_POLL_FAILED) -> False."""
        runner, logger = _make_runner(tmp_path)

        def _inject():
            if device == "PSU":
                runner._instruments.psu.simulate_output_state_readback_timeout = True
            else:
                runner._instruments.load.simulate_input_state_readback_timeout = True

        _f42_inject_after_safe_off(runner, monkeypatch, _inject)

        result = _f42_drive(runner, path)
        logger.close()

        assert result.status == _F42_EXPECTED_STATUS[path]
        assert result.safe_off_result[device] is False
        assert _f42_read_checkpoint(tmp_path)["last_safe_off_result"][device] is False

    @pytest.mark.parametrize("path", _F42_PATHS)
    def test_load_check_error_timeout_skip_demotes_load(self, tmp_path, monkeypatch, path):
        """3. F-05: a Load SYST:ERR? InstrumentTimeoutError-ja miatt az INP?
        ugyanebben a pollban kimarad -> az energia-állapot nem igazolt ->
        Load False."""
        from Prog.src.exceptions import InstrumentTimeoutError

        runner, logger = _make_runner(tmp_path)
        load = runner._instruments.load

        def _timeout_check_error(clear_first=True):
            load.calls.append("check_error()")
            raise InstrumentTimeoutError("simulated Load SYST:ERR? timeout")

        _f42_inject_after_safe_off(
            runner, monkeypatch,
            lambda: monkeypatch.setattr(load, "check_error", _timeout_check_error),
        )

        result = _f42_drive(runner, path)
        logger.close()

        assert result.status == _F42_EXPECTED_STATUS[path]
        assert result.safe_off_result["Load"] is False
        assert result.safe_off_result["PSU"] is True
        assert _f42_read_checkpoint(tmp_path)["last_safe_off_result"]["Load"] is False
        # az F-05 rövidzár: az INP? a safe-off utáni pollban nem futott
        last_safe_off = len(load.calls) - 1 - load.calls[::-1].index("safe_off()")
        assert "read_input_state()" not in load.calls[last_safe_off:]

    @pytest.mark.parametrize("path", _F42_PATHS)
    def test_clean_readback_keeps_true_and_emits_no_event(self, tmp_path, path):
        """4a. Ellenpróba: tiszta readback -> True marad, nincs esemény."""
        runner, logger = _make_runner(tmp_path)
        events: list = []
        runner.on_event = events.append

        result = _f42_drive(runner, path)
        logger.close()

        assert result.safe_off_result["PSU"] is True
        assert result.safe_off_result["Load"] is True
        assert _f42_read_checkpoint(tmp_path)["last_safe_off_result"]["PSU"] is True
        assert not [
            e for e in events if e.get("event_code") == "SAFE_OFF_RESULT_UNCERTAIN"
        ]

    @pytest.mark.parametrize("path", _F42_PATHS)
    def test_non_energy_scpi_error_after_safe_off_does_not_demote(
        self, tmp_path, monkeypatch, path
    ):
        """4b. Ellenpróba: egy nem energia-jellegű (-113) bejegyzés naplózódik,
        de nem minősít vissza."""
        runner, logger = _make_runner(tmp_path)
        events: list = []
        runner.on_event = events.append
        _f42_inject_after_safe_off(
            runner, monkeypatch,
            lambda: runner._instruments.psu.pending_errors.append("-113,Undefined header"),
        )

        result = _f42_drive(runner, path)
        logger.close()

        assert result.communication_faults_count == 1
        assert result.safe_off_result["PSU"] is True
        assert result.safe_off_result["Load"] is True
        assert not [
            e for e in events if e.get("event_code") == "SAFE_OFF_RESULT_UNCERTAIN"
        ]

    @pytest.mark.parametrize("path", _F42_PATHS)
    def test_no_promotion_of_command_level_failure(self, tmp_path, path):
        """5. Előléptetés nincs: a parancs-szintű False egy tiszta (OFF)
        readback mellett is False marad."""
        runner, logger = _make_runner(tmp_path)
        runner._instruments.psu.raise_on_safe_off = True

        result = _f42_drive(runner, path)
        logger.close()

        assert runner._instruments.psu._output_is_on is False
        assert result.safe_off_result["PSU"] is False
        assert result.safe_off_result["Load"] is True
        assert _f42_read_checkpoint(tmp_path)["last_safe_off_result"]["PSU"] is False

    @pytest.mark.parametrize("path", _F42_PATHS)
    def test_safety_elements_on_demotion(self, tmp_path, monkeypatch, path):
        """6. rules/10: Load OFF, PSU ALL OFF, nincs további ON-parancs, a
        terminális esemény naplózva -- a lefokozás ezeket nem érinti."""
        runner, logger = _make_runner(tmp_path)
        psu = runner._instruments.psu
        load = runner._instruments.load
        _f42_inject_after_safe_off(runner, monkeypatch, lambda: self._psu_on(runner))

        result = _f42_drive(runner, path)
        logger.close()

        assert result.safe_off_result["PSU"] is False
        assert load.called("safe_off()")
        assert psu.called("all_outputs_off()")
        assert not psu.called("output_on()")
        assert not load.called("input_on()")
        terminal_code = {
            "done": None,
            "emergency": "EMERGENCY_STOP",
            "graceful": "GRACEFUL_STOP",
            "manual": "MANUAL_BQ_CHECKPOINT_REACHED",
        }[path]
        codes = [r["event_code"] for r in _f42_read_events_csv(tmp_path)]
        assert "SAFE_OFF_RESULT_UNCERTAIN" in codes
        if terminal_code is not None:
            assert terminal_code in codes

    @pytest.mark.parametrize("path", ["emergency", "graceful", "manual"])
    def test_keyboard_interrupt_in_demotion_helper_still_logs_and_checkpoints(
        self, tmp_path, monkeypatch, path
    ):
        """7. KeyboardInterrupt a helperben: a meglévő `except BaseException`
        miatt a terminális napló/checkpoint/close lefut."""
        runner, logger = _make_runner(tmp_path)
        _f42_inject_after_safe_off(runner, monkeypatch, lambda: self._psu_on(runner))

        def _interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            runner, "_demote_safe_off_result_on_energy_uncertainty", _interrupt
        )

        result = _f42_drive(runner, path)

        assert result.status == _F42_EXPECTED_STATUS[path]
        assert _f42_read_checkpoint(tmp_path)["status"] == _F42_EXPECTED_STATUS[path]
        if path != "manual":
            assert logger._closed is True
        logger.close()
        terminal_code = {
            "emergency": "EMERGENCY_STOP",
            "graceful": "GRACEFUL_STOP",
            "manual": "MANUAL_BQ_CHECKPOINT_REACHED",
        }[path]
        assert terminal_code in [r["event_code"] for r in _f42_read_events_csv(tmp_path)]

    def test_keyboard_interrupt_in_demotion_helper_on_done_tail_keeps_reraise(
        self, tmp_path, monkeypatch
    ):
        """7b. DONE-tail: a KeyboardInterrupt tulajdonosa a külső guard
        (DONE_TAIL_INTERRUPTED + close + újradobás) -- változatlan
        szemantika."""
        runner, logger = _make_runner(tmp_path)
        _f42_inject_after_safe_off(runner, monkeypatch, lambda: self._psu_on(runner))

        def _interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            runner, "_demote_safe_off_result_on_energy_uncertainty", _interrupt
        )

        with pytest.raises(KeyboardInterrupt):
            _f42_drive(runner, "done")

        assert logger._closed is True
        codes = [r["event_code"] for r in _f42_read_events_csv(tmp_path)]
        assert "DONE_TAIL_INTERRUPTED" in codes

    @pytest.mark.parametrize("path", _F42_PATHS)
    def test_device_error_logging_failure_does_not_lose_demotion(
        self, tmp_path, monkeypatch, path
    ):
        """FINAL B7S/1 (BLOKKOLÓ): ON-readback + `log_device_error()`
        OSError + sikeres terminális checkpoint -> False. A lefokozás nem
        függhet a `_poll_device_errors()` naplózásának sikerétől (egy, a
        poll visszatérése UTÁN hívott helper itt nem futna le)."""
        runner, logger = _make_runner(tmp_path)
        events: list = []
        runner.on_event = events.append
        _f42_inject_after_safe_off(runner, monkeypatch, lambda: self._psu_on(runner))

        def _log_device_error_fails(device, error):
            raise OSError("simulated device_errors.csv write failure")

        monkeypatch.setattr(runner._logger, "log_device_error", _log_device_error_fails)

        result = _f42_drive(runner, path)
        logger.close()

        assert result.status == _F42_EXPECTED_STATUS[path]
        assert result.safe_off_result["PSU"] is False
        cp = _f42_read_checkpoint(tmp_path)
        assert cp["status"] == _F42_EXPECTED_STATUS[path]
        assert cp["last_safe_off_result"]["PSU"] is False
        if path == "manual":
            reached = [
                e for e in events
                if e.get("event_code") == "MANUAL_BQ_CHECKPOINT_REACHED"
            ]
            assert reached[0]["safe_off_result"]["PSU"] is False


def _r1_hook_advance(monkeypatch, ctrl, hook, *, on_tick=None, on_states=None):
    """A controller advance()-ét burkolja: `hook()` az `on_tick`-edik
    advance() után, vagy amikor az advance() `on_states` egyikét adja."""
    real = ctrl.advance
    counter = {"n": 0}

    def _advance(dt_s):
        state = real(dt_s)
        counter["n"] += 1
        if (on_tick is not None and counter["n"] == on_tick) or (
            on_states is not None and state in on_states
        ):
            hook()
        return state

    monkeypatch.setattr(ctrl, "advance", _advance)
    return counter


class TestR1R1StepBoundaryEnergyStatePoll:
    """[regresszió, R1 R-1, B7_B7S_fixplan.md B7S + FINAL B7S/2] A RELAX_
    DONE- (vagy egy energiaátadó lépés utolsó) tickje csak a 60 s-os
    kadenciával pollolt; a következő energiaátadó lépés első advance()-e
    friss PSU/Load readback nélkül indult. Most minden energiaátadó lépés
    controller-létrehozása ELŐTT (a DMM-konfiguráció UTÁN) egy lépéshatár-
    poll fut a teljes kritikus policyvel; a kadencia-ütemezés változatlan."""

    @staticmethod
    def _assert_boundary_emergency_stop(result, runner, tmp_path, events):
        psu = runner._instruments.psu
        load = runner._instruments.load
        assert result.status == "FAULT"
        assert result.safe_off_attempted is True
        assert load.called("safe_off()")
        assert psu.called("all_outputs_off()")
        assert psu._output_is_on is False
        assert load._input_is_on is False
        assert not psu.called("output_on()")
        assert not load.called("input_on()")
        assert runner._active_controller is None
        assert any(e.get("event_code") == "EMERGENCY_STOP" for e in events)
        assert "EMERGENCY_STOP" in (tmp_path / "events.csv").read_text(encoding="utf-8")

    def test_relax_to_charge_load_on_faults_before_charge(self, tmp_path, monkeypatch):
        """1. RELAX -> CHARGE, alap 60 s kadencia: a RELAX utolsó advance()-e
        után Load actual=ON -> FAULT a CHARGE előtt (régi: DONE, 3 charge-
        tick)."""
        cc = _StubChargeCtrl(steps_to_done=3)
        rc = _StubRelaxCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc, rc=rc)
        assert runner._config.device_error_poll_interval_s == 60.0
        _r1_hook_advance(
            monkeypatch, rc,
            lambda: setattr(runner._instruments.load, "_input_is_on", True),
            on_states=(RelaxState.RELAX_DONE,),
        )
        events: list = []
        runner.on_event = events.append

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax"), TestStep(StepKind.CHARGE, "charge")),
        ))
        logger.close()

        assert result.reason.startswith("SCPI_CRITICAL_ERROR[Load]: INPUT_STATE_MISMATCH")
        assert "actual=ON" in result.reason
        assert result.active_step_label == "charge"
        assert cc._tick == 0
        self._assert_boundary_emergency_stop(result, runner, tmp_path, events)

    def test_charge_to_discharge_psu_on_faults_before_discharge(self, tmp_path, monkeypatch):
        """2. CHARGE -> DISCHARGE: a CHARGE utolsó advance()-e után PSU
        actual=ON -> FAULT a DISCHARGE előtt, nincs Load input_on."""
        cc = _StubChargeCtrl(steps_to_done=3)
        dc = _StubDischargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc, dc=dc)
        _r1_hook_advance(
            monkeypatch, cc,
            lambda: setattr(runner._instruments.psu, "_output_is_on", True),
            on_states=(ChargeState.CHARGE_DONE,),
        )
        events: list = []
        runner.on_event = events.append

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"), TestStep(StepKind.DISCHARGE, "discharge")),
        ))
        logger.close()

        assert result.reason.startswith("SCPI_CRITICAL_ERROR[PSU]: OUTPUT_STATE_MISMATCH")
        assert result.active_step_label == "discharge"
        assert cc._tick == 3
        assert dc._tick == 0
        self._assert_boundary_emergency_stop(result, runner, tmp_path, events)

    def test_scpi_error_at_end_of_charge_faults_at_discharge_boundary(
        self, tmp_path, monkeypatch
    ):
        """3a. -113 a CHARGE utolsó tickjében -> FAULT a DISCHARGE határán
        (aktív lépés: a teljes kritikus policy)."""
        cc = _StubChargeCtrl(steps_to_done=3)
        dc = _StubDischargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc, dc=dc)
        _r1_hook_advance(
            monkeypatch, cc,
            lambda: runner._instruments.psu.pending_errors.append("-113,Undefined header"),
            on_states=(ChargeState.CHARGE_DONE,),
        )
        events: list = []
        runner.on_event = events.append

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"), TestStep(StepKind.DISCHARGE, "discharge")),
        ))
        logger.close()

        assert result.reason == "SCPI_CRITICAL_ERROR[PSU]: -113,Undefined header"
        assert result.active_step_label == "discharge"
        assert dc._tick == 0
        self._assert_boundary_emergency_stop(result, runner, tmp_path, events)

    def test_scpi_error_at_end_of_charge_before_relax_is_only_logged(
        self, tmp_path, monkeypatch
    ):
        """3b. Ellenpróba: RELAX előtt nincs lépéshatár-poll; a -113 csak
        naplózódik (a DONE-tail kényszerített pollja), nincs FAULT."""
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        _r1_hook_advance(
            monkeypatch, cc,
            lambda: runner._instruments.psu.pending_errors.append("-113,Undefined header"),
            on_states=(ChargeState.CHARGE_DONE,),
        )

        result = runner.run(TestPlan.charge_only())
        logger.close()

        assert result.status == "DONE"
        assert result.communication_faults_count == 1

    def test_boundary_poll_preserves_first_tick_cadence_poll(self, tmp_path, monkeypatch):
        """4. Kadencia-megőrzés: a lépéshatár-poll visszaállítja a kadencia-
        időbélyeget, így az 1. tick meglévő pollja továbbra is lefut és
        elkapja az első advance() által okozott hibát (visszaállítás nélkül
        a hiba csak a DONE-tailben naplózódna, DONE)."""
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        _r1_hook_advance(
            monkeypatch, cc,
            lambda: runner._instruments.psu.pending_errors.append('-222,"Data out of range"'),
            on_tick=1,
        )

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))
        logger.close()

        assert result.status == "FAULT"
        assert result.reason == 'SCPI_CRITICAL_ERROR[PSU]: -222,"Data out of range"'
        assert cc._tick == 1

    def test_clean_boundary_poll_keeps_done_and_fault_count(self, tmp_path):
        """5. Ellenpróba: tiszta állapot -> DONE, nincs eszközhiba."""
        runner, logger = _make_runner(tmp_path)

        result = runner.run(TestPlan.characterization())
        logger.close()

        assert result.status == "DONE"
        assert result.communication_faults_count == 0

    def test_boundary_poll_does_not_call_regulation_poll(self, tmp_path, monkeypatch):
        """6. R-4: a lépéshatár-poll nem hívja a read_regulation_state()-et;
        a regulation-poll a cadence-kapu alatt marad (1. tick)."""
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        psu = runner._instruments.psu
        received: list = []
        runner.on_psu_regulation = received.append
        marks: dict = {}
        _r1_hook_advance(
            monkeypatch, cc,
            lambda: marks.setdefault("calls_after_first_advance", list(psu.calls)),
            on_tick=1,
        )

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))
        logger.close()

        before_first_advance = marks["calls_after_first_advance"]
        assert result.status == "DONE"
        assert "read_output_state()" in before_first_advance  # lépéshatár-poll
        assert "read_regulation_state()" not in before_first_advance
        assert psu.calls.count("read_regulation_state()") == 1
        assert len(received) == 1

    def test_energy_transfer_step_kinds_is_complete(self):
        """7. Meta: minden StepKind vagy energiaátadó, vagy indokolt kivétel
        (RELAX: nincs PSU/Load-vezérlés; MANUAL_CHECKPOINT: a runner saját
        safe-offja fut). A cadence-ág és a lépéshatár-poll ugyanazt a
        szűrőt használja."""
        from Prog.src.test_runner import (
            _ENERGY_TRANSFER_STEP_KINDS,
            _critical_polled_errors,
        )

        exceptions = {StepKind.RELAX, StepKind.MANUAL_CHECKPOINT}
        assert isinstance(_ENERGY_TRANSFER_STEP_KINDS, frozenset)
        assert not (_ENERGY_TRANSFER_STEP_KINDS & exceptions)
        assert _ENERGY_TRANSFER_STEP_KINDS | exceptions == set(StepKind)
        scpi = {"device": "PSU", "error": "-113,Undefined header"}
        energy = {"device": "Load", "error": "INPUT_STATE_MISMATCH: commanded=OFF actual=ON",
                  "energy_state_uncertain": True}
        for kind in StepKind:
            step = TestStep(kind, kind.value.lower())
            expected_scpi = [scpi] if kind in _ENERGY_TRANSFER_STEP_KINDS else []
            assert _critical_polled_errors(step, [scpi]) == expected_scpi
            assert _critical_polled_errors(step, [energy]) == [energy]

    def test_critical_dmm_error_during_set_nplc_faults_before_factory(
        self, tmp_path, monkeypatch
    ):
        """FINAL B7S/2 elhelyezési diszkriminátor: a `_configure_dmm_for_step()`
        set_nplc()-je alatt keletkező kritikus DMM-hiba a controller-factory
        hívása ELŐTT FAULT-ot okoz (factory-hívásszám 0, advance 0). Egy
        set_nplc() ELŐTTI poll ezt nem látná."""
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        dmm_v = runner._instruments.dmm_voltage
        real_set_nplc = dmm_v.set_nplc

        def _set_nplc_with_error(nplc):
            real_set_nplc(nplc)
            dmm_v.pending_errors.append('-222,"Data out of range"')

        monkeypatch.setattr(dmm_v, "set_nplc", _set_nplc_with_error)
        factory_calls: list = []

        def _counting_factory():
            factory_calls.append(1)
            return cc

        runner._charge_ctrl_factory = _counting_factory
        events: list = []
        runner.on_event = events.append

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))
        logger.close()

        assert any(c.startswith("set_nplc(") for c in dmm_v.calls)
        assert result.reason == 'SCPI_CRITICAL_ERROR[DMM_V]: -222,"Data out of range"'
        assert result.active_step_label == "charge"
        assert factory_calls == []
        assert cc._tick == 0
        self._assert_boundary_emergency_stop(result, runner, tmp_path, events)

    @pytest.mark.parametrize("kind", [
        StepKind.CHARGE, StepKind.DISCHARGE, StepKind.OCV_SOC, StepKind.CHEMID,
        StepKind.TRADITIONAL_LEARNING, StepKind.GOLDEN_GG, StepKind.IEC_DC_RESISTANCE,
    ])
    def test_every_energy_step_kind_polls_before_factory(self, tmp_path, kind):
        """Minden energiaátadó StepKind: induláskori Load actual=ON ->
        FAULT a factory hívása előtt (régi: factory + 1 advance)."""
        runner, logger = _make_runner(tmp_path)
        factory_calls: list = []
        stubs = {
            StepKind.CHARGE: ("_charge_ctrl_factory", _StubChargeCtrl),
            StepKind.DISCHARGE: ("_discharge_ctrl_factory", _StubDischargeCtrl),
            StepKind.OCV_SOC: ("_ocv_soc_ctrl_factory", _StubOcvSocCtrl),
            StepKind.CHEMID: ("_chemid_ctrl_factory", _StubChemIdCtrl),
            StepKind.TRADITIONAL_LEARNING: (
                "_traditional_learning_ctrl_factory", _StubLearningCtrl,
            ),
            StepKind.GOLDEN_GG: ("_golden_gg_ctrl_factory", _StubGoldenGgCtrl),
            StepKind.IEC_DC_RESISTANCE: (
                "_iec_dc_resistance_ctrl_factory", _StubIecDcResistanceCtrl,
            ),
        }
        attr, stub_cls = stubs[kind]

        def _factory():
            factory_calls.append(kind)
            return stub_cls()

        setattr(runner, attr, _factory)
        runner._instruments.load._input_is_on = True
        events: list = []
        runner.on_event = events.append

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(kind, "energy_step"),),
        ))
        logger.close()

        assert result.reason.startswith("SCPI_CRITICAL_ERROR[Load]: INPUT_STATE_MISMATCH")
        assert result.active_step_label == "energy_step"
        assert factory_calls == []
        self._assert_boundary_emergency_stop(result, runner, tmp_path, events)


# ------------------------------------------------------------------ #
# R1 B7: F-14 (periodikus checkpoint flush) + F-16 (pending-rollback) #
# ------------------------------------------------------------------ #

def _r1b7_record_writes(logger, fail_if=None):
    """Teszt-segéd: a write_checkpoint() minden hívását rögzíti (payload-
    másolat + kimenetel). `fail_if(payload, recorded) -> bool` True esetén
    OSError-t dob (a rekord kimenetele "FAIL"), különben a valódi írás fut."""
    original = logger.write_checkpoint
    recorded: list[tuple[dict, str]] = []

    def _write(payload):
        snapshot = dict(payload)
        if fail_if is not None and fail_if(snapshot, recorded):
            recorded.append((snapshot, "FAIL"))
            raise OSError("simulated checkpoint write failure")
        recorded.append((snapshot, "OK"))
        return original(payload)

    logger.write_checkpoint = _write
    return recorded


def _r1b7_running(recorded, outcome=None) -> list[dict]:
    return [
        p for p, o in recorded
        if p.get("status") == "RUNNING" and (outcome is None or o == outcome)
    ]


class _R1F14CountingRelaxCtrl(_StubRelaxCtrl):
    """RELAX-stub, amely tickenként növeli a dmm_fault_count-ot (a
    relax_dmm_fault_count session-összeg forrása, l. _flush_totals())."""

    def __init__(self, steps_to_done: int = 4):
        super().__init__(steps_to_done)
        self.dmm_fault_count = 0

    def advance(self, dt_s: float) -> RelaxState:
        self.dmm_fault_count += 1
        return super().advance(dt_s)


class TestR1F14PeriodicCheckpointFlushesStepTotals:
    """[regresszió, R1 F-14, DFAM-001+DFAM-021] a periodikus RUNNING
    checkpoint korábban a `_flush_totals()` ELŐTTI (lépés-belépéskori)
    session-összegeket írta: lépés közben az Ah/Wh és a diagnosztikai
    számlálók a lemezen 0-n / a lépés eleji értéken álltak. A RUNNING
    checkpoint a ténylegesen resume-képes írás (RELAX-nál safe_boundary=
    True) -- egy crash után a resume ezt seedeli, tehát a lépés közbeni
    számláló-haladás elveszett."""

    def test_running_checkpoint_carries_live_relax_counter(self, tmp_path):
        rc = _R1F14CountingRelaxCtrl(steps_to_done=4)
        runner, logger = _make_runner(tmp_path, rc=rc)
        runner._checkpoint_interval_s = 0.0
        recorded = _r1b7_record_writes(logger)

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.RELAX, "relax"),),
        ))
        logger.close()

        assert result.status == "DONE"
        assert rc._tick == 4
        running = _r1b7_running(recorded, "OK")
        assert [p["relax_dmm_fault_count"] for p in running] == [1, 2, 3, 4]
        assert result.relax_dmm_fault_count == 4

    def test_running_checkpoint_carries_live_charge_ah_and_wh(self, tmp_path):
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._checkpoint_interval_s = 0.0
        recorded = _r1b7_record_writes(logger)

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))
        logger.close()

        assert result.status == "DONE"
        assert cc._tick == 3
        running = _r1b7_running(recorded, "OK")
        assert len(running) == 3
        assert [p["charge_ah"] for p in running] == pytest.approx([0.01, 0.02, 0.03])
        assert [p["charge_wh"] for p in running] == pytest.approx([0.13, 0.26, 0.39])

    @pytest.mark.parametrize("interval_s", [0.0, 1000.0])
    def test_periodic_flush_does_not_double_count(self, tmp_path, interval_s):
        """A periodikus flush után a finally-flush és a minta-oldali
        (total + controller - before) számítás nem duplázhat: a végső
        összeg és a samples.csv futó összege független attól, hány
        periodikus checkpoint készült (interval 0 = minden tick, 1000 =
        csak az első tick)."""
        cc = _StubChargeCtrl(steps_to_done=3)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._checkpoint_interval_s = interval_s
        samples: list[dict] = []
        runner.on_sample = samples.append
        recorded = _r1b7_record_writes(logger)

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))
        logger.close()

        assert result.status == "DONE"
        assert len(_r1b7_running(recorded, "OK")) == (3 if interval_s == 0.0 else 1)
        assert result.total_charge_ah == pytest.approx(0.03)
        assert result.total_charge_wh == pytest.approx(0.39)
        assert [s["accumulated_charge_Ah"] for s in samples] == pytest.approx(
            [0.01, 0.02, 0.03]
        )
        assert [s["accumulated_charge_Wh"] for s in samples] == pytest.approx(
            [0.13, 0.26, 0.39]
        )
        import csv
        with (tmp_path / "samples.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert [float(r["accumulated_charge_Ah"]) for r in rows] == pytest.approx(
            [0.01, 0.02, 0.03]
        )
        done_cp = [p for p, o in recorded if p.get("status") == "DONE"]
        assert done_cp and done_cp[-1]["charge_ah"] == pytest.approx(0.03)

    def test_resume_from_mid_relax_running_checkpoint_keeps_segment1_counter(
        self, tmp_path
    ):
        """[GOV-013, teljes resume-kör] 1. szegmens: CHARGE (3 tick) +
        RELAX; a process a RELAX 2. periodikus checkpointja után "hal meg"
        (a lemezen az utolsó RUNNING checkpoint marad). 2. szegmens: a
        main_window resume-seedelésének megfelelő mezőkből indul a RELAX
        lépéstől. A végső számláló = 1. szegmens + 2. szegmens; az Ah nem
        duplázódik."""
        from Prog.src.session_recovery import evaluate_resume

        seg1_dir = tmp_path / "seg1"
        seg1_dir.mkdir()
        rc1 = _R1F14CountingRelaxCtrl(steps_to_done=4)
        runner1, logger1 = _make_runner(seg1_dir, cc=_StubChargeCtrl(steps_to_done=3), rc=rc1)
        _configure_traceability(runner1)
        runner1._checkpoint_interval_s = 0.0
        recorded = _r1b7_record_writes(logger1)
        plan = TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"), TestStep(StepKind.RELAX, "relax")),
        )
        runner1.run(plan)
        logger1.close()

        relax_running = [p for p in _r1b7_running(recorded, "OK") if p["step"] == "relax"]
        assert len(relax_running) == 4
        crash_cp = relax_running[1]  # a 2. RELAX-tick utáni checkpoint
        assert crash_cp["safe_boundary"] is True
        assert crash_cp["next_safe_step_index"] == 1
        assert crash_cp["relax_dmm_fault_count"] == 2
        assert crash_cp["charge_ah"] == pytest.approx(0.03)
        assert evaluate_resume(
            crash_cp, crash_cp["config_hash"], crash_cp["profile_hash"],
            crash_cp["instrument_identities"], crash_cp["sample_id"],
            total_step_count=len(plan.steps),
        ).allowed

        seg2_dir = tmp_path / "seg2"
        seg2_dir.mkdir()
        rc2 = _R1F14CountingRelaxCtrl(steps_to_done=4)
        runner2, logger2 = _make_runner(seg2_dir, rc=rc2)
        cfg = runner2._config
        cfg.initial_charge_ah = crash_cp.get("charge_ah", 0.0) or 0.0
        cfg.initial_discharge_ah = crash_cp.get("discharge_ah", 0.0) or 0.0
        cfg.initial_charge_wh = crash_cp.get("charge_wh", 0.0) or 0.0
        cfg.initial_discharge_wh = crash_cp.get("discharge_wh", 0.0) or 0.0
        cfg.initial_relax_dmm_fault_count = crash_cp.get("relax_dmm_fault_count", 0) or 0
        result = runner2.run(plan, start_step_index=crash_cp["next_safe_step_index"])
        logger2.close()

        assert result.status == "DONE"
        assert rc2._tick == 4
        assert result.relax_dmm_fault_count == 2 + 4
        assert result.total_charge_ah == pytest.approx(0.03)

    def test_running_checkpoint_key_set_is_summary_plus_traceability(self, tmp_path):
        """Kulcskészlet-paritás: a periodikus RUNNING checkpoint mezői =
        {status, step, elapsed_s} + _checkpoint_summary_fields() +
        _checkpoint_traceability_fields() (nem diszkrimináló a régi kódon --
        a helper-csere kulcs-hűségének őre)."""
        cc = _StubChargeCtrl(steps_to_done=2)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._checkpoint_interval_s = 0.0
        recorded = _r1b7_record_writes(logger)

        runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))
        logger.close()

        running = _r1b7_running(recorded, "OK")
        assert running
        expected = (
            {"status", "step", "elapsed_s"}
            | set(runner._checkpoint_summary_fields().keys())
            | set(runner._checkpoint_traceability_fields(
                safe_boundary=False, next_safe_step_index=0,
            ).keys())
        )
        for payload in running:
            assert set(payload.keys()) == expected


_R1F16_DF_RESIDUAL = {
    "field": "cell_bl_set_volt_mV",
    "original_mV": 1950,
    "restore_confirmed": False,
    "bq_unsealed": True,
}
_R1F16_UNSEALED_RESIDUAL = {
    "controller": "GOLDEN_GG",
    "reason": "SOURCE_SNAPSHOT_READ_REQUIRES_UNSEALED",
    "bq_unsealed": True,
}


def _r1f16_make_ctrl(field, *, residual_from_tick, steps_to_done=6,
                     fault_at=None, clear_at=None):
    """TLC-stub (pending_bq_df_restore) vagy GG-stub (pending_bq_unsealed),
    amely `residual_from_tick`-től a maradványt, `clear_at`-tól None-t
    mutat; az emergency_stop() hívásait számolja."""
    base = _StubLearningCtrl if field == "pending_bq_df_restore" else _StubGoldenGgCtrl
    residual = (
        _R1F16_DF_RESIDUAL if field == "pending_bq_df_restore" else _R1F16_UNSEALED_RESIDUAL
    )

    class _Ctrl(base):
        pending_bq_df_restore: dict | None = None
        pending_bq_unsealed: dict | None = None

        def __init__(self):
            super().__init__(steps_to_done=steps_to_done, fault_at_step=fault_at)
            self.emergency_stop_calls = 0

        def advance(self, dt_s):
            stt = super().advance(dt_s)
            if self._tick >= residual_from_tick:
                setattr(self, field, dict(residual))
            if clear_at is not None and self._tick >= clear_at:
                setattr(self, field, None)
            return stt

        def emergency_stop(self, reason=""):
            self.emergency_stop_calls += 1

    return _Ctrl()


def _r1f16_runner(tmp_path, field, ctrl):
    if field == "pending_bq_df_restore":
        runner, logger = _make_runner(tmp_path, tlc=ctrl)
        kind = StepKind.TRADITIONAL_LEARNING
    else:
        runner, logger = _make_runner(tmp_path, ggc=ctrl)
        kind = StepKind.GOLDEN_GG
    _configure_traceability(runner)
    plan = TestPlan(
        test_type=TestType.CHARACTERIZATION,
        steps=(TestStep(kind, "bq_step"),),
    )
    return runner, logger, plan


def _r1f16_fail_first_residual_running(field):
    state = {"failed": False}

    def _fail_if(payload, _recorded):
        if (
            payload.get("status") == "RUNNING"
            and payload.get(field) is not None
            and not state["failed"]
        ):
            state["failed"] = True
            return True
        return False

    return _fail_if


def _r1f16_read_checkpoint(tmp_path) -> dict:
    import json
    return json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))


_R1F16_FIELDS = ["pending_bq_df_restore", "pending_bq_unsealed"]


class TestR1F16PendingResidualSurvivesFailedPeriodicWrite:
    """[regresszió, R1 F-16, DFAM-016+DFAM-018] a bukott periodikus
    write_checkpoint() utáni rollback a `_last_pending_bq_*`-t a lemezen
    lévő (régi) értékre állította vissza -- ha ugyanabban a tickben a
    controller FAULT/DONE-ba lépett, a terminális írás (amely a már
    None-ra állított `_active_controller` miatt a `_last_pending_bq_*`
    fallbackre esik) a régi értéket írta: elveszett maradvány (None) vagy
    hamis, már megszűnt maradvány. A fix után a `_last_pending_bq_*` az
    utolsó MEGFIGYELT érték; a "lemez nem friss" állapotot kizárólag a
    `_periodic_checkpoint_write_failing` jelzi (OR-trigger)."""

    @pytest.mark.parametrize("field", _R1F16_FIELDS)
    def test_fault_path_terminal_checkpoint_keeps_residual(self, tmp_path, field):
        ctrl = _r1f16_make_ctrl(field, residual_from_tick=3, fault_at=3)
        runner, logger, plan = _r1f16_runner(tmp_path, field, ctrl)
        recorded = _r1b7_record_writes(logger, _r1f16_fail_first_residual_running(field))

        result = runner.run(plan)
        logger.close()

        assert result.status == "FAULT"
        assert ctrl._tick == 3
        assert any(
            o == "FAIL" and p.get("status") == "RUNNING" and p.get(field) for p, o in recorded
        ), "a harness nem váltotta ki a maradványos RUNNING írás hibáját"
        cp = _r1f16_read_checkpoint(tmp_path)
        assert cp["status"] != "RUNNING"
        assert cp[field] == getattr(ctrl, field)
        assert cp[field] is not None

    @pytest.mark.parametrize("field", _R1F16_FIELDS)
    def test_done_path_terminal_checkpoint_keeps_residual(self, tmp_path, field):
        ctrl = _r1f16_make_ctrl(field, residual_from_tick=3, steps_to_done=3)
        runner, logger, plan = _r1f16_runner(tmp_path, field, ctrl)
        recorded = _r1b7_record_writes(logger, _r1f16_fail_first_residual_running(field))

        result = runner.run(plan)
        logger.close()

        assert result.status == "DONE"
        assert ctrl._tick == 3
        assert any(o == "FAIL" for _, o in recorded)
        cp = _r1f16_read_checkpoint(tmp_path)
        assert cp["status"] == "DONE"
        assert cp[field] == getattr(ctrl, field)
        assert cp[field] is not None

    @pytest.mark.parametrize("field", _R1F16_FIELDS)
    def test_cleared_residual_with_failed_write_is_not_resurrected(self, tmp_path, field):
        """dict -> None tisztulás bukott írással: a terminális checkpoint
        None-t mutasson (régi: a rollback a régi dictet állította vissza,
        hamis maradvány-figyelmeztetés a startup-dialógusban)."""
        ctrl = _r1f16_make_ctrl(field, residual_from_tick=2, clear_at=3, fault_at=3)
        runner, logger, plan = _r1f16_runner(tmp_path, field, ctrl)

        def _fail_on_clear(payload, recorded):
            seen_residual = any(
                o == "OK" and p.get("status") == "RUNNING" and p.get(field)
                for p, o in recorded
            )
            return (
                payload.get("status") == "RUNNING"
                and seen_residual
                and payload.get(field) is None
            )

        recorded = _r1b7_record_writes(logger, _fail_on_clear)

        result = runner.run(plan)
        logger.close()

        assert result.status == "FAULT"
        assert ctrl._tick == 3
        assert any(
            o == "OK" and p.get("status") == "RUNNING" and p.get(field) for p, o in recorded
        ), "a maradványnak előbb sikeresen lemezre kellett kerülnie"
        assert any(
            o == "FAIL" and p.get("status") == "RUNNING" and p.get(field) is None
            for p, o in recorded
        ), "a harness nem váltotta ki a tisztuló írás hibáját"
        assert getattr(ctrl, field) is None
        cp = _r1f16_read_checkpoint(tmp_path)
        assert cp["status"] != "RUNNING"
        assert cp[field] is None

    @pytest.mark.parametrize("field", _R1F16_FIELDS)
    def test_retry_stops_after_successful_write(self, tmp_path, field):
        """Nagy intervallum (1000 s): tick1 idő-alapú írás, tick2 maradvány
        -> bukott írás, tick3 sikeres retry, utána NINCS további RUNNING
        írás (a retry-feltétel sikeres írás után megszűnik)."""
        ctrl = _r1f16_make_ctrl(field, residual_from_tick=2, steps_to_done=6)
        runner, logger, plan = _r1f16_runner(tmp_path, field, ctrl)
        runner._checkpoint_interval_s = 1000.0
        recorded = _r1b7_record_writes(logger, _r1f16_fail_first_residual_running(field))

        result = runner.run(plan)
        logger.close()

        assert result.status == "DONE"
        assert ctrl._tick == 6
        running = _r1b7_running(recorded)
        assert [o for p, o in recorded if p.get("status") == "RUNNING"] == ["OK", "FAIL", "OK"]
        assert running[2][field] == getattr(ctrl, field)

    @pytest.mark.parametrize("field", _R1F16_FIELDS)
    def test_fault_path_does_not_call_controller_emergency_stop(self, tmp_path, field):
        """A fix nem változtatja a C-13 mintát: a saját maga által FAULT-olt
        controller emergency_stop()-ja nem hívódik (a runner saját
        safe-offja fut)."""
        ctrl = _r1f16_make_ctrl(field, residual_from_tick=3, fault_at=3)
        runner, logger, plan = _r1f16_runner(tmp_path, field, ctrl)
        _r1b7_record_writes(logger, _r1f16_fail_first_residual_running(field))

        result = runner.run(plan)
        logger.close()

        assert result.status == "FAULT"
        assert ctrl._tick == 3
        assert ctrl.emergency_stop_calls == 0

    def test_persistent_failure_without_residual_retries_every_tick_single_event(
        self, tmp_path
    ):
        """[FINAL B7/2, explicit viselkedésváltozás] maradvány NÉLKÜL, nagy
        (1000 s) intervallummal: a tartós OSError alatt MINDEN tickben új
        írási kísérlet (OR-trigger a _periodic_checkpoint_write_failing-
        re), EGYETLEN PERIODIC_CHECKPOINT_WRITE_FAILED esemény (átmenet),
        a sikeres írás után az időzítés visszaáll (további RUNNING írás
        nincs). Régi kód: csak a tick1 idő-alapú kísérlet, retry nélkül."""
        cc = _StubChargeCtrl(steps_to_done=8)
        runner, logger = _make_runner(tmp_path, cc=cc)
        runner._checkpoint_interval_s = 1000.0
        events: list = []
        runner.on_event = events.append
        attempt_ticks: list[int] = []

        def _fail_first_three_running(payload, recorded):
            if payload.get("status") != "RUNNING":
                return False
            attempt_ticks.append(cc._tick)
            return len(attempt_ticks) <= 3

        recorded = _r1b7_record_writes(logger, _fail_first_three_running)

        result = runner.run(TestPlan(
            test_type=TestType.CHARACTERIZATION,
            steps=(TestStep(StepKind.CHARGE, "charge"),),
        ))
        logger.close()

        assert result.status == "DONE"
        assert cc._tick == 8
        assert attempt_ticks == [1, 2, 3, 4]
        assert [o for p, o in recorded if p.get("status") == "RUNNING"] == [
            "FAIL", "FAIL", "FAIL", "OK",
        ]
        failure_events = [
            e for e in events if e.get("event_code") == "PERIODIC_CHECKPOINT_WRITE_FAILED"
        ]
        assert len(failure_events) == 1
        assert runner._periodic_checkpoint_write_failing is False


# ------------------------------------------------------------------ #
# R1 B8b, F-26 (P3, DFAM-021 + DFAM-017): sikeres NPLC-váltás         #
# naplózása (NPLC_SET), parancsnyugta és readback különválasztva.     #
# ------------------------------------------------------------------ #

_F26_NPLC_SET_MESSAGE_1 = (
    "VOLT:DC:NPLC 1 parancs nyugtázva (*OPC?); műszer-visszaolvasás nincs"
)
_F26_NPLC_SET_MESSAGE_10 = (
    "VOLT:DC:NPLC 10 parancs nyugtázva (*OPC?); műszer-visszaolvasás nincs"
)


def _f26_events_rows(tmp_path, code: str) -> list:
    import csv
    with (tmp_path / "events.csv").open(encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r["event_code"] == code]


def _f26_mock_logger_runner(config):
    from unittest.mock import MagicMock

    from Prog.src.instrument_manager import InstrumentManager
    from Prog.tests.mock_drivers.mock_dmm import MockDMM
    from Prog.tests.mock_drivers.mock_load import MockLoad
    from Prog.tests.mock_drivers.mock_psu import MockPSU

    dmm_v = MockDMM(voltage_V=12.5)
    instruments = InstrumentManager(
        psu=MockPSU(voltage_V=14.4, current_A=1.75),
        load=MockLoad(voltage_V=12.5, current_A=0.7),
        dmm_voltage=dmm_v,
        dmm_temperature=MockDMM(temperature_C=25.0),
    )
    logger = MagicMock()
    runner = TestRunner(
        instrument_manager=instruments,
        safety=MagicMock(),
        logger=logger,
        profile=MagicMock(),
        config=config,
        charge_ctrl_factory=lambda: None,
        discharge_ctrl_factory=lambda: None,
        relax_ctrl_factory=lambda step: None,
    )
    events: list = []
    runner.on_event = events.append
    return runner, logger, dmm_v, events


def _f26_log_calls(logger, code: str) -> list:
    return [c.args for c in logger.log_event.call_args_list if c.args and c.args[0] == code]


class TestF26NplcSetLogged:
    """[regresszió, R1 B8b, F-26 (P3)] a sikeres set_nplc() korábban csak a
    `_last_configured_nplc` cache-t frissítette -- az events.csv-ből nem
    volt rekonstruálható, mikor milyen NPLC-t parancsolt a runner (a bring-
    up DMM_CONFIGURATION literál nplc=10-et mutatott a teljes sessionre).
    Fix: NPLC_SET esemény CSAK tényleges változáskor (None is változás), a
    parancsnyugta (`VOLT:DC:NPLC x;*OPC?`) és a readback hiánya jelölve; on_
    event nélkül; a naplózási hiba nem kerül az instrumentum-hibakezelésbe."""

    def test_first_configuration_logs_nplc_set_once_without_on_event(self):
        config = TestRunnerConfig(charge_discharge_nplc=1.0, relax_ocv_nplc=10.0)
        runner, logger, _dmm_v, events = _f26_mock_logger_runner(config)

        runner._configure_dmm_for_step(TestStep(StepKind.CHARGE, "charge"))

        assert runner._last_configured_nplc == 1.0
        assert _f26_log_calls(logger, "NPLC_SET") == [("NPLC_SET", _F26_NPLC_SET_MESSAGE_1)]
        assert _f26_log_calls(logger, "NPLC_SET_FAILED") == []
        assert all(ev.get("event_code") != "NPLC_SET" for ev in events)
        assert events == []

    def test_repeating_same_value_logs_no_new_event(self):
        config = TestRunnerConfig(charge_discharge_nplc=1.0, relax_ocv_nplc=10.0)
        runner, logger, dmm_v, _events = _f26_mock_logger_runner(config)

        runner._configure_dmm_for_step(TestStep(StepKind.CHARGE, "charge"))
        runner._configure_dmm_for_step(TestStep(StepKind.DISCHARGE, "discharge"))

        # Az SCPI-viselkedés változatlan (lépés-eleji set_nplc() mindig fut) ...
        assert [c for c in dmm_v.calls if c.startswith("set_nplc(")] == [
            "set_nplc(1.0)", "set_nplc(1.0)",
        ]
        # ... de az azonos érték ismétlése nem új esemény.
        assert len(_f26_log_calls(logger, "NPLC_SET")) == 1

    def test_successful_retry_after_failure_logs_nplc_set(self):
        config = TestRunnerConfig(charge_discharge_nplc=1.0, relax_ocv_nplc=10.0)
        runner, logger, dmm_v, _events = _f26_mock_logger_runner(config)
        _make_flaky_set_nplc(dmm_v, fail_first_n=1)
        step = TestStep(StepKind.CHARGE, "charge")

        runner._configure_dmm_for_step(step)  # bukik -> NPLC_SET_FAILED
        assert runner._last_configured_nplc is None
        assert _f26_log_calls(logger, "NPLC_SET") == []
        assert len(_f26_log_calls(logger, "NPLC_SET_FAILED")) == 1

        runner._reconfigure_dmm_nplc_for_relax_substate(step, _StubChargeCtrl())  # retry

        assert runner._last_configured_nplc == 1.0
        assert _f26_log_calls(logger, "NPLC_SET") == [("NPLC_SET", _F26_NPLC_SET_MESSAGE_1)]

        runner._reconfigure_dmm_nplc_for_relax_substate(step, _StubChargeCtrl())  # nincs változás
        assert len(_f26_log_calls(logger, "NPLC_SET")) == 1

    def test_charge_discharge_relax_plan_logs_only_at_transitions(self, tmp_path):
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        runner, logger = _make_runner(
            tmp_path,
            cc=_StubChargeCtrl(steps_to_done=3),
            dc=_StubDischargeCtrl(steps_to_done=3),
            rc=_StubRelaxCtrl(steps_to_done=3),
            config=config,
        )
        dmm_v = runner._instruments.dmm_voltage
        events: list = []
        runner.on_event = events.append
        plan = TestPlan(
            test_type=None,
            steps=(
                TestStep(StepKind.CHARGE, "charge"),
                TestStep(StepKind.DISCHARGE, "discharge"),
                TestStep(StepKind.RELAX, "relax"),
            ),
        )

        result = runner.run(plan)
        logger.close()

        assert result.status != "FAULT"
        assert [c for c in dmm_v.calls if c.startswith("set_nplc(")] == [
            "set_nplc(1.0)", "set_nplc(1.0)", "set_nplc(10.0)",
        ]
        rows = _f26_events_rows(tmp_path, "NPLC_SET")
        assert [r["event_message"] for r in rows] == [
            _F26_NPLC_SET_MESSAGE_1, _F26_NPLC_SET_MESSAGE_10,
        ]
        assert all(ev.get("event_code") != "NPLC_SET" for ev in events)

    def test_dynamic_relax_substate_switch_logs_each_change_once(self, tmp_path):
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        ggc = _ScriptedGoldenGgCtrl([
            GoldenGgState.PRECOND, GoldenGgState.RELAX_2H, GoldenGgState.RELAX_2H,
            GoldenGgState.SYSTEM_RATE_DISCHARGE, GoldenGgState.DONE,
        ])
        runner, logger = _make_runner(tmp_path, ggc=ggc, config=config)
        plan = TestPlan(
            test_type=TestType.GOLDEN_GG,
            steps=(TestStep(StepKind.GOLDEN_GG, "golden_gg"),),
        )

        runner.run(plan)
        logger.close()

        rows = _f26_events_rows(tmp_path, "NPLC_SET")
        assert [r["event_message"] for r in rows] == [
            _F26_NPLC_SET_MESSAGE_1,   # lépés eleje
            _F26_NPLC_SET_MESSAGE_10,  # RELAX_2H belépés (ismétlésnél nincs)
            _F26_NPLC_SET_MESSAGE_1,   # SYSTEM_RATE_DISCHARGE
        ]

    def test_nplc_set_logging_oserror_is_not_fault_and_keeps_cache(self, tmp_path):
        config = TestRunnerConfig(
            runner_tick_s=1.0, sleep_enabled=False, test_name="t",
            charge_discharge_nplc=1.0, relax_ocv_nplc=10.0,
        )
        ggc = _ScriptedGoldenGgCtrl([
            GoldenGgState.PRECOND, GoldenGgState.RELAX_2H, GoldenGgState.RELAX_2H,
            GoldenGgState.DONE,
        ])
        runner, logger = _make_runner(tmp_path, ggc=ggc, config=config)
        dmm_v = runner._instruments.dmm_voltage
        real_log_event = runner._logger.log_event
        attempted: list = []

        def _failing_nplc_set_log(code, *args, **kwargs):
            attempted.append(code)
            if code == "NPLC_SET":
                raise OSError("disk full (szimulált)")
            return real_log_event(code, *args, **kwargs)

        runner._logger.log_event = _failing_nplc_set_log
        events: list = []
        runner.on_event = events.append
        plan = TestPlan(
            test_type=TestType.GOLDEN_GG,
            steps=(TestStep(StepKind.GOLDEN_GG, "golden_gg"),),
        )

        result = runner.run(plan)
        logger.close()

        assert result.status != "FAULT"
        assert "UNHANDLED_EXCEPTION" not in (result.reason or "")
        # Mindkét sikerág (lépés-eleji + dinamikus) megkísérelte a naplózást
        # (1,0 lépés eleje; 10,0 RELAX_2H; 1,0 a DONE-tick nem-relax állapota).
        assert attempted.count("NPLC_SET") == 3
        # A cache a műszer tényleges (parancsolt) állapotát tükrözi: nincs
        # nullázás, ezért nincs felesleges retry-SCPI sem.
        assert runner._last_configured_nplc == 1.0
        assert [c for c in dmm_v.calls if c.startswith("set_nplc(")] == [
            "set_nplc(1.0)", "set_nplc(10.0)", "set_nplc(1.0)",
        ]
        assert "NPLC_SET_FAILED" not in attempted
        assert all(
            ev.get("event_code") not in ("NPLC_SET", "NPLC_SET_FAILED") for ev in events
        )
        assert runner._nplc_set_failing is False


class TestR6B3CompositeIntegrationSourceToSampleRow:
    """[regresszio, Claude-Codex loop R6 B3: R6-metrology-01, DFAM-001
    metadata-sibling] Az ot osszetett controller `last_integration_source`-a
    a kompozit SAJAT, tick-scoped rogzitett erteke (nem a gyermek elo
    proxyja). Szerzodes: (1) a kompozit advance() eleje "ZERO"-ra nullaz;
    (2) kisutesi integralasi pont: LOAD_READBACK (mert nulla aramnal is);
    (3) toltesi ag: a gyermek ChargeController MEGORZOTT forrasanak
    atvetele a gyermek advance()-a UTAN, az elengedese ELOTT -- a masolt
    PSU_READBACK onmagaban NEM bizonyit friss integralast vagy nem nulla
    deltat (a kenyszeritett CHARGE_DONE kontroll-ticken 0 Ah delta mellett
    is PSU_READBACK). Ezert a tesztek a gyermek ELSO tenylegesen integralo
    tickjet (nem nulla delta) es egy valodi, integralassal lezarulo
    TAPER_HOLD -> CHARGE_DONE atmenetet is a naplosorig kovetik; a
    kisutesi readback-hiba (-> FAULT) sora ZERO, az Ah/Wh valtozatlan.
    Lanc: valodi controller -> valodi TestRunner._build_sample() -> valodi
    Logger.log_sample() -> samples.csv integration_current_source oszlop.
    Az Ah/Wh szamitas nem valtozik. Unit/mock szint, nem HIL."""

    @staticmethod
    def _rows(tmp_path):
        import csv
        with open(tmp_path / "samples.csv", newline="", encoding="utf-8") as fh:
            return [r["integration_current_source"] for r in csv.DictReader(fh)]

    @staticmethod
    def _tick(runner, logger, step, ctrl, dt_s, out):
        ctrl.advance(dt_s)
        sample = runner._build_sample(step, ctrl)
        logger.log_sample(sample)
        out.append(sample["integration_current_source"])

    @staticmethod
    def _energy(ctrl):
        return (
            ctrl.accumulated_charge_Ah, ctrl.accumulated_charge_Wh,
            ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh,
        )

    def _charge_logged_until_child_integrates(self, runner, logger, step, ctrl, psu, got, max_ticks=20):
        """A gyermek letrejottetol MINDEN kompozit-tick naplozva; az ELSO
        olyan tickig, amelyen a gyermek tenylegesen integral (forrasa
        PSU_READBACK-re valt ES nem nulla Ah-delta). Visszaadja az addigi
        (nem integralo) tickek szamat. A forrasmasolas gyermek-advance()
        ELE mozgatva ezen az elso integralo ticken meg a gyermek elozo
        (ZERO) forrasat adna."""
        child = ctrl._charge_ctrl
        assert child is not None
        assert child.last_integration_source == "ZERO"
        psu.current_A = 0.7  # valodi, nem nulla toltoaram (MockPSU alapja 0,0 A)
        for _ in range(max_ticks):
            child_ah = child.accumulated_charge_Ah
            session_ah = ctrl.accumulated_charge_Ah
            self._tick(runner, logger, step, ctrl, 1.0, got)
            if child.last_integration_source == "PSU_READBACK":
                assert child.accumulated_charge_Ah > child_ah
                assert ctrl.accumulated_charge_Ah > session_ah
                return len(got) - 1
        raise AssertionError("a gyermek ChargeController nem integralt PSU_READBACK-kel")

    def _charge_release_relax(self, runner, logger, step, ctrl, psu, got):
        """Kozos toltesi szakasz: az elso valodi PSU_READBACK-integralo
        gyermek-tick naplosora, egy tovabbi toltesi tick, majd a
        kenyszeritett CHARGE_DONE elengedo tickje (kontroll: 0 Ah delta,
        a gyermek megorzott forrasa). Visszaadja az integralas elotti
        tickek szamat."""
        from Prog.src.charge_controller import ChargeState
        n_pre = self._charge_logged_until_child_integrates(runner, logger, step, ctrl, psu, got)
        self._tick(runner, logger, step, ctrl, 1.0, got)    # toltes
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        before = ctrl.accumulated_charge_Ah
        self._tick(runner, logger, step, ctrl, 1.0, got)    # elengedo tick (kenyszeritett)
        assert ctrl._charge_ctrl is None
        assert ctrl.accumulated_charge_Ah == before         # 0 delta, megis PSU_READBACK
        psu.all_outputs_off()  # a kenyszeritett CHARGE_DONE nem kapcsolta le
        return n_pre

    def _assert_discharge_readback_failure(self, runner, logger, step, ctrl, load, got, fault_state):
        """Kisutes kozbeni Load-aramreadback-hiba: FAULT, a sor ZERO, az
        Ah/Wh (toltesi es kisutesi) valtozatlan."""
        energy_before = self._energy(ctrl)
        load.simulate_current_readback_failure = True
        self._tick(runner, logger, step, ctrl, 1.0, got)
        assert ctrl.state == fault_state
        assert self._energy(ctrl) == energy_before
        assert got[-1] == "ZERO"

    def test_iec_dc_resistance(self, tmp_path):
        from Prog.tests.test_iec_dc_resistance_controller import make_iec_controller

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.IEC_DC_RESISTANCE, "iec")
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller()
        psu.measure_output_voltage = lambda: 12.9  # soros-ejtes kapu alatt
        got: list = []
        ctrl.advance(0.0)                                   # INIT -> PRECHARGE
        n_pre = self._charge_release_relax(runner, logger, step, ctrl, psu, got)
        self._tick(runner, logger, step, ctrl, 1.0, got)    # relax -> LOAD_1
        assert ctrl.state == IecDcResistanceState.LOAD_1
        dmm_v.voltage_V = 12.0
        self._tick(runner, logger, step, ctrl, 0.0, got)    # LOAD_1 setup (nincs integralas)
        self._tick(runner, logger, step, ctrl, 1.0, got)    # LOAD_1 kisutes
        self._tick(runner, logger, step, ctrl, 1.0, got)    # allapotvalto tick -> OPEN_CIRCUIT_REST
        assert ctrl.state == IecDcResistanceState.OPEN_CIRCUIT_REST
        self._tick(runner, logger, step, ctrl, 3.0, got)    # rest -> LOAD_2
        assert ctrl.state == IecDcResistanceState.LOAD_2
        dmm_v.voltage_V = 11.5
        self._tick(runner, logger, step, ctrl, 0.0, got)    # LOAD_2 setup
        load.current_A = 0.0
        discharge_before = ctrl.accumulated_discharge_Ah
        self._tick(runner, logger, step, ctrl, 0.1, got)    # mert NULLA aram
        assert ctrl.accumulated_discharge_Ah == discharge_before
        self._assert_discharge_readback_failure(
            runner, logger, step, ctrl, load, got, IecDcResistanceState.FAULT)
        logger.close()

        expected = ["ZERO"] * n_pre + [
            "PSU_READBACK", "PSU_READBACK", "PSU_READBACK", "ZERO", "ZERO", "LOAD_READBACK",
            "LOAD_READBACK", "ZERO", "ZERO", "LOAD_READBACK", "ZERO",
        ]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def _chemid_to_discharge(self, tmp_path):
        from Prog.tests.test_chemid_controller import make_controller

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.CHEMID, "chemid")
        ctrl, psu, load, dmm_v, _dmm_t = make_controller()
        psu.measure_output_voltage = lambda: 13.1
        got: list = []
        ctrl.advance(0.0)
        ctrl.advance(0.0)
        ctrl.advance(7200.1)                                 # FULL_CHARGE
        n_pre = self._charge_release_relax(runner, logger, step, ctrl, psu, got)
        self._tick(runner, logger, step, ctrl, 1.0, got)     # CHARGE_TERMINATION_VERIFIED
        self._tick(runner, logger, step, ctrl, 18000.0, got) # relax, majd C20
        assert ctrl.state == ChemIdState.C20_DISCHARGE_TO_MANUFACTURER_MINIMUM
        self._tick(runner, logger, step, ctrl, 1.0, got)     # kisutes
        load.current_A = 0.0
        before = ctrl.accumulated_discharge_Ah
        self._tick(runner, logger, step, ctrl, 1.0, got)     # mert nulla
        assert ctrl.accumulated_discharge_Ah == before
        prefix = ["ZERO"] * n_pre + [
            "PSU_READBACK", "PSU_READBACK", "PSU_READBACK", "ZERO", "ZERO",
            "LOAD_READBACK", "LOAD_READBACK",
        ]
        return runner, logger, step, ctrl, load, dmm_v, got, prefix

    def test_chemid(self, tmp_path):
        runner, logger, step, ctrl, load, dmm_v, got, prefix = self._chemid_to_discharge(tmp_path)
        load.current_A = 0.35
        dmm_v.voltage_V = 10.5
        self._tick(runner, logger, step, ctrl, 1.0, got)     # allapotvalto tick
        assert ctrl.state == ChemIdState.RELAX_5H_EMPTY
        self._tick(runner, logger, step, ctrl, 1.0, got)     # relax
        logger.close()
        expected = prefix + ["LOAD_READBACK", "ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def test_chemid_discharge_readback_failure(self, tmp_path):
        runner, logger, step, ctrl, load, _dmm_v, got, prefix = self._chemid_to_discharge(tmp_path)
        self._assert_discharge_readback_failure(runner, logger, step, ctrl, load, got, ChemIdState.FAULT)
        logger.close()
        expected = prefix + ["ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def _golden_gg_to_discharge(self, tmp_path):
        from Prog.tests.test_golden_gg_controller import make_controller

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.GOLDEN_GG, "gg")
        ctrl, psu, load, dmm_v, _dmm_t, _bq = make_controller()
        psu.measure_output_voltage = lambda: 13.1
        got: list = []
        ctrl.advance(0.0)
        ctrl.advance(1.0)                                    # FULL_CHARGE
        n_pre = self._charge_release_relax(runner, logger, step, ctrl, psu, got)
        self._tick(runner, logger, step, ctrl, 0.1, got)     # CHARGE_TERMINATION_VERIFIED
        n_relax = 0
        for _ in range(6):
            self._tick(runner, logger, step, ctrl, 1.0, got)  # relax
            n_relax += 1
            if ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE:
                break
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        self._tick(runner, logger, step, ctrl, 1.0, got)     # kisutes
        load.current_A = 0.0
        before = ctrl.accumulated_discharge_Ah
        self._tick(runner, logger, step, ctrl, 1.0, got)     # mert nulla
        assert ctrl.accumulated_discharge_Ah == before
        prefix = (
            ["ZERO"] * n_pre + ["PSU_READBACK", "PSU_READBACK", "PSU_READBACK", "ZERO"]
            + ["ZERO"] * n_relax + ["LOAD_READBACK", "LOAD_READBACK"]
        )
        return runner, logger, step, ctrl, load, dmm_v, got, prefix

    def test_golden_gg(self, tmp_path):
        runner, logger, step, ctrl, load, dmm_v, got, prefix = self._golden_gg_to_discharge(tmp_path)
        load.current_A = 2.0
        dmm_v.voltage_V = 10.5
        self._tick(runner, logger, step, ctrl, 1.0, got)     # allapotvalto tick
        assert ctrl.state == GoldenGgState.RELAX_5H
        self._tick(runner, logger, step, ctrl, 1.0, got)     # relax
        logger.close()
        expected = prefix + ["LOAD_READBACK", "ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def test_golden_gg_discharge_readback_failure(self, tmp_path):
        runner, logger, step, ctrl, load, _dmm_v, got, prefix = self._golden_gg_to_discharge(tmp_path)
        self._assert_discharge_readback_failure(runner, logger, step, ctrl, load, got, GoldenGgState.FAULT)
        logger.close()
        expected = prefix + ["ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def _traditional_learning_to_discharge(self, tmp_path):
        from Prog.src.traditional_learning_controller import LearningConfig
        from Prog.tests.test_traditional_learning_controller import (
            _accept_relax,
            _FakeBqState,
            _relax_tick_s,
            make_controller,
        )

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "tl")
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=LearningConfig(relax_after_charge_s=1.0, it_enable_settle_s=0.5, verify_settle_s=0.5),
        )
        psu.measure_output_voltage = lambda: 13.6
        got: list = []
        ctrl.advance(0.0)
        ctrl.advance(1.0)                                    # PRECHARGE
        n_pre = self._charge_release_relax(runner, logger, step, ctrl, psu, got)
        _accept_relax(ctrl, fake_bq)
        n_relax = 0
        # R16-B4: a relax-kapuhoz teljes, ervenyes dV/dt-ablak kell (Q-18 B).
        dt_relax_s = _relax_tick_s()
        for _ in range(int(RelaxConfig().sample_period_s / dt_relax_s) + 3):
            if ctrl.state != LearningState.PRECHARGE_RELAX:
                break
            self._tick(runner, logger, step, ctrl, dt_relax_s, got)  # relax
            n_relax += 1
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        fake_bq.set_qen(True)
        fake_bq.set_vok(True)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.DISCHARGE_1
        self._tick(runner, logger, step, ctrl, 1.0, got)     # kisutes
        load.current_A = 0.0
        before = ctrl.accumulated_discharge_Ah
        self._tick(runner, logger, step, ctrl, 1.0, got)     # mert nulla
        assert ctrl.accumulated_discharge_Ah == before
        prefix = (
            ["ZERO"] * n_pre + ["PSU_READBACK", "PSU_READBACK", "PSU_READBACK"]
            + ["ZERO"] * n_relax + ["LOAD_READBACK", "LOAD_READBACK"]
        )
        return runner, logger, step, ctrl, load, dmm_v, got, prefix

    def test_traditional_learning(self, tmp_path):
        runner, logger, step, ctrl, load, dmm_v, got, prefix = self._traditional_learning_to_discharge(tmp_path)
        load.current_A = 0.7
        dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.05
        self._tick(runner, logger, step, ctrl, 1.0, got)     # allapotvalto tick
        assert ctrl.state not in (LearningState.DISCHARGE_1, LearningState.FAULT)
        logger.close()
        expected = prefix + ["LOAD_READBACK"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def test_traditional_learning_discharge_readback_failure(self, tmp_path):
        runner, logger, step, ctrl, load, _dmm_v, got, prefix = self._traditional_learning_to_discharge(tmp_path)
        self._assert_discharge_readback_failure(runner, logger, step, ctrl, load, got, LearningState.FAULT)
        logger.close()
        expected = prefix + ["ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def _ocv_soc_to_discharge(self, tmp_path):
        from Prog.tests.test_ocv_soc_controller import make_ocv_soc_controller

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.OCV_SOC, "ocv")
        ctrl, psu, load, dmm_v, _dmm_t = make_ocv_soc_controller(capacity_Ah=7.0)
        psu.measure_output_voltage = lambda: dmm_v.voltage_V + 0.1
        got: list = []
        ctrl.advance(0.0)                                    # PRECHARGE
        n_pre = self._charge_release_relax(runner, logger, step, ctrl, psu, got)
        assert ctrl.state == OcvSocState.PRECHARGE_RELAX
        # STEP_DISCHARGE: a GUI-ut Q-6 mogott; belepes a meglevo
        # LOG_SOC_POINT-mintaval (l. test_ocv_soc_controller.py).
        ctrl._state = OcvSocState.LOG_SOC_POINT
        ctrl._grid_pos = 1
        ctrl._u_batt = dmm_v.voltage_V
        ctrl._run_log_soc_point()
        assert ctrl.state == OcvSocState.STEP_DISCHARGE
        self._tick(runner, logger, step, ctrl, 1.0, got)     # kisutes
        load.current_A = 0.0
        before = ctrl.accumulated_discharge_Ah
        self._tick(runner, logger, step, ctrl, 1.0, got)     # mert nulla
        assert ctrl.accumulated_discharge_Ah == before
        prefix = ["ZERO"] * n_pre + [
            "PSU_READBACK", "PSU_READBACK", "PSU_READBACK",
            "LOAD_READBACK", "LOAD_READBACK",
        ]
        return runner, logger, step, ctrl, load, dmm_v, got, prefix

    def test_ocv_soc(self, tmp_path):
        runner, logger, step, ctrl, load, _dmm_v, got, prefix = self._ocv_soc_to_discharge(tmp_path)
        load.current_A = 0.7
        for _ in range(3000):
            ctrl.advance(60.0)
            if ctrl.state != OcvSocState.STEP_DISCHARGE:
                break
        sample = runner._build_sample(step, ctrl)            # allapotvalto tick sora
        logger.log_sample(sample)
        got.append(sample["integration_current_source"])
        assert ctrl.state == OcvSocState.STEP_RELAX
        self._tick(runner, logger, step, ctrl, 1.0, got)     # relax
        logger.close()
        expected = prefix + ["LOAD_READBACK", "ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def test_ocv_soc_discharge_readback_failure(self, tmp_path):
        runner, logger, step, ctrl, load, _dmm_v, got, prefix = self._ocv_soc_to_discharge(tmp_path)
        self._assert_discharge_readback_failure(runner, logger, step, ctrl, load, got, OcvSocState.FAULT)
        logger.close()
        expected = prefix + ["ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    # -------------------------------------------------------------- #
    # Valodi, integralassal lezarulo toltesi atmenet (nem kenyszeritett
    # CHARGE_DONE): CC -> CV -> TAPER_HOLD -> CHARGE_DONE, az elengedo
    # ticken a gyermek tenylegesen integral (nem nulla delta).
    # -------------------------------------------------------------- #

    @staticmethod
    def _reach_charge_child(kind, charge_config):
        """A kompozit a toltesi fazisba lep, a gyermek ChargeController
        letrejott, de meg nem lepett. Visszaad: (ctrl, psu, dmm_v, step,
        a gyermek elengedese utani kompozit-allapot)."""
        if kind == "iec":
            from Prog.tests.test_iec_dc_resistance_controller import make_iec_controller
            ctrl, psu, _load, dmm_v, _dmm_t = make_iec_controller(charge_config=charge_config)
            ctrl.advance(0.0)
            return ctrl, psu, dmm_v, TestStep(StepKind.IEC_DC_RESISTANCE, "iec"), IecDcResistanceState.PRECHARGE_RELAX
        if kind == "chemid":
            from Prog.tests.test_chemid_controller import make_controller
            ctrl, psu, _load, dmm_v, _dmm_t = make_controller(charge_config=charge_config)
            ctrl.advance(0.0)
            ctrl.advance(0.0)
            ctrl.advance(7200.1)
            return ctrl, psu, dmm_v, TestStep(StepKind.CHEMID, "chemid"), ChemIdState.CHARGE_TERMINATION_VERIFIED
        if kind == "golden_gg":
            from Prog.tests.test_golden_gg_controller import make_controller
            ctrl, psu, _load, dmm_v, _dmm_t, _bq = make_controller(charge_config=charge_config)
            ctrl.advance(0.0)
            ctrl.advance(1.0)
            return ctrl, psu, dmm_v, TestStep(StepKind.GOLDEN_GG, "gg"), GoldenGgState.CHARGE_TERMINATION_VERIFIED
        if kind == "traditional_learning":
            from Prog.src.traditional_learning_controller import LearningConfig
            from Prog.tests.test_traditional_learning_controller import make_controller
            ctrl, psu, _load, dmm_v, _dmm_t, _bq = make_controller(
                dmm_voltage_V=13.5, charge_config=charge_config,
                config=LearningConfig(relax_after_charge_s=1.0, it_enable_settle_s=0.5, verify_settle_s=0.5),
            )
            ctrl.advance(0.0)
            ctrl.advance(1.0)
            return ctrl, psu, dmm_v, TestStep(StepKind.TRADITIONAL_LEARNING, "tl"), LearningState.PRECHARGE_RELAX
        assert kind == "ocv_soc"
        from Prog.tests.test_ocv_soc_controller import make_ocv_soc_controller
        ctrl, psu, _load, dmm_v, _dmm_t = make_ocv_soc_controller(capacity_Ah=7.0, charge_config=charge_config)
        ctrl.advance(0.0)
        return ctrl, psu, dmm_v, TestStep(StepKind.OCV_SOC, "ocv"), OcvSocState.PRECHARGE_RELAX

    @pytest.mark.parametrize(
        "kind", ["iec", "chemid", "golden_gg", "traditional_learning", "ocv_soc"],
    )
    def test_natural_taper_charge_done_release_tick(self, tmp_path, kind):
        from Prog.src.charge_controller import ChargeConfig, ChargeState

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        ctrl, psu, dmm_v, step, released_state = self._reach_charge_child(
            kind, ChargeConfig(taper_hold_s=3.0),
        )
        child = ctrl._charge_ctrl
        assert child is not None and child.state == ChargeState.INIT
        target_V = child._effective_charge_target_V()
        # A celfeszultsegen allo akku, taper-kuszob alatti (de nem nulla)
        # toltoaram, a soros-ejtes kapu alatti PSU-kimenet.
        dmm_v.voltage_V = target_V
        psu.measure_output_voltage = lambda: dmm_v.voltage_V + 0.1
        got: list = []
        child_deltas: list = []
        session_deltas: list = []
        n_pre = self._charge_logged_until_child_integrates(runner, logger, step, ctrl, psu, got)
        child_states: list = [child.state]  # az elso integralo (CC) tick utan
        psu.current_A = 0.1  # < taper-kuszob (effective_taper_A)
        for _ in range(20):
            child_ah = child.accumulated_charge_Ah
            session_ah = ctrl.accumulated_charge_Ah
            self._tick(runner, logger, step, ctrl, 1.0, got)
            child_deltas.append(child.accumulated_charge_Ah - child_ah)
            session_deltas.append(ctrl.accumulated_charge_Ah - session_ah)
            child_states.append(child.state)
            if ctrl._charge_ctrl is None:
                break
        # Valodi (nem kenyszeritett) atmenet: CV es TAPER_HOLD utan a
        # TAPER_HOLD-idozito jart le; az elengedo ticken a gyermek
        # tenylegesen integralt, es a delta a session-szintu Ah-ba is atkerult.
        assert ChargeState.CHARGE_CV_DMM_CONTROL in child_states
        assert ChargeState.TAPER_HOLD in child_states
        assert child_states[-1] == ChargeState.CHARGE_DONE
        assert ctrl._charge_ctrl is None
        assert ctrl.state == released_state
        assert child_deltas[-1] > 0.0
        assert session_deltas[-1] == pytest.approx(child_deltas[-1], rel=1e-12, abs=1e-15)
        assert all(d > 0.0 for d in child_deltas)
        self._tick(runner, logger, step, ctrl, 0.1, got)     # elengedes utani tick
        logger.close()
        expected = ["ZERO"] * n_pre + ["PSU_READBACK"] * (1 + len(child_deltas)) + ["ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    # -------------------------------------------------------------- #
    # Fixup (R6-B3 verifier N2): kontrollok a controller ->
    # _build_sample() -> samples.csv lancon.
    # -------------------------------------------------------------- #

    @pytest.mark.parametrize(
        "kind", ["iec", "chemid", "golden_gg", "traditional_learning", "ocv_soc"],
    )
    def test_terminal_tick_after_integrating_charge_is_zero(self, tmp_path, kind):
        """Integralo toltesi tick (PSU_READBACK) -> a kompozit
        emergency_stop()-ja (mock muszerekkel) -> a kovetkezo, terminalis
        early-returnnel lezarulo advance() sora ZERO: a kompozit tick eleji
        reset a terminalis return ELOTT fut. Ah/Wh a terminalis ticken
        valtozatlan."""
        from Prog.src.charge_controller import ChargeConfig

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        ctrl, psu, dmm_v, step, _released = self._reach_charge_child(kind, ChargeConfig())
        psu.measure_output_voltage = lambda: dmm_v.voltage_V + 0.1
        got: list = []
        n_pre = self._charge_logged_until_child_integrates(runner, logger, step, ctrl, psu, got)
        assert got[-1] == "PSU_READBACK"
        ctrl.emergency_stop("R6B3_TEST_TERMINAL_TICK")
        terminal_state = ctrl.state
        assert terminal_state.name == "FAULT"
        energy_before = self._energy(ctrl)
        self._tick(runner, logger, step, ctrl, 1.0, got)    # terminalis early-return
        assert ctrl.state == terminal_state
        assert self._energy(ctrl) == energy_before
        logger.close()
        expected = ["ZERO"] * n_pre + ["PSU_READBACK", "ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def test_traditional_learning_discharge_2_ticks_are_load_readback(self, tmp_path):
        """A TLC DISCHARGE_2 (a RECHARGE utani masodik kisutes) integralo
        tickjei -- nem nulla kisutesi Ah-deltaval -- LOAD_READBACK sort
        adnak (nem csak a DISCHARGE_1); az allapotvalto (cutoff alatti)
        tick is integral, utana a nem integralo tick ZERO. A belepes a
        meglevo TestSup03Learn007NoManualSuccess._drive_to_done()
        szekvenciajat koveti (kenyszeritett CHARGE_DONE-okkal)."""
        from Prog.src.traditional_learning_controller import LearningConfig
        from Prog.tests.test_traditional_learning_controller import (
            _FakeBqState,
            _force_charge_done,
            make_controller,
        )

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "tl")
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0, rebound_settle_s=1.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            fake_bq.set_ocvtaken(True)
            fake_bq.set_vok(False)
            ctrl.advance(100.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable trigger
        ctrl.advance(1.0)  # it_enable settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        assert ctrl.state == LearningState.DISCHARGE_1
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)  # discharge -> rebound_check_1
        ctrl.advance(1.0)  # rebound_check_1 -> relax_1
        for _ in range(5):
            if ctrl.state == LearningState.VERIFY_UPDATE_STATUS_05:
                break
            fake_bq.set_ocvtaken(True)
            fake_bq.set_vok(False)
            ctrl.advance(100.0)
        ctrl.advance(1.0)  # -> RECHARGE
        dmm_v.voltage_V = 13.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # -> RELAX_PRE_DISCHARGE_2
        for _ in range(5):
            if ctrl.state == LearningState.DISCHARGE_2:
                break
            fake_bq.set_ocvtaken(True)
            fake_bq.set_vok(False)
            ctrl.advance(100.0)
        assert ctrl.state == LearningState.DISCHARGE_2
        got: list = []
        dmm_v.voltage_V = 12.0                               # cutoff folott
        before = self._energy(ctrl)
        self._tick(runner, logger, step, ctrl, 1.0, got)     # DISCHARGE_2 integralo tick
        assert ctrl.state == LearningState.DISCHARGE_2
        after = self._energy(ctrl)
        assert after[2] > before[2] and after[3] > before[3]  # nem nulla kisutesi Ah/Wh-delta
        assert after[:2] == before[:2]
        dmm_v.voltage_V = 9.0
        self._tick(runner, logger, step, ctrl, 1.0, got)     # allapotvalto tick -> REBOUND_CHECK_2
        assert ctrl.state == LearningState.REBOUND_CHECK_2
        self._tick(runner, logger, step, ctrl, 1.0, got)     # nem integralo tick
        assert ctrl.state not in (LearningState.DISCHARGE_2, LearningState.FAULT)
        logger.close()
        expected = ["LOAD_READBACK", "LOAD_READBACK", "ZERO"]
        assert got == expected
        assert self._rows(tmp_path) == expected

    def test_iec_load_1_measured_zero_current_tick_is_load_readback(self, tmp_path):
        """IEC LOAD_1: mert NULLA Load-aram melletti integralo tick sora is
        LOAD_READBACK (a LOAD_2 testverenek megfeleloen); a kisutesi
        Ah/Wh ezen a ticken valtozatlan."""
        from Prog.tests.test_iec_dc_resistance_controller import make_iec_controller

        runner, logger = _make_runner(tmp_path)
        runner._start_time = datetime.now(timezone.utc)
        step = TestStep(StepKind.IEC_DC_RESISTANCE, "iec")
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller()
        psu.measure_output_voltage = lambda: 12.9  # soros-ejtes kapu alatt
        got: list = []
        ctrl.advance(0.0)                                   # INIT -> PRECHARGE
        n_pre = self._charge_release_relax(runner, logger, step, ctrl, psu, got)
        self._tick(runner, logger, step, ctrl, 1.0, got)    # relax -> LOAD_1
        assert ctrl.state == IecDcResistanceState.LOAD_1
        dmm_v.voltage_V = 12.0
        self._tick(runner, logger, step, ctrl, 0.0, got)    # LOAD_1 setup (nincs integralas)
        load.current_A = 0.0
        before = self._energy(ctrl)
        self._tick(runner, logger, step, ctrl, 1.0, got)    # LOAD_1, mert NULLA aram
        assert ctrl.state == IecDcResistanceState.LOAD_1
        assert self._energy(ctrl) == before
        logger.close()
        expected = ["ZERO"] * n_pre + [
            "PSU_READBACK", "PSU_READBACK", "PSU_READBACK", "ZERO", "ZERO", "LOAD_READBACK",
        ]
        assert got == expected
        assert self._rows(tmp_path) == expected


class TestR10B3DocumentedNplc:
    """R10-B3: dokumentalt 34465A-alapertek es kimeneti parancs; unit/mock."""

    @staticmethod
    def _runner(monkeypatch, config):
        from unittest.mock import MagicMock

        from Prog.drivers.device_dmm import Keysight34465ADMM

        runner, logger, dmm_v, events = _f26_mock_logger_runner(config)
        driver = Keysight34465ADMM(max_retries=1)
        resource = MagicMock()
        resource.query.return_value = "1"
        driver._resource = resource
        driver._connected = True
        monkeypatch.setattr(dmm_v, "set_nplc", driver.set_nplc)
        return runner, logger, resource, events

    def test_default_is_in_documented_set_and_equals_user_decision(self):
        # 34460-90901, Edition 7, [SENSe:]VOLTage[:DC]:NPLC, 382. o., DIG nelkul.
        documented = {0.02, 0.06, 0.2, 1.0, 10.0, 100.0}
        config = TestRunnerConfig()
        assert config.charge_discharge_nplc in documented
        assert config.charge_discharge_nplc == 1.0
        assert config.relax_ocv_nplc == 10.0

    @pytest.mark.parametrize("kind,expected", [
        (StepKind.CHARGE, 1.0),
        (StepKind.DISCHARGE, 1.0),
        (StepKind.TRADITIONAL_LEARNING, 1.0),
        (StepKind.GOLDEN_GG, 1.0),
        (StepKind.RELAX, 10.0),
        (StepKind.OCV_SOC, 10.0),
        (StepKind.CHEMID, 10.0),
        (StepKind.IEC_DC_RESISTANCE, 10.0),
    ])
    def test_step_default_reaches_driver_and_ack_log(self, monkeypatch, kind, expected):
        runner, logger, resource, events = self._runner(monkeypatch, TestRunnerConfig())
        runner._configure_dmm_for_step(TestStep(kind, "nplc"))

        assert [c.args[0] for c in resource.query.call_args_list] == [
            f"VOLT:DC:NPLC {expected:g};*OPC?",
        ]
        resource.write.assert_not_called()
        assert runner._last_configured_nplc == expected
        message = _F26_NPLC_SET_MESSAGE_1 if expected == 1.0 else _F26_NPLC_SET_MESSAGE_10
        assert _f26_log_calls(logger, "NPLC_SET") == [("NPLC_SET", message)]
        assert events == []

    @pytest.mark.parametrize("fast,slow", [(None, 10.0), (0.2, 100.0)])
    @pytest.mark.parametrize("kind,fast_state,slow_state", [
        (StepKind.TRADITIONAL_LEARNING, LearningState.DISCHARGE_1, state)
        for state in (
            LearningState.PRECHARGE_RELAX, LearningState.RELAX_1,
            LearningState.RELAX_PRE_DISCHARGE_2, LearningState.RELAX_2,
            LearningState.REBOUND_CHECK_1, LearningState.REBOUND_CHECK_2,
        )
    ] + [
        (StepKind.GOLDEN_GG, GoldenGgState.SYSTEM_RATE_DISCHARGE, GoldenGgState.RELAX_2H),
        (StepKind.GOLDEN_GG, GoldenGgState.SYSTEM_RATE_DISCHARGE, GoldenGgState.RELAX_5H),
        (StepKind.OCV_SOC, OcvSocState.PRECHARGE, OcvSocState.PRECHARGE_RELAX),
        (StepKind.CHEMID, ChemIdState.FULL_CHARGE_MANUFACTURER_METHOD, ChemIdState.RELAX_5H_FULL),
        (StepKind.IEC_DC_RESISTANCE, IecDcResistanceState.PRECHARGE,
         IecDcResistanceState.PRECHARGE_RELAX),
    ])
    def test_fast_slow_fast_commands_and_dedup(
        self, monkeypatch, kind, fast_state, slow_state, fast, slow,
    ):
        from types import SimpleNamespace

        config = TestRunnerConfig()
        if fast is not None:
            config.charge_discharge_nplc = fast
            config.relax_ocv_nplc = slow
        runner, logger, resource, _events = self._runner(monkeypatch, config)
        step = TestStep(kind, "nplc")
        controller = SimpleNamespace(state=fast_state)
        expected_fast = 1.0 if fast is None else fast

        runner._reconfigure_dmm_nplc_for_relax_substate(step, controller)
        controller.state = slow_state
        runner._reconfigure_dmm_nplc_for_relax_substate(step, controller)
        runner._reconfigure_dmm_nplc_for_relax_substate(step, controller)
        controller.state = fast_state
        runner._reconfigure_dmm_nplc_for_relax_substate(step, controller)

        assert [c.args[0] for c in resource.query.call_args_list] == [
            f"VOLT:DC:NPLC {value:g};*OPC?" for value in (expected_fast, slow, expected_fast)
        ]
        resource.write.assert_not_called()
        assert runner._last_configured_nplc == expected_fast
        assert len(_f26_log_calls(logger, "NPLC_SET")) == 3
        assert _f26_log_calls(logger, "NPLC_SET_FAILED") == []

    @pytest.mark.parametrize("kind,expected", [
        (StepKind.CHARGE, 1.0),
        (StepKind.DISCHARGE, 1.0),
        (StepKind.TRADITIONAL_LEARNING, 1.0),
        (StepKind.GOLDEN_GG, 1.0),
        (StepKind.RELAX, 10.0),
        (StepKind.OCV_SOC, 10.0),
        (StepKind.CHEMID, 10.0),
        (StepKind.IEC_DC_RESISTANCE, 10.0),
    ])
    def test_failed_step_configuration_retries_same_default(
        self, monkeypatch, kind, expected,
    ):
        from types import SimpleNamespace

        runner, logger, resource, events = self._runner(monkeypatch, TestRunnerConfig())
        resource.query.side_effect = [RuntimeError("mock SCPI timeout"), "1"]
        step = TestStep(kind, "nplc")
        runner._configure_dmm_for_step(step)

        assert runner._last_configured_nplc is None
        assert len(_f26_log_calls(logger, "NPLC_SET_FAILED")) == 1
        assert [ev["event_code"] for ev in events] == ["NPLC_SET_FAILED"]
        assert _f26_log_calls(logger, "NPLC_SET") == []

        runner._reconfigure_dmm_nplc_for_relax_substate(step, SimpleNamespace(state=None))

        assert [c.args[0] for c in resource.query.call_args_list] == [
            f"VOLT:DC:NPLC {expected:g};*OPC?",
            f"VOLT:DC:NPLC {expected:g};*OPC?",
        ]
        assert runner._last_configured_nplc == expected
        assert runner._nplc_set_failing is False
        assert len(_f26_log_calls(logger, "NPLC_SET")) == 1


class TestR16B5aDeferredPollEvents:
    def test_owned_dmm_poll_is_visible_without_fault_or_device_error(self):
        from unittest.mock import MagicMock

        from Prog.drivers.device_dmm import Keysight34465ADMM
        from Prog.src.test_runner import _critical_polled_errors

        runner, logger, _, events = _f26_mock_logger_runner(TestRunnerConfig())
        dmm = Keysight34465ADMM()
        resource = MagicMock()
        resource.query.return_value = '+0,"No error"'
        dmm._resource = resource
        dmm._connected = True
        runner._instruments._dmm_v = dmm
        device_errors = []
        runner.on_device_error = device_errors.append
        owner = object()
        dmm.acquire_measurement(owner)
        result = runner._poll_device_errors()
        assert result == [{
            "device": "DMM_V", "error": "ERROR_QUEUE_POLL_DEFERRED", "status": "HALASZTVA",
        }]
        for kind in StepKind:
            assert _critical_polled_errors(TestStep(kind, "poll"), result) == []
        assert runner._device_error_total_count == 0
        logger.log_device_error.assert_not_called()
        assert device_errors == []
        message = "DMM_V: hibasor-poll HALASZTVA (DMM foglalt)"
        logger.log_event.assert_called_once_with("ERROR_QUEUE_POLL_DEFERRED", message)
        assert events == [{
            "event_code": "ERROR_QUEUE_POLL_DEFERRED", "event_message": message,
        }]
        resource.query.assert_not_called()
        dmm.release_measurement(owner)
        assert runner._poll_device_errors() == []
        assert resource.query.call_args.args == ("SYST:ERR?",)

    def test_deferred_status_preserves_real_error_and_safe_off_demote(self):
        from unittest.mock import MagicMock

        from Prog.src.test_runner import _critical_polled_errors

        runner, logger, _, events = _f26_mock_logger_runner(TestRunnerConfig())
        deferred = {
            "device": "DMM_V", "error": "ERROR_QUEUE_POLL_DEFERRED", "status": "HALASZTVA",
        }
        real_error = {
            "device": "PSU", "error": "OUTPUT_STATE_MISMATCH", "energy_state_uncertain": True,
        }
        runner._instruments.poll_device_errors = MagicMock(return_value=[deferred, real_error])
        device_errors = []
        runner.on_device_error = device_errors.append
        safe_off = {"PSU": True, "DMM_V": True}
        result = runner._poll_device_errors(safe_off)
        assert result == [deferred, real_error]
        assert safe_off == {"PSU": False, "DMM_V": True}
        assert runner._device_error_total_count == 1
        logger.log_device_error.assert_called_once_with("PSU", "OUTPUT_STATE_MISMATCH")
        assert len(device_errors) == 1 and device_errors[0]["device"] == "PSU"
        for kind in StepKind:
            assert _critical_polled_errors(TestStep(kind, "poll"), result) == [real_error]
        assert any(e["event_code"] == "ERROR_QUEUE_POLL_DEFERRED" for e in events)

    def test_deferred_event_logging_failure_does_not_become_fault(self):
        from unittest.mock import MagicMock

        from Prog.src.test_runner import _critical_polled_errors

        runner, logger, _, events = _f26_mock_logger_runner(TestRunnerConfig())
        deferred = {
            "device": "DMM_V", "error": "ERROR_QUEUE_POLL_DEFERRED", "status": "HALASZTVA",
        }
        runner._instruments.poll_device_errors = MagicMock(return_value=[deferred])
        logger.log_event.side_effect = OSError("mock disk full")
        result = runner._poll_device_errors()
        assert result == [deferred]
        assert _critical_polled_errors(TestStep(StepKind.CHARGE, "poll"), result) == []
        assert runner._device_error_total_count == 0
        assert events[0]["event_code"] == "ERROR_QUEUE_POLL_DEFERRED"


class TestR16B5bRunner:
    @staticmethod
    def make():
        from types import SimpleNamespace
        from unittest.mock import Mock

        from Prog.src.relax_controller import RelaxController
        from Prog.tests.test_relax_controller import _B5bRig

        rig = _B5bRig()
        ctrl = RelaxController(rig.dmm, RelaxConfig(min_relax_s=1.0))
        runner = TestRunner(
            instrument_manager=SimpleNamespace(dmm_voltage=rig.dmm),
            safety=Mock(), logger=Mock(), profile=Mock(),
            config=TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=Mock(), discharge_ctrl_factory=Mock(),
            relax_ctrl_factory=lambda step: ctrl,
        )
        runner._last_configured_nplc = None
        runner._nplc_set_failing = False
        runner._relax_sampler_controller = None
        runner._relax_sampling_service = None
        runner._relax_sampler_started = False
        runner._relax_totals_flush = None
        runner._logger = Mock()
        runner._safe_all_off_with_uncertainty_check = Mock(return_value={})
        runner.stop_requested = False
        runner.emergency_stop_requested = False
        runner.on_event = None
        return runner, ctrl, rig

    def test_nplc_ack_precedes_reset_ownership_and_worker_read(self, monkeypatch):
        from threading import get_ident

        import Prog.src.test_runner as module

        runner, ctrl, rig = self.make()
        step = TestStep(StepKind.RELAX, "relax")
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        assert rig.hardware.commands == []
        runner._configure_dmm_for_step(step)
        runner._prepare_relax_sampling(step, ctrl)
        rig.requests.get(timeout=2)
        try:
            commands = [command for command, _ in rig.hardware.commands]
            assert commands == [
                "VOLT:DC:NPLC 10;*OPC?",
                "ABOR;:SAMP:COUN 1;:SAMP:SOUR IMM;*OPC?",
                "READ?",
            ]
            assert rig.hardware.commands[0][1] == get_ident()
            assert rig.hardware.commands[-1][1] != get_ident()
            before = len(rig.hardware.starts)
            ctrl.advance(30.0)
            runner._reconfigure_dmm_nplc_for_relax_substate(step, ctrl)
            assert len(rig.hardware.starts) == before
        finally:
            rig.stop()
        runner._finish_relax_sampling()
        assert runner._relax_sampler_controller is None

    def test_failed_config_has_no_worker_and_retry_is_explicit(self, monkeypatch):
        import Prog.src.test_runner as module

        runner, ctrl, rig = self.make()
        step = TestStep(StepKind.RELAX, "relax")
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        original = rig.dmm.set_nplc

        def fail(nplc):
            raise RuntimeError("NPLC-hiba")

        monkeypatch.setattr(rig.dmm, "set_nplc", fail)
        runner._configure_dmm_for_step(step)
        runner._prepare_relax_sampling(step, ctrl)
        ctrl.advance(5.0)
        assert ctrl.sampling_mode == "SERVICE"
        assert not ctrl.dmm_valid
        assert rig.hardware.starts == []
        assert not runner._relax_sampler_started
        monkeypatch.setattr(rig.dmm, "set_nplc", original)
        runner._reconfigure_dmm_nplc_for_relax_substate(step, ctrl)
        runner._start_prepared_relax_sampling()
        rig.requests.get(timeout=2)
        rig.stop()
        runner._finish_relax_sampling()
        assert len(rig.hardware.starts) == 1

    @pytest.mark.parametrize("read_error", [RuntimeError("késői válasz"), TimeoutError("DMM-timeout")])
    def test_shutdown_tick_gates_configuration_and_next_step(self, monkeypatch, read_error):
        from threading import Event, get_ident

        import Prog.src.test_runner as module
        from Prog.src.relax_controller import RelaxSamplingService

        runner, ctrl, rig = self.make()
        entered, proceed = Event(), Event()
        forwarded = []

        def delayed_error():
            entered.set()
            assert proceed.wait(2)
            raise read_error

        rig.hardware.read_hook = delayed_error
        service = RelaxSamplingService(rig.dmm, clock=rig.clock)
        rig.service = service
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: service)
        step = TestStep(StepKind.RELAX, "relax")
        runner._configure_dmm_for_step(step)
        runner._prepare_relax_sampling(step, ctrl)
        # A runner eseményútja a GUI-worker által is használt on_event.
        ctrl.on_event = lambda event: runner.on_event(event)
        runner.on_event = lambda event: forwarded.append((event, get_ident()))
        try:
            assert entered.wait(2)
            ctrl.request_sampling_stop()
            assert not runner.poll_relax_sampling_shutdown()
            with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
                runner._configure_dmm_for_step(TestStep(StepKind.CHARGE, "következő"))
            with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
                runner._run_step(1, TestStep(StepKind.DISCHARGE, "következő"))
            assert len(rig.hardware.commands) == 3
        finally:
            proceed.set()
            service._thread.join(2)
        assert runner.poll_relax_sampling_shutdown()
        assert ctrl.dmm_fault_count == 1
        faults = [item for item in forwarded if item[0]["event_code"] == "RELAX_DMM_FAULT"]
        assert len(faults) == 1
        assert str(read_error) in faults[0][0]["event_message"]
        assert faults[0][1] == get_ident()
        critical = [event for event, _ in forwarded if event["event_code"] == "RELAX_RELEASE_FAILED"]
        assert critical and all(event["severity"] == "CRITICAL" for event in critical)
        runner._configure_dmm_for_step(TestStep(StepKind.CHARGE, "következő"))
        assert rig.hardware.nplc == 1.0

    def test_release_failure_cannot_ack_or_configure(self, monkeypatch):
        runner, ctrl, rig = self.make()
        rig.start()
        ctrl.attach_sampling_service(rig.service)
        runner._relax_sampler_controller = ctrl
        runner._relax_sampling_service = rig.service
        runner._relax_sampler_started = True

        def fail_release(owner):
            raise RuntimeError("felszabadítási hiba")

        original = rig.dmm.release_measurement
        monkeypatch.setattr(rig.dmm, "release_measurement", fail_release)
        rig.stop()
        with pytest.raises(RuntimeError, match="RELEASE_ERROR"):
            runner.poll_relax_sampling_shutdown()
        with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
            runner._configure_dmm_for_step(TestStep(StepKind.CHARGE, "következő"))
        # Csak a fake tulajdonjogát állítjuk helyre a teszt végén.
        original(rig.service._owner)

    def test_closing_loop_polls_and_safes_off_once_without_join(self, monkeypatch):
        from unittest.mock import Mock

        import Prog.src.test_runner as module

        runner, ctrl, rig = self.make()
        ctrl.attach_sampling_service(rig.service)
        runner._relax_sampler_controller = ctrl
        runner.stop_requested = True
        runner.poll_relax_sampling_shutdown = Mock(side_effect=[False, False, True])
        waits = []
        monkeypatch.setattr(module.time, "sleep", waits.append)
        runner._finish_relax_sampling()
        assert waits == [0.01, 0.01]
        runner._safe_all_off_with_uncertainty_check.assert_called_once()

    def test_run_step_finally_closes_on_unexpected_exception(self, monkeypatch):
        # A valós _run_step_body finally ágát futtatjuk, fájlos logger nélkül.
        from unittest.mock import Mock

        import Prog.src.test_runner as module

        runner, ctrl, rig = self.make()
        runner._active_plan = None
        runner.on_step_changed = None
        runner._controller_for_step = lambda step: ctrl
        runner._total_charge_ah = runner._total_discharge_ah = 0.0
        runner._total_charge_wh = runner._total_discharge_wh = 0.0
        runner._total_extreme_dt_s_count = runner._total_relax_dmm_fault_count = 0
        runner._total_temperature_out_of_range_count = runner._total_temp_dmm_fault_ticks = 0
        runner._max_observed_dt_s = 0.0
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)

        def fail_after_start(controller):
            rig.requests.get(timeout=2)
            # Szálvég nélküli stopot a finally pollja fejez be.
            rig.permits.put(False)
            raise ArithmeticError("controller-programhiba")

        runner._is_finished = fail_after_start
        closed = Mock(wraps=runner._finish_relax_sampling)
        runner._finish_relax_sampling = closed
        with pytest.raises(ArithmeticError, match="programhiba"):
            runner._run_step(0, TestStep(StepKind.RELAX, "relax"))
        assert closed.call_count == 1
        assert ctrl.sampling_shutdown_complete
        assert runner._relax_sampler_controller is None
        assert runner._total_relax_dmm_fault_count == 0

    @pytest.mark.parametrize("terminal", ["DONE", "FAULT", "STOP", "ABORT", "exception"])
    def test_real_step_terminal_paths_release_before_result_and_logger_close(self, monkeypatch, terminal):

        import Prog.src.test_runner as module

        runner, ctrl, rig = self.make()
        runner._active_plan = None
        runner._build_sample = lambda *args, **kwargs: {}
        runner._should_poll_device_errors = lambda: False
        runner._poll_device_errors = lambda *args: []
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        monkeypatch.setattr(module.time, "perf_counter", rig.clock)
        original_advance = ctrl.advance
        ticks = []

        def advance(dt_s):
            if not ticks:
                rig.requests.get(timeout=2)
            original_advance(dt_s)
            rig.step()
            ticks.append(dt_s)
            if len(ticks) == 1:
                if terminal == "STOP":
                    runner.request_stop()
                elif terminal == "ABORT":
                    runner.request_emergency_stop("USER_ABORT")
                elif terminal == "exception":
                    raise ArithmeticError("programhiba")

        ctrl.advance = advance
        runner._controller_faulted = lambda controller: terminal == "FAULT" and bool(ticks)

        def pump_close(delay):
            assert delay == 0.01
            rig.permits.put(False)
            rig.service._thread.join(2)
            assert not rig.service._thread.is_alive()

        monkeypatch.setattr(module.time, "sleep", pump_close)
        close_order = []
        runner._logger.close.side_effect = lambda: close_order.append(ctrl.sampling_shutdown_complete)
        if terminal == "exception":
            with pytest.raises(ArithmeticError, match="programhiba"):
                runner._run_step(0, TestStep(StepKind.RELAX, "relax"))
        else:
            result = runner._run_step(0, TestStep(StepKind.RELAX, "relax"))
            expected = {"DONE": "DONE", "FAULT": "FAULT", "STOP": "STOPPED", "ABORT": "FAULT"}[terminal]
            assert result.status == expected
        assert ctrl.sampling_shutdown_complete
        assert runner._relax_sampler_controller is None
        # A közös terminális lezáró most a közvetlen FAULT-returnt is lezárja.
        assert close_order == ([True] if terminal in ("FAULT", "STOP", "ABORT") else [])
        assert runner._relax_totals_flush is None
        if terminal in ("STOP", "ABORT"):
            assert runner._safe_all_off_with_uncertainty_check.called

    def test_late_inflight_error_is_in_final_step_result(self, monkeypatch):
        from threading import Event

        import Prog.src.test_runner as module
        from Prog.src.relax_controller import RelaxSamplingService

        runner, ctrl, rig = self.make()
        ctrl._config.min_relax_s = 0.0
        runner._active_plan = None
        runner._build_sample = lambda *args, **kwargs: {}
        runner._should_poll_device_errors = lambda: False
        entered, proceed = Event(), Event()
        service = RelaxSamplingService(rig.dmm, clock=rig.clock)
        rig.service = service

        def late_error():
            entered.set()
            assert proceed.wait(2)
            raise RuntimeError("utolsó olvasás hibája")

        rig.hardware.read_hook = late_error
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: service)
        original_advance = ctrl.advance

        def advance(dt_s):
            assert entered.wait(2)
            return original_advance(dt_s)

        ctrl.advance = advance
        events = []
        ctrl.on_event = events.append

        def finish_read(delay):
            proceed.set()
            service._thread.join(2)
            assert not service._thread.is_alive()

        monkeypatch.setattr(module.time, "sleep", finish_read)
        try:
            result = runner._run_step(0, TestStep(StepKind.RELAX, "relax"))
        finally:
            proceed.set()
        assert result.status == "DONE"
        assert result.relax_dmm_fault_count == 1
        assert runner._total_relax_dmm_fault_count == 1
        assert "utolsó olvasás hibája" in events[0]["event_message"]
        assert ctrl.sampling_shutdown_complete


class TestR16B5bIntegration:
    @pytest.mark.parametrize("tick", [5.0, 30.0])
    def test_j1_real_runner_controller_and_independent_poll_pause(self, monkeypatch, tick):
        import Prog.src.test_runner as module

        runner, ctrl, rig = TestR16B5bRunner.make()
        ctrl._config.min_relax_s = 90.0
        runner._config.runner_tick_s = tick
        runner._active_plan = None
        runner._build_sample = lambda *a, **k: {}
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        monkeypatch.setattr(module.time, "perf_counter", rig.clock)
        original_advance = ctrl.advance
        checked = []
        next_deadline = rig.clock() + 1.5
        initialized = False

        def sample_until(target):
            nonlocal next_deadline
            while next_deadline <= target:
                delay = rig.step(next_deadline - rig.clock())
                next_deadline = rig.clock() + delay
            rig.clock.now = target

        def advance(dt_s):
            nonlocal initialized
            if not initialized:
                rig.requests.get(timeout=2)
                initialized = True
            sample_until(rig.clock() + tick)
            state = original_advance(dt_s)
            if rig.clock() >= 1060.0:
                checked.append(ctrl.dvdt_valid)
            return state

        pauses = iter([2.0, 7.0])
        def poll(*a):
            pause = next(pauses, 0.0)
            before = len(rig.hardware.starts)
            sample_until(rig.clock() + pause)
            if pause:
                assert len(rig.hardware.starts) > before
            return []

        ctrl.advance = advance
        runner._should_poll_device_errors = lambda: True
        runner._poll_device_errors = poll

        def finish_read(delay):
            rig.permits.put(False)
            rig.service._thread.join(2)
            assert not rig.service._thread.is_alive()
        monkeypatch.setattr(module.time, "sleep", finish_read)
        result = runner._run_step(0, TestStep(StepKind.RELAX, "relax"))
        assert result.status == "DONE"
        assert checked and all(checked)
        assert ctrl.dvdt_V_per_s == pytest.approx(1e-6, abs=1e-12)
        assert ctrl.sampling_shutdown_complete
        assert ctrl.dmm_fault_count == 0

    def test_owned_read_all_finishes_other_channels_and_cache_is_explicit(self):
        from unittest.mock import Mock

        from Prog.src.instrument_manager import InstrumentManager

        _runner, ctrl, rig = TestR16B5bRunner.make()
        ctrl.attach_sampling_service(rig.service)
        try:
            rig.start()
            before = len(rig.hardware.starts)
            manager = InstrumentManager(Mock(), Mock(), rig.dmm, Mock())
            readings = manager.read_all()
            assert readings["battery_voltage_V"] is None
            assert readings["battery_voltage_V_skipped"]
            assert readings["battery_voltage_V_quality"] == "OWNED_DEFERRED"
            assert readings["load_readback_voltage_V"] is not None
            ctrl.advance(5.0)
            assert ctrl.dmm_valid
            assert ctrl.voltage_sample_age_s == 0.0
            assert ctrl.voltage_sample_quality == "CACHE_FRESH"
            assert len(rig.hardware.starts) == before
            rig.clock.now += 4.01
            ctrl.advance(5.0)
            assert not ctrl.dmm_valid
            assert ctrl.voltage_sample_quality == "STALE"
            assert ctrl.dmm_fault_count == 0
        finally:
            rig.stop()

    def test_heartbeat_and_release_timeout_are_critical_and_keep_gate(self, monkeypatch):
        import Prog.src.test_runner as module
        runner, ctrl, rig = TestR16B5bRunner.make()
        ctrl.attach_sampling_service(rig.service)
        runner._relax_sampler_controller = ctrl
        runner._relax_sampling_service = rig.service
        runner._relax_sampler_started = True
        events = []
        runner.on_event = events.append
        rig.clock.now += rig.service.shutdown_timeout_s + 1
        with pytest.raises(RuntimeError, match="HEARTBEAT_LOST"):
            runner._start_prepared_relax_sampling()
        runner.poll_relax_sampling_shutdown = lambda: False
        times = iter([0.0, rig.service.shutdown_timeout_s + 1])
        monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
        with pytest.raises(RuntimeError, match="RELEASE_TIMEOUT"):
            runner._finish_relax_sampling()
        with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
            runner._configure_dmm_for_step(TestStep(StepKind.DISCHARGE, "tiltott"))
        assert len(events) == 3
        assert all(event["severity"] == "CRITICAL" for event in events)
        assert rig.hardware.commands == []

    def test_deferred_event_is_transition_based_and_duration_is_counted(self, monkeypatch):
        import Prog.src.test_runner as module
        runner, _, _ = TestR16B5bRunner.make()
        deferred = {"device": "DMM_V", "status": "HALASZTVA", "error": "foglalt"}
        runner._instruments.poll_device_errors = lambda **k: [deferred]
        now = [10.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
        events = []
        runner.on_event = events.append
        for second in [10.0, 15.0, 25.0]:
            now[0] = second
            assert runner._poll_device_errors() == [deferred]
        assert len(events) == 1
        assert runner._device_error_total_count == 0
        assert runner.error_queue_deferred_s == {"DMM_V": 15.0}
        runner._instruments.poll_device_errors = lambda **k: []
        now[0] = 30.0
        runner._poll_device_errors()
        assert runner.error_queue_deferred_s == {"DMM_V": 20.0}
        runner._instruments.poll_device_errors = lambda **k: [deferred]
        runner._poll_device_errors()
        assert len(events) == 2


    def test_pulse_after_release_has_only_pulse_owner_during_chunk(self, monkeypatch):
        from unittest.mock import Mock

        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.pulse_measurement_service import PulseMeasurementService
        from Prog.src.relax_controller import RelaxController, RelaxSamplingService
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad

        runner, _, _ = TestR16B5bRunner.make()
        dmm = MockDMM()
        service = RelaxSamplingService(dmm)
        ctrl = RelaxController(dmm, RelaxConfig())
        ctrl.attach_sampling_service(service)
        owner = object()
        dmm.acquire_measurement(owner)
        runner._relax_sampler_controller = ctrl
        runner._relax_sampling_service = service
        pulse = PulseMeasurementService(
            MockLoad(), dmm, 10.8, sample_interval_s=1.0,
            safety=SafetyManager(
                profile=BatteryProfile(
                    battery_name="TEST", manufacturer="FIAMM", model="FG20721",
                    nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.2,
                ), psu_mode=PsuMode.INDEPENDENT,
            ),
            max_voltage_V=15.0, max_load_power_W=100.0,
        )
        call = Mock(wraps=pulse.begin_pulse)
        runner._controller_for_step = lambda step: call(0.1, 1.0)
        runner._instruments = Mock()
        runner._instruments.dmm_voltage = dmm
        with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
            runner._run_step(1, TestStep(StepKind.OCV_SOC, "pulse"))
        call.assert_not_called()
        dmm.release_measurement(owner)
        service.stop()
        assert runner.poll_relax_sampling_shutdown()
        assert dmm._measurement_owner is None
        observed = []
        for name in [
            "read_voltage", "configure_timed_acquisition", "arm_timed_acquisition",
            "fetch_timed_acquisition", "reset_to_single_sample", "check_error",
        ]:
            original = getattr(dmm, name)
            def checked(*a, _name=name, _method=original, **k):
                assert dmm._measurement_owner is not owner
                if _name == "read_voltage":
                    assert dmm._measurement_owner is None
                else:
                    assert dmm._measurement_owner is not None
                    assert k["owner"] is dmm._measurement_owner
                observed.append(_name)
                return _method(*a, **k)
            monkeypatch.setattr(dmm, name, checked)
        assert pulse.begin_pulse(0.1, 1.0)
        pulse.step_chunk()
        pulse.finalize()
        assert "fetch_timed_acquisition" in observed
        assert "reset_to_single_sample" in observed
        assert dmm._measurement_owner is None

    def test_cleanup_error_does_not_replace_original_program_error(self, monkeypatch):
        import Prog.src.test_runner as module

        runner, _ctrl, rig = TestR16B5bRunner.make()
        runner._active_plan = None
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        original = ArithmeticError("eredeti becslőhiba")
        def fail(controller):
            rig.requests.get(timeout=2)
            raise original
        runner._is_finished = fail
        finish = runner._finish_relax_sampling
        def failing_release():
            rig.stop()
            rig.service._shutdown_error = "RELEASE_ERROR: másodlagos hiba"
            return finish()
        runner._finish_relax_sampling = failing_release
        with pytest.raises(ArithmeticError) as caught:
            runner._run_step(0, TestStep(StepKind.RELAX, "relax"))
        assert caught.value is original
        runner._logger.log_event.assert_any_call(
            "RELAX_RELEASE_FAILED", "RELEASE_ERROR: másodlagos hiba", is_critical=True,
        )

    def test_new_runner_also_rejects_an_existing_owner_before_factory(self):
        from unittest.mock import Mock

        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        runner, _, _ = TestR16B5bRunner.make()
        dmm = MockDMM()
        runner._instruments.dmm_voltage = dmm
        owner = object()
        dmm.acquire_measurement(owner)
        factory = Mock()
        runner._controller_for_step = factory
        events = []
        runner.on_event = events.append
        try:
            with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
                runner._run_step(0, TestStep(StepKind.DISCHARGE, "tiltott"))
            factory.assert_not_called()
            assert dmm.calls == []
            assert events[0]["severity"] == "CRITICAL"
        finally:
            dmm.release_measurement(owner)


# R16-B5bF2: kizárólag memóriabeli napló és mockolt műszertransport.
class TestR16B5bF2:
    @staticmethod
    def stuck_run(monkeypatch, stop_kind, *, interrupt=False):
        from copy import deepcopy
        from threading import Event

        import Prog.src.test_runner as module
        from Prog.src.relax_controller import RelaxSamplingService

        runner, ctrl, rig = TestR16B5bRunner.make()
        ctrl._config.min_relax_s = 100.0
        entered, proceed = Event(), Event()
        service = RelaxSamplingService(rig.dmm, clock=rig.clock)
        service.shutdown_timeout_s = 0.02
        checkpoints, events, sleeps = [], [], []
        runner.on_event = events.append
        runner._logger.write_checkpoint.side_effect = lambda data: checkpoints.append(deepcopy(data))
        runner._poll_device_errors = lambda *args: []
        runner._should_poll_device_errors = lambda: False
        runner._build_sample = lambda *args, **kwargs: {}
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: service)
        monkeypatch.setattr(module.time, "monotonic", rig.clock)
        monkeypatch.setattr(module.time, "perf_counter", rig.clock)

        def blocked_read():
            entered.set()
            assert proceed.wait(2)

        def clock_sleep(delay):
            sleeps.append(delay)
            if interrupt:
                raise KeyboardInterrupt("lezárás közbeni Ctrl+C")
            rig.clock.now += delay

        rig.hardware.read_hook = blocked_read
        monkeypatch.setattr(module.time, "sleep", clock_sleep)
        original_advance = ctrl.advance

        def advance(dt_s):
            assert entered.wait(2)
            original_advance(dt_s)
            if stop_kind == "emergency":
                runner.request_emergency_stop("EREDETI_VESZ_OK")
            else:
                runner.request_stop()

        ctrl.advance = advance
        plan = TestPlan(TestType.CHARACTERIZATION, (
            TestStep(StepKind.RELAX, "relax"), TestStep(StepKind.DISCHARGE, "tiltott"),
        ))
        try:
            try:
                result = runner.run(plan)
            except BaseException as exc:  # noqa: BLE001 -- a Ctrl+C is tesztbukás, nem pytest-megszakítás
                pytest.fail(f"A terminális ok elveszett: {type(exc).__name__}: {exc}")
            reason = "EREDETI_VESZ_OK" if stop_kind == "emergency" else "USER_STOP_REQUESTED"
            assert result.status == "FAULT"
            # Az alapok elsőbbsége megmarad; minden release-hiba egyszeres utótag.
            failure = (
                "KeyboardInterrupt: lezárás közbeni Ctrl+C" if interrupt else
                "RuntimeError: RELAX_SAMPLING_RELEASE_TIMEOUT"
            )
            assert result.reason == f"{reason}; RELAX_RELEASE_FAILED: {failure}"
            assert checkpoints[-1]["reason"] == result.reason
            assert sum(c.args[0] == "EMERGENCY_STOP" for c in runner._logger.log_event.call_args_list) == 1
            assert sum(ev["event_code"] == "EMERGENCY_STOP" for ev in events) == 1
            assert sleeps == ([0.01] if interrupt else [0.01, 0.01])
            runner._safe_all_off_with_uncertainty_check.assert_called_once()
            assert not ctrl.sampling_shutdown_complete
            with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
                runner._run_step(1, plan.steps[1])
            runner._discharge_ctrl_factory.assert_not_called()
            before = list(sleeps)
            with pytest.raises(RuntimeError):
                runner._finish_relax_sampling()
            assert sleeps == before
            return result, checkpoints[-1]
        finally:
            service.stop()
            proceed.set()
            if service._thread is not None:
                service._thread.join(2)
                assert not service._thread.is_alive()

    @pytest.mark.parametrize("stop_kind", ["emergency", "graceful"])
    @pytest.mark.parametrize("interrupt", [False, True])
    def test_terminal_release_waits_once_and_preserves_reason(self, monkeypatch, stop_kind, interrupt):
        from dataclasses import asdict

        from Prog.src.report_generator import ReportGenerator

        result, checkpoint = self.stuck_run(monkeypatch, stop_kind, interrupt=interrupt)
        report = ReportGenerator().generate(asdict(result))
        assert report["reason"] == checkpoint["reason"]

    def test_processed_record_identity_survives_worker_progress_before_logging(self, monkeypatch):
        from threading import Event, Thread

        runner, _logger, _dmm, _events = _f26_mock_logger_runner(TestRunnerConfig())
        _other, ctrl, rig = TestR16B5bRunner.make()
        runner._instruments._dmm_v = rig.dmm
        ctrl.attach_sampling_service(rig.service)
        drained, advanced = Event(), Event()
        batches = []
        original_drain = rig.service.drain

        def drain_with_gate():
            batch = original_drain()
            batches.append(batch)
            drained.set()
            assert advanced.wait(2)
            return batch

        def advance_worker():
            assert drained.wait(2)
            try:
                for _ in range(4):
                    rig.step()
            finally:
                advanced.set()

        try:
            rig.start()
            monkeypatch.setattr(rig.service, "drain", drain_with_gate)
            mover = Thread(target=advance_worker)
            mover.start()
            ctrl.advance(0.0)
            mover.join(2)
            assert not mover.is_alive()
            record = batches[0].records[-1]
            assert rig.service._latest.sequence > record.sequence
            sample = runner._build_sample(TestStep(StepKind.RELAX, "relax"), ctrl)
            assert sample["battery_voltage_V"] == record.voltage_V == ctrl.last_voltage_V
            assert not sample["dmm_voltage_valid"]
            assert ctrl.voltage_sample_age_s == 6.0
            assert ctrl.voltage_sample_quality == "STALE"
            assert not ctrl.dmm_valid
            assert ctrl._voltage_record is record
        finally:
            advanced.set()
            rig.stop()

    def test_sample_uses_one_snapshot_even_when_read_all_advances_time(self, monkeypatch):
        from unittest.mock import Mock

        runner, _logger, _dmm, _events = _f26_mock_logger_runner(TestRunnerConfig())
        _other, ctrl, rig = TestR16B5bRunner.make()
        ctrl.attach_sampling_service(rig.service)
        try:
            rig.start()
            ctrl.advance(0.0)
            snapshot = Mock(wraps=ctrl.voltage_snapshot)
            monkeypatch.setattr(ctrl, "voltage_snapshot", snapshot)

            def read_all(**kwargs):
                assert kwargs["skip_battery_voltage"]
                rig.clock.now += 6.0
                return {}

            monkeypatch.setattr(runner._instruments, "read_all", read_all)
            sample = runner._build_sample(TestStep(StepKind.RELAX, "relax"), ctrl)
            assert sample["dmm_voltage_valid"]
            assert sample["battery_voltage_V"] == ctrl.last_voltage_V
            assert snapshot.call_count == 1
            assert ctrl.voltage_sample_quality == "STALE"
        finally:
            rig.stop()

    def test_service_before_first_valid_record_logs_none_and_skips_dmm(self, monkeypatch):
        from unittest.mock import Mock

        runner, _logger, _dmm, _events = _f26_mock_logger_runner(TestRunnerConfig())
        _other, ctrl, rig = TestR16B5bRunner.make()
        ctrl.attach_sampling_service(rig.service)
        read_all = Mock(return_value={"battery_voltage_V": 99.0})
        monkeypatch.setattr(runner._instruments, "read_all", read_all)
        sample = runner._build_sample(TestStep(StepKind.RELAX, "relax"), ctrl)
        assert sample["battery_voltage_V"] is None
        assert not sample["dmm_voltage_valid"]
        assert read_all.call_args.kwargs["skip_battery_voltage"]
        assert ctrl.u_batt_V is None
        assert ctrl.voltage_sample_age_s is None
        assert ctrl.voltage_sample_quality == "NO_DATA"

    def test_outer_except_does_not_hide_release_failure(self, monkeypatch):
        import Prog.src.test_runner as module

        runner, _ctrl, rig = TestR16B5bRunner.make()
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        # A normál DONE út kizárólag a finally lezárásával hibázik.
        runner._is_finished = lambda controller: True

        def fail_finish():
            rig.stop()
            raise RuntimeError("valódi lezárási hiba")

        runner._finish_relax_sampling = fail_finish
        try:
            raise ValueError("külső kezelt hiba")
        except ValueError:
            with pytest.raises(RuntimeError, match="valódi lezárási hiba"):
                runner._run_step(0, TestStep(StepKind.RELAX, "relax"))

    def test_run_copies_deferred_totals_into_result(self, monkeypatch):
        import Prog.src.test_runner as module

        runner, logger, _dmm, _events = _f26_mock_logger_runner(TestRunnerConfig())
        now = [100.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
        runner._instruments.poll_device_errors = lambda **kwargs: [
            {"device": "DMM_V", "status": "HALASZTVA", "error": "foglalt"},
        ]
        runner._poll_device_errors()
        now[0] += 5.0
        result = runner.run(TestPlan.charge_only(), start_step_index=-1)
        assert result.error_queue_deferred_s == {"DMM_V": 5.0}
        now[0] += 20.0
        assert result.error_queue_deferred_s == {"DMM_V": 5.0}
        result.error_queue_deferred_s["DMM_V"] = 999.0
        assert runner.error_queue_deferred_s == {"DMM_V": 25.0}
        logger.close.assert_called_once()


    def test_resume_deferred_seed_is_copied_once_without_offline_time(self, monkeypatch):
        import Prog.src.test_runner as module

        inherited = {"DMM_V": 120.0}
        config = TestRunnerConfig(initial_error_queue_deferred_s=inherited)
        runner, logger, _dmm, _events = _f26_mock_logger_runner(config)
        inherited["DMM_V"] = 900.0
        now = [100000.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
        runner._instruments.poll_device_errors = lambda **kwargs: [
            {"device": "DMM_V", "status": "HALASZTVA", "error": "foglalt"},
        ]
        assert runner.error_queue_deferred_s == {"DMM_V": 120.0}
        runner._poll_device_errors()
        now[0] += 5.0
        runner.request_stop()
        result = runner.run(TestPlan.charge_only())
        assert result.error_queue_deferred_s == {"DMM_V": 125.0}
        assert logger.write_checkpoint.call_args.args[0]["error_queue_deferred_s"] == {"DMM_V": 125.0}
        now[0] += 5.0
        second = runner.run(TestPlan.charge_only())
        assert second.error_queue_deferred_s == {"DMM_V": 130.0}


    def test_deferred_logging_failure_is_visible_on_the_live_channel(self):
        runner, logger, _dmm, events = _f26_mock_logger_runner(TestRunnerConfig())
        runner._instruments.poll_device_errors = lambda **kwargs: [
            {"device": "DMM_V", "status": "HALASZTVA", "error": "foglalt"},
        ]
        logger.log_event.side_effect = OSError("memóriabeli naplóhiba")
        runner._poll_device_errors()
        assert [ev["event_code"] for ev in events] == [
            "ERROR_QUEUE_POLL_DEFERRED", "ERROR_QUEUE_POLL_DEFERRED_LOGGING_FAILED",
        ]
        assert runner._device_error_total_count == 0

    def test_mock_dmm_rejects_foreign_read_without_consuming_measurement(self):
        from Prog.drivers.device_dmm import DMMMeasurementBusy
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        dmm = MockDMM(voltage_V=12.5)
        owner = object()
        dmm.acquire_measurement(owner)
        try:
            with pytest.raises(DMMMeasurementBusy):
                dmm.read_voltage()
            assert dmm.calls == []
            assert dmm.read_voltage(owner=owner) == 12.5
        finally:
            dmm.release_measurement(owner)

    @pytest.mark.parametrize("controller_fault", [False, True])
    def test_late_read_error_is_in_emergency_checkpoint_and_result(self, monkeypatch, controller_fault):
        from threading import Event

        import Prog.src.test_runner as module
        from Prog.src.relax_controller import RelaxSamplingService

        runner, ctrl, rig = TestR16B5bRunner.make()
        ctrl._config.min_relax_s = 100.0
        entered, proceed = Event(), Event()
        service = RelaxSamplingService(rig.dmm, clock=rig.clock)
        runner._build_sample = lambda *args, **kwargs: {}
        runner._poll_device_errors = lambda *args: []
        runner._should_poll_device_errors = lambda: False
        runner._controller_faulted = lambda controller: controller_fault
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: service)

        def late_error():
            entered.set()
            assert proceed.wait(2)
            raise RuntimeError("késői DMM-hiba")

        def advance(dt_s):
            assert entered.wait(2)
            if not controller_fault:
                runner.request_emergency_stop("EREDETI_OK")

        def finish_read(delay):
            proceed.set()
            service._thread.join(2)
            assert not service._thread.is_alive()

        rig.hardware.read_hook = late_error
        ctrl.advance = advance
        monkeypatch.setattr(module.time, "sleep", finish_read)
        try:
            result = runner._run_step(0, TestStep(StepKind.RELAX, "relax"))
            assert result.reason == ("CONTROLLER_FAULT" if controller_fault else "EREDETI_OK")
            assert result.relax_dmm_fault_count == 1
            if not controller_fault:
                checkpoint = runner._logger.write_checkpoint.call_args.args[0]
                assert checkpoint["relax_dmm_fault_count"] == 1
                assert checkpoint["reason"] == result.reason
        finally:
            service.stop()
            proceed.set()
            if service._thread is not None:
                service._thread.join(2)
                assert not service._thread.is_alive()


class TestR16B5bF3:
    @pytest.mark.parametrize("failure", [
        "PSU", "Load", "exception", "heartbeat", "controller", "last_tick_stop", "last_tick_abort",
    ])
    def test_fault_safe_off_precedes_blocked_read_release(self, monkeypatch, failure):
        from threading import Event, get_ident
        from unittest.mock import Mock

        import Prog.src.test_runner as module
        from Prog.src.relax_controller import RelaxSamplingService, RelaxState

        runner, ctrl, rig = TestR16B5bRunner.make()
        other, _logger, _dmm, _events = _f26_mock_logger_runner(TestRunnerConfig())
        runner._instruments = other._instruments
        runner._instruments._dmm_v = rig.dmm
        runner._safe_all_off_with_uncertainty_check = (
            TestRunner._safe_all_off_with_uncertainty_check.__get__(runner)
        )
        manager = runner._instruments
        safe_off = Mock(wraps=manager.safe_all_off)
        monkeypatch.setattr(manager, "safe_all_off", safe_off)
        order = []
        for name, device in (("Load", manager.load), ("PSU", manager.psu)):
            original = device.safe_off
            def off(name=name, original=original):
                order.append((name, get_ident()))
                return original()
            monkeypatch.setattr(device, "safe_off", off)
        entered, proceed = Event(), Event()
        service = RelaxSamplingService(rig.dmm, clock=rig.clock)
        ctrl._config.min_relax_s = 1000.0
        events, before_release = [], []
        runner.on_event = events.append
        runner._build_sample = lambda *args, **kwargs: {}
        runner._should_poll_device_errors = lambda: True
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: service)
        monkeypatch.setattr(module.time, "monotonic", rig.clock)

        def late_read_error():
            entered.set()
            assert proceed.wait(2)
            raise RuntimeError("késői DMM-hiba")

        original_advance = ctrl.advance
        def advance(dt_s):
            assert entered.wait(2)
            original_advance(dt_s)
            if failure == "exception":
                raise ArithmeticError("eredeti tick-hiba")
            if failure == "heartbeat":
                rig.clock.now += service.shutdown_timeout_s + 1.0
            if failure in ("last_tick_stop", "last_tick_abort"):
                ctrl._state = RelaxState.RELAX_DONE
                if failure == "last_tick_stop":
                    runner.request_stop()
                else:
                    runner.request_emergency_stop("EREDETI_GUI_ABORT")

        def poll(*args):
            if failure in ("PSU", "Load") and entered.is_set() and not safe_off.called:
                return [{
                    "device": failure, "error": "actual ON / commanded OFF",
                    "energy_state_uncertain": True,
                }]
            return []

        def release_read(delay):
            # A worker csak itt léphet tovább; az OFF már előtte kötelező.
            before_release.append(safe_off.call_count)
            proceed.set()
            service._thread.join(2)
            assert not service._thread.is_alive()

        rig.hardware.read_hook = late_read_error
        ctrl.advance = advance
        runner._poll_device_errors = poll
        runner._controller_faulted = lambda controller: failure == "controller"
        monkeypatch.setattr(module.time, "sleep", release_read)
        plan = TestPlan(TestType.CHARACTERIZATION, (
            TestStep(StepKind.RELAX, "relax"), TestStep(StepKind.DISCHARGE, "tiltott"),
        ))
        try:
            result = runner.run(plan)
            assert before_release == [1]
            safe_off.assert_called_once()
            assert order == [("Load", get_ident()), ("PSU", get_ident())]
            assert result.status == ("STOPPED" if failure == "last_tick_stop" else "FAULT")
            expected = {
                "exception": "UNHANDLED_EXCEPTION: eredeti tick-hiba",
                "heartbeat": "UNHANDLED_EXCEPTION: RELAX_SAMPLING_HEARTBEAT_LOST",
                "controller": "CONTROLLER_FAULT",
                "last_tick_stop": "USER_STOP_REQUESTED",
                "last_tick_abort": "EREDETI_GUI_ABORT",
            }.get(failure, f"SCPI_CRITICAL_ERROR[{failure}]: actual ON / commanded OFF")
            assert result.reason == expected
            checkpoint = runner._logger.write_checkpoint.call_args.args[0]
            assert checkpoint["reason"] == expected
            assert result.safe_off_attempted
            assert result.safe_off_result["Load"] and result.safe_off_result["PSU"]
            assert checkpoint["relax_dmm_fault_count"] == result.relax_dmm_fault_count == 1
            terminal_code = "GRACEFUL_STOP" if failure == "last_tick_stop" else "EMERGENCY_STOP"
            assert len(_f26_log_calls(runner._logger, terminal_code)) == 1
            assert sum(e["event_code"] == terminal_code for e in events) == 1
            runner._discharge_ctrl_factory.assert_not_called()
        finally:
            service.stop()
            proceed.set()
            if service._thread is not None:
                service._thread.join(2)
                assert not service._thread.is_alive()

    def test_deadline_totals_include_final_drain_once_and_resume_seed(self, monkeypatch):
        import Prog.src.test_runner as module

        runner, _ctrl, rig = TestR16B5bRunner.make()
        runner._total_relax_missed_deadline_count = 7
        runner._active_plan = None
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        # A végső rekordokat a lezáró drain kapja, nem egy runner-tick.
        def finished(controller):
            rig.requests.get(timeout=2)
            rig.service._publish(rig.clock.now, rig.clock.now, None, "DEADLINE_MISSED", 3)
            return True
        runner._is_finished = finished
        monkeypatch.setattr(module.time, "sleep", lambda delay: rig.stop())
        try:
            result = runner._run_step(0, TestStep(StepKind.RELAX, "relax"))
            assert result.relax_missed_deadline_count == 10
            assert runner._checkpoint_summary_fields()["relax_missed_deadline_count"] == 10
            assert runner._total_relax_dmm_fault_count == 0
        finally:
            if rig.service._thread is not None and rig.service._thread.is_alive():
                rig.stop()

    def test_deadline_seed_survives_repeated_run_and_invalid_start(self):
        from dataclasses import asdict

        from Prog.src.report_generator import ReportGenerator

        runner, logger, _dmm, _events = _f26_mock_logger_runner(
            TestRunnerConfig(initial_relax_missed_deadline_count=7),
        )
        runner.request_stop()
        for _ in range(2):
            result = runner.run(TestPlan.charge_only())
            assert result.relax_missed_deadline_count == 7
            assert logger.write_checkpoint.call_args.args[0]["relax_missed_deadline_count"] == 7
        invalid = runner.run(TestPlan.charge_only(), start_step_index=-1)
        assert invalid.relax_missed_deadline_count == 7
        assert ReportGenerator().generate(asdict(invalid))["relax_missed_deadline_count"] == 7
        assert ReportGenerator().generate({})["relax_missed_deadline_count"] == 0


class TestR16B5bF4:
    @staticmethod
    def done_drain_run(monkeypatch, stop_kind, *, primary=None):
        from threading import Event, get_ident
        from unittest.mock import Mock

        import Prog.src.test_runner as module
        from Prog.src.relax_controller import RelaxSamplingService, RelaxState

        runner, ctrl, rig = TestR16B5bRunner.make()
        other, _logger, _dmm, _events = _f26_mock_logger_runner(TestRunnerConfig())
        runner._instruments = other._instruments
        runner._instruments._dmm_v = rig.dmm
        runner._safe_all_off_with_uncertainty_check = (
            TestRunner._safe_all_off_with_uncertainty_check.__get__(runner)
        )
        manager = runner._instruments
        safe_off = Mock(wraps=manager.safe_all_off)
        monkeypatch.setattr(manager, "safe_all_off", safe_off)
        order = []
        for name, device in (("Load", manager.load), ("PSU", manager.psu)):
            original = device.safe_off
            def off(name=name, original=original):
                assert not proceed.is_set()
                order.append((name, get_ident(), rig.clock()))
                return original()
            monkeypatch.setattr(device, "safe_off", off)

        entered, proceed = Event(), Event()
        service = RelaxSamplingService(rig.dmm, clock=rig.clock)
        service.shutdown_timeout_s = 0.20
        ctrl._config.min_relax_s = 1000.0
        runner._build_sample = lambda *args, **kwargs: {}
        runner._should_poll_device_errors = lambda: False
        runner._poll_device_errors = lambda *args: []
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: service)
        monkeypatch.setattr(module.time, "monotonic", rig.clock)
        monkeypatch.setattr(module.time, "perf_counter", rig.clock)

        def blocked_read():
            entered.set()
            assert proceed.wait(2)

        rig.hardware.read_hook = blocked_read
        original_advance = ctrl.advance
        def advance(dt_s):
            assert entered.wait(2)
            original_advance(dt_s)
            ctrl._state = RelaxState.RELAX_DONE
            if primary == "tick":
                raise ArithmeticError("EREDETI_TICK_HIBA")

        ctrl.advance = advance
        runner._controller_faulted = lambda controller: primary == "fault"
        sleeps, requested_at = [], []
        def drain_sleep(delay):
            assert delay == 0.01
            assert entered.is_set() and not ctrl.sampling_shutdown_complete
            if not sleeps:
                # A kérés csak a normál DONE-drain megkezdése UTÁN érkezik.
                assert ctrl.state == RelaxState.RELAX_DONE
                assert safe_off.call_count == (0 if primary is None else 1)
                requested_at.append(rig.clock())
                if stop_kind == "graceful":
                    runner.request_stop()
                else:
                    runner.request_emergency_stop("GUI_ABORT_OK")
                    # Egyidejű STOP mellett is a vészok az elsődleges.
                    runner.request_stop()
            sleeps.append(delay)
            rig.clock.now = round(rig.clock.now + delay, 6)

        monkeypatch.setattr(module.time, "sleep", drain_sleep)
        plan = TestPlan(TestType.CHARACTERIZATION, (
            TestStep(StepKind.RELAX, "relax"), TestStep(StepKind.DISCHARGE, "tiltott"),
        ))
        try:
            result = runner.run(plan)
            assert result.status == "FAULT"
            if primary == "tick":
                expected = "UNHANDLED_EXCEPTION: EREDETI_TICK_HIBA"
            elif primary == "fault":
                expected = "CONTROLLER_FAULT"
            elif stop_kind == "emergency":
                expected = "GUI_ABORT_OK"
            else:
                expected = "USER_STOP_REQUESTED"
            # A release-diagnózis minden alapokhoz ugyanúgy és egyszer kapcsolódik.
            expected += "; RELAX_RELEASE_FAILED: RuntimeError: RELAX_SAMPLING_RELEASE_TIMEOUT"
            assert result.reason == expected, (result.reason, expected)
            checkpoint = runner._logger.write_checkpoint.call_args.args[0]
            assert checkpoint["reason"] == expected
            assert len(_f26_log_calls(runner._logger, "EMERGENCY_STOP")) == 1
            assert not _f26_log_calls(runner._logger, "GRACEFUL_STOP")
            assert any(
                "RELAX_SAMPLING_RELEASE_TIMEOUT" in str(call)
                for call in _f26_log_calls(runner._logger, "RELAX_RELEASE_FAILED")
            )
            safe_off.assert_called_once()
            assert [(name, thread) for name, thread, _ in order] == [
                ("Load", get_ident()), ("PSU", get_ident()),
            ]
            assert order[0][2] - requested_at[0] <= 0.011
            assert len(sleeps) == 20
            assert result.safe_off_attempted
            assert result.safe_off_result["Load"] and result.safe_off_result["PSU"]
            assert runner._pending_relax_safe_off_result is None
            assert not ctrl.sampling_shutdown_complete
            runner._discharge_ctrl_factory.assert_not_called()
            return result, checkpoint
        finally:
            service.stop()
            proceed.set()
            if service._thread is not None:
                service._thread.join(2)
                assert not service._thread.is_alive()

    @pytest.mark.parametrize("stop_kind", ["emergency", "graceful"])
    def test_done_drain_timeout_preserves_operator_reason(self, monkeypatch, stop_kind):
        self.done_drain_run(monkeypatch, stop_kind)

    @pytest.mark.parametrize("primary", ["tick", "fault"])
    @pytest.mark.parametrize("stop_kind", ["emergency", "graceful"])
    def test_done_drain_request_does_not_replace_primary_reason(self, monkeypatch, primary, stop_kind):
        self.done_drain_run(monkeypatch, stop_kind, primary=primary)

    def test_terminal_safe_off_consumes_pending_result_once(self):
        from unittest.mock import Mock

        runner, _ctrl, _rig = TestR16B5bRunner.make()
        pending = {"Load": False, "PSU": True}
        fresh = {"Load": True, "PSU": False}
        runner._pending_relax_safe_off_result = pending
        runner._safe_all_off_with_uncertainty_check = Mock(return_value=fresh)
        assert runner._terminal_safe_off() is pending
        assert runner._pending_relax_safe_off_result is None
        runner._safe_all_off_with_uncertainty_check.assert_not_called()
        # A következő terminális ág friss OFF-ot kér, nem a régi nyugtát.
        assert runner._terminal_safe_off() is fresh
        runner._safe_all_off_with_uncertainty_check.assert_called_once()

    @pytest.mark.parametrize("stop_kind", ["emergency", "graceful"])
    def test_prefinish_off_and_stop_flag_do_not_repeat_off(self, monkeypatch, stop_kind):
        from unittest.mock import Mock

        import Prog.src.test_runner as module

        runner, ctrl, rig = TestR16B5bRunner.make()
        runner._relax_sampler_controller = ctrl
        runner._relax_sampling_service = rig.service
        # A finally már OFF-olt; a drain belépésekor a stop is látható.
        pending = runner._safe_all_off_with_uncertainty_check()
        runner._pending_relax_safe_off_result = pending
        if stop_kind == "emergency":
            runner.request_emergency_stop("GUI_ABORT_OK")
        else:
            runner.request_stop()
        monkeypatch.setattr(ctrl, "request_sampling_stop", Mock())
        monkeypatch.setattr(runner, "poll_relax_sampling_shutdown", Mock(side_effect=[False, False, True]))
        monkeypatch.setattr(module.time, "sleep", lambda delay: None)
        runner._finish_relax_sampling()
        assert runner._pending_relax_safe_off_result is pending
        assert runner._terminal_safe_off() is pending
        assert runner._pending_relax_safe_off_result is None
        runner._safe_all_off_with_uncertainty_check.assert_called_once()


# R16-B5bF5/F6: a 120 kombináció mindegyike szöveges, üres és None okkal is fut.
class TestR16B5bF5:
    ORDERS = ("none", "stop", "abort", "stop_abort", "abort_stop")
    STAGES = ("before", "drain", "handler", "after")
    RELEASES = ("ok", "timeout", "late")

    @staticmethod
    def run_case(
        monkeypatch, order, stage, release, primary, *, interrupt=False,
        emergency_reason="GUI_ABORT_F5", graceful_reason="USER_STOP_REQUESTED",
    ):
        from threading import Event, get_ident
        from unittest.mock import Mock

        import Prog.src.test_runner as module
        from Prog.src.relax_controller import RelaxSamplingService, RelaxState

        runner, ctrl, rig = TestR16B5bRunner.make()
        started_at = rig.clock()
        entered, proceed, handler_entered, injected = Event(), Event(), Event(), Event()
        service = RelaxSamplingService(rig.dmm, clock=rig.clock)
        service.shutdown_timeout_s = 0.02
        ctrl._config.min_relax_s = 1000.0
        runner._build_sample = lambda *args, **kwargs: {}
        runner._should_poll_device_errors = lambda: False
        runner._poll_device_errors = lambda *args: []
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: service)
        monkeypatch.setattr(module.time, "monotonic", rig.clock)
        monkeypatch.setattr(module.time, "perf_counter", rig.clock)
        events, off_times, request_times = [], [], []
        runner.on_event = events.append

        def off():
            off_times.append((rig.clock(), get_ident()))
            return {"Load": True, "PSU": True}

        runner._safe_all_off_with_uncertainty_check = Mock(side_effect=off)

        def inject():
            if injected.is_set():
                return
            injected.set()
            for request in {
                "none": (), "stop": ("stop",), "abort": ("abort",),
                "stop_abort": ("stop", "abort"), "abort_stop": ("abort", "stop"),
            }[order]:
                request_times.append(rig.clock())
                if request == "stop":
                    runner.request_stop()
                else:
                    runner.request_emergency_stop(emergency_reason)

        def read():
            entered.set()
            assert proceed.wait(2)

        def release_worker():
            proceed.set()
            service._thread.join(2)
            assert not service._thread.is_alive()

        rig.hardware.read_hook = read
        original_advance = ctrl.advance

        def advance(dt_s):
            assert entered.wait(2)
            original_advance(dt_s)
            if stage == "before":
                inject()
            if stage == "handler":
                # A már futó graceful handlerhez előzetes STOP tartozik.
                runner.request_stop()
                request_times.append(rig.clock())
            else:
                ctrl._state = RelaxState.RELAX_DONE
            if primary:
                raise ArithmeticError("TICK_F5")

        ctrl.advance = advance
        # A meglévő handler-határokat kapuzzuk: a teszt az F4-kódon is futtatható.
        for name in ("_graceful_stop", "_emergency_stop"):
            original = getattr(runner, name)

            def terminal(*args, _original=original, _name=name, **kwargs):
                handler_entered.set()
                if stage == "handler" and primary:
                    inject()
                if _name == "_graceful_stop":
                    return _original(graceful_reason)
                return _original(*args, **kwargs)

            monkeypatch.setattr(runner, name, terminal)
        original_poll = runner.poll_relax_sampling_shutdown

        def poll():
            done = original_poll()
            if done and stage == "after":
                inject()
            return done

        runner.poll_relax_sampling_shutdown = poll
        original_critical = runner._relax_critical

        def critical(message):
            original_critical(message)
            if message == "RELAX_SAMPLING_RELEASE_TIMEOUT":
                # Timeoutnál az "after" a sikertelen release-kísérlet utáni kapu.
                if release == "late":
                    release_worker()
                if stage == "after":
                    inject()

        runner._relax_critical = critical

        def sleep(delay):
            if interrupt:
                raise KeyboardInterrupt("CTRL_C_F5")
            assert entered.is_set()
            if stage == "drain" or (stage == "handler" and handler_entered.is_set()):
                inject()
            if primary or stage == "handler" or (request_times and off_times):
                assert len(off_times) == 1
            if release == "ok":
                release_worker()
            rig.clock.now = round(rig.clock.now + delay, 6)

        monkeypatch.setattr(module.time, "sleep", sleep)
        steps = [TestStep(StepKind.RELAX, "relax")]
        if primary or order != "none" or stage == "handler" or release != "ok":
            steps.append(TestStep(StepKind.DISCHARGE, "tiltott"))
        plan = TestPlan(TestType.CHARACTERIZATION, tuple(steps))
        try:
            result = runner.run(plan)
            assert injected.is_set()
            # Az orákulum a bemeneti forgatókönyvből számol, nem a runner flagjeiből.
            emergency = order in ("abort", "stop_abort", "abort_stop")
            graceful = stage == "handler" or order in ("stop", "stop_abort", "abort_stop")
            expected = (
                "UNHANDLED_EXCEPTION: TICK_F5" if primary else
                (emergency_reason or "USER_EMERGENCY_STOP") if emergency else
                (graceful_reason or "USER_STOP_REQUESTED") if graceful else ""
            )
            if release != "ok":
                suffix = "RELAX_RELEASE_FAILED: RuntimeError: RELAX_SAMPLING_RELEASE_TIMEOUT"
                expected = f"{expected}; {suffix}" if expected else suffix
            status = (
                "FAULT" if primary or emergency or release != "ok" else
                "STOPPED" if graceful else "DONE"
            )
            assert result.status == status
            assert result.reason == expected
            checkpoint = runner._logger.write_checkpoint.call_args.args[0]
            assert checkpoint["status"] == status
            assert checkpoint.get("reason", "") == expected
            codes = [c.args[0] for c in runner._logger.log_event.call_args_list]
            terminal_code = "EMERGENCY_STOP" if status == "FAULT" else "GRACEFUL_STOP"
            terminal_count = int(status != "DONE")
            assert codes.count(terminal_code) == terminal_count
            assert codes.count("EMERGENCY_STOP") + codes.count("GRACEFUL_STOP") == terminal_count
            assert sum(e["event_code"] in ("EMERGENCY_STOP", "GRACEFUL_STOP") for e in events) == terminal_count
            runner._safe_all_off_with_uncertainty_check.assert_called_once()
            assert off_times[0][1] == get_ident()
            if primary or stage == "handler":
                assert off_times[0][0] == started_at
            elif request_times:
                assert off_times[0][0] - min(request_times) <= 0.01
            assert runner._pending_relax_safe_off_result is None
            runner._discharge_ctrl_factory.assert_not_called()
            if release == "late":
                assert ctrl.sampling_shutdown_complete
                assert result.reason.count("RELAX_RELEASE_FAILED:") == 1
            return result, checkpoint
        except KeyboardInterrupt:
            assert interrupt
            checkpoint = runner._logger.write_checkpoint.call_args.args[0]
            assert checkpoint["status"] == "FAULT"
            assert checkpoint["reason"] == (
                "UNHANDLED_EXCEPTION: CTRL_C_F5; "
                "RELAX_RELEASE_FAILED: KeyboardInterrupt: CTRL_C_F5"
            )
            runner._safe_all_off_with_uncertainty_check.assert_called_once()
            assert len(_f26_log_calls(runner._logger, "EMERGENCY_STOP")) == 1
            raise
        finally:
            service.stop()
            proceed.set()
            if service._thread is not None:
                service._thread.join(2)
                assert not service._thread.is_alive()

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("stage", STAGES)
    @pytest.mark.parametrize("release", RELEASES)
    @pytest.mark.parametrize("primary", (False, True))
    @pytest.mark.parametrize(
        ("emergency_reason", "graceful_reason"),
        [("GUI_ABORT_F5", "USER_STOP_REQUESTED"), ("", ""), (None, None)],
    )
    def test_terminal_order_matrix(
        self, monkeypatch, order, stage, release, primary, emergency_reason, graceful_reason,
    ):
        self.run_case(
            monkeypatch, order, stage, release, primary,
            emergency_reason=emergency_reason, graceful_reason=graceful_reason,
        )


    @staticmethod
    def run_final_poll_case(
        monkeypatch, boundary, order, emergency_reason, graceful_reason,
    ):
        runner, _ctrl, _rig = TestR16B5bRunner.make()
        plan = TestPlan(TestType.CHARACTERIZATION, (TestStep(StepKind.RELAX, "relax"),))
        runner._active_plan = plan
        events, sent = [], []
        runner.on_event = events.append

        def poll(*args):
            if not sent:
                sent.append(True)
                for request in {
                    "stop": ("stop",), "abort": ("abort",),
                    "stop_abort": ("stop", "abort"), "abort_stop": ("abort", "stop"),
                }[order]:
                    if request == "stop":
                        runner.request_stop()
                    else:
                        runner.request_emergency_stop(emergency_reason)
            return []

        runner._poll_device_errors = poll
        if boundary == "DONE":
            monkeypatch.setattr(runner, "_run_step", lambda *args: TestResult(status="DONE"))
            result = runner.run(plan)
        elif boundary == "GRACEFUL":
            # A vészleállítás csak a graceful handler végső polljában érkezik.
            runner.request_stop()
            result = runner._graceful_stop(graceful_reason)
        else:
            result = runner._run_manual_checkpoint(0, plan.steps[0])
        expected = (
            (graceful_reason or "USER_STOP_REQUESTED")
            if order == "stop" else (emergency_reason or "USER_EMERGENCY_STOP")
        )
        status = "STOPPED" if order == "stop" else "FAULT"
        assert result.reason == expected
        assert result.status == status
        checkpoint = runner._logger.write_checkpoint.call_args.args[0]
        assert checkpoint["reason"] == expected
        assert checkpoint["status"] == status
        runner._safe_all_off_with_uncertainty_check.assert_called_once()
        assert runner._pending_relax_safe_off_result is None
        runner._discharge_ctrl_factory.assert_not_called()
        terminal = [
            c for c in runner._logger.log_event.call_args_list
            if c.args[0] in ("EMERGENCY_STOP", "GRACEFUL_STOP")
        ]
        code = "GRACEFUL_STOP" if order == "stop" else "EMERGENCY_STOP"
        assert len(terminal) == 1
        assert terminal[0].args == (code, expected)
        assert terminal[0].kwargs["is_critical"] is (status == "FAULT")
        assert [e for e in events if e["event_code"] in ("EMERGENCY_STOP", "GRACEFUL_STOP")] == [
            {"event_code": code, "event_message": expected},
        ]
        return result, checkpoint

    @pytest.mark.parametrize("boundary", ("DONE", "CHECKPOINT", "GRACEFUL"))
    @pytest.mark.parametrize("order", ("stop", "abort", "stop_abort", "abort_stop"))
    @pytest.mark.parametrize(
        ("emergency_reason", "graceful_reason"),
        [("POLL_ABORT_F5", "USER_STOP_REQUESTED"), ("", ""), (None, None)],
    )
    def test_request_in_final_instrument_poll_reuses_off(
        self, monkeypatch, boundary, order, emergency_reason, graceful_reason,
    ):
        self.run_final_poll_case(monkeypatch, boundary, order, emergency_reason, graceful_reason)


    def test_ctrl_c_without_stop_is_logged_and_reraised(self, monkeypatch):
        with pytest.raises(KeyboardInterrupt, match="CTRL_C_F5"):
            self.run_case(monkeypatch, "none", "before", "timeout", False, interrupt=True)


class TestR16B5bF6:
    @pytest.mark.parametrize("reason", ("", None))
    @pytest.mark.parametrize("via_request", (False, True))
    def test_emergency_flag_without_reason_is_fault(self, reason, via_request):
        runner, _ctrl, _rig = TestR16B5bRunner.make()
        runner._poll_device_errors = lambda *args: []
        if via_request:
            runner.request_emergency_stop(reason)
        else:
            # A feloldót a producer normalizálása önmagában nem helyettesíti.
            runner.emergency_stop_reason = reason
            runner.emergency_stop_requested = True
        result = runner.run(TestPlan(
            TestType.CHARACTERIZATION, (TestStep(StepKind.DISCHARGE, "tiltott"),),
        ))
        assert result.status == "FAULT"
        assert result.reason == "USER_EMERGENCY_STOP"
        checkpoint = runner._logger.write_checkpoint.call_args.args[0]
        assert (checkpoint["status"], checkpoint["reason"]) == ("FAULT", result.reason)
        runner._logger.log_event.assert_any_call(
            "EMERGENCY_STOP", result.reason, is_critical=True,
        )
        runner._safe_all_off_with_uncertainty_check.assert_called_once()
        runner._discharge_ctrl_factory.assert_not_called()

    @pytest.mark.parametrize("reason", ("", None))
    def test_empty_graceful_reason_without_flag_uses_default(self, reason):
        runner, _ctrl, _rig = TestR16B5bRunner.make()
        runner._poll_device_errors = lambda *args: []
        assert not runner.stop_requested
        result = runner._graceful_stop(reason)
        assert result.status == "STOPPED"
        assert result.reason == "USER_STOP_REQUESTED"
        checkpoint = runner._logger.write_checkpoint.call_args.args[0]
        assert (checkpoint["status"], checkpoint["reason"]) == ("STOPPED", result.reason)
        runner._logger.log_event.assert_called_once_with(
            "GRACEFUL_STOP", result.reason, is_critical=False,
        )
        runner._safe_all_off_with_uncertainty_check.assert_called_once()



class TestR16B5bF7:
    @staticmethod
    def run_empty_primary_case(reason):
        # A handler flag nelkul is hibaut: az ures ok nem jelent sikeres zarast.
        runner, _ctrl, _rig = TestR16B5bRunner.make()
        runner._poll_device_errors = lambda *args: []
        events = []
        runner.on_event = events.append
        assert not runner.emergency_stop_requested
        assert not runner.stop_requested
        result = runner._emergency_stop(reason)
        assert (runner.status, result.status, result.reason) == ("FAULT", "FAULT", "STEP_FAULT")
        checkpoint = runner._logger.write_checkpoint.call_args.args[0]
        assert (checkpoint["status"], checkpoint["reason"]) == ("FAULT", "STEP_FAULT")
        runner._logger.log_event.assert_called_once_with(
            "EMERGENCY_STOP", "STEP_FAULT", is_critical=True,
        )
        assert events == [{"event_code": "EMERGENCY_STOP", "event_message": "STEP_FAULT"}]
        assert result.safe_off_attempted is True
        runner._safe_all_off_with_uncertainty_check.assert_called_once()
        assert runner._pending_relax_safe_off_result is None
        runner._discharge_ctrl_factory.assert_not_called()
        runner._logger.write_checkpoint.assert_called_once()
        runner._logger.close.assert_called_once()
        return result, checkpoint

    @pytest.mark.parametrize("reason", ("", None))
    def test_empty_primary_without_flag_is_fault(self, reason):
        self.run_empty_primary_case(reason)

class TestR16B5c:
    @staticmethod
    def parent(kind="tlc", phase=None):
        from Prog.tests.test_golden_gg_controller import make_controller as make_gg
        from Prog.tests.test_traditional_learning_controller import (
            make_controller as make_tlc,
        )

        ctrl, _psu, load, _dmm_v, _dmm_t, bq = (make_tlc() if kind == "tlc" else make_gg())
        ctrl._state = phase or (
            LearningState.PRECHARGE_RELAX if kind == "tlc" else GoldenGgState.RELAX_2H
        )
        return ctrl, load, bq

    @staticmethod
    def seeded(monkeypatch, age=0.0):
        from unittest.mock import Mock

        from Prog.src.relax_controller import RelaxController, RelaxSamplingService

        ctrl, _load, _bq = TestR16B5c.parent()
        clock = [1060.0 + age]
        service = RelaxSamplingService(Mock(timeout_ms=10000, max_retries=3), clock=lambda: clock[0])
        child = RelaxController(ctrl._dmm_v, RelaxConfig(min_relax_s=1.0))
        child.attach_sampling_service(service)
        for n in range(41):
            t = 1000.0 + 1.5 * n
            service._publish(t, t, 12.5 + (t - 1000.0) * 1e-6)
        child.collect_sampling_records()
        child._state = RelaxState.RELAX_DONE
        ctrl._relax_ctrl = child
        monkeypatch.setattr(ctrl, "_check_relax_bq_acceptance", lambda *a: (True, True, False))
        monkeypatch.setattr(ctrl, "_record_gauge_sample", lambda dt: None)
        return ctrl, child, service, clock

    @pytest.mark.parametrize("age,accepted", [(4.0, True), (4.001, False)])
    def test_read_start_freshness_boundary(self, monkeypatch, age, accepted):
        ctrl, child, _service, _clock = self.seeded(monkeypatch, age)
        ctrl._run_relax(0.0, LearningState.INITIAL_SNAPSHOT, True)
        assert (ctrl.state == LearningState.INITIAL_SNAPSHOT) is accepted
        if not accepted:
            assert child.acceptance_snapshot(4e-6).reason == "STALE"

    def test_freshness_is_checked_after_bq_poll(self, monkeypatch):
        ctrl, child, _service, clock = self.seeded(monkeypatch)
        def poll(*a):
            clock[0] += 5.0
            return True, True, False
        monkeypatch.setattr(ctrl, "_check_relax_bq_acceptance", poll)
        ctrl._run_relax(0.0, LearningState.INITIAL_SNAPSHOT, True)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert child.acceptance_snapshot(4e-6).reason == "STALE"

    def test_new_worker_records_during_bq_poll_are_used(self, monkeypatch):
        ctrl, child, service, clock = self.seeded(monkeypatch)
        def poll(*a):
            for n in range(1, 5):
                t = 1060.0 + n * 1.5
                service._publish(t, t, 12.5 + (t - 1000.0) * 1e-6)
            clock[0] += 6.0
            return True, True, False
        monkeypatch.setattr(ctrl, "_check_relax_bq_acceptance", poll)
        ctrl._run_relax(0.0, LearningState.INITIAL_SNAPSHOT, True)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        assert child.voltage_snapshot().read_start == 1066.0

    @pytest.mark.parametrize("slope", [-4e-6, 4e-6])
    def test_exact_slope_threshold_stays_strict(self, monkeypatch, slope):
        ctrl, child, _service, _clock = self.seeded(monkeypatch)
        child._dvdt = slope
        ctrl._run_relax(0.0, LearningState.INITIAL_SNAPSHOT, True)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert child.acceptance_snapshot(4e-6).reason == "VALID_AT_OR_ABOVE_THRESHOLD"

    @pytest.mark.parametrize("reason", [
        "INSUFFICIENT_DATA", "GAP_EXCEEDED", "DMM_READ_ERROR", "BUFFER_OVERRUN", "STALE",
    ])
    def test_timeout_keeps_compatible_flag_and_diagnoses_reason(self, monkeypatch, reason):
        ctrl, child, service, clock = self.seeded(monkeypatch)
        if reason == "INSUFFICIENT_DATA":
            child._dvdt_samples.clear()
            child._dvdt = float("nan")
        elif reason == "GAP_EXCEEDED":
            service._publish(1065.0, 1065.0, 12.5)
            clock[0] = 1065.0
        elif reason == "DMM_READ_ERROR":
            service._publish(1061.5, 1061.5, None, "DMM_READ_ERROR: mock")
            clock[0] = 1061.5
        elif reason == "BUFFER_OVERRUN":
            service._overflow = True
        else:
            clock[0] += 4.001
        ctrl._config.max_relax_extra_wait_s = 0.0
        events = []
        ctrl.on_event = events.append
        ctrl._run_relax(0.0, LearningState.INITIAL_SNAPSHOT, True)
        assert any(f.startswith("RELAX_ACCEPTANCE_NOT_CONFIRMED_AT_PRECHARGE_RELAX") for f in ctrl.quality_flags)
        event = next(e for e in events if e["event_code"] == "LEARNING_RELAX_ACCEPTANCE_TIMEOUT")
        assert event["dmm_reason"] == reason
        assert event["ocvtaken"] is True and event["vok"] is False
        assert "sample_age_s" in event and "sample_count" in event and "dvdt_V_per_s" in event
        assert reason in event["event_message"] and "sample_age_s" in event["event_message"]
        assert "decision_monotonic_s" in event["event_message"]

    def test_release_ack_precedes_transition_and_late_error_carry_is_once(self, monkeypatch):
        from unittest.mock import Mock

        ctrl, child, service, _clock = self.seeded(monkeypatch)
        service.stop = Mock()
        ctrl._run_relax(0.0, LearningState.INITIAL_SNAPSHOT, True)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl._relax_ctrl is child
        service._publish(1061.5, 1061.5, None, "DMM_READ_ERROR: kesoi")
        service._publish(1063.0, 1063.0, None, "DEADLINE_MISSED", 3)
        service._released = True
        ctrl._run_relax(30.0, LearningState.INITIAL_SNAPSHOT, True)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        assert ctrl._relax_ctrl is None
        assert ctrl.relax_dmm_fault_total == 1
        assert ctrl._relax_dmm_fault_total == 1
        assert ctrl.missed_deadline_count == ctrl._relax_missed_deadline_total == 3

    @pytest.mark.parametrize("tick", [5.0, 30.0])
    @pytest.mark.parametrize("phase,next_state", [
        (LearningState.PRECHARGE_RELAX, LearningState.INITIAL_SNAPSHOT),
        (LearningState.RELAX_1, LearningState.VERIFY_UPDATE_STATUS_05),
        (LearningState.RELAX_PRE_DISCHARGE_2, LearningState.DISCHARGE_2),
        (LearningState.RELAX_2, LearningState.VERIFY_UPDATE_STATUS_06),
    ])
    def test_j1_all_tlc_phases_real_runner_worker_extra_wait(self, monkeypatch, tick, phase, next_state):
        self.run_chain(monkeypatch, "tlc", phase, next_state, tick)

    @pytest.mark.parametrize("phase,next_state", [
        (GoldenGgState.RELAX_2H, GoldenGgState.SYSTEM_RATE_DISCHARGE),
        (GoldenGgState.RELAX_5H, GoldenGgState.EXPORT_PACKAGE),
    ])
    @pytest.mark.parametrize("tick", [5.0, 30.0])
    def test_golden_time_only_and_release_before_discharge(self, monkeypatch, phase, next_state, tick):
        self.run_chain(monkeypatch, "gg", phase, next_state, tick)

    @staticmethod
    def run_chain(monkeypatch, kind, phase, next_state, tick):
        from unittest.mock import Mock

        import Prog.src.test_runner as module

        runner, _unused, rig = TestR16B5bRunner.make()
        ctrl, load, bq = TestR16B5c.parent(kind, phase)
        ctrl._dmm_v = rig.dmm
        ctrl._config.relax_after_charge_s = ctrl._config.relax_after_discharge_s = (
            65.0 if kind == "tlc" else 1.0
        )
        if kind == "tlc":
            ctrl._config.max_relax_extra_wait_s = 600.0
            bq.set_ocvtaken(False)
            # CF-06: RPD2-ben a VOK set marad; a tobbi fazisban torolt.
            bq.set_vok(phase == LearningState.RELAX_PRE_DISCHARGE_2)
        step = TestStep(
            StepKind.TRADITIONAL_LEARNING if kind == "tlc" else StepKind.GOLDEN_GG, "embedded"
        )
        runner._controller_for_step = lambda step: ctrl
        runner._active_plan = None
        runner._config.runner_tick_s = tick
        runner._config.sleep_enabled = True
        runner._build_sample = lambda *a, **k: {}
        runner._poll_device_errors = lambda *a: []
        runner._should_poll_device_errors = lambda: False
        runner._is_finished = lambda c: c.state != phase
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        monkeypatch.setattr(module.time, "perf_counter", rig.clock)
        original_advance = ctrl.advance
        initialized = False
        next_deadline = 0.0
        seen = []
        held = []
        finalized = Mock(wraps=ctrl._finalize_relax_ctrl_totals) if kind == "tlc" else None
        if finalized is not None:
            monkeypatch.setattr(ctrl, "_finalize_relax_ctrl_totals", finalized)

        def sample_until(target):
            nonlocal next_deadline
            if initialized:
                while next_deadline <= target:
                    delay = rig.step(next_deadline - rig.clock())
                    next_deadline = rig.clock() + delay
            rig.clock.now = target

        def poll(*a):
            if ctrl._relax_ctrl.elapsed_s < 100.0:
                before = len(rig.hardware.starts)
                sample_until(rig.clock() + 7.0)
                assert len(rig.hardware.starts) > before
                return False, False, phase == LearningState.RELAX_PRE_DISCHARGE_2
            # Gyors elfogadási poll után a teljes pacing a release-várakozást öregíti.
            return True, True, phase == LearningState.RELAX_PRE_DISCHARGE_2

        if kind == "tlc":
            # A BQ transport eleve FakePicoSerial; a poll varakozasa alatt a worker fut.
            original_check = ctrl._check_relax_bq_acceptance
            def delayed_check(require_vok_cleared=True):
                poll()
                bq.set_ocvtaken(ctrl._relax_ctrl.elapsed_s >= 100.0)
                return original_check(require_vok_cleared)
            monkeypatch.setattr(ctrl, "_check_relax_bq_acceptance", delayed_check)

        def advance(dt_s):
            nonlocal initialized, next_deadline
            assert len(seen) < 50, "A relax nem fejezodik be a celzott idon belul"
            if runner._relax_sampler_started and not initialized:
                delay = rig.requests.get(timeout=2)
                initialized = True
                next_deadline = rig.clock() + delay
            if rig.service._stop.is_set():
                # A nyugta elotti tickben nincs atmenet, konfiguracio vagy Load ON.
                held.append(ctrl.state)
                assert ctrl.state == phase
                assert "input_on()" not in load.calls
                assert rig.hardware.nplc == 10.0
                rig.permits.put(False)
                rig.service._thread.join(2)
                assert not rig.service._thread.is_alive()
            state = original_advance(dt_s)
            child = ctrl._relax_ctrl
            if child is not None:
                seen.append((child.sampling_mode, child.state, child.dvdt_valid, ctrl._relax_extra_wait_s if kind == "tlc" else 0.0))
            return state

        monkeypatch.setattr(ctrl, "advance", advance)
        def paced_sleep(delay):
            # A valódi runner pacingje a stopkérés és a release-poll között is telik.
            if rig.service._stop.is_set():
                rig.clock.now += delay
            else:
                sample_until(rig.clock() + delay)
        monkeypatch.setattr(module.time, "sleep", paced_sleep)
        try:
            result = runner._run_step(0, step)
            assert result.status == "DONE"
            assert ctrl.state == next_state
            assert seen and all(row[0] == "SERVICE" for row in seen)
            assert held == [phase]
            assert ctrl._relax_ctrl is None and runner._relax_sampler_controller is None
            assert rig.service.drain().released
            if kind == "tlc":
                assert any(row[1] == RelaxState.RELAX_DONE and row[3] > 0 for row in seen)
                assert any(row[2] for row in seen)
                finalized.assert_called_once()
                assert not any("RELAX_ACCEPTANCE_NOT_CONFIRMED" in f for f in ctrl.quality_flags)
            else:
                # 60 s OLS-ablak nelkul is idore zarul, de csak nyugta utan.
                assert not any(row[2] for row in seen)
                assert ("input_on()" in load.calls) is (phase == GoldenGgState.RELAX_2H)
        finally:
            if rig.service._thread is not None and rig.service._thread.is_alive():
                rig.stop()

    def test_runner_abort_origin_is_not_reapplied_while_draining(self, monkeypatch):
        from unittest.mock import Mock

        runner, child, _rig = TestR16B5bRunner.make()
        ctrl, _load, _bq = self.parent()
        ctrl._relax_ctrl = child
        runner._active_controller = ctrl
        abort = Mock(wraps=ctrl.emergency_stop)
        monkeypatch.setattr(ctrl, "emergency_stop", abort)
        runner._abort_active_controller("EMERGENCY_STOP")
        runner._abort_active_controller("EMERGENCY_STOP")
        assert ctrl.runner_abort_origin == "EMERGENCY_STOP"
        abort.assert_called_once()

    def test_golden_slow_handoff_read_cannot_start_discharge(self, monkeypatch):
        _ctrl, child, _service, clock = self.seeded(monkeypatch)
        golden, load, _bq = self.parent("gg")
        golden._relax_ctrl = child
        events = []
        golden.on_event = events.append
        def record(*a, **kw):
            clock[0] += 5.0
        monkeypatch.setattr(golden, "_record_sample", record)
        def slow_read():
            clock[0] += 5.0
            return 12.5
        monkeypatch.setattr(golden._relax_ctrl._dmm_v, "read_voltage", slow_read)
        golden._run_relax(0.0, True)
        assert golden.state == GoldenGgState.FAULT
        assert golden.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        event = next(e for e in events if e["event_code"] == "GG_RELAX_DMM_NOT_FRESH")
        assert event["dmm_reason"] == "STALE" and event["sample_age_s"] == 5.0

    @pytest.mark.parametrize("kind", ["tlc", "gg"])
    def test_embedded_logger_skips_owned_dmm_before_first_record(self, monkeypatch, kind):
        from unittest.mock import Mock

        runner, _logger, _dmm, _events = _f26_mock_logger_runner(TestRunnerConfig())
        _other, child, rig = TestR16B5bRunner.make()
        parent, _load, _bq = self.parent(kind)
        child.attach_sampling_service(rig.service)
        parent._relax_ctrl = child
        runner._relax_sampler_controller = child
        read_all = Mock(return_value={"battery_voltage_V": 99.0})
        monkeypatch.setattr(runner._instruments, "read_all", read_all)
        step = TestStep(
            StepKind.TRADITIONAL_LEARNING if kind == "tlc" else StepKind.GOLDEN_GG, "embedded"
        )
        sample = runner._build_sample(step, parent)
        assert sample["battery_voltage_V"] is None
        assert not sample["dmm_voltage_valid"]
        assert read_all.call_args.kwargs["skip_battery_voltage"]


class TestR16B5cF1:
    @pytest.mark.parametrize("kind", ["tlc", "gg"])
    @pytest.mark.parametrize("failure", ["exception", "nan", "inf", "slow"])
    def test_handoff_read_failure_is_diagnosed_before_load_on(self, monkeypatch, kind, failure):
        ctrl, child, _service, clock = TestR16B5c.seeded(monkeypatch)
        if kind == "gg":
            ctrl, load, _bq = TestR16B5c.parent("gg")
            ctrl._relax_ctrl = child
            ctrl._dmm_v = child._dmm_v
        else:
            ctrl._state = LearningState.RELAX_PRE_DISCHARGE_2
            load = ctrl._load
        events = []
        ctrl.on_event = events.append
        def read():
            assert child.sampling_shutdown_complete
            if failure == "exception":
                raise RuntimeError("átadási mérési hiba")
            if failure == "slow":
                clock[0] += 4.001
                return 12.5
            return float(failure)
        monkeypatch.setattr(child._dmm_v, "read_voltage", read)
        if kind == "gg":
            ctrl._run_relax(0.0, True)
        else:
            ctrl._run_relax(0.0, LearningState.DISCHARGE_2, True, False)
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert ctrl.state.name == "FAULT"
        assert "input_on()" not in load.calls
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in ctrl._psu.calls
        code = "GG_RELAX_DMM_NOT_FRESH" if kind == "gg" else "LEARNING_RELAX_DMM_NOT_FRESH"
        evidence = next(e for e in events if e["event_code"] == code)
        assert evidence["read_start"] == 1060.0
        assert evidence["sample_age_s"] == pytest.approx(4.001 if failure == "slow" else 0.0)
        assert evidence["dmm_reason"] == (
            "STALE" if failure == "slow" else "DMM_READ_ERROR" if kind == "gg" else "READ_ERROR"
        )
        assert ctrl.relax_dmm_fault_total == (0 if failure == "slow" else 1)
        assert any("DMM_FEEDBACK_LOST" in e.get("event_message", "") for e in events)

    @pytest.mark.parametrize("kind,phase", [
        ("tlc", LearningState.PRECHARGE_RELAX),
        ("tlc", LearningState.RELAX_1),
        ("tlc", LearningState.RELAX_PRE_DISCHARGE_2),
        ("tlc", LearningState.RELAX_2),
        ("gg", GoldenGgState.RELAX_2H),
        ("gg", GoldenGgState.RELAX_5H),
    ])
    @pytest.mark.parametrize("release_error", [False, True])
    def test_natural_controller_fault_keeps_reason_with_blocked_worker(
        self, monkeypatch, kind, phase, release_error,
    ):
        from threading import Event
        from unittest.mock import Mock

        import Prog.src.test_runner as module
        from Prog.src.relax_controller import RelaxSamplingService

        runner, _unused, rig = TestR16B5bRunner.make()
        ctrl, load, _bq = TestR16B5c.parent(kind, phase)
        ctrl._dmm_v = rig.dmm
        ctrl._config.relax_after_charge_s = ctrl._config.relax_after_discharge_s = 10000.0
        other, _logger, _dmm, _events = _f26_mock_logger_runner(TestRunnerConfig())
        runner._instruments = other._instruments
        runner._instruments._dmm_v = rig.dmm
        runner._safe_all_off_with_uncertainty_check = (
            TestRunner._safe_all_off_with_uncertainty_check.__get__(runner)
        )
        manager = runner._instruments
        safe_off = Mock(wraps=manager.safe_all_off)
        monkeypatch.setattr(manager, "safe_all_off", safe_off)
        order = []
        for name, device in (("Load", manager.load), ("PSU", manager.psu)):
            original = device.safe_off
            def off(name=name, original=original):
                order.append(name)
                return original()
            monkeypatch.setattr(device, "safe_off", off)
        entered, proceed = Event(), Event()
        service = RelaxSamplingService(rig.dmm, clock=rig.clock)
        def blocked_read():
            entered.set()
            assert proceed.wait(2)
        rig.hardware.read_hook = blocked_read
        original_release = rig.dmm.release_measurement
        if release_error:
            def broken_release(owner):
                raise RuntimeError("teszt release-hiba")
            monkeypatch.setattr(rig.dmm, "release_measurement", broken_release)
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: service)
        runner._controller_for_step = lambda step: ctrl
        runner._build_sample = lambda *a, **k: {}
        runner._poll_device_errors = lambda *a, **k: []
        runner._should_poll_device_errors = lambda: False
        events, waits = [], []
        runner.on_event = events.append
        original_advance = ctrl.advance
        def advance(dt_s):
            if runner._relax_sampler_started:
                assert entered.wait(2)
                ctrl._dmm_t.simulate_timeout = True
                state = original_advance(ctrl._safety.temperature_dmm_fault_timeout_s + 1.0)
                assert ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
                assert getattr(ctrl, "runner_abort_origin", None) is None
                return state
            return original_advance(dt_s)
        monkeypatch.setattr(ctrl, "advance", advance)
        def release_read(delay):
            # A közös OFF egyszer, a worker várakoztatásának feloldása előtt fut.
            waits.append(safe_off.call_count)
            assert order == ["Load", "PSU"]
            proceed.set()
            service._thread.join(2)
            assert not service._thread.is_alive()
        monkeypatch.setattr(module.time, "sleep", release_read)
        step = TestStep(
            StepKind.TRADITIONAL_LEARNING if kind == "tlc" else StepKind.GOLDEN_GG,
            "beágyazott relax",
        )
        try:
            result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
            primary = "TEMPERATURE_MONITOR_LOST_CRITICAL"
            assert result.status == "FAULT"
            if release_error:
                assert result.reason.startswith(primary + "; RELAX_RELEASE_FAILED:"), result.reason
                assert "teszt release-hiba" in result.reason
            else:
                assert result.reason == primary, result.reason
            assert waits == [1]
            safe_off.assert_called_once()
            assert order == ["Load", "PSU"]
            assert result.safe_off_attempted
            assert result.safe_off_result["Load"] and result.safe_off_result["PSU"]
            checkpoint = runner._logger.write_checkpoint.call_args.args[0]
            assert checkpoint["reason"] == result.reason
            terminal = [e for e in events if e["event_code"] == "EMERGENCY_STOP"]
            assert len(terminal) == 1 and result.reason in terminal[0]["event_message"]
            assert len(_f26_log_calls(runner._logger, "EMERGENCY_STOP")) == 1
            assert "input_on()" not in load.calls
            assert [c for c, _tid in rig.hardware.commands if "NPLC" in c] == [
                "VOLT:DC:NPLC 1;*OPC?", "VOLT:DC:NPLC 10;*OPC?",
            ]
            assert getattr(ctrl, "runner_abort_origin", None) is None
        finally:
            service.stop()
            proceed.set()
            if service._thread is not None:
                service._thread.join(2)
                assert not service._thread.is_alive()
            if rig.dmm.measurement_owned:
                original_release(service._owner)

    def test_real_overflow_preserves_root_cause_and_refills(self):
        from unittest.mock import Mock

        from Prog.src.relax_controller import RelaxController, RelaxSamplingService

        clock = [1000.0]
        service = RelaxSamplingService(
            Mock(timeout_ms=10000, max_retries=3), capacity=2, clock=lambda: clock[0],
        )
        child = RelaxController(Mock(), RelaxConfig(min_relax_s=1.0))
        child.attach_sampling_service(service)
        events = []
        child.on_event = events.append
        for n in range(3):
            clock[0] = 1000.0 + 1.5 * n
            service._publish(clock[0], clock[0], 12.5)
        child.collect_sampling_records()
        snapshot = child.acceptance_snapshot(4e-6)
        assert snapshot.reason == "BUFFER_OVERRUN"
        assert not snapshot.dvdt_valid
        assert snapshot.sample_count == 2
        assert {e["event_code"] for e in events} == {
            "RELAX_BUFFER_OVERRUN", "RELAX_SEQUENCE_GAP",
        }
        for n in range(3, 43):
            clock[0] = 1000.0 + 1.5 * n
            service._publish(clock[0], clock[0], 12.5)
            child.collect_sampling_records()
            snapshot = child.acceptance_snapshot(4e-6)
            assert snapshot.reason == (
                "BUFFER_OVERRUN" if n < 41 else "VALID_BELOW_THRESHOLD"
            )
        assert snapshot.coverage_s == 60.0
        assert snapshot.max_gap_s == 1.5


class TestR16B5cF2:
    @pytest.mark.parametrize("read_case", ["ok", "nan"])
    @pytest.mark.parametrize("kind", ["tlc", "gg"])
    @pytest.mark.parametrize("tick", [5.0, 30.0])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency", "stop_abort", "abort_stop"])
    def test_stop_during_handoff_cannot_configure_or_start_load(
        self, monkeypatch, kind, tick, stop_kind, read_case,
    ):
        from threading import Event, Thread, get_ident
        from unittest.mock import Mock

        import Prog.src.test_runner as module

        runner, _unused, rig = TestR16B5bRunner.make()
        phase = (
            LearningState.RELAX_PRE_DISCHARGE_2 if kind == "tlc"
            else GoldenGgState.RELAX_2H
        )
        ctrl, load, _bq = TestR16B5c.parent(kind, phase)
        ctrl._dmm_v = rig.dmm
        ctrl._config.relax_after_charge_s = 65.0 if kind == "tlc" else 1.0
        if kind == "tlc":
            # A vizsgált rés a már elfogadott relax utáni kapumérés.
            monkeypatch.setattr(ctrl, "_check_relax_bq_acceptance", lambda *a: (True, True, True))
        other, logger, _dmm, events = _f26_mock_logger_runner(TestRunnerConfig())
        runner._logger = logger
        runner._instruments = other._instruments
        runner._instruments._dmm_v = rig.dmm
        runner._instruments._psu = ctrl._psu
        runner._instruments._load = load
        runner._safe_all_off_with_uncertainty_check = (
            TestRunner._safe_all_off_with_uncertainty_check.__get__(runner)
        )
        safe_off = Mock(wraps=runner._instruments.safe_all_off)
        monkeypatch.setattr(runner._instruments, "safe_all_off", safe_off)
        off_order = []
        for name, device in (("Load", load), ("PSU", ctrl._psu)):
            original = device.safe_off
            def off(name=name, original=original):
                off_order.append(name)
                return original()
            monkeypatch.setattr(device, "safe_off", off)
        runner.on_event = events.append
        runner._controller_for_step = lambda step: ctrl
        runner._config.runner_tick_s = tick
        runner._config.sleep_enabled = True
        runner._build_sample = lambda *a, **k: {}
        runner._should_poll_device_errors = lambda: False
        runner._poll_device_errors = lambda *a, **k: []
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        monkeypatch.setattr(module.time, "perf_counter", rig.clock)
        entered, proceed = Event(), Event()
        thread_errors, request_threads, after_read = [], [], []
        original_advance = ctrl.advance
        initialized = False
        next_deadline = 0.0
        ticks = 0

        def advance(dt_s):
            nonlocal ticks
            ticks += 1
            assert ticks < 50, "A célzott átadás nem zárult le"
            if rig.service._stop.is_set():
                # Valódi worker-release, majd késői számlálók a végső drainhez.
                rig.permits.put(False)
                rig.service._thread.join(2)
                assert not rig.service._thread.is_alive()
                rig.service._publish(rig.clock(), rig.clock(), None, "DMM_READ_ERROR: késői")
                rig.service._publish(rig.clock(), rig.clock(), None, "DEADLINE_MISSED", 3)
            state = original_advance(dt_s)
            if entered.is_set():
                after_read.append((ctrl.state, ctrl._relax_ctrl))
            return state

        monkeypatch.setattr(ctrl, "advance", advance)
        def paced_sleep(delay):
            nonlocal initialized, next_deadline
            target = rig.clock() + delay
            if runner._relax_sampler_started and not initialized:
                # Az első olvasás és várakozás előzze meg az óra léptetését.
                # Ezután szándékosan egy runner-tickkel később ébred a worker:
                # így a 3/20 kihagyás nem függ a szálak indulási sorrendjétől.
                rig.requests.get(timeout=2)
                initialized = True
                wait = rig.step(delay)
                next_deadline = rig.clock() + wait
            if initialized and not rig.service._stop.is_set():
                while next_deadline <= target:
                    wait = rig.step(next_deadline - rig.clock())
                    next_deadline = rig.clock() + wait
            rig.clock.now = target
        monkeypatch.setattr(module.time, "sleep", paced_sleep)

        def gate_read():
            if not rig.dmm.measurement_owned:
                # A felszabadított DMM friss kapumérése a runner szálán fut.
                assert not rig.service._thread.is_alive()
                entered.set()
                assert proceed.wait(2)
                rig.clock.now += 1.0
        rig.hardware.read_hook = gate_read
        original_read = rig.dmm.read_voltage
        def read_voltage(*args, **kwargs):
            value = original_read(*args, **kwargs)
            return float("nan") if read_case == "nan" and not rig.dmm.measurement_owned else value
        monkeypatch.setattr(rig.dmm, "read_voltage", read_voltage)

        def inject():
            try:
                assert entered.wait(2), "Nem indult kapumérés"
                request_threads.append(get_ident())
                for action in {
                    "stop": ("stop",), "abort": ("abort",), "emergency": ("emergency",),
                    "stop_abort": ("stop", "abort"), "abort_stop": ("abort", "stop"),
                }[stop_kind]:
                    if action == "stop":
                        runner.request_stop()
                    else:
                        # A GUI worker abortja is request_emergency_stop(reason)-t hív.
                        runner.request_emergency_stop(
                            "ABORT_DURING_HANDOFF" if action == "abort" else "USER_EMERGENCY_STOP"
                        )
            except BaseException as exc:  # noqa: BLE001 -- a segédszál hibáját a főszál ellenőrzi
                thread_errors.append(exc)
            finally:
                proceed.set()

        injector = Thread(target=inject)
        injector.start()
        step = TestStep(
            StepKind.TRADITIONAL_LEARNING if kind == "tlc" else StepKind.GOLDEN_GG,
            "kapumérés alatti leállítás",
        )
        try:
            result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
            assert not thread_errors, thread_errors
            assert request_threads and request_threads[0] != get_ident()
            assert len(after_read) == 1
            # Egyetlen konfiguráló parancs sem előzheti meg a terminális feloldót.
            assert not any(c.startswith(("set_mode_cc(", "set_current(", "input_on(")) for c in load.calls), load.calls
            assert after_read[0][0] == (phase if read_case == "ok" else type(phase).FAULT)
            assert after_read[0][1] is None
            assert ctrl._relax_ctrl is None
            expected_status = "STOPPED" if stop_kind == "stop" and read_case == "ok" else "FAULT"
            expected_reason = (
                "USER_STOP_REQUESTED" if stop_kind == "stop" else
                "USER_EMERGENCY_STOP" if stop_kind == "emergency" else "ABORT_DURING_HANDOFF"
            )
            if read_case == "nan":
                # A mérési fault elsőbbséget élvez a közben érkező leállítással szemben.
                expected_reason = "DMM_FEEDBACK_LOST"
            assert (result.status, result.reason) == (expected_status, expected_reason)
            safe_off.assert_called_once()
            assert off_order == ["Load", "PSU"]
            assert result.safe_off_attempted
            assert result.safe_off_result["Load"] and result.safe_off_result["PSU"]
            assert not load.input_commanded_on and not ctrl._psu.output_commanded_on
            checkpoint = logger.write_checkpoint.call_args.args[0]
            assert (checkpoint["status"], checkpoint["reason"]) == (expected_status, expected_reason)
            code = "GRACEFUL_STOP" if expected_status == "STOPPED" else "EMERGENCY_STOP"
            terminal = [e for e in events if e["event_code"] in ("GRACEFUL_STOP", "EMERGENCY_STOP", "TEST_DONE")]
            assert len(terminal) == 1, terminal
            assert terminal[0]["event_code"] == code
            assert expected_reason in terminal[0]["event_message"]
            assert len(_f26_log_calls(logger, code)) == 1
            assert ctrl.fault_reason == expected_reason
            if kind == "tlc":
                assert ctrl.runner_abort_origin == (expected_reason if read_case == "ok" else None)
            # Az élő és átvitt tag kizárja egymást; a határidőszámláló is egyszeres.
            expected_faults = 1 if read_case == "ok" else 2
            assert ctrl.relax_dmm_fault_total == ctrl._relax_dmm_fault_total == expected_faults, (ctrl.relax_dmm_fault_total, ctrl._relax_dmm_fault_total, expected_faults)
            # Az első vezérelt késés 3/20 kihagyása és három késői határidő.
            expected_missed = 6 if tick == 5.0 else 23
            assert result.relax_missed_deadline_count == checkpoint["relax_missed_deadline_count"] == expected_missed
            runner._finish_relax_sampling(safe_off_done=True)
            assert ctrl.relax_dmm_fault_total == expected_faults
            assert runner._total_relax_missed_deadline_count == expected_missed
            safe_off.assert_called_once()
        finally:
            proceed.set()
            injector.join(2)
            assert not injector.is_alive()
            if rig.service._thread is not None and rig.service._thread.is_alive():
                rig.stop()

    @pytest.mark.parametrize("stop_kind", ["stop", "abort"])
    def test_tlc_first_discharge_uses_same_stop_contract(self, monkeypatch, stop_kind):
        from unittest.mock import Mock

        runner, _unused, _rig = TestR16B5bRunner.make()
        ctrl, load, _bq = TestR16B5c.parent("tlc", LearningState.VERIFY_VOK_QEN)
        ctrl._config.verify_settle_s = 0.0
        ctrl._dmm_valid = True
        ctrl._u_batt = 12.5
        runner._controller_for_step = lambda step: ctrl
        runner._build_sample = lambda *a, **k: {}
        runner._should_poll_device_errors = lambda: False
        runner._poll_device_errors = lambda *a, **k: []
        runner._safe_all_off_with_uncertainty_check = Mock(
            return_value={"Load": True, "PSU": True},
        )
        def read_status():
            # A BQ-olvasás vége előtt érkező kérés a közös indítót is tiltja.
            if stop_kind == "stop":
                runner.request_stop()
            else:
                runner.request_emergency_stop("ABORT_DURING_VERIFY")
            return 0x0003
        monkeypatch.setattr(ctrl._bq_driver, "read_control_status", read_status)
        step = TestStep(StepKind.TRADITIONAL_LEARNING, "első kisütési átadás")
        result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        assert not any(c.startswith(("set_mode_cc(", "set_current(", "input_on(")) for c in load.calls)
        assert (result.status, result.reason) == (
            ("STOPPED", "USER_STOP_REQUESTED") if stop_kind == "stop"
            else ("FAULT", "ABORT_DURING_VERIFY")
        )
        runner._safe_all_off_with_uncertainty_check.assert_called_once()


class TestR16B5cF3:
    @staticmethod
    def run_chain(monkeypatch, kind, phase, target, tick, *, timeout=False, gate_case="ok", stop_kind=None):
        from unittest.mock import Mock

        import Prog.src.test_runner as module

        runner, _unused, rig = TestR16B5bRunner.make()
        ctrl, load, bq = TestR16B5c.parent(kind, phase)
        ctrl._dmm_v = rig.dmm
        ctrl._config.relax_after_charge_s = ctrl._config.relax_after_discharge_s = (
            1.0 if timeout or kind == "gg" else 65.0
        )
        if kind == "tlc":
            ctrl._config.max_relax_extra_wait_s = 0.0 if timeout else 600.0
            bq.set_ocvtaken(not timeout)
            bq.set_vok(False)
        other, logger, _dmm, events = _f26_mock_logger_runner(TestRunnerConfig())
        runner._logger = logger
        runner._instruments = other._instruments
        runner._instruments._dmm_v = rig.dmm
        runner._instruments._psu = ctrl._psu
        runner._instruments._load = load
        runner._safe_all_off_with_uncertainty_check = (
            TestRunner._safe_all_off_with_uncertainty_check.__get__(runner)
        )
        safe_off = Mock(wraps=runner._instruments.safe_all_off)
        monkeypatch.setattr(runner._instruments, "safe_all_off", safe_off)
        runner.on_event = events.append
        ctrl.on_event = events.append
        runner._controller_for_step = lambda step: ctrl
        runner._config.runner_tick_s = tick
        runner._config.sleep_enabled = True
        runner._build_sample = lambda *a, **k: {}
        runner._should_poll_device_errors = lambda: False
        runner._poll_device_errors = lambda *a, **k: []
        runner._is_finished = lambda c: c.state in (target, type(phase).FAULT)
        monkeypatch.setattr(module, "RelaxSamplingService", lambda dmm: rig.service)
        monkeypatch.setattr(module.time, "perf_counter", rig.clock)
        monkeypatch.setattr(module.time, "monotonic", rig.clock)
        original_advance = ctrl.advance
        initialized = False
        next_deadline = 0.0
        trace, children, pending, gates = [], [], [], []

        def advance(dt_s):
            nonlocal initialized, next_deadline
            assert len(trace) < 100, "A célzott runner-lánc nem zárult le"
            if runner._relax_sampler_started and not initialized:
                delay = rig.requests.get(timeout=2)
                initialized = True
                next_deadline = rig.clock() + delay
            holding = rig.service._stop.is_set() and rig.service._thread.is_alive()
            if holding:
                # A timeout/elfogadás UTÁNI teljes runner-tickben sincs release.
                pending.append(ctrl.state)
                assert ctrl.state == phase
                assert "input_on()" not in load.calls
            before = ctrl.state
            state = original_advance(dt_s)
            trace.append((before, state, ctrl._dmm_valid))
            if ctrl._relax_ctrl is not None:
                children.append(ctrl._relax_ctrl)
                assert ctrl._relax_ctrl.sampling_mode == "SERVICE"
            if holding:
                assert ctrl.state == phase
                rig.permits.put(False)
                rig.service._thread.join(2)
                assert not rig.service._thread.is_alive()
            return state

        monkeypatch.setattr(ctrl, "advance", advance)

        def paced_sleep(delay):
            nonlocal initialized, next_deadline
            # A worker első várakozása előtt nem ugorhat a virtuális runner-óra.
            if runner._relax_sampler_started and not initialized:
                wait = rig.requests.get(timeout=2)
                initialized = True
                next_deadline = rig.clock() + wait
            finish = rig.clock() + delay
            if initialized and not rig.service._stop.is_set():
                if timeout:
                    # Explicit határidőkiesés, nem az OS szálütemezése dönti el az okot.
                    assert delay >= rig.service.PERIOD_S
                    wait = rig.step(delay)
                    next_deadline = rig.clock() + wait
                else:
                    steps = 0
                    while next_deadline <= finish:
                        steps += 1
                        assert steps <= 100, "F3 sampler nem lépett előre"
                        wait = rig.step(next_deadline - rig.clock())
                        assert wait > 0.0
                        next_deadline = rig.clock() + wait
            rig.clock.now = finish

        monkeypatch.setattr(module.time, "sleep", paced_sleep)
        original_read = rig.dmm.read_voltage

        def read_voltage(*args, **kwargs):
            if rig.dmm.measurement_owned:
                return original_read(*args, **kwargs)
            assert rig.service.drain().released
            assert not rig.service._thread.is_alive()
            gates.append((ctrl.state, rig.clock()))
            if stop_kind == "stop":
                runner.request_stop()
            elif stop_kind == "abort":
                runner.request_emergency_stop("ABORT_DURING_D1_GATE")
            if gate_case == "exception":
                raise RuntimeError("Kapumérési próbahiba")
            if gate_case in ("nan", "inf"):
                return float(gate_case)
            rig.clock.now += {"ok": 0.0, "boundary": 4.0, "slow": 4.001}[gate_case]
            return 12.5

        monkeypatch.setattr(rig.dmm, "read_voltage", read_voltage)
        step = TestStep(
            StepKind.TRADITIONAL_LEARNING if kind == "tlc" else StepKind.GOLDEN_GG,
            "F3 teljes átadási lánc",
        )
        try:
            result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
            assert children and pending == [phase]
            assert ctrl._relax_ctrl is None
            assert runner._relax_sampler_controller is None
            assert rig.service.drain().released
            safe_off.assert_called_once()
            assert not load.input_commanded_on and not ctrl._psu.output_commanded_on
            return ctrl, load, events, result, trace, gates
        finally:
            if rig.service._thread is not None and rig.service._thread.is_alive():
                rig.stop()

    @pytest.mark.parametrize("tick", [5.0, 30.0])
    @pytest.mark.parametrize("gate_case", ["ok", "boundary", "slow", "exception", "nan", "inf"])
    def test_precharge_relax_to_first_discharge_fresh_gate(self, monkeypatch, tick, gate_case):
        ctrl, load, events, result, trace, gates = self.run_chain(
            monkeypatch, "tlc", LearningState.PRECHARGE_RELAX,
            LearningState.DISCHARGE_1, tick, gate_case=gate_case,
        )
        assert [after for before, after, _valid in trace if before != after][:3] == [
            LearningState.INITIAL_SNAPSHOT, LearningState.IT_ENABLE_STEP,
            LearningState.VERIFY_VOK_QEN,
        ]
        assert len(gates) == 1 and gates[0][0] == LearningState.VERIFY_VOK_QEN
        # A release utáni elavult állapotot csak az új kapumérés állíthatja helyre.
        assert any(after == LearningState.INITIAL_SNAPSHOT and not valid for _, after, valid in trace)
        accepted = gate_case in ("ok", "boundary")
        assert ("input_on()" in load.calls) is accepted
        diagnostic = [e for e in events if e["event_code"] == "LEARNING_RELAX_DMM_NOT_FRESH"]
        if accepted:
            assert ctrl.state == LearningState.DISCHARGE_1
            assert result.status == "DONE" and ctrl.dmm_valid
            assert not diagnostic
        else:
            assert (result.status, result.reason) == ("FAULT", "DMM_FEEDBACK_LOST")
            assert ctrl.state == LearningState.FAULT
            assert not any(c.startswith(("set_mode_cc(", "set_current(", "input_on(")) for c in load.calls)
            assert "input_off()" in load.calls and "all_outputs_off()" in ctrl._psu.calls
            assert len(diagnostic) == 1
            assert diagnostic[0]["dmm_reason"] == ("STALE" if gate_case == "slow" else "READ_ERROR")
            assert diagnostic[0]["read_start"] == gates[0][1]
            assert diagnostic[0]["sample_age_s"] == pytest.approx(4.001 if gate_case == "slow" else 0.0)
            assert any("DMM_FEEDBACK_LOST" in e.get("event_message", "") for e in events)

    @pytest.mark.parametrize("tick", [5.0, 30.0])
    def test_relax_5h_export_has_no_freshness_fault_or_fabricated_validity(self, monkeypatch, tick):
        ctrl, load, events, result, trace, gates = self.run_chain(
            monkeypatch, "gg", GoldenGgState.RELAX_5H, GoldenGgState.EXPORT_PACKAGE, tick,
        )
        assert result.status == "DONE" and ctrl.state == GoldenGgState.EXPORT_PACKAGE
        assert not gates and "input_on()" not in load.calls
        assert trace[-1][2] is False and not ctrl.dmm_valid
        assert not any(e["event_code"] == "GG_RELAX_DMM_NOT_FRESH" for e in events)

    @pytest.mark.parametrize("tick", [5.0, 30.0])
    @pytest.mark.parametrize("phase,target", [
        (LearningState.PRECHARGE_RELAX, LearningState.INITIAL_SNAPSHOT),
        (LearningState.RELAX_1, LearningState.VERIFY_UPDATE_STATUS_05),
        (LearningState.RELAX_PRE_DISCHARGE_2, LearningState.DISCHARGE_2),
        (LearningState.RELAX_2, LearningState.VERIFY_UPDATE_STATUS_06),
    ])
    def test_timeout_pending_release_reports_once(self, monkeypatch, tick, phase, target):
        ctrl, _load, events, result, _trace, _gates = self.run_chain(
            monkeypatch, "tlc", phase, target, tick, timeout=True,
        )
        assert result.status == "DONE" and ctrl.state == target
        flags = [f for f in ctrl.quality_flags if f.startswith("RELAX_ACCEPTANCE_NOT_CONFIRMED_AT_")]
        assert len(flags) == 1 and flags[0].startswith(f"RELAX_ACCEPTANCE_NOT_CONFIRMED_AT_{phase.value}(")
        timeouts = [e for e in events if e["event_code"] == "LEARNING_RELAX_ACCEPTANCE_TIMEOUT"]
        assert len(timeouts) == 1
        assert timeouts[0]["dmm_reason"] == "GAP_EXCEEDED"
        assert not any(e["event_code"] == "LEARNING_RELAX_ACCEPTED" for e in events)

    @pytest.mark.parametrize("kind", ["tlc", "gg"])
    def test_released_gate_accepts_exactly_four_seconds(self, monkeypatch, kind):
        _parent, child, _service, clock = TestR16B5c.seeded(monkeypatch)
        ctrl, load, _bq = TestR16B5c.parent(
            kind, LearningState.RELAX_PRE_DISCHARGE_2 if kind == "tlc" else GoldenGgState.RELAX_2H,
        )
        if kind == "tlc":
            ctrl = _parent
            ctrl._state = LearningState.RELAX_PRE_DISCHARGE_2
            load = ctrl._load
        ctrl._relax_ctrl = child
        events = []
        ctrl.on_event = events.append

        def read():
            assert child.sampling_shutdown_complete
            clock[0] += 4.0
            return 12.5

        monkeypatch.setattr(child._dmm_v, "read_voltage", read)
        if kind == "tlc":
            ctrl._run_relax(0.0, LearningState.DISCHARGE_2, True, False)
            assert ctrl.state == LearningState.DISCHARGE_2
        else:
            ctrl._run_relax(0.0, True)
            assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        assert "input_on()" in load.calls and ctrl.dmm_valid
        assert not any("NOT_FRESH" in e["event_code"] for e in events)

    @pytest.mark.parametrize("tick", [5.0, 30.0])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort"])
    @pytest.mark.parametrize("gate_case", ["ok", "nan"])
    def test_first_discharge_gate_preserves_stop_and_fault_priority(
        self, monkeypatch, tick, stop_kind, gate_case,
    ):
        ctrl, load, events, result, _trace, gates = self.run_chain(
            monkeypatch, "tlc", LearningState.PRECHARGE_RELAX,
            LearningState.DISCHARGE_1, tick, gate_case=gate_case, stop_kind=stop_kind,
        )
        assert len(gates) == 1 and gates[0][0] == LearningState.VERIFY_VOK_QEN
        assert not any(c.startswith(("set_mode_cc(", "set_current(", "input_on(")) for c in load.calls)
        expected = (
            ("FAULT", "DMM_FEEDBACK_LOST") if gate_case == "nan"
            else ("STOPPED", "USER_STOP_REQUESTED") if stop_kind == "stop"
            else ("FAULT", "ABORT_DURING_D1_GATE")
        )
        assert (result.status, result.reason) == expected
        assert ctrl.fault_reason == expected[1]
        terminal = [e for e in events if e["event_code"] in ("GRACEFUL_STOP", "EMERGENCY_STOP")]
        assert len(terminal) == 1 and expected[1] in terminal[0]["event_message"]


# R16-B12: kizárólag mockolt műszerek, FakePicoSerial és memóriabeli napló.
class TestR16B12:
    PATHS = (
        "tlc1", "tlc2", "tlc1_event", "tlc2_event", "chemid", "ocv", "iec1", "iec2", "discharge",
        "preset", "cc_raise", "cc_cv", "cv_up", "cv_down", "taper",
        "child_tlc", "child_recharge", "child_gg", "child_chemid",
        "child_ocv", "child_iec",
    )

    @staticmethod
    def case(path):
        from Prog.src.charge_controller import ChargeState
        from Prog.src.chemid_controller import ChemIdState
        from Prog.src.discharge_controller import DischargeState
        from Prog.src.iec_dc_resistance_controller import IecDcResistanceState
        from Prog.src.ocv_soc_controller import OcvSocState
        from Prog.tests.test_charge_controller import make_controller as charge
        from Prog.tests.test_chemid_controller import make_controller as chemid
        from Prog.tests.test_discharge_controller import (
            make_discharge_controller as discharge,
        )
        from Prog.tests.test_golden_gg_controller import make_controller as gg
        from Prog.tests.test_iec_dc_resistance_controller import (
            make_iec_controller as iec,
        )
        from Prog.tests.test_ocv_soc_controller import make_ocv_soc_controller as ocv
        from Prog.tests.test_traditional_learning_controller import (
            make_controller as tlc,
        )

        if path in ("tlc1", "tlc2", "tlc1_event", "tlc2_event"):
            ctrl = tlc()[0]
            ctrl._state = getattr(LearningState, "REBOUND_CHECK_" + path[3])
            ctrl._phase_elapsed_s = ctrl._config.rebound_settle_s
            ctrl._discharge_cutoff_V = 10.5
            if path.endswith("_event"):
                # Valódi gauge-callback felület, kizárólag fake BQ-mérésből.
                # A folytatás eseménye már a sikeres Load ON után érkezik.
                ctrl._log_elapsed_s = ctrl._config.sample_interval_s
                ctrl.on_gauge_sample = lambda _snapshot: ctrl._emit({
                    "event_code": "TEST_GAUGE_SAMPLE",
                    "event_message": "Mockolt gauge-mérés befejeződött",
                })
            kind = StepKind.TRADITIONAL_LEARNING
            gate = (ctrl, "_emit" if path.endswith("_event") else "_record_gauge_sample")
        elif path == "chemid":
            ctrl = chemid()[0]
            ctrl._state = ChemIdState.RELAX_5H_FULL
            ctrl._relax_elapsed_s = ctrl._config.relax_full_min_s
            kind = StepKind.CHEMID
            gate = (ctrl, "_record_sample")
        elif path == "ocv":
            ctrl = ocv()[0]
            ctrl._state = OcvSocState.LOG_SOC_POINT
            kind = StepKind.OCV_SOC
            gate = (ctrl._dmm_v, "read_voltage")
        elif path.startswith("iec"):
            ctrl = iec()[0]
            ctrl._state = getattr(IecDcResistanceState, "LOAD_" + path[-1])
            kind = StepKind.IEC_DC_RESISTANCE
            gate = (ctrl._dmm_v, "read_voltage")
        elif path == "discharge":
            ctrl = discharge()[0]
            ctrl._state = DischargeState.DISCHARGE_CC_SETUP
            kind = StepKind.DISCHARGE
            gate = (ctrl._dmm_v, "read_voltage")
        elif path.startswith("child_"):
            which = path[6:]
            factory = {
                "tlc": tlc, "recharge": tlc, "gg": gg,
                "chemid": chemid, "ocv": ocv, "iec": iec,
            }[which]
            ctrl = factory()[0]
            if which in ("tlc", "recharge"):
                ctrl._state = (
                    LearningState.PRECHARGE if which == "tlc" else LearningState.RECHARGE
                )
                # A valódi lusta gyermekkonstrukció, kizárólag fake BQ-transporttal.
                ctrl._run_charge(0.0, LearningState.PRECHARGE_RELAX)
            elif which in ("gg", "chemid"):
                ctrl._enter_full_charge()
            else:
                ctrl._enter_precharge()
            ctrl._charge_ctrl._state = ChargeState.PSU_PRESET
            kind = {
                "tlc": StepKind.TRADITIONAL_LEARNING, "recharge": StepKind.TRADITIONAL_LEARNING,
                "gg": StepKind.GOLDEN_GG, "chemid": StepKind.CHEMID,
                "ocv": StepKind.OCV_SOC, "iec": StepKind.IEC_DC_RESISTANCE,
            }[which]
            gate = (ctrl._dmm_v, "read_voltage")
        else:
            ctrl = charge()[0]
            kind = StepKind.CHARGE
            if path == "preset":
                ctrl._state = ChargeState.PSU_PRESET
                gate = (ctrl._dmm_v, "read_voltage")
            else:
                ctrl._state = {
                    "cc_raise": ChargeState.CHARGE_CC, "cc_cv": ChargeState.CHARGE_CC,
                    "cv_up": ChargeState.CHARGE_CV_DMM_CONTROL,
                    "cv_down": ChargeState.CHARGE_CV_DMM_CONTROL,
                    "taper": ChargeState.TAPER_HOLD,
                }[path]
                target = ctrl._effective_charge_target_V()
                ctrl._dmm_v.voltage_V = target + (
                    -1.0 if path == "cc_raise" else -0.03 if path == "cc_cv"
                    else -0.2 if path == "cv_up" else 0.05
                )
                ctrl._u_psu_set = target
                ctrl._charge_current_set_A = 1.0
                ctrl._psu.current_A = 0.5
                ctrl._psu.voltage_V = target + 0.1
                ctrl._psu.output_commanded_on = True
                gate = (ctrl._psu, "measure_output_current")
        return ctrl, kind, gate

    @staticmethod
    def runner(ctrl, kind, monkeypatch, tick):
        from unittest.mock import Mock

        runner, logger, _dmm, events = _f26_mock_logger_runner(
            TestRunnerConfig(runner_tick_s=tick, sleep_enabled=False),
        )
        runner._instruments._psu = ctrl._psu
        runner._instruments._load = ctrl._load
        runner._instruments._dmm_v = ctrl._dmm_v
        runner._instruments._dmm_t = ctrl._dmm_t
        runner._controller_for_step = lambda step: ctrl
        runner._build_sample = lambda *a, **k: {}
        runner._should_poll_device_errors = lambda: False
        runner._poll_device_errors = lambda *a, **k: []
        common_off = Mock(wraps=runner._instruments.safe_all_off)
        monkeypatch.setattr(runner._instruments, "safe_all_off", common_off)
        order = []
        for label, device in (("Load", ctrl._load), ("PSU", ctrl._psu)):
            original = device.safe_off
            def off(label=label, original=original):
                order.append(label)
                return original()
            monkeypatch.setattr(device, "safe_off", off)
        step = TestStep(kind, "R16-B12 átadás")
        return runner, logger, events, common_off, order, step

    @staticmethod
    def active(ctrl):
        prefixes = (
            "set_mode_cc(", "set_current(", "input_on(",
            "set_output_voltage(", "set_output_current(", "output_on(",
        )
        return [c for dev in (ctrl._load, ctrl._psu) for c in dev.calls if c.startswith(prefixes)]

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    @pytest.mark.parametrize("tick", [5.0, 30.0])
    def test_stop_during_measurement(self, monkeypatch, path, stop_kind, tick):
        from threading import Event, Thread, get_ident

        ctrl, kind, (device, method) = self.case(path)
        runner, logger, events, common_off, order, step = self.runner(
            ctrl, kind, monkeypatch, tick,
        )
        ctrl._load.calls.clear()
        ctrl._psu.calls.clear()
        entered, proceed = Event(), Event()
        errors, identities, states = [], [], []
        original = getattr(device, method)
        iec_path = path in ("iec1", "iec2")
        active_before_stop = []
        io_count = 0
        def measured(*args, **kwargs):
            nonlocal io_count
            io_count += 1
            if iec_path and io_count > 32:
                runner.request_stop()
                raise AssertionError("IEC trigger nem sult el 32 I/O alatt")
            if iec_path and not ctrl._load.input_commanded_on:
                return original(*args, **kwargs)
            if iec_path:
                active_before_stop[:] = self.active(ctrl)
            entered.set()
            assert proceed.wait(3), "A leállításkérés nem érkezett meg"
            return original(*args, **kwargs)
        monkeypatch.setattr(device, method, measured)
        reason = {
            "stop": "USER_STOP_REQUESTED",
            "abort": "ABORT_DURING_HANDOFF",
            "emergency": "USER_EMERGENCY_STOP",
        }[stop_kind]
        def inject():
            try:
                assert entered.wait(3), "A mérés nem indult el"
                identities.append(get_ident())
                if stop_kind == "stop":
                    runner.request_stop()
                else:
                    # A GUI-worker abortja ugyanide delegál.
                    runner.request_emergency_stop(reason)
            except BaseException as exc:  # noqa: BLE001 -- a főszál ellenőrzi
                errors.append(exc)
            finally:
                proceed.set()
        observed_method = (
            "run_pulse_block" if iec_path and hasattr(ctrl, "run_pulse_block") else "advance"
        )
        original_advance = getattr(ctrl, observed_method)
        def advance(dt_s):
            state = original_advance(dt_s)
            if entered.is_set():
                assert self.active(ctrl) == active_before_stop if iec_path else not self.active(ctrl)
                before = (
                    ctrl.state,
                    getattr(ctrl, "_rebound_retry_count", None),
                    getattr(ctrl, "_discharge_cutoff_V", None),
                    getattr(ctrl, "_step_integrator", None),
                    dict(getattr(ctrl, "_point_quality_counts", {})),
                    getattr(ctrl, "_u_psu_set", None),
                )
                # Terminális feloldás nélkül is tartósan tiltva maradjon.
                original_advance(0.0)
                after = (
                    ctrl.state,
                    getattr(ctrl, "_rebound_retry_count", None),
                    getattr(ctrl, "_discharge_cutoff_V", None),
                    getattr(ctrl, "_step_integrator", None),
                    dict(getattr(ctrl, "_point_quality_counts", {})),
                    getattr(ctrl, "_u_psu_set", None),
                )
                assert after == before
                assert self.active(ctrl) == active_before_stop if iec_path else not self.active(ctrl)
                states.append(state)
            return state
        monkeypatch.setattr(ctrl, observed_method, advance)
        injector = Thread(target=inject)
        injector.start()
        try:
            result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        finally:
            proceed.set()
            injector.join(3)
        assert not injector.is_alive()
        assert not errors, errors
        assert identities and identities[0] != get_ident()
        assert entered.is_set(), "Az I/O-triggernek kotelezo elsulnie"
        assert len(states) == 1
        status = "STOPPED" if stop_kind == "stop" else "FAULT"
        assert (result.status, result.reason) == (status, reason)
        assert self.active(ctrl) == active_before_stop if iec_path else not self.active(ctrl)
        common_off.assert_called_once()
        assert order == ["Load", "PSU"]
        assert result.safe_off_attempted
        assert result.safe_off_result["Load"] and result.safe_off_result["PSU"]
        assert not ctrl._load.input_commanded_on and not ctrl._psu.output_commanded_on
        checkpoint = logger.write_checkpoint.call_args.args[0]
        assert (checkpoint["status"], checkpoint["reason"]) == (status, reason)
        code = "GRACEFUL_STOP" if stop_kind == "stop" else "EMERGENCY_STOP"
        terminal = [e for e in events if e["event_code"] in (
            "GRACEFUL_STOP", "EMERGENCY_STOP", "TEST_DONE",
        )]
        assert len(terminal) == 1
        assert terminal[0]["event_code"] == code
        assert reason in terminal[0]["event_message"]
        assert len(_f26_log_calls(logger, code)) == 1

    @pytest.mark.parametrize("path", PATHS)
    def test_safety_fault_precedes_pending_stop(self, monkeypatch, path):
        ctrl, kind, _gate = self.case(path)
        runner, logger, _events, common_off, _order, step = self.runner(
            ctrl, kind, monkeypatch, 5.0,
        )
        # A hook már igaz, de a sikertelen mérés továbbra is mérési FAULT.
        runner._prepare_relax_sampling(step, ctrl)
        runner.request_stop()
        def unavailable():
            raise RuntimeError("R16-B12 teszt: DMM-olvasási hiba")
        monkeypatch.setattr(ctrl._dmm_v, "read_voltage", unavailable)
        ctrl.advance(5.0)
        assert ctrl.state.name == "FAULT"
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert not self.active(ctrl)
        common_off.assert_not_called()
        assert not logger.write_checkpoint.called

    @pytest.mark.parametrize("path", ["tlc1", "tlc2", "chemid", "ocv", "iec1", "iec2", "discharge", "preset"])
    def test_final_safety_gate_is_not_skipped(self, monkeypatch, path):
        from Prog.src.safety import FaultCode, SafetyResult

        ctrl, kind, (device, method) = self.case(path)
        runner, _logger, _events, _off, _order, step = self.runner(
            ctrl, kind, monkeypatch, 5.0,
        )
        runner._prepare_relax_sampling(step, ctrl)
        original = getattr(device, method)
        def measured(*args, **kwargs):
            value = original(*args, **kwargs)
            runner.request_stop()
            return value
        monkeypatch.setattr(device, method, measured)
        # A túl korai őr ezt a már meglévő safety-kaput fedné el.
        if path == "preset":
            ctrl._load.input_commanded_on = True
        else:
            original_check = ctrl._safety.check_concurrent_psu_load
            def concurrent(*, psu_commanded_on, load_commanded_on):
                if load_commanded_on:
                    return SafetyResult(fault=FaultCode.CONCURRENT_PSU_LOAD_ON)
                return original_check(
                    psu_commanded_on=psu_commanded_on, load_commanded_on=load_commanded_on,
                )
            monkeypatch.setattr(ctrl._safety, "check_concurrent_psu_load", concurrent)
        ctrl.advance(5.0)
        assert ctrl.state.name == "FAULT"
        assert ctrl.fault_reason == "CONCURRENT_PSU_LOAD_ON"
        assert not self.active(ctrl), self.active(ctrl)

    @pytest.mark.parametrize("path", PATHS)
    def test_none_hook_keeps_working_path(self, path):
        ctrl, _kind, _gate = self.case(path)
        ctrl._load.calls.clear()
        ctrl._psu.calls.clear()
        ctrl.advance(5.0)
        assert self.active(ctrl), path
        assert ctrl.state.name != "FAULT", ctrl.fault_reason


    @pytest.mark.parametrize("path", PATHS)
    def test_pending_stop_preserves_uncommitted_state(self, monkeypatch, path):
        ctrl, kind, _gate = self.case(path)
        runner, _logger, _events, _off, _order, step = self.runner(
            ctrl, kind, monkeypatch, 5.0,
        )
        runner._prepare_relax_sampling(step, ctrl)
        runner.request_stop()
        ctrl._load.calls.clear()
        ctrl._psu.calls.clear()
        fields = (
            "_rebound_retry_count", "_discharge_cutoff_V", "_step_integrator",
            "_step_discharge_phase_elapsed_s", "_target_removed_Ah",
            "_u_psu_set", "_charge_current_set_A", "_i_discharge_set",
        )
        target = ctrl._charge_ctrl if path.startswith("child_") else ctrl
        before = tuple(getattr(target, name, None) for name in fields)
        ctrl.advance(5.0)
        assert not self.active(ctrl)
        assert tuple(getattr(target, name, None) for name in fields) == before

    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    @pytest.mark.parametrize("tick", [5.0, 30.0])
    def test_stop_during_fast_loop_wait(self, monkeypatch, stop_kind, tick):
        import Prog.src.charge_controller as module

        ctrl, kind, _gate = self.case("cv_up")
        runner, logger, events, common_off, order, step = self.runner(
            ctrl, kind, monkeypatch, tick,
        )
        ctrl._config.fast_loop_enabled = True
        ctrl._config.fast_loop_max_substeps = 3
        now, waits = [0.0], []
        monkeypatch.setattr(module.time, "perf_counter", lambda: now[0])
        reason = (
            "USER_STOP_REQUESTED" if stop_kind == "stop"
            else "ABORT_DURING_HANDOFF" if stop_kind == "abort"
            else "USER_EMERGENCY_STOP"
        )
        def wait(delay):
            # A várakozás mockolt: a jelzés biztosan a résztickek között érkezik.
            now[0] += delay
            waits.append(delay)
            if stop_kind == "stop":
                runner.request_stop()
            else:
                runner.request_emergency_stop(reason)
            ctrl._load.calls.clear()
            ctrl._psu.calls.clear()
        monkeypatch.setattr(module.time, "sleep", wait)
        original_advance = ctrl.advance
        monkeypatch.setattr(ctrl, "advance", lambda _dt: original_advance(tick))
        result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        assert waits
        assert not self.active(ctrl), self.active(ctrl)
        assert (result.status, result.reason) == (
            "STOPPED" if stop_kind == "stop" else "FAULT", reason,
        )
        common_off.assert_called_once()
        assert order == ["Load", "PSU"]
        assert logger.write_checkpoint.call_args.args[0]["reason"] == reason
        assert any(reason in e.get("event_message", "") for e in events)


# R16-B12F2: OFF a következő várakozás előtt, mért töltés és faultelsőbbség.
class TestR16B12F2:
    @staticmethod
    def request(runner, stop_kind):
        reason = {
            "stop": "USER_STOP_REQUESTED",
            "abort": "ABORT_DURING_HANDOFF",
            "emergency": "USER_EMERGENCY_STOP",
        }[stop_kind]
        if stop_kind == "stop":
            runner.request_stop()
        else:
            runner.request_emergency_stop(reason)
        return reason

    @staticmethod
    def terminal(result, logger, events, common_off, order, reason, status):
        assert (result.status, result.reason) == (status, reason)
        common_off.assert_called_once()
        assert order == ["Load", "PSU"]
        assert result.safe_off_attempted
        assert result.safe_off_result["Load"] and result.safe_off_result["PSU"]
        checkpoint = logger.write_checkpoint.call_args.args[0]
        assert (checkpoint["status"], checkpoint["reason"]) == (status, reason)
        terminal = [e for e in events if e["event_code"] in (
            "GRACEFUL_STOP", "EMERGENCY_STOP", "TEST_DONE",
        )]
        assert len(terminal) == 1
        assert terminal[0]["event_code"] == (
            "GRACEFUL_STOP" if status == "STOPPED" else "EMERGENCY_STOP"
        )
        assert reason in terminal[0]["event_message"]

    @pytest.mark.parametrize("tick", [5.0, 30.0])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    @pytest.mark.parametrize("arrival", ["first_read", "fast_wait", "substep_read"])
    def test_fast_loop_returns_before_next_blocking_read(
        self, monkeypatch, tick, stop_kind, arrival,
    ):
        from threading import Event, Thread

        import Prog.src.charge_controller as module

        ctrl, kind, _gate = TestR16B12.case("cv_down")
        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, kind, monkeypatch, tick,
        )
        runner._config.sleep_enabled = True
        ctrl._config.fast_loop_enabled = True
        ctrl._config.fast_loop_max_substeps = 4
        now = [0.0]
        entered, proceed, release_read = Event(), Event(), Event()
        blocked_read, late_waits, errors, reasons = [], [], [], []
        monkeypatch.setattr(module.time, "perf_counter", lambda: now[0])

        def inject():
            try:
                assert entered.wait(3), "A jelzés helye nem érhető el"
                reasons.append(self.request(runner, stop_kind))
                ctrl._psu.calls.clear()
                ctrl._load.calls.clear()
            except BaseException as exc:  # noqa: BLE001 -- a főszál ellenőrzi
                errors.append(exc)
            finally:
                proceed.set()

        original_current = ctrl._psu.measure_output_current
        reads = [0]
        def current():
            value = original_current()
            reads[0] += 1
            if (arrival == "first_read" and reads[0] == 1) or (
                arrival == "substep_read" and reads[0] == 2
            ):
                entered.set()
                assert proceed.wait(3)
            return value
        monkeypatch.setattr(ctrl._psu, "measure_output_current", current)
        original_voltage = ctrl._dmm_v.read_voltage
        def voltage():
            if runner.stop_requested or runner.emergency_stop_requested:
                # A hibás kód következő olvasása vezérelten blokkolható.
                blocked_read.append(True)
                release_read.wait(0.05)
            return original_voltage()
        monkeypatch.setattr(ctrl._dmm_v, "read_voltage", voltage)

        def wait(delay):
            if runner.stop_requested or runner.emergency_stop_requested:
                late_waits.append((delay, ctrl._psu.output_commanded_on))
            elif arrival == "fast_wait":
                entered.set()
                assert proceed.wait(3)
            now[0] += delay
        monkeypatch.setattr(module.time, "sleep", wait)
        original_advance = ctrl.advance
        monkeypatch.setattr(ctrl, "advance", lambda _dt: original_advance(tick))
        def sample(*args, **kwargs):
            # C-01/V-6: graceful STOP-nál a megszakított tick még naplózandó.
            assert stop_kind == "stop"
            assert common_off.call_count == 0
            assert ctrl._psu.output_commanded_on
            return {}
        monkeypatch.setattr(runner, "_build_sample", sample)
        injector = Thread(target=inject)
        injector.start()
        try:
            result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        finally:
            release_read.set()
            proceed.set()
            injector.join(3)
        assert not injector.is_alive() and not errors, errors
        assert reasons and not blocked_read and not late_waits
        assert logger.log_sample.call_count == (1 if stop_kind == "stop" else 0)
        assert not TestR16B12.active(ctrl)
        self.terminal(
            result, logger, events, common_off, order, reasons[0],
            "STOPPED" if stop_kind == "stop" else "FAULT",
        )

    @pytest.mark.parametrize("tick", [5.0, 30.0])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    @pytest.mark.parametrize("path", ["cv_up", "cv_down", "taper"])
    @pytest.mark.parametrize("fault", ["none", "time", "series", "both"])
    def test_stopped_write_keeps_tick_safety_and_single_integration(
        self, monkeypatch, tick, stop_kind, path, fault,
    ):
        from unittest.mock import Mock

        import Prog.src.charge_controller as module

        ctrl, kind, _gate = TestR16B12.case(path)
        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, kind, monkeypatch, tick,
        )
        runner._config.sleep_enabled = True
        if fault in ("time", "both"):
            ctrl._elapsed_s = ctrl._config.max_charge_time_s
        if fault in ("series", "both"):
            ctrl._psu.voltage_V = ctrl._effective_charge_target_V() + 5.0
        original_current = ctrl._psu.measure_output_current
        reasons = []
        def current():
            value = original_current()
            reasons.append(self.request(runner, stop_kind))
            return value
        monkeypatch.setattr(ctrl._psu, "measure_output_current", current)
        integration = Mock(wraps=ctrl._integrate)
        monkeypatch.setattr(ctrl, "_integrate", integration)
        original_advance = ctrl.advance
        monkeypatch.setattr(ctrl, "advance", lambda _dt: original_advance(tick))
        late_waits = []
        monkeypatch.setattr(module.time, "sleep", lambda delay: late_waits.append(delay))
        ctrl._psu.calls.clear()
        result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        # HEAD: 0,5 A * 30 s = 0,0041666667 Ah; sem elvesztés, sem kettőzés.
        expected_ah = 0.5 * tick / 3600.0
        integration.assert_called_once_with(tick, signed_current_A=0.5, source="PSU_READBACK")
        assert ctrl.accumulated_charge_Ah == pytest.approx(expected_ah)
        assert result.total_charge_ah == pytest.approx(expected_ah)
        assert logger.write_checkpoint.call_args.args[0]["charge_ah"] == pytest.approx(expected_ah)
        assert not late_waits and not TestR16B12.active(ctrl)
        reason = (
            "MAX_CHARGE_TIME_REACHED" if fault in ("time", "both")
            else "SERIES_DROP_TOO_HIGH" if fault == "series" else reasons[0]
        )
        status = "STOPPED" if fault == "none" and stop_kind == "stop" else "FAULT"
        self.terminal(result, logger, events, common_off, order, reason, status)

    @pytest.mark.parametrize("tick", [5.0, 30.0])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    def test_signal_during_flush_precedes_runner_pacing(self, monkeypatch, tick, stop_kind):
        import Prog.src.test_runner as module

        ctrl, kind, _gate = TestR16B12.case("cv_down")
        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, kind, monkeypatch, tick,
        )
        runner._config.sleep_enabled = True
        reasons, waits = [], []
        def flush():
            if not reasons:
                reasons.append(self.request(runner, stop_kind))
        logger.flush_all.side_effect = flush
        monkeypatch.setattr(module.time, "sleep", lambda delay: waits.append(delay))
        result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        assert not waits
        self.terminal(
            result, logger, events, common_off, order, reasons[0],
            "STOPPED" if stop_kind == "stop" else "FAULT",
        )


# R16-B12F3: C-01/V-6 mintamegőrzés, pacing és további I/O nélkül.
class TestR16B12F3:
    @pytest.mark.parametrize("tick", [5.0, 30.0])
    def test_graceful_stop_keeps_csv_tick_before_single_off_without_pacing(
        self, monkeypatch, tmp_path, tick,
    ):
        import csv
        from unittest.mock import Mock

        import Prog.src.test_runner as module
        from Prog.src.logger import LogConfig, Logger

        ctrl, kind, _gate = TestR16B12.case("cv_down")
        ctrl._config.fast_loop_enabled = False
        runner, _logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, kind, monkeypatch, tick,
        )
        logger = Mock(wraps=Logger(tmp_path, LogConfig()))
        runner._logger = logger
        runner._config.sleep_enabled = True
        now, ticks, trace, waits = [0.0], [0], [], []
        monkeypatch.setattr(module.time, "perf_counter", lambda: now[0])
        original_advance = ctrl.advance
        def advance(_dt):
            original_advance(tick)
            ticks[0] += 1
            if ticks[0] == 2:
                trace.append("stop")
                runner.request_stop()
        monkeypatch.setattr(ctrl, "advance", advance)

        def wait(delay):
            waits.append((ticks[0], delay))
            assert not runner.stop_requested, "STOP után pacing-sleep történt"
            now[0] += delay
        monkeypatch.setattr(module.time, "sleep", wait)

        original_sample = TestRunner._build_sample.__get__(runner)
        def sample(*args, **kwargs):
            if runner.stop_requested:
                assert common_off.call_count == 0
                trace.append("sample")
            return original_sample(*args, **kwargs)
        monkeypatch.setattr(runner, "_build_sample", sample)
        runner.on_sample = lambda row: trace.append("logged") if runner.stop_requested else None

        # A mintasor olvasása a régi szerződés része; az ezt követő
        # diagnosztika és konfiguráció csak az OFF után futhat.
        original_poll = runner._poll_device_errors
        def poll(*args, **kwargs):
            if runner.stop_requested:
                assert common_off.call_count == 1
                trace.append("poll_after_off")
            return original_poll(*args, **kwargs)
        monkeypatch.setattr(runner, "_poll_device_errors", poll)
        monkeypatch.setattr(runner, "_should_poll_device_errors", lambda: True)
        for name in ("_reconfigure_dmm_nplc_for_relax_substate", "_start_prepared_relax_sampling"):
            original = getattr(runner, name)
            def configure(*args, _original=original, **kwargs):
                assert not runner.stop_requested, "STOP után új konfiguráció indult"
                return _original(*args, **kwargs)
            monkeypatch.setattr(runner, name, configure)
        original_off = type(runner._instruments).safe_all_off
        def off():
            trace.append("off")
            return original_off(runner._instruments)
        common_off.side_effect = off

        result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        rows = list(csv.DictReader((tmp_path / "samples.csv").read_text(encoding="utf-8").splitlines()))
        assert len(rows) == 2, "A graceful STOP tickjének CSV-sora elveszett"
        assert logger.log_sample.call_count == 2
        assert ticks == [2] and len(waits) == 1 and waits[0][0] == 1
        assert trace == ["stop", "sample", "logged", "off", "poll_after_off"]
        expected_ah = 2 * 0.5 * tick / 3600.0
        assert float(rows[-1]["accumulated_charge_Ah"]) == pytest.approx(expected_ah)
        assert result.total_charge_ah == pytest.approx(expected_ah)
        TestR16B12F2.terminal(
            result, logger, events, common_off, order, "USER_STOP_REQUESTED", "STOPPED",
        )


# R16-B12F4: terminális mintamegőrzés és meg nem történt parancsok naplózása.
class TestR16B12F4:
    @pytest.mark.parametrize("end", ["fault", "done"])
    @pytest.mark.parametrize("signal", ["emergency", "stop"])
    @pytest.mark.parametrize("tick", [5.0, 30.0])
    def test_finished_tick_keeps_sample_without_reabort_or_wait(
        self, monkeypatch, end, signal, tick,
    ):
        from unittest.mock import Mock

        import Prog.src.test_runner as module

        devices, kind, _gate = TestR16B12.case("cv_down")
        ctrl = _StubChargeCtrl(
            steps_to_done=2 if end == "done" else 100,
            fault_at_step=2 if end == "fault" else None,
        )
        for name in ("_psu", "_load", "_dmm_v", "_dmm_t"):
            setattr(ctrl, name, getattr(devices, name))
        ctrl.emergency_stop = Mock()
        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, kind, monkeypatch, tick,
        )
        runner._config.sleep_enabled = True
        now, waits, trace = [0.0], [], []
        monkeypatch.setattr(module.time, "perf_counter", lambda: now[0])
        original_advance = ctrl.advance
        def advance(dt_s):
            state = original_advance(dt_s)
            if ctrl._tick == 2:
                trace.append("request")
                TestR16B12F2.request(runner, signal)
            return state
        monkeypatch.setattr(ctrl, "advance", advance)

        def sample(*args, **kwargs):
            trace.append("sample")
            return {"state": ctrl.state.name, "fault_reason": ctrl.fault_reason}
        monkeypatch.setattr(runner, "_build_sample", sample)
        def wait(delay):
            assert not (runner.stop_requested or runner.emergency_stop_requested)
            waits.append(delay)
            now[0] += delay
        monkeypatch.setattr(module.time, "sleep", wait)
        def poll(*args, **kwargs):
            if runner.stop_requested or runner.emergency_stop_requested:
                assert common_off.call_count == 1, "Terminális poll történt az OFF előtt"
            return []
        monkeypatch.setattr(runner, "_poll_device_errors", poll)
        monkeypatch.setattr(runner, "_should_poll_device_errors", lambda: True)
        original_off = type(runner._instruments).safe_all_off
        def off():
            trace.append("off")
            return original_off(runner._instruments)
        common_off.side_effect = off

        result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        reason = (
            "STUB_CHARGE_FAULT" if end == "fault"
            else "USER_EMERGENCY_STOP" if signal == "emergency"
            else "USER_STOP_REQUESTED"
        )
        status = "STOPPED" if end == "done" and signal == "stop" else "FAULT"
        assert logger.log_sample.call_count == 2
        assert logger.log_sample.call_args.args[0]["state"] == (
            "FAULT" if end == "fault" else "CHARGE_DONE"
        )
        assert trace == ["sample", "request", "sample", "off"]
        assert ctrl._tick == 2 and len(waits) == 1
        ctrl.emergency_stop.assert_not_called()
        assert result.total_charge_ah == pytest.approx(0.02)
        TestR16B12F2.terminal(
            result, logger, events, common_off, order, reason, status,
        )

    @pytest.mark.parametrize("path", ["tlc1", "tlc2"])
    @pytest.mark.parametrize("outcome", ["stop", "success", "concurrent", "power", "on_failure"])
    def test_rebound_continuation_event_requires_successful_resume(
        self, monkeypatch, path, outcome,
    ):
        from Prog.src.safety import FaultCode, SafetyResult

        ctrl, _kind, _gate = TestR16B12.case(path)
        assert ctrl._discharge_cutoff_V == 10.5
        before = (ctrl._rebound_retry_count, ctrl._discharge_cutoff_V)
        trace, events = [], []
        def event(payload):
            events.append(payload)
            if payload["event_code"] == "LEARNING_REBOUND_TOO_HIGH":
                assert ctrl._load.input_commanded_on
                trace.append("event")
        ctrl.on_event = event
        ctrl.handoff_stop_requested = lambda: outcome == "stop"
        original_on = ctrl._load.input_on
        def on(*args, **kwargs):
            if outcome == "on_failure":
                raise RuntimeError("Mockolt Load ON hiba")
            original_on(*args, **kwargs)
            trace.append("on")
        monkeypatch.setattr(ctrl._load, "input_on", on)
        original_concurrent = ctrl._safety.check_concurrent_psu_load
        def concurrent(*, psu_commanded_on, load_commanded_on):
            if outcome == "concurrent" and load_commanded_on:
                return SafetyResult(fault=FaultCode.CONCURRENT_PSU_LOAD_ON)
            return original_concurrent(
                psu_commanded_on=psu_commanded_on, load_commanded_on=load_commanded_on,
            )
        monkeypatch.setattr(ctrl._safety, "check_concurrent_psu_load", concurrent)
        if outcome == "power":
            monkeypatch.setattr(
                ctrl._safety, "check_load_power",
                lambda *args: SafetyResult(fault=FaultCode.LOAD_POWER_LIMIT),
            )
        ctrl.advance(5.0)
        continuation = [e for e in events if e["event_code"] == "LEARNING_REBOUND_TOO_HIGH"]
        if outcome == "success":
            assert trace == ["on", "event"] and len(continuation) == 1
            assert ctrl.state == getattr(LearningState, "DISCHARGE_" + path[-1])
            assert ctrl._rebound_retry_count == before[0] + 1
            expected = 1.73 * ctrl._profile.cell_count  # Q-34: első retry.
            assert ctrl._discharge_cutoff_V == pytest.approx(expected)
            assert f"{expected:.3f}V" in continuation[0]["event_message"]
        else:
            assert not continuation and not trace
            assert not ctrl._load.input_commanded_on
            if outcome == "stop":
                assert (ctrl._rebound_retry_count, ctrl._discharge_cutoff_V) == before
                assert ctrl.state == getattr(LearningState, "REBOUND_CHECK_" + path[-1])
            else:
                assert ctrl.state == LearningState.FAULT
                assert ctrl.fault_reason == {
                    "concurrent": "CONCURRENT_PSU_LOAD_ON",
                    "power": "LOAD_POWER_LIMIT_EXCEEDED",
                    "on_failure": "DISCHARGE_RESUME_AFTER_REBOUND_FAILED: Mockolt Load ON hiba",
                }[outcome]

    def test_ocv_pending_setup_logs_zero_commanded_current_on_stop(self, monkeypatch):
        ctrl, kind, (device, method) = TestR16B12.case("ocv")
        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, kind, monkeypatch, 5.0,
        )
        original_read = getattr(device, method)
        def read():
            value = original_read()
            runner.request_stop()
            return value
        monkeypatch.setattr(device, method, read)
        runner._build_sample = TestRunner._build_sample.__get__(runner)
        result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        assert logger.log_sample.call_args.args[0]["state"] == "STEP_DISCHARGE"
        assert not ctrl._step_discharge_started
        assert logger.log_sample.call_count == 1
        assert logger.log_sample.call_args.args[0]["load_set_current_A"] == 0.0
        assert not TestR16B12.active(ctrl)
        TestR16B12F2.terminal(
            result, logger, events, common_off, order, "USER_STOP_REQUESTED", "STOPPED",
        )
        # A már megkezdett kisütés naplózási szerződése változatlan.
        ctrl._state = OcvSocState.STEP_DISCHARGE
        ctrl._step_discharge_started = True
        assert ctrl.i_load_set_A == ctrl._discharge_current_A > 0.0


# R16-B13: élő pulse-stop, kizárólag mock műszerek és memóriabeli napló.
@pytest.mark.usefixtures("_legacy_runner_hardware_acquisition")
class TestR16B13:
    @staticmethod
    def make(fast, mode):
        from Prog.src.ocv_soc_controller import OcvSocConfig
        from Prog.tests.test_ocv_soc_controller import make_ocv_soc_controller

        ctrl = make_ocv_soc_controller(config=OcvSocConfig(
            pulse_trigger_mode=mode, fast_transient_enabled=fast,
        ))[0]
        ctrl._state = OcvSocState.IMPULSE_FAST_ON if fast else OcvSocState.IMPULSE_ON
        ctrl._u_batt = 13.0
        ctrl._dmm_valid = True
        ctrl._impulse_current_A_set = 1.4
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        service._terminate_voltage_V = 11.4
        return ctrl, service

    @staticmethod
    def run_signal(monkeypatch, ctrl, device, method, stop_kind, verify_tick):
        from threading import Event, Thread, get_ident

        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, StepKind.OCV_SOC, monkeypatch, 5.0,
        )
        entered, proceed = Event(), Event()
        errors, reasons, identities = [], [], []
        original = getattr(device, method)

        def blocked(*args, **kwargs):
            value = original(*args, **kwargs)
            entered.set()
            assert proceed.wait(3), "A pulse-stop nem érkezett meg"
            return value

        monkeypatch.setattr(device, method, blocked)

        def inject():
            try:
                assert entered.wait(3), "A pulse mérési kapuja nem érhető el"
                identities.append(get_ident())
                reasons.append(TestR16B12F2.request(runner, stop_kind))
            except BaseException as exc:  # noqa: BLE001 -- a főszál ellenőrzi
                errors.append(exc)
            finally:
                proceed.set()

        original_advance = ctrl.advance

        def advance(dt_s):
            state = original_advance(dt_s)
            if entered.is_set():
                verify_tick()
            return state

        monkeypatch.setattr(ctrl, "advance", advance)
        ctrl._load.calls.clear()
        ctrl._psu.calls.clear()
        injector = Thread(target=inject)
        injector.start()
        try:
            result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        finally:
            proceed.set()
            injector.join(3)
        assert not injector.is_alive() and not errors, errors
        assert identities and identities[0] != get_ident()
        TestR16B12F2.terminal(
            result, logger, events, common_off, order, reasons[0],
            "STOPPED" if stop_kind == "stop" else "FAULT",
        )
        assert ctrl.fault_reason == reasons[0]
        assert not ctrl._load.input_commanded_on and not ctrl._psu.output_commanded_on
        return result

    @pytest.mark.parametrize("mode", [
        "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED", "HARDWARE_PULSE_SOFTWARE_TRIGGERED",
    ])
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("window", [False, True])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    def test_baseline_stop_prevents_all_load_commands(
        self, monkeypatch, mode, fast, window, stop_kind,
    ):
        ctrl, service = self.make(fast, mode)
        if window:
            service._baseline_window_s = 0.4
        initial_state = ctrl.state

        def verify():
            assert ctrl.state == initial_state
            assert not service._started
            assert not any(c.startswith((
                "set_mode_cc(", "set_current(", "input_on(",
                "configure_transient_pulse(", "fire_transient_pulse(",
            )) for c in ctrl._load.calls)

        # A skalár és az ablakos baseline is a valódi szolgáltatáson fut.
        self.run_signal(
            monkeypatch, ctrl, ctrl._dmm_v,
            "fetch_timed_acquisition" if window else "read_voltage",
            stop_kind, verify,
        )

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    @pytest.mark.parametrize("arrival", ["before_config", "before_on", "before_fire"])
    def test_deferred_hardware_gates(self, monkeypatch, fast, stop_kind, arrival):
        ctrl, service = self.make(fast, "HARDWARE_PULSE_SOFTWARE_TRIGGERED")
        assert service.begin_pulse(1.4, 0.2)
        ctrl._state = (
            OcvSocState.IMPULSE_FAST_ACQUIRE if fast else OcvSocState.IMPULSE_ACQUIRE
        )
        initial_state = ctrl.state
        device, method = {
            "before_config": (ctrl._dmm_t, "read_temperature"),
            "before_on": (ctrl._dmm_v, "arm_timed_acquisition"),
            "before_fire": (ctrl._load, "input_on"),
        }[arrival]

        def verify():
            assert ctrl.state == initial_state
            forbidden = ["fire_transient_pulse("]
            if arrival != "before_fire":
                forbidden.append("input_on(")
            if arrival == "before_config":
                forbidden.append("configure_transient_pulse(")
            assert not any(c.startswith(tuple(forbidden)) for c in ctrl._load.calls)

        self.run_signal(monkeypatch, ctrl, device, method, stop_kind, verify)

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    def test_standard_gate_rechecks_after_configuration(self, monkeypatch, fast, stop_kind):
        ctrl, _service = self.make(fast, "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED")
        initial_state = ctrl.state

        def verify():
            assert ctrl.state == initial_state
            assert not any(c.startswith("input_on(") for c in ctrl._load.calls)

        self.run_signal(
            monkeypatch, ctrl, ctrl._load, "set_current", stop_kind, verify,
        )


# R16-B13F1: az aktív fetch után az OFF az első műszerparancs.
@pytest.mark.usefixtures("_legacy_runner_hardware_acquisition")
class TestR16B13F1:
    @pytest.mark.parametrize("mode", [
        "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED", "HARDWARE_PULSE_SOFTWARE_TRIGGERED",
    ])
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    def test_active_fetch_off_precedes_reads_and_cleanup(
        self, monkeypatch, mode, fast, stop_kind,
    ):
        from Prog.tests.mock_drivers.fake_clock import (
            InstrumentLatencyClock,
            bind_pulse_instrument_latency,
        )

        ctrl, service = TestR16B13.make(fast, mode)
        clock = InstrumentLatencyClock()
        bind_pulse_instrument_latency(clock, ctrl._dmm_v, ctrl._load)
        service._time_source = clock
        ctrl._load.trace_response = "1.4,1.4"
        assert service.begin_pulse(1.4, 2.0)
        hardware = mode == "HARDWARE_PULSE_SOFTWARE_TRIGGERED"
        if not hardware:
            assert service.step_chunk()
        prefix = service.measured_energy_so_far()
        ctrl._book_measured_pulse_energy(service)
        before_samples = len(service._samples)
        ctrl._state = (
            OcvSocState.IMPULSE_FAST_ACQUIRE if fast else OcvSocState.IMPULSE_ACQUIRE
        )
        service._recovery_duration_s = 0.4
        transcript, signal_at = [], []
        original_request = TestR16B12F2.request

        def request(runner, kind):
            reason = original_request(runner, kind)
            signal_at.append(clock.now)
            return reason

        monkeypatch.setattr(TestR16B12F2, "request", staticmethod(request))
        for device, label, methods in (
            (ctrl._load, "Load", (
                "input_off", "input_on", "measure_current", "fetch_trace",
                "fire_transient_pulse",
            )),
            (ctrl._psu, "PSU", ("all_outputs_off",)),
            (ctrl._dmm_v, "DMM", (
                "reset_to_single_sample", "set_io_timeout_ms", "set_nplc",
                "configure_timed_acquisition", "arm_timed_acquisition",
                "fetch_timed_acquisition",
            )),
        ):
            for method in methods:
                original = getattr(device, method)

                def recorded(*args, original=original, method=method, label=label, **kwargs):
                    if signal_at:
                        transcript.append((label, method, clock.now))
                        # A retry-budgetek virtuálisak, nincs valódi várakozás.
                        clock.advance({
                            "reset_to_single_sample": 30.0, "measure_current": 15.0,
                            "fetch_trace": 15.0, "set_io_timeout_ms": 7.0, "set_nplc": 5.0,
                        }.get(method, 0.0))
                    return original(*args, **kwargs)

                monkeypatch.setattr(device, method, recorded)

        def forbidden_finalize():
            raise AssertionError("Megszakítás után nem indulhat véglegesítés vagy recovery")

        monkeypatch.setattr(service, "finalize", forbidden_finalize)

        def verify():
            assert transcript[0] == ("Load", "input_off", signal_at[0]), transcript
            assert service._done and not ctrl._load.input_commanded_on
            assert len(service._samples) > before_samples
            if not hardware:
                assert service.measured_energy_so_far() == prefix
            assert not any(method in {
                "input_on", "fire_transient_pulse", "measure_current",
                "configure_timed_acquisition", "arm_timed_acquisition",
                "fetch_timed_acquisition",
            } for _, method, _ in transcript)

        TestR16B13.run_signal(
            monkeypatch, ctrl, ctrl._dmm_v, "fetch_timed_acquisition", stop_kind, verify,
        )
        measured = service.measured_energy_so_far()
        assert measured.reading_count > 0
        assert measured.removed_Ah > 0
        assert ctrl._pulse_energy_booked == measured
        assert ctrl._energy_integrator.accumulated_discharge_Ah == pytest.approx(
            measured.removed_Ah,
        )
        assert ctrl._energy_integrator.accumulated_discharge_Wh == pytest.approx(
            measured.removed_Wh,
        )
        ctrl._book_measured_pulse_energy(service)
        assert ctrl._energy_integrator.accumulated_discharge_Ah == pytest.approx(
            measured.removed_Ah,
        )
        before = list(transcript)
        assert not service.step_chunk()
        assert transcript == before


# R16-B13F2: reset/OFF/cleanup alatt érkező élő jelzés, teljes runner-lánccal.
@pytest.mark.usefixtures("_legacy_runner_hardware_acquisition")
class TestR16B13F2:
    @pytest.mark.parametrize("hardware,arrival", [
        (False, "reset"), (False, "off"), (False, "cutoff"), (False, "nplc"),
        (True, "reset"), (True, "off"), (True, "cutoff"),
        (True, "timeout"), (True, "nplc"),
    ])
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    @pytest.mark.parametrize("has_trace", [False, True])
    def test_terminal_io_stop_preserves_prefix(
        self, monkeypatch, hardware, arrival, fast, stop_kind, has_trace,
    ):
        from unittest.mock import Mock

        from Prog.tests.mock_drivers.fake_clock import (
            InstrumentLatencyClock,
            bind_pulse_instrument_latency,
        )

        mode = (
            "HARDWARE_PULSE_SOFTWARE_TRIGGERED" if hardware
            else "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
        )
        ctrl, service = TestR16B13.make(fast, mode)
        clock = InstrumentLatencyClock()
        bind_pulse_instrument_latency(clock, ctrl._dmm_v, ctrl._load)
        service._time_source = clock
        service._sample_interval_s = 0.2
        service._chunk_sample_count = 5
        ctrl._dmm_v.voltage_V = 13.0
        ctrl._load.trace_response = "1.4,1.4" if has_trace else ""
        # Külön vizsgáljuk az üres és a korábban már könyvelt prefixet.
        duration = 4.0 if hardware else 2.0 if has_trace else 1.0
        assert service.begin_pulse(1.4, duration)
        if not hardware and has_trace:
            assert service.step_chunk()
        prefix = service.measured_energy_so_far()
        ctrl._book_measured_pulse_energy(service)
        before_samples = len(service._samples)
        if arrival == "cutoff":
            ctrl._dmm_v.timed_voltage_fn = lambda _t: 11.0
        ctrl._state = (
            OcvSocState.IMPULSE_FAST_ACQUIRE if fast else OcvSocState.IMPULSE_ACQUIRE
        )
        service._recovery_duration_s = 0.4
        recovery = Mock(wraps=service._capture_recovery_if_configured)
        monkeypatch.setattr(service, "_capture_recovery_if_configured", recovery)
        finalize = Mock(side_effect=AssertionError("Jelzés után nincs véglegesítés"))
        monkeypatch.setattr(service, "finalize", finalize)
        signalled, acquisitions = [], []
        original_request = TestR16B12F2.request

        def request(runner, kind):
            reason = original_request(runner, kind)
            signalled.append(reason)
            return reason

        monkeypatch.setattr(TestR16B12F2, "request", staticmethod(request))
        for device, methods in (
            (ctrl._dmm_v, (
                "configure_timed_acquisition", "arm_timed_acquisition",
                "fetch_timed_acquisition",
            )),
            (ctrl._load, ("input_on", "fire_transient_pulse")),
        ):
            for method in methods:
                original = getattr(device, method)

                def recorded(*args, original=original, method=method, **kwargs):
                    if signalled:
                        acquisitions.append(method)
                    return original(*args, **kwargs)

                monkeypatch.setattr(device, method, recorded)
        device, method = {
            "reset": (ctrl._dmm_v, "reset_to_single_sample"),
            "off": (ctrl._load, "input_off"),
            "cutoff": (ctrl._load, "input_off"),
            "timeout": (ctrl._dmm_v, "set_io_timeout_ms"),
            "nplc": (ctrl._dmm_v, "set_nplc"),
        }[arrival]
        if arrival == "nplc":
            # A begin után kizárólag a terminális NPLC-visszaállítás jelez.
            service._nplc_applied = True
            service._nplc_restore_value = 10.0
        if arrival in {"timeout", "nplc"}:
            # A runner kezdeti NPLC-beállítását és a konfigurációs timeoutot
            # átengedjük; csak a szolgáltatás cleanupja kap szálkaput.
            original_cleanup = getattr(device, method)
            initial_timeout = []
            target_method = method
            method = "_r16_b13f2_terminal_io"
            monkeypatch.setattr(device, method, original_cleanup, raising=False)

            def dispatch_cleanup(*args, **kwargs):
                if arrival == "timeout" and not initial_timeout:
                    initial_timeout.append(True)
                    return original_cleanup(*args, **kwargs)
                if arrival == "nplc" and service._nplc_applied:
                    return original_cleanup(*args, **kwargs)
                return getattr(device, method)(*args, **kwargs)

            monkeypatch.setattr(device, target_method, dispatch_cleanup)
        verified = []

        def verify():
            assert service._done and not ctrl._load.input_commanded_on
            assert len(service._samples) > before_samples
            assert not acquisitions, acquisitions
            finalize.assert_not_called()
            if arrival != "nplc":
                recovery.assert_not_called()
            measured = service.measured_energy_so_far()
            if hardware and not has_trace and arrival != "nplc":
                assert measured == prefix
                assert measured.removed_Ah == measured.removed_Wh == 0.0
                assert not measured.current_degraded
                assert service._current_trace_incomplete
            elif not hardware and arrival == "reset":
                assert measured == prefix
            verified.append(True)

        TestR16B13.run_signal(
            monkeypatch, ctrl, device, method, stop_kind, verify,
        )
        assert verified
        measured = service.measured_energy_so_far()
        assert ctrl._pulse_energy_booked == measured
        assert ctrl._energy_integrator.accumulated_discharge_Ah == pytest.approx(
            measured.removed_Ah,
        )
        assert ctrl._energy_integrator.accumulated_discharge_Wh == pytest.approx(
            measured.removed_Wh,
        )
        if hardware and has_trace:
            assert measured.reading_count == 2
            assert measured.removed_Ah == pytest.approx(1.4 * 4.0 / 3600.0)
            assert measured.removed_Wh == pytest.approx(
                1.4 * (11.0 if arrival == "cutoff" else 13.0) * 4.0 / 3600.0,
            )
        if not hardware and arrival != "reset":
            end_s = duration - 0.8 if arrival == "cutoff" else duration
            assert measured.removed_Ah == pytest.approx(1.4 * end_s / 3600.0)
            voltage_integral = (
                (duration - 1.0) * 13.0 + 0.2 * 11.0
                if arrival == "cutoff" else duration * 13.0
            )
            assert measured.removed_Wh == pytest.approx(1.4 * voltage_integral / 3600.0)
        ctrl._book_measured_pulse_energy(service)
        assert ctrl._energy_integrator.accumulated_discharge_Ah == pytest.approx(
            measured.removed_Ah,
        )
        assert ctrl._energy_integrator.accumulated_discharge_Wh == pytest.approx(
            measured.removed_Wh,
        )
        before = (list(acquisitions), list(ctrl._load.calls), list(ctrl._dmm_v.calls))
        assert not service.step_chunk()
        assert before == (acquisitions, ctrl._load.calls, ctrl._dmm_v.calls)


# R16-B13F3: standard input_on alatt érkező jelzés, valódi mintasorral.
class TestR16B13F3:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("stop_kind", ["stop", "abort", "emergency"])
    def test_input_on_stop_turns_off_before_sample_and_keeps_aborted_event(
        self, monkeypatch, fast, stop_kind,
    ):
        ctrl, service = TestR16B13.make(fast, "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED")
        pulse_events, signalled, transcript, samples = [], [], [], []
        ctrl.on_event = pulse_events.append
        original_request = TestR16B12F2.request

        def request(runner, kind):
            reason = original_request(runner, kind)
            signalled.append(reason)
            return reason

        monkeypatch.setattr(TestR16B12F2, "request", staticmethod(request))
        # Minden mock-műszerhívás közös átiratban; a mintasor olvasásai is.
        for device, label in (
            (ctrl._load, "Load"), (ctrl._psu, "PSU"),
            (ctrl._dmm_v, "DMMV"), (ctrl._dmm_t, "DMMT"),
        ):
            for method in dir(type(device)):
                if method.startswith("_") or method in {"called", "reset_calls"}:
                    continue
                original = getattr(device, method)
                if not callable(original):
                    continue

                def recorded(*args, original=original, method=method, label=label, **kwargs):
                    if signalled:
                        transcript.append((label, method))
                    return original(*args, **kwargs)

                monkeypatch.setattr(device, method, recorded)

        original_runner = TestR16B12.runner

        def runner_with_real_sample(*args, **kwargs):
            parts = original_runner(*args, **kwargs)
            runner = parts[0]
            build_sample = TestRunner._build_sample.__get__(runner)

            def sample(*sample_args, **sample_kwargs):
                # A graceful tick mintája megmarad, de már kikapcsolt Loaddal.
                assert not ctrl._load.input_commanded_on
                row = build_sample(*sample_args, **sample_kwargs)
                samples.append(row)
                return row

            monkeypatch.setattr(runner, "_build_sample", sample)
            return parts

        monkeypatch.setattr(TestR16B12, "runner", staticmethod(runner_with_real_sample))

        def verify():
            assert service._started and service._done
            assert not ctrl._load.input_commanded_on
            assert ctrl.state == (
                OcvSocState.IMPULSE_FAST_ACQUIRE if fast else OcvSocState.IMPULSE_ACQUIRE
            )
            assert transcript[0] == ("Load", "input_off"), transcript

        TestR16B13.run_signal(
            monkeypatch, ctrl, ctrl._load, "input_on", stop_kind, verify,
        )
        assert len(samples) == (1 if stop_kind == "stop" else 0)
        assert sum(
            event["event_code"] == "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"
            for event in pulse_events
        ) == 1
        assert not any(method in {
            "input_on", "fire_transient_pulse", "configure_timed_acquisition",
            "arm_timed_acquisition", "fetch_timed_acquisition",
        } for _, method in transcript)
        measured = service.measured_energy_so_far()
        assert measured.reading_count == 0
        assert measured.removed_Ah == measured.removed_Wh == 0.0
        assert ctrl._pulse_energy_booked == measured
        assert ctrl._energy_integrator.accumulated_discharge_Ah == 0.0
        assert ctrl._energy_integrator.accumulated_discharge_Wh == 0.0


# R16-B10F2: valódi runner/controller/manager/ON-driver, csak mockolt transport.
class TestR16B10F2ActivationStop:
    PATHS = (
        "discharge", "ocv", "chemid", "iec1", "iec2", "tlc1", "tlc2",
        "tlc_start1", "tlc_start2", "gg", "preset", "child_tlc",
        "child_recharge", "child_gg", "child_chemid", "child_ocv", "child_iec",
    )

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize("signal", ["stop", "emergency", "abort", "both"])
    @pytest.mark.parametrize("ack", ["late", "never", "immediate"])
    def test_first_poll_stop_runs_common_off_without_delay(self, monkeypatch, path, signal, ack):
        from unittest.mock import Mock

        from Prog.drivers import device_load, device_psu
        from Prog.src.safety import PsuMode
        from Prog.tests.test_device_load import TestR16B10
        from Prog.tests.test_device_psu import _make_psu_on_stateful_fake

        if path.startswith("tlc_start"):
            from Prog.tests.test_traditional_learning_controller import make_controller

            ctrl = make_controller()[0]
            target = getattr(LearningState, "DISCHARGE_" + path[-1])
            # A valós indítómetódus a runner által bekötött hookkal fut.
            monkeypatch.setattr(ctrl, "advance", lambda dt: ctrl._begin_discharge(target))
            kind = StepKind.TRADITIONAL_LEARNING
        elif path == "gg":
            from Prog.tests.test_golden_gg_controller import make_controller

            ctrl = make_controller()[0]
            monkeypatch.setattr(ctrl, "advance", lambda dt: ctrl._begin_system_rate_discharge())
            kind = StepKind.GOLDEN_GG
        else:
            ctrl, kind, _gate = TestR16B12.case(path)

        real_load, load_res, clock = TestR16B10.make(monkeypatch)
        monkeypatch.setattr(device_psu, "time", device_load.time)
        is_charge = path == "preset" or path.startswith("child_")
        if is_charge:
            device, res = _make_psu_on_stateful_fake(PsuMode.INDEPENDENT)
            res.timeout = 5000
            ctrl._psu = device
            if path.startswith("child_"):
                ctrl._charge_ctrl._psu = device
            watched, off_command = "OUTP?", "OUTP OFF;*OPC?"
        else:
            device, res = real_load, load_res
            # A konfiguráció nem része a vizsgált ON-pollnak.
            monkeypatch.setattr(device, "set_mode_cc", ctrl._load.set_mode_cc)
            monkeypatch.setattr(device, "set_current", ctrl._load.set_current)
            ctrl._load = device
            watched, off_command = "INP?", "INP OFF;*OPC?"

        if path.startswith("tlc_start") or path == "gg":
            ctrl._dmm_valid = ctrl._temp_dmm_valid = True
            ctrl._u_batt = ctrl._dmm_v.voltage_V
        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, kind, monkeypatch, 30.0,
        )
        # R16-B10F3: a valódi mintasorépítés és read_all legyen elérhető.
        sample_builder = Mock(wraps=TestRunner._build_sample.__get__(runner))
        monkeypatch.setattr(runner, "_build_sample", sample_builder)
        trace, arrivals, first_off = [], [], []
        original_query = res.query
        rebound_before = (
            getattr(ctrl, "_rebound_retry_count", None),
            getattr(ctrl, "_discharge_cutoff_V", None),
        )

        if not is_charge:
            original_write = res.write

            def write(cmd):
                original_write(cmd)
                if cmd == "INP ON":
                    # A fizikai fake már energizált, a nyugta még késhet.
                    res.actual = True

            monkeypatch.setattr(res, "write", write)

        def query(cmd):
            trace.append(cmd)
            if cmd == watched and not first_off:
                if not arrivals:
                    assert res.output_state if is_charge else res.actual
                    arrivals.append(clock.now)
                    if signal == "both":
                        runner.request_stop()
                        runner.request_emergency_stop("USER_EMERGENCY_STOP")
                    else:
                        TestR16B12F2.request(runner, signal)
                    trace.append("STOP")
                # A régi driver 0,9 s-nál vagy 1 s-nál adná vissza a vezérlést.
                if not is_charge:
                    res.log.append(("Q", cmd, clock.now, res.timeout))
                else:
                    res.messages.append(cmd)
                return "1" if ack == "immediate" or (ack == "late" and clock.now >= 0.9) else "0"
            if cmd.startswith(("MEAS:VOLT?", "MEAS:CURR?")):
                # Érvényes válasz: egy lenyelt fake-hiba ne fedje el az új I/O-t.
                clock.now += 0.4
                if is_charge:
                    res.messages.append(cmd)
                else:
                    res.log.append(("Q", cmd, clock.now, res.timeout))
                return "12.5" if cmd.startswith("MEAS:VOLT?") else "1.4"
            if cmd == off_command and arrivals and not first_off:
                first_off.append(clock.now)
            return original_query(cmd)

        monkeypatch.setattr(res, "query", query)
        if path in ("iec1", "iec2"):
            original_voltage = ctrl._dmm_v.read_voltage
            voltage_reads = 0
            def bounded_voltage(*args, **kwargs):
                nonlocal voltage_reads
                voltage_reads += 1
                if voltage_reads > 8:
                    runner.request_stop()
                    raise AssertionError("Az ON-poll trigger nem sult el 8 minta alatt")
                return original_voltage(*args, **kwargs)
            monkeypatch.setattr(ctrl._dmm_v, "read_voltage", bounded_voltage)
        result = runner._run_step(0, step)
        reason = {
            "stop": "USER_STOP_REQUESTED", "emergency": "USER_EMERGENCY_STOP",
            "abort": "ABORT_DURING_HANDOFF", "both": "USER_EMERGENCY_STOP",
        }[signal]
        assert (result.status, result.reason) == ("STOPPED" if signal == "stop" else "FAULT", reason), (result.status, result.reason, trace)
        TestR16B12F2.terminal(
            result, logger, events, common_off, order, reason,
            "STOPPED" if signal == "stop" else "FAULT",
        )
        assert arrivals == [0.0]
        assert first_off == [arrivals[0]], (arrivals, first_off, trace)
        sample_builder.assert_not_called()
        logger.log_sample.assert_not_called()
        tail = trace[trace.index("STOP") + 1:]
        assert tail[0] == off_command
        assert watched not in tail[:tail.index(off_command)]
        assert res.timeout == 5000
        assert not device.output_commanded_on if is_charge else not device.input_commanded_on
        assert not res.energized if is_charge else not res.actual
        messages = res.messages if is_charge else res.commands("W") + res.commands("Q")
        assert messages.count("OUTP ON;*OPC?" if is_charge else "INP ON") == 1
        assert not any(e["event_code"] == "LEARNING_REBOUND_TOO_HIGH" for e in events)
        if path in ("tlc1", "tlc2"):
            assert (
                ctrl._rebound_retry_count, ctrl._discharge_cutoff_V,
            ) == rebound_before


# R16-B10F3: a sima hook-lekérdezés még nem nyugtamegszakítás.
class TestR16B10F3ActivationHook:
    def test_only_ack_interruption_marks_hook(self):
        from Prog.src.exceptions import (
            ActivationStopHook,
            TestAbortedByUser,
            check_activation_stop,
        )

        pending = [False]
        hook = ActivationStopHook(lambda: pending[0])
        check_activation_stop(hook)
        assert not hook.activation_interrupted
        pending[0] = True
        assert hook()
        assert not hook.activation_interrupted
        with pytest.raises(TestAbortedByUser):
            check_activation_stop(hook)
        assert hook.activation_interrupted

    def test_new_step_gets_fresh_hook(self):
        from Prog.src.exceptions import (
            ActivationStopHook,
            TestAbortedByUser,
            check_activation_stop,
        )

        ctrl, kind, _gate = TestR16B12.case("discharge")
        runner, _logger, _dmm, _events = _f26_mock_logger_runner(
            TestRunnerConfig(sleep_enabled=False),
        )
        step = TestStep(kind, "Nyugta-hook élettartama")
        runner._prepare_relax_sampling(step, ctrl)
        old_hook = ctrl.handoff_stop_requested
        runner.request_stop()
        with pytest.raises(TestAbortedByUser):
            check_activation_stop(old_hook)
        assert old_hook.activation_interrupted
        runner._prepare_relax_sampling(step, ctrl)
        assert isinstance(ctrl.handoff_stop_requested, ActivationStopHook)
        assert ctrl.handoff_stop_requested is not old_hook
        assert not ctrl.handoff_stop_requested.activation_interrupted
        assert ctrl.handoff_stop_requested()


# R16-B10F4: esedekes szulopoll; minden transport es muszer fake/mock.
class TestR16B10F4ParentPoll:
    PATHS = (
        "child_tlc", "child_recharge", "child_gg",
        "child_chemid", "child_ocv", "child_iec",
    )

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize("signal", ["stop", "emergency", "abort", "both"])
    @pytest.mark.parametrize("ack", ["late", "never", "immediate"])
    def test_interrupted_ack_off_precedes_due_parent_poll(
        self, monkeypatch, path, signal, ack,
    ):
        import json
        from itertools import count
        from types import SimpleNamespace
        from unittest.mock import Mock

        import Prog.src.test_runner as runner_module
        from Prog.drivers import device_load, device_psu
        from Prog.src.safety import PsuMode
        from Prog.tests.test_device_load import TestR16B10
        from Prog.tests.test_device_psu import _make_psu_on_stateful_fake

        ctrl, kind, _gate = TestR16B12.case(path)
        child = ctrl._charge_ctrl
        child._integrator.add_charge_delta(0.12, 1.6, 10.0)
        if path in ("child_tlc", "child_recharge"):
            ctrl._charge_total_accumulated_Ah = 0.4
            ctrl._charge_total_accumulated_Wh = 5.0
            ctrl._log_elapsed_s = ctrl._config.sample_interval_s - 0.5
        else:
            ctrl._energy_integrator.add_charge_delta(0.52, 6.6, 10.0)
            if path == "child_gg":
                ctrl._gauge_poll_elapsed_s = ctrl._config.sample_interval_s - 0.5
        energy_before = (ctrl.accumulated_charge_Ah, ctrl.accumulated_charge_Wh)
        child_before = (child.accumulated_charge_Ah, child.accumulated_charge_Wh)
        sample_count = len(ctrl._samples) if path == "child_gg" else None

        _load, _res, clock = TestR16B10.make(monkeypatch)
        monkeypatch.setattr(device_psu, "time", device_load.time)
        psu, res = _make_psu_on_stateful_fake(PsuMode.INDEPENDENT)
        res.timeout = 5000
        ctrl._psu = child._psu = psu
        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, kind, monkeypatch, 1.0,
        )
        # Az elso advance dt_s=1; csak a runner orajat csereljuk.
        ticks = count(1000.0, 1.0)
        monkeypatch.setattr(runner_module, "time", SimpleNamespace(
            perf_counter=lambda: next(ticks),
            monotonic=runner_module.time.monotonic,
            sleep=runner_module.time.sleep,
        ))
        sample_builder = Mock(wraps=TestRunner._build_sample.__get__(runner))
        monkeypatch.setattr(runner, "_build_sample", sample_builder)
        trace, arrivals = [], []
        for device, label, methods in (
            (ctrl._load, "Load", ("input_off", "measure_voltage", "measure_current")),
            (psu, "PSU", ("all_outputs_off", "measure_output_voltage", "measure_output_current")),
            (ctrl._dmm_v, "DMMV", ("read_voltage",)),
            (ctrl._dmm_t, "DMMT", ("read_temperature",)),
        ):
            for method in methods:
                original = getattr(device, method)

                def recorded(*args, original=original, label=label, method=method, **kwargs):
                    trace.append((label + ":" + method, clock.now))
                    return original(*args, **kwargs)

                monkeypatch.setattr(device, method, recorded)
        # A valodi poll teljes JSON-RPC lancat hagyjuk futni, fake soroson.
        transport = getattr(ctrl, "_bq_transport", None)
        if transport is not None:
            original_bq_write = transport.serial.write

            def bq_write(data):
                trace.append(("BQ:" + json.loads(data.decode("utf-8"))["method"], clock.now))
                clock.now += 0.4
                return original_bq_write(data)

            monkeypatch.setattr(transport.serial, "write", bq_write)
        original_query = res.query

        def query(cmd):
            trace.append((cmd, clock.now))
            if cmd == "OUTP?" and not arrivals:
                assert res.output_state
                arrivals.append(clock.now)
                if signal == "both":
                    runner.request_stop()
                    runner.request_emergency_stop("USER_EMERGENCY_STOP")
                else:
                    TestR16B12F2.request(runner, signal)
                trace.append(("SIGNAL", clock.now))
                return "1" if ack == "immediate" or (ack == "late" and clock.now >= 0.9) else "0"
            return original_query(cmd)

        monkeypatch.setattr(res, "query", query)
        advance = ctrl.advance
        dts = []

        def advance_once(dt_s):
            dts.append(dt_s)
            return advance(dt_s)

        monkeypatch.setattr(ctrl, "advance", advance_once)
        result = runner._run_step(0, step)
        reason = {
            "stop": "USER_STOP_REQUESTED", "emergency": "USER_EMERGENCY_STOP",
            "abort": "ABORT_DURING_HANDOFF", "both": "USER_EMERGENCY_STOP",
        }[signal]
        TestR16B12F2.terminal(
            result, logger, events, common_off, order, reason,
            "STOPPED" if signal == "stop" else "FAULT",
        )
        assert dts == [1.0]
        assert arrivals == [0.0]
        tail = trace[trace.index(("SIGNAL", arrivals[0])) + 1:]
        assert tail[:2] == [
            ("Load:input_off", arrivals[0]), ("PSU:all_outputs_off", arrivals[0]),
        ], tail
        assert child.handoff_stop_requested is ctrl.handoff_stop_requested
        assert ctrl.handoff_stop_requested.activation_interrupted
        sample_builder.assert_not_called()
        logger.log_sample.assert_not_called()
        assert (ctrl.accumulated_charge_Ah, ctrl.accumulated_charge_Wh) == pytest.approx(energy_before)
        assert (child.accumulated_charge_Ah, child.accumulated_charge_Wh) == pytest.approx(child_before)
        if path == "child_gg":
            # A belso exportsor es az idotengely nem veszhet el a pollkapunal.
            assert len(ctrl._samples) == sample_count + 1
            assert ctrl._samples[-1].elapsed_s == 1.0
        assert res.timeout == 5000
        assert res.messages.count("OUTP ON;*OPC?") == 1
        assert not res.energized and not psu.output_commanded_on

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize("interrupted", [False, True])
    def test_parent_keeps_already_integrated_child_delta(self, monkeypatch, path, interrupted):
        from unittest.mock import Mock

        from Prog.src.exceptions import (
            ActivationStopHook,
            TestAbortedByUser,
            check_activation_stop,
        )

        ctrl, _kind, _gate = TestR16B12.case(path)
        child = ctrl._charge_ctrl
        ctrl.handoff_stop_requested = ActivationStopHook(lambda: interrupted)
        if path in ("child_tlc", "child_recharge"):
            ctrl._log_elapsed_s = ctrl._config.sample_interval_s - 0.5
        elif path == "child_gg":
            ctrl._gauge_poll_elapsed_s = ctrl._config.sample_interval_s - 0.5
        child._integrator.add_charge_delta(0.12, 1.6, 10.0)
        before = (ctrl.accumulated_charge_Ah, ctrl.accumulated_charge_Wh)

        def advance(dt_s):
            # A gyermek mar mert es integralt prefixet ad at, I/O nelkul.
            child._integrator.add_sample(1.8, 13.2, dt_s)
            try:
                check_activation_stop(child.handoff_stop_requested)
            except TestAbortedByUser:
                pass
            return child.state

        monkeypatch.setattr(child, "advance", advance)
        poll = None
        if path in ("child_tlc", "child_recharge", "child_gg"):
            poll = Mock(wraps=ctrl._poll_bq_diagnostics_throttled)
            monkeypatch.setattr(ctrl, "_poll_bq_diagnostics_throttled", poll)
        ctrl.advance(1.0)
        assert child.accumulated_charge_Ah == pytest.approx(0.1205)
        assert child.accumulated_charge_Wh == pytest.approx(1.6066)
        assert ctrl.accumulated_charge_Ah == pytest.approx(before[0] + 0.0005)
        assert ctrl.accumulated_charge_Wh == pytest.approx(before[1] + 0.0066)
        if poll is not None:
            assert poll.call_count == (0 if interrupted else 1)
        if path == "child_gg":
            assert len(ctrl._samples) == 1 and ctrl._samples[0].elapsed_s == 1.0


# R16-B10b: valódi pulse/OCV/runner/manager és Load-driver, kizárólag fake I/O.
@pytest.mark.usefixtures("_legacy_runner_hardware_acquisition")
class TestR16B10b:
    MODES = (
        "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED", "HARDWARE_PULSE_SOFTWARE_TRIGGERED",
    )

    @staticmethod
    def make(monkeypatch, mode=MODES[0], *, never=False):
        import Prog.src.instrument_manager as manager_module
        from Prog.drivers import device_load
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.test_device_load import TestR16B10

        load, res, clock = TestR16B10.make(monkeypatch)
        clock.now = 100.0
        monkeypatch.setattr(manager_module, "time", device_load.time, raising=False)
        ctrl, service = TestR16B13.make(False, mode)
        old_load = ctrl._load
        # Csak az ON/OFF és állapotpárbeszéd valós driver; konfiguráció mockolt.
        for name in (
            "set_mode_cc", "set_current", "configure_transient_pulse",
            "read_transient_current_levels", "configure_trace",
            "fire_transient_pulse", "fetch_trace",
        ):
            monkeypatch.setattr(load, name, getattr(old_load, name))
        ctrl._load = service._load = load
        service._time_source = clock.monotonic
        manager = InstrumentManager(ctrl._psu, load, ctrl._dmm_v, ctrl._dmm_t)
        query = res.query

        def delayed_query(cmd):
            if cmd == "INP?":
                res.log.append(("Q", cmd, clock.now, res.timeout))
                if res.pending and not never and clock.now >= 100.3:
                    res.pending = False
                    res.actual = True
                return "1" if res.actual else "0"
            return query(cmd)

        monkeypatch.setattr(res, "query", delayed_query)
        return ctrl, service, manager, load, res, clock

    @staticmethod
    def assert_pending(entries):
        from Prog.src.test_runner import _critical_polled_errors

        assert entries == [{
            "device": "Load",
            "error": "INPUT_ACTIVATION_PENDING: commanded=ON actual=OFF",
            "status": "ON_ACK_PENDING",
        }]
        assert _critical_polled_errors(TestStep(StepKind.OCV_SOC, "pulse"), entries) == []

    @pytest.mark.parametrize("mode", MODES)
    def test_pulse_immediate_poll_and_delayed_ack(self, monkeypatch, mode):
        ctrl, service, manager, load, res, clock = self.make(monkeypatch, mode)
        observed = []

        def poll_sequence():
            observed.append(manager.poll_device_errors())
            self.assert_pending(observed[-1])
            clock.now = 100.3
            assert manager.poll_device_errors() == []
            assert load.input_commanded_on and res.actual

        if mode == self.MODES[1]:
            fetch = ctrl._dmm_v.fetch_timed_acquisition

            def fetch_with_polls():
                poll_sequence()
                return fetch()

            monkeypatch.setattr(ctrl._dmm_v, "fetch_timed_acquisition", fetch_with_polls)
        assert service.begin_pulse(1.4, 30.0)
        assert clock.now == 100.0
        if mode == self.MODES[1]:
            # Capture-ben az ON a valódi step_chunk belsejében, fetch előtt fut.
            service.step_chunk()
        else:
            assert service._t0 == 100.0
            poll_sequence()
            load.input_off()
        assert len(observed) == 1
        assert res.commands("W").count("INP ON") == 1

    def test_deadline_is_command_relative_and_poll_does_not_extend_it(self, monkeypatch):
        _ctrl, _service, manager, load, res, clock = self.make(monkeypatch, never=True)
        load.input_on(wait_for_ack=False)
        for now in (100.0, 100.3, 100.999):
            clock.now = now
            self.assert_pending(manager.poll_device_errors())
        clock.now = 101.0
        errors = manager.poll_device_errors()
        assert errors == [{
            "device": "Load", "error": "INPUT_STATE_MISMATCH: commanded=ON actual=OFF",
        }]
        assert res.commands("W").count("INP ON") == 1

    def test_first_poll_after_deadline_is_fault(self, monkeypatch):
        _ctrl, _service, manager, load, _res, clock = self.make(monkeypatch, never=True)
        load.input_on(wait_for_ack=False)
        clock.now = 105.0
        assert "INPUT_STATE_MISMATCH" in manager.poll_device_errors()[0]["error"]

    @pytest.mark.parametrize("age", [0.0, 0.3, 1.0])
    def test_off_direction_has_no_grace(self, monkeypatch, age):
        from Prog.src.test_runner import _critical_polled_errors

        _ctrl, _service, manager, load, res, clock = self.make(monkeypatch)
        load.input_on(wait_for_ack=False)
        load.input_off()
        # Egy megmaradt ON-időbélyeg sem nyithat ablakot OFF-parancs mellett.
        load.input_on_commanded_at = 100.0
        res.pending, res.actual = False, True
        clock.now += age
        entries = manager.poll_device_errors()
        assert entries == [{
            "device": "Load", "error": "INPUT_STATE_MISMATCH: commanded=OFF actual=ON",
            "energy_state_uncertain": True,
        }]
        assert _critical_polled_errors(TestStep(StepKind.RELAX, "OFF"), entries) == entries

    @pytest.mark.parametrize("fault", ["queue", "readback", "protection"])
    def test_pending_does_not_hide_other_faults(self, monkeypatch, fault):
        from Prog.src.exceptions import InstrumentTimeoutError
        from Prog.src.test_runner import _critical_polled_errors

        _ctrl, _service, manager, load, _res, _clock = self.make(monkeypatch)
        load.input_on(wait_for_ack=False)
        if fault == "queue":
            replies = iter([['-200,"Execution error"'], []])
            monkeypatch.setattr(load, "check_error", lambda: next(replies))
        elif fault == "readback":
            def failed():
                raise InstrumentTimeoutError("fake INP? timeout")
            monkeypatch.setattr(load, "read_input_state", failed)
        else:
            monkeypatch.setattr(load, "read_questionable_status", lambda: {"OV": True})
        entries = manager.poll_device_errors()
        assert _critical_polled_errors(TestStep(StepKind.OCV_SOC, "pulse"), entries)

    @pytest.mark.parametrize("tick", [0.0, 2.0, 5.0, 30.0])
    @pytest.mark.parametrize("case", ["delayed", "never", "off_on"])
    def test_runner_ocv_chain(self, monkeypatch, tick, case):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import Mock

        import Prog.src.test_runner as runner_module
        from Prog.tests.mock_drivers.fake_clock import bind_pulse_instrument_latency

        ctrl, service, manager, load, res, clock = self.make(
            monkeypatch, never=case == "never",
        )
        runner, logger, events, common_off, order, step = TestR16B12.runner(
            ctrl, StepKind.OCV_SOC, monkeypatch, tick,
        )
        runner._config = replace(runner._config, sleep_enabled=True)
        assert runner._config.device_error_poll_interval_s == 60.0
        monkeypatch.setattr(runner, "_poll_device_errors", TestRunner._poll_device_errors.__get__(runner))
        monkeypatch.setattr(runner, "_should_poll_device_errors", TestRunner._should_poll_device_errors.__get__(runner))
        monkeypatch.setattr(runner, "_build_sample", TestRunner._build_sample.__get__(runner))
        monkeypatch.setattr(clock, "advance", clock.sleep, raising=False)
        bind_pulse_instrument_latency(clock, ctrl._dmm_v, load)
        monkeypatch.setattr(runner_module, "time", SimpleNamespace(
            perf_counter=clock.monotonic, monotonic=clock.monotonic, sleep=clock.sleep,
        ))
        callback = Mock()
        runner.on_device_error = callback
        advance, poll = ctrl.advance, runner._poll_device_errors
        started, polls, active_poll_times = [], [], []

        def polled(*args, **kwargs):
            if load.input_commanded_on:
                active_poll_times.append(clock.now)
            entries = poll(*args, **kwargs)
            polls.append(entries)
            if case == "delayed" and res.actual and load.input_on_commanded_at is None:
                runner.request_stop()
            return entries

        monkeypatch.setattr(runner, "_poll_device_errors", polled)

        def advanced(dt_s):
            state = advance(dt_s)
            if not started:
                assert service._started and load.input_commanded_on
                started.append(clock.now)
                if case == "off_on":
                    load.input_off()
                    res.pending, res.actual = False, True
            return state

        monkeypatch.setattr(ctrl, "advance", advanced)
        result = runner.run(TestPlan(TestType.CHARACTERIZATION, (step,)))
        assert started == [100.0]
        assert runner._instruments.load is manager.load
        assert not load.input_commanded_on and not res.actual
        assert not ctrl._psu.output_commanded_on
        assert common_off.called and order[:2] == ["Load", "PSU"]
        assert res.commands("W").count("INP ON") == 1
        assert not ctrl._psu.called("output_on()")
        assert "INP OFF;*OPC?" in res.commands("Q")
        if case == "delayed":
            assert (result.status, result.reason) == ("STOPPED", "USER_STOP_REQUESTED")
            assert result.communication_faults_count == 0
            assert any(e["event_code"] == "INPUT_ACTIVATION_PENDING" for e in events)
            callback.assert_not_called()
            logger.log_device_error.assert_not_called()
        else:
            assert result.status == "FAULT"
            direction = "ON actual=OFF" if case == "never" else "OFF actual=ON"
            assert result.reason.startswith(
                "SCPI_CRITICAL_ERROR[Load]: INPUT_STATE_MISMATCH: commanded=" + direction
            )
            assert callback.called and logger.log_device_error.called
            logger.log_event.assert_any_call("EMERGENCY_STOP", result.reason, is_critical=True)
        if case != "off_on":
            assert any(any(e.get("status") == "ON_ACK_PENDING" for e in row) for row in polls)
            # A lejáratot a helyi ciklus veszi észre, nem a külső 2/5/30 s tick.
            assert active_poll_times
            assert active_poll_times[-1] <= 101.6 + 1e-9
            if case == "never":
                assert clock.now <= 101.6 + 1e-9
                assert ctrl.state != OcvSocState.LOG_SOC_POINT

    def test_ack_closes_window_and_new_on_opens_a_new_one(self, monkeypatch):
        _ctrl, _service, manager, load, res, clock = self.make(monkeypatch)
        load.input_on(wait_for_ack=False)
        clock.now = 100.3
        assert manager.poll_device_errors() == []
        res.actual = False
        clock.now = 100.4
        assert "INPUT_STATE_MISMATCH" in manager.poll_device_errors()[0]["error"]
        load.input_off()
        clock.now = 200.0
        load.input_on(wait_for_ack=False)
        # A fake csak a következő olvasáskor nyugtázna; itt tartós OFF-ot adunk.
        res.pending = False
        self.assert_pending(manager.poll_device_errors())

    def test_missing_command_timestamp_is_not_grace(self, monkeypatch):
        _ctrl, _service, manager, load, _res, _clock = self.make(monkeypatch)
        load.input_commanded_on = True
        assert "INPUT_STATE_MISMATCH" in manager.poll_device_errors()[0]["error"]


# R16-B10bF1: a lejárat csak a függő ON ellenőrzését hozza előre.
class TestR16B10bF1PollGate:
    def test_deadline_boundary_and_normal_cadence(self, monkeypatch):
        from types import SimpleNamespace

        import Prog.src.test_runner as module

        ctrl, _service, _manager, load, res, clock = TestR16B10b.make(monkeypatch, never=True)
        runner = TestR16B12.runner(ctrl, StepKind.OCV_SOC, monkeypatch, 5.0)[0]
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock.monotonic))
        should_poll = TestRunner._should_poll_device_errors.__get__(runner)
        assert runner._config.device_error_poll_interval_s == 60.0
        runner._last_device_error_poll_t = 100.0
        load.input_on(wait_for_ack=False)
        for now, expected in ((100.0, False), (100.999, False), (101.0, True)):
            clock.now = now
            assert should_poll() is expected
        # Az esedékesség vizsgálata nem nyugta és nem hosszabbítja az ablakot.
        assert load.input_on_commanded_at == 100.0
        assert res.commands("Q") == []
        load.input_commanded_on = False
        assert should_poll() is False
        load.input_commanded_on = True
        res.pending, res.actual = False, True
        assert load.read_input_state() is True
        assert load.input_on_commanded_at is None
        assert should_poll() is False
        load.input_off()
        clock.now = 159.999
        assert should_poll() is False
        clock.now = 160.0
        assert should_poll() is True


class TestR16B16aRunnerFloor:
    @pytest.mark.parametrize("mode", ["DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"])
    def test_real_discharge_fault_reaches_runner_result_and_event(self, monkeypatch, mode):
        from pathlib import Path
        from unittest.mock import MagicMock

        from Prog.src import logger as logger_module
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.test_discharge_controller import make_discharge_controller

        memory_logger = MagicMock()
        monkeypatch.setattr(logger_module, "Logger", lambda *a, **k: memory_logger)
        ctrl, psu, load, dmm = make_discharge_controller()
        runner, _logger = _make_runner(Path.cwd(), dc=ctrl)
        runner._instruments = InstrumentManager(psu, load, dmm, ctrl._dmm_t)
        events = []
        runner.on_event = events.append
        ctrl._config.terminate_voltage_V_override = 10.79
        ticks = []
        advance = ctrl.advance

        def guarded_advance(dt_s):
            ticks.append(True)
            assert len(ticks) <= 6, "Q46 normal-config runner bound"
            return advance(dt_s)

        monkeypatch.setattr(ctrl, "advance", guarded_advance)
        plan = TestPlan(TestType(mode), (TestStep(StepKind.DISCHARGE, "floor"),))
        result = runner.run(plan)
        assert ticks, "Q46 normal-config trigger"
        assert result.status == "FAULT"
        assert result.reason == "DISCHARGE_CONFIG_INVALID"
        assert load.called("input_off") and psu.called("all_outputs_off")
        assert any("DISCHARGE_CONFIG_INVALID" in e.get("event_message", "") for e in events)
        on_count = sum("input_on" in c for c in load.calls)
        ctrl.advance(1.0)
        assert sum("input_on" in c for c in load.calls) == on_count


class TestR16B16aF1RunnerCutoff:
    @pytest.mark.parametrize("mode", ["DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"])
    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("undershoot_V", [0.001, 0.3])
    def test_real_runner_keeps_done_result_and_overshoot_event(self, monkeypatch, mode, cells, undershoot_V):
        from pathlib import Path
        from unittest.mock import MagicMock

        from Prog.src import logger as logger_module
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.test_discharge_controller import make_discharge_controller

        memory_logger = MagicMock()
        monkeypatch.setattr(logger_module, "Logger", lambda *a, **k: memory_logger)
        ctrl, psu, load, dmm = make_discharge_controller()
        ctrl._profile.cell_count = cells
        dmm.voltage_V = 2.1 * cells
        runner, _logger = _make_runner(Path.cwd(), dc=ctrl)
        runner._instruments = InstrumentManager(psu, load, dmm, ctrl._dmm_t)
        events = []
        runner.on_event = events.append
        ctrl.on_event = events.append
        read_voltage = dmm.read_voltage
        def loaded_voltage():
            if load.input_commanded_on:
                dmm.voltage_V = 1.8 * cells - undershoot_V
            return read_voltage()
        monkeypatch.setattr(dmm, "read_voltage", loaded_voltage)
        plan = TestPlan(TestType(mode), (TestStep(StepKind.DISCHARGE, "cutoff"),))
        result = runner.run(plan)
        assert result.status == "DONE"
        assert result.total_discharge_ah > 0.0
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert any(e.get("event_code") == "NORMAL_DISCHARGE_CUTOFF_OVERSHOOT" for e in events)


@pytest.fixture
def _legacy_runner_hardware_acquisition(monkeypatch):
    # A meglévő mockolt stop-/polltesztek az acquisition belső útját
    # vizsgálják. Az éles HW-tiltást külön B16b regressziók őrzik.
    monkeypatch.setattr(
        "Prog.src.pulse_measurement_service.require_battery_pulse_mode", lambda mode: None,
    )
    monkeypatch.setattr(
        "Prog.src.ocv_soc_controller.require_battery_pulse_mode", lambda mode: None,
    )


class TestR16B17PrepNplcRetry:
    def test_prep_retries_after_failed_step_and_relax_ticks(self):
        from types import SimpleNamespace

        runner, logger, dmm, events = _f26_mock_logger_runner(TestRunnerConfig())
        step = TestStep(StepKind.OCV_SOC, "B17 PREP")
        dmm.simulate_nplc_set_failure = True
        runner._configure_dmm_for_step(step)
        ctrl = SimpleNamespace(state=OcvSocState.STEP_RELAX)
        for _ in range(2):
            runner._reconfigure_dmm_nplc_for_relax_substate(step, ctrl)
        assert runner._last_configured_nplc is None
        assert _f26_log_calls(logger, "NPLC_SET_FAILED")
        dmm.simulate_nplc_set_failure = False
        before = len(dmm.calls)
        ctrl.state = OcvSocState.IMPULSE_PREP
        runner._reconfigure_dmm_nplc_for_relax_substate(step, ctrl)
        assert dmm.calls[before:] == ["set_nplc(10.0)"]
        assert runner._last_configured_nplc == 10.0
        assert not runner._nplc_set_failing
        assert len(_f26_log_calls(logger, "NPLC_SET")) == 1
        assert [event["event_code"] for event in events] == ["NPLC_SET_FAILED"]
        before = list(dmm.calls)
        runner._reconfigure_dmm_nplc_for_relax_substate(step, ctrl)
        assert dmm.calls == before


class TestR16B18PulseOnNplcOwnership:
    @pytest.mark.parametrize("state", [OcvSocState.IMPULSE_ON, OcvSocState.IMPULSE_FAST_ON])
    @pytest.mark.parametrize("configured_nplc", [None, 1.0])
    def test_runner_does_not_write_nplc_while_service_owns_dmm(self, state, configured_nplc):
        from types import SimpleNamespace

        runner, logger, dmm, events = _f26_mock_logger_runner(TestRunnerConfig())
        step = TestStep(StepKind.OCV_SOC, "B18 NPLC")
        # Eltérő vagy hiányzó cache: az őr nélkül tényleges írás következne.
        runner._last_configured_nplc = configured_nplc
        runner._nplc_set_failing = True
        before = list(dmm.calls)
        for _ in range(2):
            runner._reconfigure_dmm_nplc_for_relax_substate(
                step, SimpleNamespace(state=state),
            )
        assert dmm.calls == before
        assert runner._last_configured_nplc == configured_nplc
        assert runner._nplc_set_failing is True
        assert not _f26_log_calls(logger, "NPLC_SET")
        assert not _f26_log_calls(logger, "NPLC_SET_FAILED")
        assert events == []


class TestR16B23aInitialStatusRunner:
    @pytest.mark.parametrize(
        "case,stop_on,read_value", (
            ("exhaust", None, None), ("stop_before_retry", 1, None),
            ("stop_in_retry", 2, None), ("stop_in_retry", 4, None),
            ("stop_in_retry", 2, 0x05), ("stop_in_retry", 4, 0x05),
            ("abort_in_retry", 2, None), ("abort_in_retry", 4, 0x05),
            ("stop_retry_event", 1, None), ("stop_snapshot_event", 4, 0x05),
            ("stop_gauge_event", 2, None), ("fault_gauge_event", 2, 0x05),
        ),
    )
    def test_initial_status_retry_stop_and_fault_chain(
        self, monkeypatch, case, stop_on, read_value,
    ):
        from unittest.mock import MagicMock

        import Prog.src.test_runner as module
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.traditional_learning_controller import LEARNING_RESULT_FAILED
        from Prog.tests.test_traditional_learning_controller import make_controller

        ctrl, psu, load, dmm, temp, bq = make_controller()
        ctrl._enter(LearningState.INITIAL_SNAPSHOT)
        instruments = InstrumentManager(
            psu=psu, load=load, dmm_voltage=dmm, dmm_temperature=temp,
        )
        runner = TestRunner(
            instrument_manager=instruments, safety=ctrl._safety,
            logger=MagicMock(), profile=ctrl._profile,
            config=TestRunnerConfig(sleep_enabled=False),
            charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None,
            traditional_learning_ctrl_factory=lambda: ctrl,
        )
        clock, ticks, reads, triggered, off = [0.0], [], [], [], []
        advance = ctrl.advance
        read_all = instruments.read_all
        load_off, psu_off = load.input_off, psu.all_outputs_off

        def now():
            clock[0] += 0.1
            return clock[0]

        def guarded_advance(dt_s):
            ticks.append(True)
            assert len(ticks) <= 8, "Q-39 runner-lánc nem állt le"
            return advance(dt_s)

        def stop():
            triggered.append(True)
            if case == "abort_in_retry":
                runner.request_emergency_stop("Q39_ABORT")
            else:
                runner.request_stop()

        def read_status():
            reads.append(True)
            assert len(reads) <= 4, "Q-39 túl sok Update Status-olvasás"
            if len(reads) == stop_on:
                if case in ("stop_in_retry", "abort_in_retry"):
                    stop()
                if read_value is not None:
                    return read_value
            raise RuntimeError("Q39-runner-read-error")

        def sample_read(*args, **kwargs):
            assert not triggered or off, "STOP után új mérési I/O az OFF előtt"
            return read_all(*args, **kwargs)

        def input_off():
            off.append("LOAD_OFF")
            load_off()

        def all_off():
            off.append("PSU_ALL_OFF")
            psu_off()

        def on_sample(_sample):
            if case == "stop_before_retry" and not triggered:
                assert len(reads) == 1, "A retry-várakozást kell megszakítani"
                assert ctrl.state == LearningState.INITIAL_SNAPSHOT
                stop()

        def event_callback(event):
            code = event["event_code"]
            if (
                case == "stop_retry_event"
                and code == "LEARNING_INITIAL_UPDATE_STATUS_RETRY"
            ) or (
                case == "stop_snapshot_event"
                and code == "LEARNING_INITIAL_SNAPSHOT"
            ):
                stop()

        def gauge_callback(_snapshot):
            if len(reads) == stop_on:
                if case == "stop_gauge_event":
                    stop()
                elif case == "fault_gauge_event":
                    triggered.append(True)
                    ctrl.emergency_stop("Q39_CALLBACK_FAULT")

        ctrl.on_event = event_callback
        ctrl.on_gauge_sample = gauge_callback
        monkeypatch.setattr(module.time, "perf_counter", now)
        monkeypatch.setattr(module.time, "monotonic", now)
        monkeypatch.setattr(ctrl, "advance", guarded_advance)
        monkeypatch.setattr(ctrl._bq_driver, "read_update_status", read_status)
        monkeypatch.setattr(instruments, "read_all", sample_read)
        monkeypatch.setattr(load, "input_off", input_off)
        monkeypatch.setattr(psu, "all_outputs_off", all_off)
        runner.on_sample = on_sample
        result = runner._run_step(
            0, TestStep(StepKind.TRADITIONAL_LEARNING, "Q39"),
        )
        assert reads and ticks, "A valódi TLC/runner-láncnak el kell indulnia"
        assert not psu.output_commanded_on and not load.input_commanded_on
        assert off[:2] == ["LOAD_OFF", "PSU_ALL_OFF"]
        assert "it_enable" not in bq.calls
        assert "write_data_flash_block" not in bq.calls
        assert "input_on()" not in load.calls
        if case == "fault_gauge_event":
            assert triggered == [True]
            assert len(reads) == stop_on
            assert result.status == "FAULT"
            assert result.reason == "Q39_CALLBACK_FAULT"
            assert ctrl.result_summary.result == LEARNING_RESULT_FAILED
        elif case == "exhaust":
            assert len(reads) == 4
            assert result.status == "FAULT"
            assert result.reason.startswith(
                "LEARNING_INITIAL_UPDATE_STATUS_UNREADABLE: attempts=4;",
            )
            assert ctrl.result_summary.result == LEARNING_RESULT_FAILED
            assert triggered == []
        else:
            assert triggered == [True], "A stop/abort trigger nem futhat ki üresen"
            assert len(reads) == stop_on
            assert result.status == (
                "FAULT" if case == "abort_in_retry" else "STOPPED"
            )
            assert result.reason == (
                "Q39_ABORT" if case == "abort_in_retry" else "USER_STOP_REQUESTED"
            )


@pytest.mark.parametrize(
    "mode,expected_status,expected_reason", (
        ("exception", "FAULT", "UNHANDLED_EXCEPTION: Q39_ABORT"),
        ("stop", "STOPPED", "USER_STOP_REQUESTED"),
        ("emergency", "FAULT", "Q39_ABORT"),
    ),
)
def test_r16b23a_fix1_retry_abort_runner_terminal_contract(
    monkeypatch, mode, expected_status, expected_reason,
):
    from unittest.mock import MagicMock

    import Prog.src.test_runner as module
    from Prog.src.exceptions import TestAbortedByUser
    from Prog.src.instrument_manager import InstrumentManager
    from Prog.src.traditional_learning_controller import LEARNING_RESULT_AWAITING
    from Prog.tests.test_traditional_learning_controller import make_controller

    ctrl, psu, load, dmm, temp, bq = make_controller()
    ctrl._enter(LearningState.INITIAL_SNAPSHOT)
    instruments = InstrumentManager(
        psu=psu, load=load, dmm_voltage=dmm, dmm_temperature=temp,
    )
    logger = MagicMock()
    runner = TestRunner(
        instrument_manager=instruments, safety=ctrl._safety,
        logger=logger, profile=ctrl._profile,
        config=TestRunnerConfig(sleep_enabled=False),
        charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: None,
        relax_ctrl_factory=lambda step: None,
        traditional_learning_ctrl_factory=lambda: ctrl,
    )
    clock, ticks, reads, triggered, off, events = [0.0], [], [], [], [], []
    advance = ctrl.advance
    read_all = instruments.read_all
    load_off, psu_off = load.input_off, psu.all_outputs_off

    def now():
        clock[0] += 0.1
        return clock[0]

    def guarded_advance(dt_s):
        ticks.append(True)
        assert len(ticks) <= 6, "Q-39 abort runner tick-korlat"
        return advance(dt_s)

    def read_status():
        reads.append(True)
        assert len(reads) <= 3, "Q-39 abort retry-korlat"
        if len(reads) == 1:
            raise RuntimeError("Q39 initial read failed")
        if len(reads) == 2:
            triggered.append(True)
            if mode == "exception":
                raise TestAbortedByUser("Q39_ABORT")
            if mode == "stop":
                runner.request_stop()
            else:
                runner.request_emergency_stop("Q39_ABORT")
        # A nyelt abort utan a kovetkezo siker nem fedheti el a megszakitast.
        return 0x05

    def sample_read(*args, **kwargs):
        assert not triggered or off, "Megszakitas utan az OFF az elso I/O"
        return read_all(*args, **kwargs)

    def input_off():
        off.append("LOAD_OFF")
        load_off()

    def all_off():
        off.append("PSU_ALL_OFF")
        psu_off()

    monkeypatch.setattr(module.time, "perf_counter", now)
    monkeypatch.setattr(module.time, "monotonic", now)
    monkeypatch.setattr(ctrl, "advance", guarded_advance)
    monkeypatch.setattr(ctrl._bq_driver, "read_update_status", read_status)
    monkeypatch.setattr(instruments, "read_all", sample_read)
    monkeypatch.setattr(load, "input_off", input_off)
    monkeypatch.setattr(psu, "all_outputs_off", all_off)
    runner.on_event = events.append

    result = runner.run(TestPlan.traditional_learning())

    assert triggered == [True], "Az abort/stop triggernek le kell futnia"
    assert len(reads) == 2 and 1 <= len(ticks) <= 6
    assert result.status == expected_status
    assert result.reason == expected_reason
    assert result.safe_off_attempted
    assert result.safe_off_result["PSU"] is True
    assert result.safe_off_result["Load"] is True
    assert off[:2] == ["LOAD_OFF", "PSU_ALL_OFF"]
    assert not psu.output_commanded_on and not load.input_commanded_on
    assert "input_on()" not in load.calls
    assert not {"unseal", "write_data_flash_block", "it_enable"}.intersection(bq.calls)
    assert ctrl.state == LearningState.FAULT
    assert ctrl.runner_abort_origin == expected_reason
    assert ctrl.result_summary.result == LEARNING_RESULT_AWAITING
    terminal_code = "GRACEFUL_STOP" if mode == "stop" else "EMERGENCY_STOP"
    terminal_events = [e for e in events if e["event_code"] == terminal_code]
    assert len(terminal_events) == 1
    assert terminal_events[0]["event_message"] == expected_reason
    checkpoints = [c.args[0] for c in logger.write_checkpoint.call_args_list]
    assert checkpoints[-1]["status"] == expected_status
    assert checkpoints[-1]["reason"] == expected_reason
    logger.close.assert_called_once()


class TestR16B24Warnings:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("terminal", ["normal", "stop", "emergency", "activation", "interrupt"])
    def test_block_warnings_survive_terminal_paths_exactly_once(self, monkeypatch, fast, terminal):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig
        from Prog.tests.test_ocv_soc_controller import _b24_bound

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        rig.temp.temperature_C = 40.0
        _B21bRig.runner(monkeypatch, rig)
        ticks = _b24_bound(monkeypatch, rig)
        events, triggered = rig.ctrl.on_event, []

        def event(payload):
            events(payload)
            if payload["event_code"] != "OCV_SOC_PULSE_TEMPERATURE" or triggered:
                return
            triggered.append(1)
            if terminal == "emergency":
                rig.runner.request_emergency_stop("B24_WARNING_EMERGENCY")
            elif terminal == "stop":
                rig.runner.request_stop()
            elif terminal == "activation":
                rig.ctrl.handoff_stop_requested.activation_interrupted = True
                rig.runner.request_stop()
            elif terminal == "interrupt":
                raise KeyboardInterrupt("B24 warning callback interrupted")

        monkeypatch.setattr(rig.ctrl, "on_event", event)
        if terminal == "interrupt":
            with pytest.raises(KeyboardInterrupt, match="B24 warning callback interrupted"):
                rig.runner.run(rig.plan)
            counts, status = dict(rig.runner._warning_code_counts), "INTERRUPTED"
        else:
            result = rig.runner.run(rig.plan)
            counts, status = result.warning_code_counts, result.status
        assert triggered == [1] and ticks
        expected = rig.ctrl.temperature_out_of_range_count
        assert expected > 1
        assert counts == {"TEMPERATURE_OUT_OF_RANGE": expected}
        assert rig.ctrl.take_pulse_warnings() == {}
        assert rig.ctrl.take_pulse_warnings() == {}
        assert status == {"normal": "DONE", "stop": "STOPPED", "activation": "STOPPED", "interrupt": "INTERRUPTED"}.get(terminal, "FAULT")
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert rig.service._worker is None and rig.dmm.nplc == 10.0
        if terminal == "stop":
            assert rig.logger.log_sample.call_count == 1
            assert rig.logger.log_sample.call_args.args[0]["warning_flags"] == "TEMPERATURE_OUT_OF_RANGE"

    def test_without_block_still_logs_and_counts_the_tick_before_stop(self, monkeypatch):
        from Prog.tests.mock_drivers.fake_clock import _B21bRig
        from Prog.tests.test_ocv_soc_controller import _b24_bound

        rig = _B21bRig.make(monkeypatch, start=False)
        rig.ctrl._state = OcvSocState.IMPULSE_PREP
        rig.temp.temperature_C = 40.0
        _B21bRig.runner(monkeypatch, rig)
        ticks = _b24_bound(monkeypatch, rig)
        advance, triggered = rig.ctrl.advance, []

        def stop_after_advance(dt_s):
            assert not triggered, "B24 no-block tick did not stop"
            value = advance(dt_s)
            triggered.append(1)
            rig.runner.request_stop()
            return value

        monkeypatch.setattr(rig.ctrl, "advance", stop_after_advance)
        result = rig.runner.run(rig.plan)
        assert triggered == [1] and len(ticks) == 1
        assert result.status == "STOPPED"
        assert result.warning_code_counts == {"TEMPERATURE_OUT_OF_RANGE": 1}
        assert rig.logger.log_sample.call_count == 1
        assert rig.service._worker is None


class TestR16B24Fix1DrainInterrupt:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("cleanup_type,publication", [
        (None, None), (RuntimeError, None),
        (KeyboardInterrupt, None), (SystemExit, None),
        (KeyboardInterrupt, "BATTERY_UNDERVOLTAGE"),
        (SystemExit, "BATTERY_UNDERVOLTAGE"),
        (KeyboardInterrupt, "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"),
        (SystemExit, "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY"),
    ])
    def test_runner_keeps_floor_event_and_original_drain_interrupt(
        self, monkeypatch, fast, cleanup_type, publication,
    ):
        from queue import Queue
        from threading import Event
        from types import SimpleNamespace

        import Prog.src.pulse_measurement_service as pms
        from Prog.tests.mock_drivers.fake_clock import _B21bRig
        from Prog.tests.test_ocv_soc_controller import _b24_bound

        rig = _B21bRig.make(monkeypatch, fast, start=False)
        _B21bRig.runner(monkeypatch, rig)
        ticks = _b24_bound(monkeypatch, rig)
        created, closed, callbacks, order = [], [], [], []
        primary = cleanup_type("B24 fix1 drain interrupted") if cleanup_type else None
        for device, name in ((rig.load, "input_off"), (rig.psu, "all_outputs_off")):
            original = getattr(device, name)

            def off(_original=original, _name=name):
                order.append(_name)
                return _original()

            monkeypatch.setattr(device, name, off)

        def close():
            assert not closed, "B24 fix1 worker close repeated"
            closed.append(1)
            order.append("close")
            assert rig.service._discharge_fault is not None
            assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
            if primary is not None:
                raise primary

        def make_worker(service):
            assert not created, "B24 fix1 worker trigger repeated"
            created.append(1)
            results = Queue()
            results.put(("13.2,10.0,13.2", service._duration_s, None))
            rig.clock.now = service._block_deadline
            return SimpleNamespace(
                results=results, cancel=Event(), request=lambda count: None,
                close_after_safe_off=close, owner_release_confirmed=True,
                _thread=SimpleNamespace(is_alive=lambda: False),
            )

        def publish(payload):
            rig.events.append(payload)
            rig.logger.log_event(
                payload["event_code"], payload["event_message"],
                is_critical=payload.get("severity") == "CRITICAL",
            )
            if payload["event_code"] == publication:
                callbacks.append(publication)
                raise SystemExit("B24 fix1 publication interrupted")

        monkeypatch.setattr(pms, "_PulseDmmWorker", make_worker)
        monkeypatch.setattr(rig.ctrl, "on_event", publish)
        if cleanup_type in (KeyboardInterrupt, SystemExit):
            with pytest.raises(cleanup_type) as caught:
                rig.runner.run(rig.plan)
            assert caught.value is primary
        else:
            result = rig.runner.run(rig.plan)
            assert result.status == "FAULT"
        assert created == closed == [1] and ticks
        assert order[:5] == ["input_off", "input_off", "input_off", "all_outputs_off", "close"]
        assert order[5:] == ["input_off", "all_outputs_off"]  # runner terminal safe-off
        assert rig.ctrl.state is OcvSocState.FAULT
        assert rig.service._discharge_fault is not None
        assert "10.000000 V < 10.800000 V" in rig.ctrl.fault_reason
        floor = [e for e in rig.events if e["event_code"] == "BATTERY_UNDERVOLTAGE"]
        assert len(floor) == 1 and floor[0]["severity"] == "CRITICAL"
        assert "10.000000 V < 10.800000 V" in floor[0]["event_message"]
        logged = [
            c for c in rig.logger.log_event.call_args_list
            if c.args and c.args[0] == "BATTERY_UNDERVOLTAGE"
        ]
        assert len(logged) == 1 and logged[0].kwargs["is_critical"]
        assert callbacks == ([publication] if publication else [])
        if publication:
            marker = (
                "PULSE_FLOOR_EVENT_FAILED" if publication == "BATTERY_UNDERVOLTAGE"
                else "PULSE_PREFIX_BOOKING_FAILED"
            )
            assert any(marker in note for note in primary.__notes__)
        assert rig.service._worker is None and rig.dmm._measurement_owner is None
        assert rig.dmm.nplc == 10.0 and not rig.service.nplc_restore_failed
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        rig.logger.close.assert_called_once()


@pytest.mark.parametrize("tick", [5.0, 30.0])
@pytest.mark.parametrize("phase,target", [
    (LearningState.PRECHARGE_RELAX, LearningState.INITIAL_SNAPSHOT),
    (LearningState.RELAX_1, LearningState.VERIFY_UPDATE_STATUS_05),
    (LearningState.RELAX_PRE_DISCHARGE_2, LearningState.DISCHARGE_2),
    (LearningState.RELAX_2, LearningState.VERIFY_UPDATE_STATUS_06),
])
def test_r16b25_timeout_with_worker_ready_before_first_sleep(monkeypatch, tick, phase, target):
    from threading import Event

    from Prog.src.relax_controller import RelaxSamplingService
    from Prog.tests.test_relax_controller import _B5bRig

    ready, waits = Event(), []
    start = RelaxSamplingService.start

    def wait(self, event, delay):
        waits.append(delay)
        assert len(waits) <= 100, "B25 worker-wait korlát"
        self.requests.put(delay)
        ready.set()
        proceed = self.permits.get(timeout=2)
        return not proceed or event.is_set()

    def synchronized_start(self, **kwargs):
        start(self, **kwargs)
        assert ready.wait(2), "B25 worker-start trigger hiányzik"

    monkeypatch.setattr(_B5bRig, "wait", wait)
    monkeypatch.setattr(RelaxSamplingService, "start", synchronized_start)
    TestR16B5cF3().test_timeout_pending_release_reports_once(
        monkeypatch, tick, phase, target,
    )
    assert ready.is_set() and waits, "A kedvezőtlen ütemezésnek ténylegesen le kell futnia"


@pytest.mark.parametrize("primary_fault", [None, "", "B25_PRIMARY_FAULT"])
@pytest.mark.parametrize("kind,location", [
    ("tlc", "snapshot"), ("tlc", "precond"), ("tlc", "it_enable"),
    ("gg", "read_chem_id"), ("gg", "read_control_status"),
])
@pytest.mark.parametrize("mode,status,reason", [
    ("exception", "FAULT", "UNHANDLED_EXCEPTION: B25_FIRST_POLL_ABORT"),
    ("stop", "STOPPED", "USER_STOP_REQUESTED"),
    ("emergency", "FAULT", "B25_FIRST_POLL_ABORT"),
])
def test_r16b25_first_poll_abort_runner_contract(
    monkeypatch, mode, status, reason, kind, location, primary_fault,
):
    from unittest.mock import MagicMock

    import Prog.src.test_runner as module
    from Prog.src.exceptions import TestAbortedByUser
    from Prog.src.instrument_manager import InstrumentManager
    from Prog.tests.test_golden_gg_controller import make_controller as make_gg
    from Prog.tests.test_traditional_learning_controller import make_controller

    ctrl, psu, load, dmm, temp, bq = (
        make_gg() if kind == "gg" else make_controller()
    )
    if kind == "gg":
        ctrl._enter(GoldenGgState.PRECOND)
        read_target, read_method = ctrl._bq_driver, location
    else:
        ctrl._enter({
            "snapshot": LearningState.INITIAL_SNAPSHOT,
            "precond": LearningState.PRECOND,
            "it_enable": LearningState.IT_ENABLE_STEP,
        }[location])
        read_target, read_method = (
            (ctrl._bq_driver, "read_update_status") if location == "snapshot"
            else (ctrl._bq_transport, "read_data_flash_block")
        )
    read_original = getattr(read_target, read_method)
    ctrl._u_batt = 12.8
    instruments = InstrumentManager(psu, load, dmm, temp)
    logger = MagicMock()
    runner = TestRunner(
        instrument_manager=instruments, safety=ctrl._safety, logger=logger,
        profile=ctrl._profile, config=TestRunnerConfig(sleep_enabled=False),
        charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: None,
        relax_ctrl_factory=lambda step: None,
        traditional_learning_ctrl_factory=lambda: ctrl,
        golden_gg_ctrl_factory=lambda: ctrl,
    )
    clock, reads, ticks, off, events = [0.0], [], [], [], []
    propagated, off_at_abort = [], []
    abort = TestAbortedByUser("B25_FIRST_POLL_ABORT")
    advance, load_off, psu_off = ctrl.advance, load.input_off, psu.all_outputs_off
    emergency_stop = MagicMock(wraps=ctrl.emergency_stop)
    write_df = MagicMock(wraps=ctrl._bq_transport.write_data_flash_block)
    it_enable = MagicMock(wraps=ctrl._bq_transport.it_enable)

    def now():
        clock[0] += 0.1
        return clock[0]

    def guarded_advance(dt_s):
        ticks.append(True)
        assert len(ticks) <= 6, "B25 első-poll runner-korlát"
        try:
            return advance(dt_s)
        except TestAbortedByUser as exc:
            assert exc is abort
            propagated.append(exc)
            raise

    def read_status(*args, **kwargs):
        reads.append(True)
        assert len(reads) <= (3 if kind == "gg" else 2), "B25 BQ-olvasási korlát"
        if len(reads) == 1:
            off_at_abort.append(len(off))
            if mode == "stop":
                runner.request_stop()
            elif mode == "emergency":
                runner.request_emergency_stop("B25_FIRST_POLL_ABORT")
            if primary_fault is not None:
                ctrl.emergency_stop(primary_fault)
            raise abort
        # GG: a második/harmadik CONTROL_STATUS a valódi fake seal-cleanupé.
        return read_original(*args, **kwargs) if kind == "gg" else 0x05

    def input_off():
        off.append("LOAD_OFF")
        load_off()

    def all_off():
        off.append("PSU_ALL_OFF")
        psu_off()

    monkeypatch.setattr(module.time, "monotonic", now)
    monkeypatch.setattr(module.time, "perf_counter", now)
    monkeypatch.setattr(ctrl, "advance", guarded_advance)
    monkeypatch.setattr(read_target, read_method, read_status)
    monkeypatch.setattr(ctrl, "emergency_stop", emergency_stop)
    monkeypatch.setattr(ctrl._bq_transport, "write_data_flash_block", write_df)
    monkeypatch.setattr(ctrl._bq_transport, "it_enable", it_enable)
    monkeypatch.setattr(load, "input_off", input_off)
    monkeypatch.setattr(psu, "all_outputs_off", all_off)
    runner.on_event = events.append
    plan = TestPlan.golden_gg() if kind == "gg" else TestPlan.traditional_learning()
    result = runner.run(plan)
    expected_reads = 3 if kind == "gg" and location == "read_control_status" else 1
    assert reads == [True] * expected_reads and 1 <= len(ticks) <= 6
    assert propagated == [abort], "Az első BQ-olvasás abortja nem jutott el a runnerig"
    if primary_fault is not None:
        status, reason = "FAULT", primary_fault or "CONTROLLER_FAULT"
    assert (result.status, result.reason) == (status, reason)
    assert result.safe_off_attempted
    assert result.safe_off_result["PSU"] is True and result.safe_off_result["Load"] is True
    assert off[:2] == ["LOAD_OFF", "PSU_ALL_OFF"]
    assert off[off_at_abort[0]:off_at_abort[0] + 2] == ["LOAD_OFF", "PSU_ALL_OFF"]
    emergency_stop.assert_called_once_with(primary_fault if primary_fault is not None else reason)
    assert not psu.output_commanded_on and not load.input_commanded_on
    assert "input_on()" not in load.calls
    assert "output_on()" not in psu.calls
    write_df.assert_not_called()
    it_enable.assert_not_called()
    if kind == "tlc":
        assert not {"write_data_flash_block", "it_enable"}.intersection(bq.calls)
        assert ("unseal" in bq.calls) is (location != "snapshot")
        assert ctrl.runner_abort_origin == (None if primary_fault is not None else reason)
    else:
        methods = [req["method"] for req in ctrl._bq_transport.serial.written_requests]
        assert not {"write_data_flash_block", "it_enable"}.intersection(methods)
        assert ("unseal" in methods) is (location == "read_control_status")
        assert bq.sealed
        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == (primary_fault if primary_fault is not None else reason)
    terminal = "GRACEFUL_STOP" if status == "STOPPED" else "EMERGENCY_STOP"
    event, = [
        e for e in events
        if e["event_code"] in ("GRACEFUL_STOP", "EMERGENCY_STOP", "TEST_DONE")
    ]
    assert (event["event_code"], event["event_message"]) == (terminal, reason)
    logged, = [
        call for call in logger.log_event.call_args_list
        if call.args[0] in ("GRACEFUL_STOP", "EMERGENCY_STOP", "TEST_DONE")
    ]
    assert logged.args == (terminal, reason)
    logger.close.assert_called_once()


@pytest.mark.parametrize("kind", ["normal", "tlc"])
def test_r16b26b_config_fault_runner_and_checkpoint(monkeypatch, kind):
    from unittest.mock import MagicMock

    import Prog.src.test_runner as module
    from Prog.src.discharge_controller import DischargeState
    from Prog.src.instrument_manager import InstrumentManager
    from Prog.tests.test_discharge_controller import make_discharge_controller
    from Prog.tests.test_traditional_learning_controller import make_controller

    if kind == "normal":
        ctrl, psu, load, dmm = make_discharge_controller()
        temp = ctrl._dmm_t
        ctrl._state = DischargeState.DISCHARGE_CC_SETUP
        ctrl._config.terminate_voltage_V_override = 9.0
        plan = TestPlan.discharge_only()
    else:
        ctrl, psu, load, dmm, temp, _bq = make_controller()
        ctrl._enter(LearningState.REBOUND_CHECK_1)
        ctrl._phase_elapsed_s = ctrl._config.rebound_settle_s
        ctrl._config.max_rebound_retries = 5
        plan = TestPlan.traditional_learning()
    dmm.voltage_V = 12.8
    logger = MagicMock()
    runner = TestRunner(
        instrument_manager=InstrumentManager(psu, load, dmm, temp),
        safety=ctrl._safety, logger=logger, profile=ctrl._profile,
        config=TestRunnerConfig(sleep_enabled=False),
        charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: ctrl,
        relax_ctrl_factory=lambda step: None,
        traditional_learning_ctrl_factory=lambda: ctrl,
    )
    clock, clocks, ticks, off, events = [0.0], [], [], [], []
    advance, load_off, psu_off = ctrl.advance, load.input_off, psu.all_outputs_off

    def now():
        clocks.append(True)
        assert len(clocks) <= 500, "Q46 runner clock bound"
        clock[0] += 0.01
        return clock[0]

    def guarded_advance(dt_s):
        ticks.append(True)
        assert len(ticks) <= 6, "Q46 runner tick bound"
        return advance(dt_s)

    def stop_load():
        off.append("LOAD")
        assert len(off) <= 12, "Q46 runner OFF bound"
        load_off()

    def stop_psu():
        off.append("PSU")
        assert len(off) <= 12, "Q46 runner OFF bound"
        psu_off()

    monkeypatch.setattr(module.time, "monotonic", now)
    monkeypatch.setattr(module.time, "perf_counter", now)
    monkeypatch.setattr(ctrl, "advance", guarded_advance)
    monkeypatch.setattr(load, "input_off", stop_load)
    monkeypatch.setattr(psu, "all_outputs_off", stop_psu)
    runner.on_event = events.append
    result = runner.run(plan)
    assert 1 <= len(ticks) <= 6, "Q46 actual-controller trigger"
    assert (result.status, result.reason) == ("FAULT", "DISCHARGE_CONFIG_INVALID")
    assert result.safe_off_attempted
    assert off[:2] == ["LOAD", "PSU"]
    assert not load.input_commanded_on and not psu.output_commanded_on
    assert not load.called("input_on") and not psu.called("output_on")
    assert any(e["event_code"] == "EMERGENCY_STOP" and e["event_message"] == result.reason for e in events)
    terminal = logger.write_checkpoint.call_args.args[0]
    assert (terminal["status"], terminal["reason"]) == ("FAULT", result.reason)
    logger.log_event.assert_any_call("EMERGENCY_STOP", result.reason, is_critical=True)
    logger.close.assert_called_once_with()


def _b23b_memory_runner(monkeypatch, scenario="done"):
    from copy import deepcopy
    from unittest.mock import MagicMock

    import Prog.src.test_runner as module
    from Prog.src.instrument_manager import InstrumentManager
    from Prog.tests.test_traditional_learning_controller import _b23b_final_ready

    ctrl, psu, load, dmm, temp, bq, _ = _b23b_final_ready()
    logger = MagicMock()
    if scenario == "psu_on":
        psu.raise_on_output_on = True
    runner = TestRunner(
        instrument_manager=InstrumentManager(psu, load, dmm, temp),
        safety=ctrl._safety, logger=logger, profile=ctrl._profile,
        config=TestRunnerConfig(sleep_enabled=False),
        charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: None,
        relax_ctrl_factory=lambda step: None, traditional_learning_ctrl_factory=lambda: ctrl,
    )
    checkpoints, samples, ticks, fired, off = [], [], [], [], []
    guard_failures = []

    def guard(callback):
        def checked(*args, **kwargs):
            try:
                return callback(*args, **kwargs)
            except AssertionError:
                guard_failures.append(callback.__name__)
                raise
        return checked

    clock = [0.0, 0]
    original_advance = ctrl.advance
    original_voltage = dmm.read_voltage

    def now():
        clock[1] += 1
        assert clock[1] <= 4000, "B23b runner clock bound"
        clock[0] += 0.01
        return clock[0]

    def checkpoint(record):
        assert len(checkpoints) < 80, "B23b checkpoint bound"
        checkpoints.append(deepcopy(record))
        if scenario == "checkpoint" and record.get("tlc_completion", {}).get("learning_evidence"):
            fired.append("checkpoint")
            raise OSError("B23b forced checkpoint unavailable")

    def voltage(**kwargs):
        child = ctrl._charge_ctrl
        if scenario in ("stop_before_on", "estop_before_on") and child is not None and child.state == ChargeState.PSU_PRESET:
            assert not fired, "B23b before-ON trigger must be unique"
            fired.append(scenario)
            if scenario == "stop_before_on":
                runner.request_stop()
            else:
                runner.request_emergency_stop("USER_EMERGENCY_STOP")
        return original_voltage(**kwargs)

    def advance(dt_s):
        ticks.append(True)
        assert len(ticks) <= 24, "B23b runner tick bound"
        if psu.output_commanded_on and not fired and scenario in ("stop", "estop", "dmm", "temperature", "psu"):
            fired.append(scenario)
            if scenario == "stop":
                runner.request_stop()
            elif scenario == "estop":
                runner.request_emergency_stop("USER_EMERGENCY_STOP")
            elif scenario == "dmm":
                dmm.set_dmm_invalid()
            elif scenario == "temperature":
                temp.simulate_temp_failure = True
            else:
                psu.simulate_current_readback_timeout = True
        child = ctrl._charge_ctrl
        if child is not None and child.state in (ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD):
            dmm.voltage_V = child._effective_charge_target_V()
            psu.current_A = 0.1
        return original_advance(max(dt_s, 1.0))

    def on(**kwargs):
        if scenario == "psu_on":
            fired.append("psu_on")
        assert checkpoints[-1]["safe_boundary"] is False
        record = checkpoints[-1]["tlc_completion"]
        assert record["learning_evidence"]["result_summary"]["result"] == "BQ_LEARNING_VERIFIED"
        assert record["final_recharge"]["status"] == "RUNNING"
        assert bq.control_status_word & 0x2000
        original_on(**kwargs)

    original_on = psu.output_on
    for name, device, method in (("Load", load, "input_off"), ("PSU", psu, "all_outputs_off")):
        original = getattr(device, method)
        def safe_off(name=name, original=original):
            off.append(name)
            assert len(off) <= 40, "B23b OFF bound"
            return original()
        monkeypatch.setattr(device, method, guard(safe_off))
    monkeypatch.setattr(module.time, "monotonic", guard(now))
    monkeypatch.setattr(module.time, "perf_counter", guard(now))
    if scenario in ("stop_fast", "estop_fast"):
        from dataclasses import asdict, replace
        assert hasattr(ctrl, "_final_charge_config"), "Final charge configuration must exist"
        ctrl._final_charge_config = replace(
            ctrl._final_charge_config, fast_loop_enabled=True,
            fast_loop_period_s=0.05, fast_loop_max_substeps=4,
        )
        ctrl._tlc_completion["effective_charge_config"] = asdict(ctrl._final_charge_config)
        def sleep(_seconds):
            assert not fired, "B23b fast-loop stop trigger must be unique"
            fired.append(scenario)
            if scenario == "stop_fast":
                runner.request_stop()
            else:
                runner.request_emergency_stop("USER_EMERGENCY_STOP")
        monkeypatch.setattr(module.time, "sleep", guard(sleep))
    monkeypatch.setattr(ctrl, "advance", guard(advance))
    monkeypatch.setattr(dmm, "read_voltage", guard(voltage))
    monkeypatch.setattr(psu, "output_on", guard(on))
    logger.write_checkpoint.side_effect = guard(checkpoint)
    runner.on_sample = lambda sample: samples.append((ctrl.state, dict(sample)))
    runner._last_checkpoint_t = 1e9
    return runner, ctrl, logger, checkpoints, samples, ticks, fired, off, guard_failures


@pytest.mark.parametrize("scenario", ["done", "stop", "estop", "dmm", "temperature", "psu", "psu_on", "checkpoint", "stop_before_on", "estop_before_on", "stop_fast", "estop_fast"])
def test_b23b_runner_final_recharge_checkpoint_and_fault_chain(monkeypatch, scenario):
    from Prog.src.session_recovery import ResumeDecision, evaluate_resume

    runner, ctrl, logger, checkpoints, samples, ticks, fired, off, guard_failures = _b23b_memory_runner(monkeypatch, scenario)
    result = runner.run(TestPlan.traditional_learning())
    assert not guard_failures, f"B23b swallowed callback AssertionError: {guard_failures}"
    assert 1 <= len(ticks) <= 24, "B23b real controller trigger"
    if scenario != "done":
        assert fired, "B23b fault/stop injection must actually fire"
    if scenario == "done":
        assert result.status == "DONE"
        assert hasattr(LearningState, "FINAL_RECHARGE"), "Final charge state must exist"
        active = [s for state, s in samples if state == LearningState.FINAL_RECHARGE and s.get("signed_current_A", 0) > 0]
        assert active, "Final recharge current must reach real runner samples"
        assert all(s["charge_current_A"] == s["signed_current_A"] > 0 for s in active)
        assert result.total_charge_ah == pytest.approx(ctrl.accumulated_charge_Ah)
        assert result.total_charge_wh == pytest.approx(ctrl.accumulated_charge_Wh)
        assert result.total_charge_ah > 0
    else:
        assert result.status == ("STOPPED" if scenario.startswith("stop") else "FAULT")
        expected_event = "GRACEFUL_STOP" if result.status == "STOPPED" else "EMERGENCY_STOP"
        assert any(call.args[0] == expected_event for call in logger.log_event.call_args_list)
    record = runner.tlc_completion
    assert record["learning_evidence"]["result_summary"]["result"] == "BQ_LEARNING_VERIFIED"
    assert checkpoints[-1]["tlc_completion"] == record
    final = record["final_recharge"]
    assert final["status"] == ("COMPLETED" if scenario == "done" else "INCOMPLETE")
    if scenario != "done":
        assert final["completed_at_utc"] is None
        assert checkpoints[-1]["safe_boundary"] is False
        verdict = evaluate_resume(checkpoints[-1], "", "", {}, "", total_step_count=1)
        assert verdict.decision == ResumeDecision.BLOCKED_UNSAFE_BOUNDARY
    if scenario in ("checkpoint", "stop_before_on", "estop_before_on"):
        assert not ctrl._psu.called("output_on")
    assert off[:2] == ["Load", "PSU"]
    assert not ctrl._load.input_commanded_on and not ctrl._psu.output_commanded_on
    logger.close.assert_called_once_with()
