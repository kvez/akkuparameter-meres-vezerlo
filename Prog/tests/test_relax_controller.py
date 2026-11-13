"""
RelaxController unit tesztek.
Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: last_voltage_V/
dmm_fault_count property-k — korábban a hívók (traditional_learning_
controller.py/golden_gg_controller.py) egy HARMADIK, önálló DMM-olvasással
duplikálták a RelaxController saját, már beolvasott értékét, és a
dmm_fault_count sosem volt kiolvasva (CLAUDE.md §9).
"""
from __future__ import annotations

import pytest

from Prog.src.battery_profile import BatteryProfile
from Prog.src.relax_controller import RelaxConfig, RelaxController
from Prog.src.safety import PsuMode, SafetyManager, TempCompMode
from Prog.tests.mock_drivers.mock_dmm import MockDMM


def _make_safety(**profile_kwargs) -> SafetyManager:
    profile = BatteryProfile(
        battery_name="TEST", manufacturer="TEST", model="TST7AH",
        nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        **profile_kwargs,
    )
    return SafetyManager(
        profile=profile, psu_mode=PsuMode.INDEPENDENT, temp_comp_mode=TempCompMode.MONITOR_ONLY,
    )


class TestLastVoltageV:
    def test_zero_before_any_advance(self):
        ctrl = RelaxController(MockDMM(voltage_V=12.5), RelaxConfig(min_relax_s=10.0))
        assert ctrl.last_voltage_V == 0.0

    def test_reflects_init_tick_reading(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)  # INIT -> RELAXING, első olvasás
        assert ctrl.last_voltage_V == 12.5

    def test_reflects_relaxing_tick_reading(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)  # INIT
        dmm.voltage_V = 12.3
        ctrl.advance(1.0)  # RELAXING
        assert ctrl.last_voltage_V == 12.3

    def test_does_not_call_read_voltage_extra_times(self):
        """A hívónak a last_voltage_V-t kell használnia, NEM szabad újra
        olvasnia a DMM-et — ez a teszt magát a RelaxControllert igazolja:
        egyetlen advance() hívás legfeljebb egyszer olvas."""
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(1.0)
        assert dmm.calls.count("read_voltage()") == 1


class TestDmmFaultCount:
    def test_zero_when_no_faults(self):
        ctrl = RelaxController(MockDMM(voltage_V=12.5), RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.dmm_fault_count == 0

    def test_increments_on_init_tick_failure(self):
        dmm = MockDMM(voltage_V=12.5, simulate_timeout=True)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        assert ctrl.dmm_fault_count == 1

    def test_increments_on_relaxing_tick_failure(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        dmm.simulate_timeout = True
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.dmm_fault_count == 2

    def test_last_voltage_unchanged_after_failed_read(self):
        """Sikertelen olvasás esetén a last_voltage_V a legutóbbi SIKERES
        értéken marad (nem 0/None-ra esik)."""
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        assert ctrl.last_voltage_V == 12.5
        dmm.simulate_timeout = True
        ctrl.advance(1.0)
        assert ctrl.last_voltage_V == 12.5


class TestBatch42bF2RelaxDmmFaultEventFiresOnlyOnTransition:
    """[regresszió, BATCH 4.2b F-2, 2026-09-13, DECISION_REGISTER.md
    GOV-024, "5. csoport"] korábban a RELAX_DMM_FAULT esemény MINDEN
    ticken tüzelt, amíg a DMM-kiesés tartott -- egy hosszú relax alatti
    tartós kiesés ezres nagyságrendű azonos eseményt generálhatott,
    kiürítve az EventLogWidget 500-soros gyűrűjéből a korábbi
    kontextust. Diszkrimináló: a régi kódon 5 egymást követő hibás tick
    5 eseményt adna, nem 1-et."""

    def test_consecutive_relaxing_failures_fire_event_only_once(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=1000.0))
        ctrl.advance(0.0)  # INIT -> RELAXING, sikeres olvasás
        events: list = []
        ctrl.on_event = events.append
        dmm.simulate_timeout = True

        for _ in range(5):
            ctrl.advance(1.0)

        fault_events = [e for e in events if e["event_code"] == "RELAX_DMM_FAULT"]
        assert len(fault_events) == 1
        assert ctrl.dmm_fault_count == 5  # a számláló továbbra is minden ticken nő

    def test_recovery_then_new_failure_fires_a_second_event(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=1000.0))
        ctrl.advance(0.0)
        events: list = []
        ctrl.on_event = events.append

        dmm.simulate_timeout = True
        ctrl.advance(1.0)
        dmm.simulate_timeout = False
        ctrl.advance(1.0)  # helyreáll
        dmm.simulate_timeout = True
        ctrl.advance(1.0)  # új kiesés -- ez is egy ÚJ átmenet

        fault_events = [e for e in events if e["event_code"] == "RELAX_DMM_FAULT"]
        assert len(fault_events) == 2

    def test_init_failure_continuing_into_relaxing_does_not_double_fire(self):
        """Az INIT-ági hiba (egyszeri) UTÁN, ha a RELAXING ELSŐ tickje is
        hibás, ez NEM új átmenet (self._dmm_valid már False volt az INIT
        hiba óta) -- tehát csak az INIT-esemény tüzel, a folytatódó
        (nem ÚJ) RELAXING-hiba nem duplikálja."""
        dmm = MockDMM(voltage_V=12.5, simulate_timeout=True)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=1000.0))
        events: list = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)  # INIT hiba -- 1 esemény
        ctrl.advance(1.0)  # RELAXING, MÉG MINDIG hibás -- NEM új átmenet

        fault_events = [e for e in events if e["event_code"] == "RELAX_DMM_FAULT"]
        assert len(fault_events) == 1


class TestR16B4WindowEstimator:
    @staticmethod
    def _new(**overrides):
        cfg = RelaxConfig(**overrides)
        dmm = MockDMM(voltage_V=12.5)
        return RelaxController(dmm, cfg), dmm, cfg

    @staticmethod
    def _fill(ctrl, dmm, cfg, slope=0.0):
        dt = min(cfg.dvdt_max_gap_s, cfg.sample_period_s / (cfg.dvdt_min_samples - 1))
        start = ctrl.elapsed_s
        for _ in range(int(cfg.sample_period_s / dt) + 1):
            dmm.voltage_V = 12.5 + slope * (ctrl.elapsed_s + dt - start)
            ctrl.advance(dt)

    def test_initial_and_short_windows_are_invalid_not_zero(self):
        from math import isnan

        ctrl, _dmm, _cfg = self._new()
        assert isnan(ctrl.dvdt_V_per_s)
        ctrl.advance(0.0)
        ctrl.advance(2.0)
        assert not ctrl.dvdt_valid
        assert isnan(ctrl.dvdt_V_per_s)

    @pytest.mark.parametrize("window,count", [(30.0, 16), (60.0, 31), (120.0, 61)])
    def test_actual_jittered_time_and_left_boundary(self, window, count):
        ctrl, dmm, _cfg = self._new(sample_period_s=window, dvdt_min_samples=count)
        ctrl.advance(0.0)
        # Aszimmetrikus időközök: páronként 0,6 + 1,0 = 1,6 névleges tick.
        # V(t) = 12,5 V + (10 uV/s)*t, tehát kézzel: dV/dt = 10 uV/s.
        # Névleges időalappal kb. 8 uV/s adódna, ezért az a mutáns bukik.
        times = [0.0]
        dt = window / (count - 1)
        for i in range(2 * count):
            delta = dt * (0.6 if i % 2 == 0 else 1.0)
            times.append(times[-1] + delta)
            dmm.voltage_V = 12.5 + 10e-6 * times[-1]
            ctrl.advance(delta)
        assert ctrl.dvdt_valid
        assert ctrl.dvdt_V_per_s == pytest.approx(10e-6, abs=1e-12)
        assert ctrl._dvdt_samples[0][0] <= ctrl.elapsed_s - window
        assert ctrl._dvdt_samples[1][0] > ctrl.elapsed_s - window

    def test_ols_uses_all_samples_not_endpoints(self):
        ctrl, dmm, _cfg = self._new(sample_period_s=6.0, dvdt_min_samples=4)
        # Independent OLS oracle: t=[0,2,4,6], V=[12.5,12.501,12.5,12.5].
        for dt, v in [(0.0, 12.5), (2.0, 12.501), (2.0, 12.5), (2.0, 12.5)]:
            dmm.voltage_V = v
            ctrl.advance(dt)
        assert ctrl.dvdt_valid
        assert ctrl.dvdt_V_per_s == pytest.approx(-0.00005, abs=1e-12)

    def test_full_span_with_too_few_samples_is_invalid(self):
        ctrl, _dmm, cfg = self._new()
        ctrl.advance(0.0)
        dt = cfg.dvdt_max_gap_s
        for _ in range(int(cfg.sample_period_s / dt) + 1):
            ctrl.advance(dt)
        assert ctrl.elapsed_s >= cfg.sample_period_s
        assert not ctrl.dvdt_valid

    def test_duplicate_timestamps_never_count_as_distinct(self):
        ctrl, _dmm, cfg = self._new()
        for _ in range(cfg.dvdt_min_samples + 2):
            ctrl.advance(0.0)
        assert not ctrl.dvdt_valid
        assert len(ctrl._dvdt_samples) == 1

    @pytest.mark.parametrize("failure", ["init", "dmm", 4.01, 5.0])
    def test_invalidates_and_refills_without_interpolation(self, failure):
        from math import isnan

        ctrl, dmm, _cfg = self._new(min_relax_s=1.0)
        if failure != "init":
            ctrl.advance(0.0)
            for _ in range(60):
                ctrl.advance(1.0)
            assert ctrl.dvdt_valid
            assert len(ctrl._dvdt_samples) == 61
        if failure in ("init", "dmm"):
            dmm.simulate_timeout = True
            ctrl.advance(1.0)
            assert not ctrl.dmm_valid
            assert not ctrl.dvdt_valid
            assert isnan(ctrl.dvdt_V_per_s)
            assert len(ctrl._dvdt_samples) == 0
            dmm.simulate_timeout = False
            ctrl.advance(1.0)
        else:
            ctrl.advance(failure)
        assert ctrl.dmm_valid
        assert not ctrl.dvdt_valid
        assert isnan(ctrl.dvdt_V_per_s)
        assert len(ctrl._dvdt_samples) == 1
        for _ in range(59):
            ctrl.advance(1.0)
            assert not ctrl.dvdt_valid
            assert isnan(ctrl.dvdt_V_per_s)
        # 0,01 s ráhagyás a 4,01 s-os rés lebegőpontos időhatárához.
        ctrl.advance(1.01)
        assert ctrl.dvdt_valid
        assert ctrl.dvdt_V_per_s == pytest.approx(0.0)

    def test_gap_limit_itself_does_not_reset(self):
        ctrl, dmm, cfg = self._new()
        ctrl.advance(0.0)
        self._fill(ctrl, dmm, cfg)
        for _ in range(int(cfg.sample_period_s)):
            ctrl.advance(1.0)
        ctrl.advance(cfg.dvdt_max_gap_s)
        assert ctrl.dvdt_valid

    def test_zero_regression_denominator_is_invalid(self):
        from math import isnan

        ctrl, _dmm, _cfg = self._new(sample_period_s=1e-200, dvdt_min_samples=2)
        ctrl.advance(0.0)
        ctrl.advance(1e-200)
        assert not ctrl.dvdt_valid
        assert isnan(ctrl.dvdt_V_per_s)
        assert ctrl.dmm_fault_count == 0
        assert ctrl.dmm_valid is True
        assert ctrl.last_voltage_V == 12.5

    @pytest.mark.parametrize("change", ["ramp", "transient"])
    def test_late_change_updates_window_after_relax_done(self, change):
        ctrl, dmm, cfg = self._new(min_relax_s=1.0)
        ctrl.advance(0.0)
        self._fill(ctrl, dmm, cfg)
        assert ctrl.dvdt_valid
        assert ctrl.dvdt_V_per_s == pytest.approx(0.0)
        start = ctrl.elapsed_s
        dt = cfg.sample_period_s / (cfg.dvdt_min_samples - 1)
        for _ in range(cfg.dvdt_min_samples):
            elapsed = ctrl.elapsed_s + dt - start
            dmm.voltage_V = 12.5 + (20e-6 * elapsed if change == "ramp" else 0.02)
            ctrl.advance(dt)
            if change == "transient":
                break
        assert ctrl.dvdt_valid
        assert abs(ctrl.dvdt_V_per_s) > 4e-6

    def test_standalone_minimum_time_does_not_require_window(self):
        from Prog.src.relax_controller import RelaxState

        ctrl, _dmm, _cfg = self._new(min_relax_s=1.0)
        assert ctrl.advance(0.0) == RelaxState.RELAXING
        assert ctrl.advance(1.0) == RelaxState.RELAX_DONE
        assert not ctrl.dvdt_valid


class TestDmmValid:
    """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), Szoftver/
    Architektúra P2 lelet] a testvér controllerek mind exponálnak dmm_valid
    property-t — ez hiányzott, ezért az önálló StepKind.RELAX úton
    (test_runner.py::_build_sample() getattr(controller, "dmm_valid", None))
    a samples.csv dmm_voltage_valid mezője mindig a hamis "True" fallback-re
    esett (mert last_voltage_V sticky, sosem None), függetlenül attól, hogy
    az adott ticken ténylegesen sikerült-e olvasni."""

    def test_true_by_default_before_any_advance(self):
        ctrl = RelaxController(MockDMM(voltage_V=12.5), RelaxConfig(min_relax_s=10.0))
        assert ctrl.dmm_valid is True

    def test_false_on_init_tick_failure(self):
        dmm = MockDMM(voltage_V=12.5, simulate_timeout=True)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        assert ctrl.dmm_valid is False

    def test_false_on_relaxing_tick_failure(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        dmm.simulate_timeout = True
        ctrl.advance(1.0)
        assert ctrl.dmm_valid is False

    def test_recovers_to_true_after_successful_read(self):
        dmm = MockDMM(voltage_V=12.5, simulate_timeout=True)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        assert ctrl.dmm_valid is False
        dmm.simulate_timeout = False
        ctrl.advance(1.0)
        assert ctrl.dmm_valid is True


class TestExtremeDtS:
    """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), Szoftver/
    Architektúra P2 lelet] egy kiugró dt_s (blokkoló VISA I/O, GUI-torlódás)
    a RELAX fázis alatt korábban sosem jutott el a session-szintű
    TestResult.extreme_dt_s_count-ig (9. kör capacity_result_quality
    kapuja) — a RelaxController egyáltalán nem exponálta ezt a property-t."""

    def test_zero_by_default(self):
        ctrl = RelaxController(MockDMM(voltage_V=12.5), RelaxConfig(min_relax_s=10.0))
        assert ctrl.extreme_dt_s_count == 0
        assert ctrl.max_observed_dt_s == 0.0

    def test_extreme_dt_s_counted_during_relax(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)  # INIT -> RELAXING
        ctrl.advance(301.0)  # kiugró dt_s (>300s küszöb)
        assert ctrl.extreme_dt_s_count == 1
        assert ctrl.max_observed_dt_s == 301.0

    def test_normal_dt_s_not_counted(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        ctrl.advance(30.0)
        assert ctrl.extreme_dt_s_count == 0


class TestTemperatureMonitoring:
    """[regresszió, Teljes kódbázis review 22. kör (2026-09-07), Szoftver/
    Architektúra P2 lelet] az önálló StepKind.RELAX úton (CHARACTERIZATION/
    BQ_LEARNING_PHYSICAL/CHARGE_ONLY tervek relax-lépései) korábban SEMMI
    nem figyelte a hőmérséklet-DMM kiesését/a profil hőmérséklet-
    tartományát — a testvér controllerek relax-alállapotai (golden_gg/
    traditional_learning KÜLSŐ advance()-je) ezt már megtették."""

    def test_no_temperature_monitoring_without_dmm_temperature(self):
        """Beágyazott golden_gg/traditional_learning használat (dmm_
        temperature=None) — a KÜLSŐ controller már fedi a hőmérsékletet,
        a RelaxController maga NE duplázza."""
        dmm_v = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm_v, RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.temperature_out_of_range_count == 0
        assert ctrl.last_warning_code == ""

    def test_battery_temperature_out_of_profile_range_surfaces_as_warning(self):
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=35.0)  # 30°C alapértelmezett max felett
        safety = _make_safety()
        ctrl = RelaxController(
            dmm_v, RelaxConfig(min_relax_s=10.0), dmm_temperature=dmm_t, safety=safety,
        )
        ctrl.advance(0.0)  # INIT -> RELAXING (1. tick, hőmérséklet-ellenőrzés fut)
        ctrl.advance(1.0)  # RELAXING (2. tick, ismét fut)
        assert ctrl.last_warning_code == "TEMPERATURE_OUT_OF_RANGE"
        assert ctrl.temperature_out_of_range_count == 2

    def test_battery_temperature_in_range_no_warning(self):
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = _make_safety()
        ctrl = RelaxController(
            dmm_v, RelaxConfig(min_relax_s=10.0), dmm_temperature=dmm_t, safety=safety,
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.last_warning_code == ""
        assert ctrl.temperature_out_of_range_count == 0

    def test_temperature_dmm_lost_surfaces_as_warning_within_timeout(self):
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0, simulate_temp_failure=True)
        safety = _make_safety()
        ctrl = RelaxController(
            dmm_v, RelaxConfig(min_relax_s=10.0), dmm_temperature=dmm_t, safety=safety,
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)  # timeout (60s, MONITOR_ONLY) alatt -> warning
        assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"

    def test_temperature_dmm_lost_warning_not_overwritten_by_range_warning(self):
        """A stale (DMM-kiesés alatt nem frissülő) battery_temperature_C a
        profil tartományán kívül eshet — a DMM-kiesés jelzésének elsőbbséget
        kell élveznie, ugyanaz a minta, mint a 6 fizikai-ciklusú
        controlleren (21. kör)."""
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=35.0, simulate_temp_failure=True)
        safety = _make_safety()
        ctrl = RelaxController(
            dmm_v, RelaxConfig(min_relax_s=10.0), dmm_temperature=dmm_t, safety=safety,
        )
        ctrl._battery_temperature_C = 35.0  # stale, a max felett
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"


class TestTempDmmValidProperty:
    """[regresszió, review-kör 3 (2026-09-09), MAX verifier
    (a9cb161469c468fc8) P2-1] a temp_dmm_valid property (Metrológia
    M3-01, P1) egyik controlleren sem kapott önálló, controller-szintű
    regressziós tesztet -- ez a hiány engedte át a chemid_controller.py-
    beli P1-1 blokkoló hibát 1792 zöld teszt mellett."""

    def test_true_after_successful_temperature_read(self):
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = _make_safety()
        ctrl = RelaxController(
            dmm_v, RelaxConfig(min_relax_s=10.0), dmm_temperature=dmm_t, safety=safety,
        )
        ctrl.advance(0.0)
        assert ctrl.temp_dmm_valid is True

    def test_false_after_failed_temperature_read(self):
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0, simulate_temp_failure=True)
        safety = _make_safety()
        ctrl = RelaxController(
            dmm_v, RelaxConfig(min_relax_s=10.0), dmm_temperature=dmm_t, safety=safety,
        )
        ctrl.advance(0.0)
        assert ctrl.temp_dmm_valid is False


class TestTempDmmFaultTicks:
    """[regresszió, review-kör 4 (2026-09-09), Metrológia M4-01] a
    RelaxController volt az egyetlen step-controller, amelyről hiányzott
    a temp_dmm_fault_ticks property -- a temp_dmm_valid mellé nem került
    be, holott a testvér temperature_out_of_range_count property már
    exponálva volt. Emiatt egy önálló StepKind.RELAX session hőmérséklet-
    DMM-kiesését a test_runner.py generikus aggregációja (getattr(...,
    "temp_dmm_fault_ticks", 0)) sosem látta."""

    def test_counts_failed_reads_not_seconds(self):
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = _make_safety()
        ctrl = RelaxController(
            dmm_v, RelaxConfig(min_relax_s=10.0), dmm_temperature=dmm_t, safety=safety,
        )
        ctrl.advance(0.0)
        assert ctrl.temp_dmm_fault_ticks == 0

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_fault_ticks == 1

        ctrl.advance(1.0)
        assert ctrl.temp_dmm_fault_ticks == 2

        dmm_t.simulate_temp_failure = False
        ctrl.advance(1.0)  # sikeres olvasás -- a tick-számláló NEM nullázódik
        assert ctrl.temp_dmm_fault_ticks == 2

    def test_zero_when_embedded_no_dmm_t(self):
        """Beágyazott használatnál (dmm_temperature=None) a számláló inaktív
        marad -- a KÜLSŐ controller fedi a hőmérséklet-DMM-et, l. __init__
        docstringje."""
        ctrl = RelaxController(MockDMM(voltage_V=12.5), RelaxConfig(min_relax_s=10.0))
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_fault_ticks == 0


class TestR11B3TemperatureFaultEvents:
    """R11-B3: esemeny a fault-atmenetkor, a tick-szamlalo valtozatlan."""

    @staticmethod
    def _make_ctrl(mode, *, elapsed_fault_s=0.0):
        safety = _make_safety()
        safety.temp_comp_mode = mode
        safety.temperature_dmm_fault_timeout_s = 60.0
        safety.temperature_dmm_fault_s = elapsed_fault_s
        dmm_t = MockDMM(temperature_C=22.0, simulate_temp_failure=True)
        ctrl = RelaxController(
            MockDMM(voltage_V=12.5), RelaxConfig(min_relax_s=1000.0),
            dmm_temperature=dmm_t, safety=safety,
        )
        events = []
        ctrl.on_event = events.append
        return ctrl, dmm_t, safety, events

    def test_enabled_100_failed_ticks_emit_one_critical_event(self):
        ctrl, dmm_t, safety, events = self._make_ctrl(TempCompMode.ENABLED)

        for _ in range(100):
            ctrl.advance(2.0)
            assert ctrl.last_warning_code == "TEMPERATURE_MONITOR_LOST_CRITICAL"

        assert len(events) == 1
        assert events[0]["event_code"] == "RELAX_TEMPERATURE_DMM_FAULT"
        assert events[0]["severity"] == "CRITICAL"
        assert ctrl.temp_dmm_fault_ticks == 100
        assert ctrl.temp_dmm_valid is False
        assert safety.temperature_dmm_fault_s == 200.0
        assert dmm_t.calls.count("read_temperature()") == 100

    def test_monitor_only_emits_at_timeout_not_at_first_failed_read(self):
        ctrl, _dmm_t, safety, events = self._make_ctrl(TempCompMode.MONITOR_ONLY)

        for _ in range(29):
            ctrl.advance(2.0)
            assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"
            assert events == []
        assert safety.temperature_dmm_fault_s == 58.0

        ctrl.advance(2.0)
        assert safety.temperature_dmm_fault_s == 60.0
        assert len(events) == 1
        assert events[0]["event_code"] == "RELAX_TEMPERATURE_DMM_FAULT"
        assert events[0]["severity"] == "CRITICAL"

        for _ in range(70):
            ctrl.advance(2.0)
            assert ctrl.last_warning_code == "TEMPERATURE_MONITOR_LOST_CRITICAL"
            assert len(events) == 1
        assert ctrl.temp_dmm_fault_ticks == 100

    @pytest.mark.parametrize("mode", [TempCompMode.ENABLED, TempCompMode.MONITOR_ONLY])
    def test_recovery_rearms_event_without_resetting_failed_tick_count(self, mode):
        ctrl, dmm_t, safety, events = self._make_ctrl(mode)

        for _ in range(100):
            ctrl.advance(2.0)
        assert len(events) == 1

        dmm_t.simulate_temp_failure = False
        ctrl.advance(2.0)
        assert safety.temperature_dmm_fault_s == 0.0
        assert ctrl.temp_dmm_valid is True
        assert ctrl.last_warning_code == ""
        assert ctrl.temp_dmm_fault_ticks == 100
        assert len(events) == 1

        dmm_t.simulate_temp_failure = True
        first_fault_tick = 1 if mode == TempCompMode.ENABLED else 30
        for tick in range(1, 101):
            ctrl.advance(2.0)
            assert len(events) == (1 if tick < first_fault_tick else 2)

        assert all(e["event_code"] == "RELAX_TEMPERATURE_DMM_FAULT" for e in events)
        assert all(e["severity"] == "CRITICAL" for e in events)
        assert ctrl.temp_dmm_fault_ticks == 200
        assert ctrl.temp_dmm_valid is False
        assert ctrl.last_warning_code == "TEMPERATURE_MONITOR_LOST_CRITICAL"

    def test_inherited_fault_history_emits_on_first_local_tick(self):
        ctrl, _dmm_t, safety, events = self._make_ctrl(
            TempCompMode.MONITOR_ONLY, elapsed_fault_s=60.0,
        )

        for _ in range(100):
            ctrl.advance(2.0)

        assert len(events) == 1
        assert events[0]["event_code"] == "RELAX_TEMPERATURE_DMM_FAULT"
        assert events[0]["severity"] == "CRITICAL"
        assert ctrl.temp_dmm_fault_ticks == 100
        assert safety.temperature_dmm_fault_s == 260.0

    def test_off_keeps_fault_events_silent_and_counts_failed_reads(self):
        ctrl, _dmm_t, _safety, events = self._make_ctrl(TempCompMode.OFF)

        for _ in range(100):
            ctrl.advance(2.0)
            assert ctrl.last_warning_code == ""

        assert events == []
        assert ctrl.temp_dmm_fault_ticks == 100


class TestR16B4F1WindowPolicy:
    def test_q18_b_defaults_are_user_decision_literals(self):
        # Q-18 = B, felhasználói döntés 2026-11-07; DFAM-008 prevention.
        cfg = RelaxConfig()
        assert (cfg.sample_period_s, cfg.dvdt_min_samples, cfg.dvdt_max_gap_s) == (60.0, 31, 4.0)

    def test_sample_count_without_full_time_coverage_is_invalid(self):
        from math import isnan

        ctrl = RelaxController(MockDMM(voltage_V=12.5), RelaxConfig())
        ctrl.advance(0.0)
        for _ in range(30):
            ctrl.advance(1.0)
        assert len(ctrl._dvdt_samples) == 31
        assert ctrl.elapsed_s == 30.0
        assert ctrl.dmm_valid
        assert not ctrl.dvdt_valid
        assert isnan(ctrl.dvdt_V_per_s)
        for _ in range(29):
            ctrl.advance(1.0)
            assert not ctrl.dvdt_valid
            assert isnan(ctrl.dvdt_V_per_s)
        ctrl.advance(1.0)
        assert ctrl.dvdt_valid
        assert ctrl.dvdt_V_per_s == pytest.approx(0.0)

    @pytest.mark.parametrize("init_tick", [True, False])
    def test_estimator_exception_is_not_a_dmm_fault(self, monkeypatch, init_tick):
        ctrl = RelaxController(MockDMM(voltage_V=12.5), RelaxConfig())
        if not init_tick:
            ctrl.advance(0.0)
        events = []
        ctrl.on_event = events.append

        def fail_estimator(_voltage, _sample_time):
            raise ArithmeticError("becslőhiba")

        monkeypatch.setattr(ctrl, "_update_dvdt", fail_estimator)
        with pytest.raises(ArithmeticError, match="becslőhiba"):
            ctrl.advance(1.0)
        assert ctrl.dmm_valid is True
        assert ctrl.dmm_fault_count == 0
        assert ctrl.last_voltage_V == 12.5
        assert events == []


# R16-B5b: valódi worker, de kizárólag memóriabeli, tartós műszerállapot.
class _B5bClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _B5bHardware:
    def __init__(self, clock):
        self.clock = clock
        self.nplc = 1.0
        self.count = 8
        self.source = "TIM"
        self.armed = True
        self.errors = ["korábbi hiba"]
        self.commands = []
        self.starts = []
        self.durations = []
        self.read_hook = None

    def session(self):
        from Prog.drivers.device_dmm import Keysight34465ADMM

        dmm = Keysight34465ADMM()
        # A transport helyett a sessionváltáson át megmaradó állapot válaszol.
        dmm._query = self.query
        return dmm

    def query(self, command):
        from threading import get_ident

        self.commands.append((command, get_ident()))
        if command == "READ?":
            assert self.nplc == 10.0
            assert (self.count, self.source, self.armed) == (1, "IMM", False)
            start = self.clock()
            self.starts.append(start)
            if self.read_hook is not None:
                self.read_hook()
            duration = self.durations.pop(0) if self.durations else 0.0
            self.clock.now += duration
            return str(12.5 + 1e-6 * (start + duration / 2))
        for part in command.split(";"):
            part = part.lstrip(":")
            if part == "ABOR":
                self.armed = False
            elif part.startswith("SAMP:COUN "):
                self.count = int(part.split()[-1])
            elif part.startswith("SAMP:SOUR "):
                self.source = part.split()[-1]
            elif part.startswith("VOLT:DC:NPLC "):
                self.nplc = float(part.split()[-1])
            elif part != "*OPC?":
                raise AssertionError(command)
        return "1"


class _B5bRig:
    def __init__(self, capacity=256):
        from queue import Queue

        from Prog.src.relax_controller import RelaxSamplingService

        self.clock = _B5bClock()
        self.hardware = _B5bHardware(self.clock)
        self.dmm = self.hardware.session()
        self.requests = Queue()
        self.permits = Queue()
        self.service = RelaxSamplingService(
            self.dmm, capacity=capacity, clock=self.clock, wait=self.wait,
        )

    def wait(self, event, delay):
        self.requests.put(delay)
        # Csak eseményszinkron; az időt a főszál lépteti, nincs sleep.
        proceed = self.permits.get(timeout=2)
        return not proceed or event.is_set()

    def start(self):
        self.dmm.set_nplc(10.0)
        self.service.start(configuration_ready=True)
        return self.requests.get(timeout=2)

    def step(self, advance=1.5):
        self.clock.now += advance
        self.permits.put(True)
        return self.requests.get(timeout=2)

    def stop(self):
        self.service.stop()
        self.permits.put(False)
        self.service._thread.join(2)  # Kizárólag deadlock-watchdog, nem időalap.
        assert not self.service._thread.is_alive()


class TestR16B5bSampling:
    @pytest.mark.parametrize("runner_tick", [5.0, 30.0])
    def test_j1_service_window_independent_of_runner_tick_and_bq_pause(self, runner_tick):
        rig = _B5bRig()
        ctrl = RelaxController(rig.dmm, RelaxConfig(min_relax_s=1.0))
        ctrl.attach_sampling_service(rig.service)
        events = []
        ctrl.on_event = events.append
        assert ctrl.sampling_mode == "SERVICE"
        assert not ctrl.dmm_valid
        origin = rig.clock()
        try:
            next_deadline = rig.clock() + rig.start()

            def sample_until(elapsed):
                nonlocal next_deadline
                target = origin + elapsed
                while next_deadline <= target:
                    delay = rig.step(next_deadline - rig.clock())
                    next_deadline = rig.clock() + delay
                rig.clock.now = target

            ctrl.advance(0.0)
            for n in range(1, int(60 / runner_tick) + 1):
                sample_until(n * runner_tick)
                ctrl.advance(runner_tick)
                assert ctrl.dvdt_valid is (n * runner_tick >= 60.0)
            assert ctrl.elapsed_s == 60.0
            assert ctrl.dvdt_V_per_s == pytest.approx(1e-6, abs=1e-12)
            assert len(rig.hardware.starts) == 41
            assert ctrl.state.name == "RELAX_DONE"
            # Két szinkron BQ-poll alatti idő modellje: a runner nem advance-el,
            # a DMM-worker közben a saját határidőire tovább gyűjt.
            for pause in (2.0, 7.0):
                before = len(rig.hardware.starts)
                sample_until(ctrl.elapsed_s + pause)
                assert len(rig.hardware.starts) > before
                ctrl.advance(pause)
                assert ctrl.dvdt_valid
            # RELAX_DONE csak minimumidő: az extra-várakozásban is gyűjt.
            sample_until(ctrl.elapsed_s + runner_tick)
            ctrl.advance(runner_tick)
            assert ctrl.dvdt_valid
            assert ctrl.elapsed_s == 69.0 + runner_tick
            assert events == []
        finally:
            rig.stop()
        ctrl.collect_sampling_records()
        assert ctrl.sampling_shutdown_complete

    def test_legacy_mode_is_explicit_and_keeps_tick_reads(self):
        dmm = MockDMM(voltage_V=12.5)
        ctrl = RelaxController(dmm, RelaxConfig(min_relax_s=2.0))
        assert ctrl.sampling_mode == "LEGACY_TICK"
        ctrl.advance(0.0)
        ctrl.advance(5.0)
        ctrl.advance(30.0)
        assert dmm.calls.count("read_voltage()") == 3
        assert ctrl.elapsed_s == 35.0
        assert not ctrl.dvdt_valid

    def test_absolute_deadlines_and_midpoints_with_asymmetric_latency(self):
        rig = _B5bRig()
        rig.hardware.durations = [0.2, 0.8, 0.1]
        try:
            assert rig.start() == pytest.approx(1.3)
            rig.step(1.3)
            rig.step(0.7)
        finally:
            rig.stop()
        records = rig.service.drain().records
        assert rig.hardware.starts == [1000.0, 1001.5, 1003.0]
        assert [r.sample_time for r in records] == pytest.approx([1000.1, 1001.9, 1003.05])
        assert [(r.epoch, r.sequence) for r in records] == [
            (rig.service.epoch, n) for n in (1, 2, 3)
        ]
        assert [r.read_end - r.read_start for r in records] == pytest.approx([0.2, 0.8, 0.1])

    def test_overdue_deadlines_are_skipped_without_catchup(self):
        rig = _B5bRig()
        rig.hardware.durations = [5.2]
        try:
            assert rig.start() == pytest.approx(0.8)
            # Az 1,5/3/4,5 s határidő kimaradt, a következő 6 s.
            assert rig.hardware.starts == [1000.0]
            rig.step(0.8)
            assert rig.hardware.starts == [1000.0, 1006.0]
        finally:
            rig.stop()
        records = rig.service.drain().records
        assert [r.missed_deadlines for r in records] == [0, 3, 0]
        assert records[1].error == "DEADLINE_MISSED"

    def test_scheduler_delay_has_diagnostic_and_no_burst(self):
        rig = _B5bRig()
        try:
            rig.start()
            assert rig.step(5.0) == 1.0
            assert rig.hardware.starts == [1000.0]
            rig.step(1.0)
            assert rig.hardware.starts == [1000.0, 1006.0]
        finally:
            rig.stop()
        assert sum(r.missed_deadlines for r in rig.service.drain().records) == 3

    def test_ownership_and_sticky_state_survive_session_change(self):
        from Prog.drivers.device_dmm import DMMMeasurementBusy

        rig = _B5bRig()
        try:
            rig.start()
            with pytest.raises(DMMMeasurementBusy):
                rig.dmm.read_voltage()
            with pytest.raises(DMMMeasurementBusy):
                rig.dmm.set_nplc(1.0)
            with pytest.raises(DMMMeasurementBusy):
                rig.dmm.disconnect()
        finally:
            rig.stop()
        assert rig.service.drain().released
        assert rig.hardware.errors == ["korábbi hiba"]
        next_session = rig.hardware.session()
        next_session.set_nplc(10.0)
        assert next_session.read_voltage() > 12.5
        assert (rig.hardware.count, rig.hardware.source, rig.hardware.armed) == (1, "IMM", False)

    def test_configuration_ack_and_single_epoch_start(self):
        rig = _B5bRig()
        with pytest.raises(RuntimeError, match="NPLC"):
            rig.service.start(configuration_ready=False)
        assert rig.hardware.commands == []
        try:
            rig.start()
            with pytest.raises(RuntimeError, match="újra"):
                rig.service.start(configuration_ready=True)
        finally:
            rig.stop()
        other = _B5bRig()
        assert other.service.epoch != rig.service.epoch
        other.service.stop()
        assert other.service.drain().released

    def test_stop_during_inflight_delivers_final_error_before_ack(self):
        from threading import Event

        from Prog.src.relax_controller import RelaxSamplingService

        clock = _B5bClock()
        hardware = _B5bHardware(clock)
        dmm = hardware.session()
        entered, proceed = Event(), Event()

        def late_error():
            entered.set()
            assert proceed.wait(2)
            raise RuntimeError("késői válaszhiba")

        hardware.read_hook = late_error
        service = RelaxSamplingService(dmm, clock=clock)
        dmm.set_nplc(10.0)
        service.start(configuration_ready=True)
        try:
            assert entered.wait(2)
            service.stop()
            assert not service.drain().released
        finally:
            proceed.set()
            service._thread.join(2)
        batch = service.drain()
        assert batch.released
        assert len(batch.records) == 1
        assert "késői válaszhiba" in batch.records[0].error
        assert hardware.starts == [1000.0]
        dmm.set_nplc(1.0)
        assert hardware.nplc == 1.0

    def test_stop_interrupts_wait_and_does_not_read_again(self):
        rig = _B5bRig()
        rig.start()
        rig.service.stop()
        rig.permits.put(True)
        rig.service._thread.join(2)
        assert not rig.service._thread.is_alive()
        assert rig.service.drain().released
        assert len(rig.hardware.starts) == 1


class _B5bRecordSource:
    def __init__(self):
        self.epoch = 100
        self.records = []
        self.overflow = False
        self.released = False

    def drain(self):
        from Prog.src.relax_controller import RelaxSampleBatch

        batch = RelaxSampleBatch(tuple(self.records), self.overflow, self.released)
        self.records.clear()
        self.overflow = False
        return batch

    def add(self, t, *, error=None, voltage=None):
        from Prog.src.relax_controller import RelaxSample

        sequence = self.records[-1].sequence + 1 if self.records else self.next_sequence
        self.next_sequence = sequence + 1
        self.records.append(RelaxSample(
            self.epoch, sequence, t - 0.1, t + 0.1, t,
            12.5 + 1e-6 * t if voltage is None else voltage, error,
        ))

    next_sequence = 1


class TestR16B5bRecords:
    @staticmethod
    def make():
        source = _B5bRecordSource()
        ctrl = RelaxController(MockDMM(), RelaxConfig())
        ctrl.attach_sampling_service(source)
        return ctrl, source

    @pytest.mark.parametrize("gap,valid", [(4.0, True), (4.001, False), (0.0, False), (-1.0, False)])
    def test_gap_and_duplicate_timestamp_rules(self, gap, valid):
        ctrl, source = self.make()
        for n in range(41):
            source.add(1.5 * n)
        ctrl.advance(5.0)
        assert ctrl.dvdt_valid
        source.add(60.0 + gap)
        ctrl.advance(30.0)
        assert ctrl.dvdt_valid is valid
        assert ctrl.elapsed_s == 35.0

    def test_many_samples_without_coverage_stay_invalid(self):
        ctrl, source = self.make()
        for n in range(100):
            source.add(n / 10)
        ctrl.advance(60.0)
        assert not ctrl.dvdt_valid

    @pytest.mark.parametrize("reason", ["error", "overflow", "epoch", "sequence"])
    def test_discontinuity_requires_full_refill(self, reason):
        ctrl, source = self.make()
        for n in range(41):
            source.add(1.5 * n)
        ctrl.advance(5.0)
        assert ctrl.dvdt_valid
        events = []
        ctrl.on_event = events.append
        if reason == "error":
            source.add(61.5, error="DMM_READ_ERROR: fake")
        elif reason == "overflow":
            source.overflow = True
        elif reason == "epoch":
            source.epoch += 1
        else:
            source.next_sequence += 1
        source.add(63.0)
        ctrl.advance(30.0)
        assert not ctrl.dvdt_valid
        assert ctrl.elapsed_s == 35.0
        for n in range(1, 41):
            source.add(63.0 + 1.5 * n)
        ctrl.advance(30.0)
        assert ctrl.dvdt_valid
        assert ctrl.dvdt_V_per_s == pytest.approx(1e-6, abs=1e-12)
        assert ctrl.dmm_fault_count == (1 if reason == "error" else 0)
        if reason in ("overflow", "sequence", "error"):
            assert events

    def test_ols_program_error_is_not_a_dmm_failure(self, monkeypatch):
        ctrl, source = self.make()
        source.add(10.0)

        def broken_estimator(*args):
            raise ArithmeticError("becslőhiba")

        monkeypatch.setattr(ctrl, "_update_dvdt", broken_estimator)
        with pytest.raises(ArithmeticError):
            ctrl.advance(1.0)
        assert ctrl.dmm_fault_count == 0

    def test_real_bounded_buffer_reports_loss(self):
        rig = _B5bRig(capacity=2)
        try:
            rig.start()
            rig.step()
            rig.step()
        finally:
            rig.stop()
        batch = rig.service.drain()
        assert batch.overflow
        assert batch.released
        assert [r.sequence for r in batch.records] == [2, 3]
        assert not rig.service.drain().overflow

    def test_stop_during_preparation_is_nonblocking_and_cannot_ack_early(self, monkeypatch):
        from threading import Event, Thread

        rig = _B5bRig()
        entered, proceed = Event(), Event()
        reset = rig.dmm.reset_to_single_sample

        def prepare():
            entered.set()
            assert proceed.wait(2)
            reset()

        monkeypatch.setattr(rig.dmm, "reset_to_single_sample", prepare)
        rig.dmm.set_nplc(10.0)
        starter = Thread(target=lambda: rig.service.start(configuration_ready=True))
        starter.start()
        try:
            assert entered.wait(2)
            rig.service.stop()
            assert not rig.service.drain().released
        finally:
            proceed.set()
            starter.join(2)
        assert not starter.is_alive()
        assert rig.service.drain().released
        assert rig.hardware.starts == []


class TestR16B5bF2Records:
    def test_read_start_is_the_age_origin_at_the_freshness_boundary(self):
        from Prog.src.relax_controller import RelaxSamplingService

        clock = _B5bClock()
        service = RelaxSamplingService(MockDMM(), clock=clock)
        ctrl = RelaxController(MockDMM(), RelaxConfig())
        ctrl.attach_sampling_service(service)
        service._publish(10.0, 13.0, 12.5)
        clock.now = 14.0
        ctrl.collect_sampling_records()
        assert service.snapshot().age_s == 4.0
        assert ctrl.voltage_sample_age_s == 4.0
        assert ctrl.dmm_valid
        clock.now += 0.001
        assert service.snapshot().quality == "STALE"
        assert ctrl.voltage_sample_quality == "STALE"
        assert not ctrl.dmm_valid

    def test_post_read_deadlines_count_but_only_emit_on_transition(self):
        rig = _B5bRig()
        ctrl = RelaxController(rig.dmm, RelaxConfig())
        ctrl.attach_sampling_service(rig.service)
        events = []
        ctrl.on_event = events.append
        rig.hardware.durations = [2.0, 2.0]
        try:
            assert rig.start() == 1.0
            ctrl.collect_sampling_records()
            first_count = getattr(ctrl, "missed_deadline_count", None)
            assert ctrl.dmm_fault_count == 0
            assert rig.step(1.0) == 1.0
            ctrl.collect_sampling_records()
            assert ctrl.dmm_fault_count == 0
            assert [ev["event_code"] for ev in events] == ["RELAX_DEADLINE_MISSED"]
            assert first_count == 1
            assert ctrl.missed_deadline_count == 2
            # Egy valódi érvényes rekord lezárja a kihagyásos szakaszt.
            assert rig.step(1.0) == 1.5
            ctrl.collect_sampling_records()
            assert ctrl.dmm_valid
            rig.hardware.durations = [2.0]
            assert rig.step(1.5) == 1.0
            ctrl.collect_sampling_records()
            assert ctrl.missed_deadline_count == 3
            assert ctrl.dmm_fault_count == 0
            assert [ev["event_code"] for ev in events] == [
                "RELAX_DEADLINE_MISSED", "RELAX_DEADLINE_MISSED",
            ]
        finally:
            rig.stop()


class TestR16B5bF3Records:
    def test_processed_error_after_valid_record_overrides_snapshot_quality(self):
        from Prog.src.relax_controller import RelaxSamplingService

        clock = _B5bClock()
        service = RelaxSamplingService(MockDMM(), clock=clock)
        ctrl = RelaxController(MockDMM(), RelaxConfig())
        ctrl.attach_sampling_service(service)
        service._publish(clock.now, clock.now, 12.5)
        ctrl.collect_sampling_records()
        assert ctrl.dmm_valid
        record = ctrl._voltage_record
        service._publish(clock.now, clock.now, None, "DMM_READ_ERROR: próbahiba")
        ctrl.collect_sampling_records()
        snapshot = ctrl.voltage_snapshot()
        assert ctrl._voltage_record is record
        assert snapshot.voltage_V == 12.5
        assert snapshot.quality == "READ_ERROR"
        assert not ctrl.dmm_valid
        assert ctrl.dmm_fault_count == 1


class TestR16B16aF2RawSampleOrder:
    def test_callback_precedes_overwrite_and_keeps_all_records(self):
        from Prog.src.relax_controller import RelaxSample

        ctrl, source = TestR16B5bRecords.make()
        records = [
            RelaxSample(source.epoch, 1, 99.9, 100.1, 100.0, 10.799),
            RelaxSample(source.epoch, 2, 101.4, 101.6, 101.5, 10.801),
        ]
        seen = []
        ctrl.on_sample = lambda record: seen.append((record, ctrl.last_voltage_V))
        source.records = list(records)

        ctrl.collect_sampling_records()

        assert seen == [(records[0], 0.0), (records[1], 10.799)]
        assert all(seen[n][0] is record for n, record in enumerate(records))
        assert list(ctrl._dvdt_samples) == [(100.0, 10.799), (101.5, 10.801)]
        assert ctrl.last_voltage_V == 10.801


class TestR16B19HandoffDiagnosticDispatch:
    @pytest.mark.parametrize(
        ("mode", "error_type"),
        [("default", None), ("default", RuntimeError), ("default", KeyboardInterrupt),
         ("deferred", None), ("deferred", RuntimeError), ("deferred", KeyboardInterrupt),
         ("absent", None)],
    )
    def test_fault_dispatch_preserves_default_and_allows_deferral(self, mode, error_type):
        from Prog.src.relax_controller import RelaxSamplingService

        dmm = MockDMM(dmm_valid=False)
        ctrl = RelaxController(dmm, RelaxConfig())
        service = RelaxSamplingService(dmm, clock=lambda: 123.0)
        ctrl.attach_sampling_service(service)
        assert ctrl.poll_sampling_stop()
        events, pending = [], []

        def on_event(event):
            events.append(event)
            if error_type is not None:
                raise error_type("B19 diagnosztika")

        ctrl.on_event = None if mode == "absent" else on_event
        if mode == "default" and error_type is not None:
            with pytest.raises(error_type, match="B19 diagnosztika"):
                ctrl.read_handoff_voltage()
        else:
            snapshot = (
                ctrl.read_handoff_voltage(emit=pending.append)
                if mode == "deferred" else ctrl.read_handoff_voltage()
            )
            assert snapshot.quality == "READ_ERROR" and snapshot.voltage_V is None
            assert snapshot.read_start == 123.0 and snapshot.age_s == 0.0
        assert dmm.calls == ["read_voltage()"]
        assert ctrl.dmm_fault_count == 1 and not ctrl.dmm_valid
        if mode == "deferred":
            assert events == []
            event, = pending
            if error_type is None:
                on_event(event)
            else:
                with pytest.raises(error_type, match="B19 diagnosztika"):
                    on_event(event)
        else:
            assert pending == []
        if mode == "absent":
            assert events == []
        else:
            event, = events
            assert event == {
                "event_code": "RELAX_DMM_FAULT",
                "event_message": "Átadási DMM_READ_ERROR: MockDMM: dmm_valid=False",
            }
