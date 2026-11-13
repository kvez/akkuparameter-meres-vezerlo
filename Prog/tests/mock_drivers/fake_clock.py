"""
FakeMonotonicClock — determinisztikus, hívásonként előre léptethető óra
PulseMeasurementService (és más monotonic-clock-függő komponens) teszteléséhez.

Minden hívás a JELENLEGI értéket adja vissza, majd a belső értéket step_s-sel
előre lépteti — így egy `PulseMeasurementService(max_voltage_V=14.55,
max_load_power_W=SafetyManager.max_load_power_W, time_source=FakeMonotonicClock(
step_s=chunk_sample_count*sample_interval_s))` pontosan "ideális, holtidő
nélküli hardver" időzítést szimulál: minden step_chunk() hívás valós ideje
pontosan a chunk deklarált (mintaszám × intervallum) hosszával egyezik meg.

drift_after_call_index-szel egyszeri, kontrollált holtidő (pl. TestRunner
tick-padding szimulációja) injektálható egy adott hívás UTÁN.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FakeMonotonicClock:
    start: float = 0.0
    step_s: float = 0.0
    drift_after_call_index: Optional[int] = None
    drift_s: float = 0.0

    _value: float = field(init=False, repr=False)
    _call_count: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._value = self.start

    def __call__(self) -> float:
        v = self._value
        self._call_count += 1
        self._value += self.step_s
        if self.drift_after_call_index is not None and self._call_count == self.drift_after_call_index:
            self._value += self.drift_s
        return v


class TickClock:
    """Determinisztikus monotonic-óra a Kör 20 Batch E1 (METRO-R20-1)
    load-on időorigó regressziós teszteihez.

    A `tick_time` a tick KEZDETE (a TestRunner `_tick_start_t`-jének
    megfelelője, test_runner.py:1313), az `intra` a ticken BELÜL,
    SCPI-hívásokkal eltöltött valós idő. Ezek szétválasztása nélkül a fix
    előtti és utáni viselkedés azonos lenne, és a regressziós teszt semmit
    nem diszkriminálna -- l. bind_tick_clock()/burn_before_input_on().
    """

    def __init__(self, start: float = 0.0) -> None:
        self.tick_time = start
        self.intra = 0.0

    def __call__(self) -> float:
        return self.tick_time + self.intra

    def start_tick(self, dt_s: float) -> None:
        self.tick_time += dt_s
        self.intra = 0.0

    def burn(self, seconds: float) -> None:
        self.intra += seconds


def bind_tick_clock(ctrl):
    """A controllerbe injektált TickClock-ot az `advance()` hívásokhoz
    köti: minden `advance(dt_s)` a tick KEZDETÉRE lépteti az órát, pontosan
    úgy, ahogy a TestRunner teszi (test_runner.py:1313-1316). Alapértelmezett
    ε=0 -> minden MEGLÉVŐ, a fix előtt írt teszt bit-azonos marad; ε-t a
    burn_before_input_on() injektál explicit."""
    clock = ctrl._time_source
    real_advance = ctrl.advance

    def advance(dt_s: float):
        clock.start_tick(dt_s)
        return real_advance(dt_s)

    ctrl.advance = advance  # type: ignore[method-assign]
    return ctrl


def burn_before_input_on(load, clock: TickClock, seconds: float) -> None:
    """A setup-tick `input_on()` ELŐTTI, valós SCPI-időfogyasztás (ε)
    szimulációja: az óra a load tényleges bekapcsolásáig `seconds`-szel
    előre lép, mielőtt a valódi `input_on()` lefutna."""
    real = load.input_on

    def input_on(*args, **kwargs):
        clock.burn(seconds)
        return real(*args, **kwargs)

    load.input_on = input_on  # type: ignore[method-assign]


class InstrumentLatencyClock:
    """R1 B8a (F-21/F-22) determinisztikus monoton óra a PulseMeasurement-
    Service-hez: NEM hívásonként lép (mint a FakeMonotonicClock, amely az
    OFF-nyugta lekérdezésére is egy teljes chunkidőt gyártana), hanem csak
    akkor, amikor a mockolt műszerhívások valós időt "fogyasztanak" -- l.
    bind_pulse_instrument_latency(). Nulla latenciákkal ez az "ideális,
    holtidő nélküli hardver": az idő kizárólag a DMM-fetch-ben (a chunk
    mintaszám × intervallum hosszával) lép, a Load OFF-nyugta azonnali."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def bind_pulse_instrument_latency(
    clock: InstrumentLatencyClock,
    dmm,
    load,
    *,
    reset_s: float = 0.0,
    measure_current_s: float = 0.0,
    input_off_s: float = 0.0,
) -> None:
    """A mock DMM/Load hívásait az órához köti: a fetch_timed_acquisition()
    a legutóbb konfigurált mintaszám × intervallum idővel VISSZATÉRÉSKOR
    lépteti az órát (a mérés a fetch alatt zajlik); a reset_to_single_
    sample(), measure_current(), input_off() a megadott latenciával a
    tényleges hívás ELŐTT (a hívás visszatérésekor az idő már eltelt)."""
    acq = {"n": 0, "interval": 0.0}
    real_configure = dmm.configure_timed_acquisition
    real_fetch = dmm.fetch_timed_acquisition
    real_reset = dmm.reset_to_single_sample
    real_measure = load.measure_current
    real_off = load.input_off

    def configure_timed_acquisition(sample_count, interval_s, **kwargs):
        acq["n"] = sample_count
        acq["interval"] = interval_s
        return real_configure(sample_count=sample_count, interval_s=interval_s, **kwargs)

    def fetch_timed_acquisition(**kwargs):
        raw = real_fetch(**kwargs)
        clock.advance(acq["n"] * acq["interval"])
        return raw

    def reset_to_single_sample(**kwargs):
        clock.advance(reset_s)
        return real_reset(**kwargs)

    def measure_current():
        clock.advance(measure_current_s)
        return real_measure()

    def input_off():
        clock.advance(input_off_s)
        return real_off()

    dmm.configure_timed_acquisition = configure_timed_acquisition  # type: ignore[method-assign]
    dmm.fetch_timed_acquisition = fetch_timed_acquisition  # type: ignore[method-assign]
    dmm.reset_to_single_sample = reset_to_single_sample  # type: ignore[method-assign]
    load.measure_current = measure_current  # type: ignore[method-assign]
    load.input_off = input_off  # type: ignore[method-assign]


class _B21bRig:
    """Valódi PMS/OCV és worker; kizárólag mock műszerek és injektált óra."""

    @staticmethod
    def make(monkeypatch, fast=False, *, start=True, config_s=0.0, fetch_s=0.0, temp_s=0.0, **latencies):
        from types import SimpleNamespace

        from Prog.src.ocv_soc_controller import OcvSocConfig, OcvSocState
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.test_ocv_soc_controller import make_ocv_soc_controller

        clock = InstrumentLatencyClock()
        ctrl, psu, load, dmm, temp = make_ocv_soc_controller(
            dmm_voltage_V=13.5, pulse_time_source=clock,
            config=OcvSocConfig(fast_transient_enabled=fast),
        )
        assert isinstance(psu, MockPSU)
        bind_pulse_instrument_latency(clock, dmm, load, **latencies)
        dmm.sample_timer_min_raw_override = "0.001"
        dmm.timed_voltage_fn = lambda _t: 13.2
        ctrl._state = OcvSocState.IMPULSE_FAST_ON if fast else OcvSocState.IMPULSE_ON
        ctrl._u_batt = 13.5
        if start:
            ctrl.prepare_pulse_block()
            ctrl.advance(0.0)
        for device, name, delay in (
            (dmm, "configure_timed_acquisition", config_s),
            (dmm, "fetch_timed_acquisition", fetch_s),
            (temp, "read_temperature", temp_s),
        ):
            fn = getattr(device, name)

            def delayed(*args, _fn=fn, _delay=delay, **kwargs):
                value = _fn(*args, **kwargs)
                clock.advance(_delay)
                return value

            monkeypatch.setattr(device, name, delayed)
        # A virtuális óra az üres, valós Queue-várakozás idejét is méri.
        # Egy már nem indítható teljes minta nem fagyaszthatja meg a deadline-t.
        from queue import Empty

        from Prog.src.pulse_measurement_service import _PulseDmmWorker

        worker_init = _PulseDmmWorker.__init__

        def initialize_worker(worker, service):
            worker_init(worker, service)
            get = worker.results.get

            def wait_result(*args, **kwargs):
                try:
                    return get(*args, **kwargs)
                except Empty:
                    clock.advance(kwargs.get("timeout", 0.0))
                    raise

            monkeypatch.setattr(worker.results, "get", wait_result)

        monkeypatch.setattr(_PulseDmmWorker, "__init__", initialize_worker)
        service = ctrl._fast_pulse_service if fast else ctrl._pulse_service
        return SimpleNamespace(
            ctrl=ctrl, psu=psu, load=load, dmm=dmm, temp=temp,
            clock=clock, service=service,
        )

    @staticmethod
    def trace(monkeypatch, rig):
        from threading import get_ident

        events = []
        for label, device, names in (
            ("load", rig.load, ("input_on", "input_off", "measure_current")),
            ("psu", rig.psu, ("all_outputs_off",)),
            ("dmm", rig.dmm, (
                "acquire_measurement", "configure_timed_acquisition", "check_error",
                "get_sample_timer_s", "arm_timed_acquisition", "fetch_timed_acquisition",
                "reset_to_single_sample", "release_measurement", "set_nplc",
            )),
        ):
            for name in names:
                original = getattr(device, name)

                def recorded(*args, _fn=original, _name=name, _label=label, **kwargs):
                    events.append((_label, _name, get_ident()))
                    return _fn(*args, **kwargs)

                monkeypatch.setattr(device, name, recorded)
        return events

    @staticmethod
    def run(rig, poll=None):
        return rig.ctrl.run_pulse_block(poll or (lambda force, before_io: None))

    @staticmethod
    def runner(monkeypatch, rig, tick=2.0):
        from types import SimpleNamespace
        from unittest.mock import Mock

        import Prog.src.test_runner as runner_module
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger
        from Prog.src.test_runner import (
            StepKind,
            TestPlan,
            TestRunner,
            TestRunnerConfig,
            TestStep,
            TestType,
        )

        rig.ctrl._grid_pos = len(rig.ctrl._grid) - 1
        rig.points = []
        rig.events = []
        rig.ctrl.on_soc_point = rig.points.append
        rig.ctrl.on_event = rig.events.append
        monkeypatch.setattr(runner_module, "time", SimpleNamespace(
            perf_counter=rig.clock, monotonic=rig.clock, sleep=rig.clock.advance,
        ))
        rig.logger = Mock(spec=Logger)
        rig.runner = TestRunner(
            instrument_manager=InstrumentManager(rig.psu, rig.load, rig.dmm, rig.temp),
            safety=rig.ctrl._safety, logger=rig.logger, profile=rig.ctrl._profile,
            config=TestRunnerConfig(
                runner_tick_s=tick, sleep_enabled=True, device_error_poll_interval_s=60.0,
            ),
            charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: None,
            relax_ctrl_factory=lambda step: None, ocv_soc_ctrl_factory=lambda: rig.ctrl,
        )
        rig.plan = TestPlan(TestType.OCV_SOC_CHARACTERIZATION, (TestStep(StepKind.OCV_SOC, "B21b"),))
        return rig.runner
